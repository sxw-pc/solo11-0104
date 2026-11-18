import json,sqlite3,threading
from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.archive_lab import register,recover
from gateflow.store import connect
from tests.archive_case_builder import sha256

FIX=Path(__file__).resolve().parents[1]/'fixtures'
A0=(FIX/'archive/A0.bin').read_bytes();B1=(FIX/'archive/B1.bin').read_bytes()
TRUE_BYTES=A0+B1;TRUE_FULL=sha256(TRUE_BYTES)

def seed(tmp):
    db=tmp/'case.db';initialize(db,FIX/'archive-config.json')
    register(db,'ARCH-1',FIX/'archive-case.json');return db

def tables(db):
    c=connect(db)
    try:return {t:c.execute('SELECT count(*) FROM %s'%t).fetchone()[0]
                 for t in ('source','event','archive_publish','archive_recovery_attempt','archive_chunk_decision')}
    finally:c.close()

class Crash(RuntimeError):pass

def test_two_recoverers_converge_to_one_publish(tmp_path):
    db=seed(tmp_path);errors=[];results=[]
    barrier=threading.Barrier(2)
    def worker():
        try:
            barrier.wait()
            results.append(recover(db,'ARCH-1','verified_mosaic',tmp_path/'out'))
        except BaseException as exc:
            errors.append(exc)
    threads=[threading.Thread(target=worker) for _ in range(2)]
    for t in threads:t.start()
    for t in threads:t.join()
    assert not errors
    assert all(r['status']=='published' for r in results)
    n=tables(db)
    assert n['source']==1 and n['event']==3 and n['archive_publish']==1
    assert n['archive_recovery_attempt']==2
    assert len({r['import']['source_row'] for r in results})==1
    assert Path(results[0]['recovered_file']).read_bytes()==TRUE_BYTES

def test_crash_after_file_replace_rolls_back_ledger_without_dangling_file(tmp_path):
    db=seed(tmp_path)
    def hook(stage):
        if stage=='after_file_replace':raise Crash('power loss before commit')
    with pytest.raises(Crash):
        recover(db,'ARCH-1','verified_mosaic',tmp_path/'out',crash_hook=hook)
    n=tables(db)
    assert n['source']==0 and n['event']==0 and n['archive_publish']==0 and n['archive_recovery_attempt']==0
    # write-ahead file exists but nothing claims it; retry must reconcile it
    written=list((tmp_path/'out').rglob('*.jsonl'))
    assert len(written)==1 and written[0].read_bytes()==TRUE_BYTES
    rep=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published' and rep['import']['events_imported']==3
    assert tables(db)['archive_publish']==1

def test_crash_before_commit_leaves_only_temp_artifacts_then_recovers(tmp_path):
    db=seed(tmp_path)
    def hook(stage):
        if stage=='before_commit':raise Crash('power loss during commit window')
    with pytest.raises(Crash):
        recover(db,'ARCH-1','verified_mosaic',tmp_path/'out',crash_hook=hook)
    n=tables(db)
    assert n['source']==0 and n['archive_publish']==0
    rep=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published'
    assert not list((tmp_path/'out').rglob('.tmp.*')) and not list((tmp_path/'out').rglob('.*.tmp.*'))

def test_ledger_says_published_but_file_missing_is_healed_not_duplicated(tmp_path):
    db=seed(tmp_path)
    first=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    assert first['status']=='published'
    target=Path(first['recovered_file']);target.unlink()
    assert not target.exists()
    healed=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    assert healed['status']=='published' and healed['reason']=='file_reconciled'
    assert target.exists() and target.read_bytes()==TRUE_BYTES
    n=tables(db)
    assert n['source']==1 and n['event']==3 and n['archive_publish']==1
    assert healed['import']['duplicate_file'] is True

def test_repeated_retries_never_duplicate_events(tmp_path):
    db=seed(tmp_path)
    for _ in range(4):
        rep=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
        assert rep['status']=='published'
    n=tables(db)
    assert n['source']==1 and n['event']==3 and n['archive_publish']==1
    assert n['archive_recovery_attempt']==4

def test_quorum_and_mosaic_attempts_coexist_in_ledger(tmp_path):
    db=seed(tmp_path)
    blocked=recover(db,'ARCH-1','two_copy_quorum',tmp_path/'out')
    published=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    assert blocked['status']=='blocked' and published['status']=='published'
    c=connect(db)
    try:
        attempts=[dict(r) for r in c.execute('SELECT contract_id,status,reason FROM archive_recovery_attempt ORDER BY id')]
        assert attempts[0]['contract_id']=='two_copy_quorum' and attempts[0]['status']=='blocked'
        assert attempts[1]['contract_id']=='verified_mosaic' and attempts[1]['status']=='published'
        decisions=c.execute('SELECT count(*) FROM archive_chunk_decision').fetchone()[0]
        assert decisions==8  # 4 shards x 2 attempts, each fully reasoned
    finally:c.close()

def test_blocked_attempt_writes_no_file_or_source(tmp_path):
    db=seed(tmp_path)
    rep=recover(db,'ARCH-1','two_copy_quorum',tmp_path/'out')
    assert rep['status']=='blocked'
    assert not (tmp_path/'out').exists()
    assert rep['recovered_file'] is None
    n=tables(db)
    assert n['source']==0 and n['event']==0 and n['archive_publish']==0
    assert n['archive_recovery_attempt']==1 and n['archive_chunk_decision']==4

def test_rollback_in_ingest_still_atomic_after_refactor(tmp_path):
    # regression guard: the connection-based import shares one transaction
    db=tmp_path/'rollback.db';initialize(db,FIX/'config.json')
    c=connect(db)
    c.execute("CREATE TRIGGER reject_evidence BEFORE INSERT ON evidence BEGIN SELECT RAISE(ABORT,'injected'); END")
    c.commit();c.close()
    with pytest.raises(sqlite3.IntegrityError):
        from gateflow.ingest import ingest as plain_ingest
        plain_ingest(FIX/'events.jsonl',db)
    assert tables(db)['source']==0 and tables(db)['event']==0
