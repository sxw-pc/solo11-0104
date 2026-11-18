import hashlib,json,shutil
from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.archive_lab import register,recover,recovery_report
from gateflow.ingest import ingest
from gateflow.store import connect
from tests.archive_case_builder import make_case,register_case,rows,sha256,flip_last,truncate_last

FIX=Path(__file__).resolve().parents[1]/'fixtures'
A0=(FIX/'archive/A0.bin').read_bytes();A1=(FIX/'archive/A1.bin').read_bytes()
B0=(FIX/'archive/B0.bin').read_bytes();B1=(FIX/'archive/B1.bin').read_bytes()
TRUE_BYTES=A0+B1
TRUE_FULL=sha256(TRUE_BYTES);CHUNK0=A0;CHUNK1=B1;BOUNDARY=len(A0)

def seed_db(tmp,case_id='ARCH-1',case_file=FIX/'archive-case.json'):
    db=tmp/'case.db';initialize(db,FIX/'archive-config.json')
    register(db,case_id,case_file);return db

def counts(db):
    c=connect(db)
    try:return (c.execute('SELECT count(*) FROM source').fetchone()[0],
                 c.execute('SELECT count(*) FROM event').fetchone()[0],
                 c.execute('SELECT count(*) FROM archive_publish').fetchone()[0])
    finally:c.close()

# ---------- the two authorized contracts on the real frozen case ----------

def test_quorum_blocks_when_each_chunk_has_only_one_good_copy(tmp_path):
    db=seed_db(tmp_path)
    rep=recover(db,'ARCH-1','two_copy_quorum',tmp_path/'out')
    assert rep['status']=='blocked' and rep['reason']=='quorum_not_met'
    assert rep['import']['events_imported']==0
    decisions={(d['chunk'],d['replica']):d for d in rep['chunks']}
    assert decisions[(0,'A')]['reason']=='shard_digest_match'
    assert decisions[(1,'B')]['reason']=='shard_digest_match'
    assert decisions[(1,'A')]['decision']=='rejected'
    assert decisions[(0,'B')]['decision']=='rejected'
    assert counts(db)==(0,0,0)
    assert not (tmp_path/'out').exists()

def test_mosaic_publishes_a0_b1_byte_identical_and_imports_three(tmp_path):
    db=seed_db(tmp_path)
    report_path=tmp_path/'evidence.json'
    rep=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out',report_path=report_path)
    assert rep['status']=='published' and rep['reason']=='published'
    chosen={(d['chunk'],d['replica']):d['decision'] for d in rep['chunks'] if d['decision']=='selected'}
    assert chosen=={(0,'A'):'selected',(1,'B'):'selected'}
    assert rep['assembly']=={'order':[0,1],'length':338,'full_sha256':TRUE_FULL}
    assert rep['manifest_digest']==sha256((FIX/'archive-case.json').read_bytes())
    out=Path(rep['recovered_file'])
    assert out.read_bytes()==TRUE_BYTES
    assert out.read_bytes()!=A0+A1 and out.read_bytes()!=B0+B1
    assert rep['import']['events_imported']==3 and rep['import']['duplicate_file'] is False
    assert json.loads(report_path.read_text(encoding='utf-8'))['attempt_id']==rep['attempt_id']
    src=rows(db,'SELECT name,sha256,raw FROM source')[0]
    assert src['sha256']==TRUE_FULL and src['raw']==TRUE_BYTES
    ev=rows(db,'SELECT device,seq,kind,source_sha256 FROM event JOIN evidence ON evidence.event_id=event.id')
    assert len(ev)==3 and {(e['device'],e['seq']) for e in ev}=={('D9',1),('D9',2),('D9',3)}
    assert all(e['source_sha256']==TRUE_FULL for e in ev)
    assert counts(db)==(1,3,1)

def test_unicode_boundary_forbids_per_chunk_decode(tmp_path):
    # chunk 0 ends with the lead byte of 展; decoding it alone must fail,
    # the recovery engine decodes only the concatenated stream.
    with pytest.raises(UnicodeDecodeError):CHUNK0.decode('utf-8')
    with pytest.raises(UnicodeDecodeError):CHUNK1.decode('utf-8')
    assert (CHUNK0+CHUNK1).decode('utf-8').count('\n')==3

def test_recovery_is_idempotent_and_reuses_one_source_identity(tmp_path):
    db=seed_db(tmp_path)
    first=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    second=recover(db,'ARCH-1','verified_mosaic',tmp_path/'out')
    third=recover(db,'ARCH-1','two_copy_quorum',tmp_path/'out')
    assert second['status']=='published' and second['reason']=='already_published'
    assert second['import']['duplicate_file'] is True
    assert third['status']=='blocked' and third['reason']=='quorum_not_met'
    assert counts(db)==(1,3,1)
    ledger=recovery_report(db,'ARCH-1')
    assert [a['attempt']['status'] for a in ledger]==['published','published','blocked']

def test_blocked_then_repaired_shard_allows_late_quorum(tmp_path):
    work=tmp_path/'casework'
    db=tmp_path/'c.db';initialize(db,FIX/'archive-config.json')
    case_src=work/'case';shutil.copytree(FIX,case_src)
    register(db,'ARCH-1',case_src/'archive-case.json')
    assert recover(db,'ARCH-1','two_copy_quorum',tmp_path/'out')['status']=='blocked'
    # repaired shards arrive after the freeze; live digest matches manifest,
    # frozen digest does not -> admissible evidence, not guessed text
    (case_src/'archive/A1.bin').write_bytes(B1)
    (case_src/'archive/B0.bin').write_bytes(A0)
    rep=recover(db,'ARCH-1','two_copy_quorum',tmp_path/'out')
    assert rep['status']=='published'
    fixes={(d['chunk'],d['replica']):d for d in rep['chunks'] if d['reason'].startswith('repaired_after_freeze')}
    assert set(fixes)=={(1,'A'),(0,'B')}
    assert all(d['decision'] in ('selected','witness') for d in fixes.values())
    assert counts(db)==(1,3,1)

# ---------- synthetic-case failure modes ----------

def synthetic(tmp,name,**kw):
    cf,manifest,doc=make_case(tmp/'cases',name,TRUE_BYTES,BOUNDARY,**kw)
    db=tmp/(name+'.db');initialize(db,FIX/'archive-config.json')
    register_case(db,name.upper(),cf);return db,manifest,doc

def test_single_bad_chunk_blocks_both_contracts(tmp_path):
    db,_,_=synthetic(tmp_path,'onebad',shard_mut={('A',1):flip_last,('B',1):flip_last})
    for contract in ('two_copy_quorum','verified_mosaic'):
        rep=recover(db,'ONEBAD',contract,tmp_path/('out-'+contract))
        assert rep['status']=='blocked' and rep['reason'] in ('quorum_not_met','shard_missing')
        assert all(d['decision']!='selected' for d in rep['chunks'] if d['chunk']==1)
    assert counts(db)==(0,0,0)

def test_complete_replica_is_preferred_in_mosaic(tmp_path):
    # both copies good; complete-replica selection must take all chunks from A
    db,_,_=synthetic(tmp_path,'complete')
    rep=recover(db,'COMPLETE','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published'
    assert {d['replica'] for d in rep['chunks'] if d['decision']=='selected'}=={'A'}
    assert all(d['reason']=='complete_replica_preferred' for d in rep['chunks'] if d['decision']=='selected')
    assert counts(db)==(1,3,1)

def test_quorum_requires_two_distinct_replicas(tmp_path):
    # only replica A survives; B shards are deleted after the freeze
    db,_,_=synthetic(tmp_path,'onlya')
    (tmp_path/'cases/onlya/archive/B0.bin').unlink()
    (tmp_path/'cases/onlya/archive/B1.bin').unlink()
    rep=recover(db,'ONLYA','two_copy_quorum',tmp_path/'out')
    assert rep['status']=='blocked'
    assert rep['reason'] in ('quorum_not_met','shard_missing')
    assert any(d['reason']=='shard_unreadable' for d in rep['chunks'])
    mosaic=recover(db,'ONLYA','verified_mosaic',tmp_path/'out2')
    assert mosaic['status']=='published'
    assert {d['replica'] for d in mosaic['chunks'] if d['decision']=='selected'}=={'A'}

def test_shuffled_shard_order_still_assembles_in_chunk_order(tmp_path):
    order=[('B',1),('A',1),('B',0),('A',0)]
    db,_,_=synthetic(tmp_path,'shuffle',shard_order=order)
    rep=recover(db,'SHUFFLE','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published'
    assert Path(rep['recovered_file']).read_bytes()==TRUE_BYTES

def test_cross_revision_mix_is_rejected(tmp_path):
    # register revision 1 manifest, then replace a shard with different bytes:
    # any assembly using it can no longer match the frozen revision-1 digest
    db,manifest,_=synthetic(tmp_path,'revmix',revision=1)
    (tmp_path/'cases/revmix/archive/B1.bin').write_bytes(flip_last(CHUNK1))
    rep=recover(db,'REVMIX','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published'  # A1 alone still matches rev1 manifest
    # now damage the remaining good chunk-1 copy too -> only foreign bytes left
    (tmp_path/'cases/revmix/archive/A1.bin').write_bytes(flip_last(CHUNK1))
    rep2=recover(db,'REVMIX','verified_mosaic',tmp_path/'out2')
    assert rep2['status']=='blocked' and rep2['reason']=='shard_missing'
    assert counts(db)==(1,3,1)

def test_manifest_revision_change_freezes_as_new_identity_and_blocks_old_ledger(tmp_path):
    # a revision-3 manifest over different bytes must never mix with rev1
    other=TRUE_BYTES.replace(b'D9',b'D8')
    cf3,m3,_=make_case(tmp_path/'cases','rev3',other,BOUNDARY,source_id='CAPTURE-X',revision=3)
    db=tmp_path/'r3.db';initialize(db,FIX/'archive-config.json')
    register(db,'R3',cf3)
    rep=recover(db,'R3','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published' and rep['revision']==3
    assert Path(rep['recovered_file']).name=='CAPTURE-X.rev3.jsonl'

def test_register_rejects_replacing_a_frozen_case_with_another_revision(tmp_path):
    # 清单换版: the same case id cannot silently change its frozen manifest
    db=seed_db(tmp_path,case_id='SWAP')
    other=TRUE_BYTES.replace(b'D9',b'D8')
    cf2,_,_=make_case(tmp_path/'cases','swap',other,BOUNDARY,source_id='CAPTURE-1',revision=3)
    with pytest.raises(ValueError,match='archive_case_conflict'):
        register(db,'SWAP',cf2)
    # original frozen revision-2 case is still intact and recoverable
    rep=recover(db,'SWAP','verified_mosaic',tmp_path/'out')
    assert rep['status']=='published' and rep['revision']==2
    assert Path(rep['recovered_file']).read_bytes()==TRUE_BYTES

def test_same_source_name_different_content_gets_distinct_case_identities(tmp_path):
    db1,_,_=synthetic(tmp_path,'name-a')
    other=TRUE_BYTES.replace(b'"D9"',b'"D7"')  # same length, different dossier bytes
    assert len(other)==len(TRUE_BYTES) and other!=TRUE_BYTES
    cf,_,_=make_case(tmp_path/'cases','name-b',other,BOUNDARY,source_id='CAPTURE-X')
    db2=tmp_path/'name-b.db';initialize(db2,FIX/'archive-config.json');register(db2,'NAME-B',cf)
    r1=recover(db1,'NAME-A','verified_mosaic',tmp_path/'oa')
    r2=recover(db2,'NAME-B','verified_mosaic',tmp_path/'ob')
    assert r1['assembly']['full_sha256']!=r2['assembly']['full_sha256']
    assert Path(r1['recovered_file']).name==Path(r2['recovered_file']).name=='CAPTURE-X.rev1.jsonl'
    assert Path(r1['recovered_file']).resolve()!=Path(r2['recovered_file']).resolve()

def test_full_digest_tamper_blocks_without_import(tmp_path):
    db,manifest,_=synthetic(tmp_path,'fdtamper',manifest_over={'full_sha256':'00'*32})
    rep=recover(db,'FDTAMPER','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked' and rep['reason']=='full_digest_mismatch'
    assert counts(db)==(0,0,0)

def test_length_tamper_blocks(tmp_path):
    db,_,_=synthetic(tmp_path,'lentamper',manifest_over={'length':999})
    rep=recover(db,'LENTAMPER','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked' and rep['reason']=='length_mismatch'

def test_chunk_digest_tamper_blocks_every_selection(tmp_path):
    db,_,_=synthetic(tmp_path,'cdtamper',
                     manifest_over={'chunk_sha256':['11'*32,'22'*32]})
    rep=recover(db,'CDTAMPER','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked'
    assert all(d['decision']=='rejected' for d in rep['chunks'])

def test_invalid_utf8_assembly_is_blocked(tmp_path):
    # truncating the last shard destroys the final newline structure and length;
    # build bytes whose assembly decodes but whose event structure is bad instead
    bad=b'{"kind":"enter","device":"D","seq":1,"at":"2026-01-01T08:00:00+00:00","gate":"G1"}\n{not json}\n'
    cf,_,_=make_case(tmp_path/'cases','badjson',bad,40,expected=2)
    db=tmp_path/'badjson.db';initialize(db,FIX/'archive-config.json');register(db,'BADJSON',cf)
    rep=recover(db,'BADJSON','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked' and rep['reason'].startswith('event_structure_invalid')
    assert counts(db)==(0,0,0)

def test_event_count_mismatch_blocks(tmp_path):
    cf,_,_=make_case(tmp_path/'cases','countm',TRUE_BYTES,BOUNDARY,expected=99)
    db=tmp_path/'countm.db';initialize(db,FIX/'archive-config.json');register(db,'COUNTM',cf)
    rep=recover(db,'COUNTM','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked' and rep['reason']=='event_count_mismatch'

def test_duplicate_chunk_entry_blocks(tmp_path):
    dup={'path':'archive/A0-copy.bin','replica':'A','chunk':0,'data':CHUNK0}
    db,_,_=synthetic(tmp_path,'dupchunk',extra_shards=[dup])
    rep=recover(db,'DUPCHUNK','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked' and rep['reason']=='duplicate_chunk'
    assert any(d['reason']=='duplicate_chunk' for d in rep['chunks'])

def test_missing_shard_file_blocks(tmp_path):
    db,_,_=synthetic(tmp_path,'missing')
    # every copy of chunk 1 is gone -> no contract can assemble the dossier
    (tmp_path/'cases/missing/archive/A1.bin').unlink()
    (tmp_path/'cases/missing/archive/B1.bin').unlink()
    rep=recover(db,'MISSING','verified_mosaic',tmp_path/'out')
    assert rep['status']=='blocked'
    assert sum(1 for d in rep['chunks'] if d['reason']=='shard_unreadable')==2
    assert counts(db)==(0,0,0)

def test_shard_drift_after_freeze_is_rejected(tmp_path):
    db,_,_=synthetic(tmp_path,'drift')
    (tmp_path/'cases/drift/archive/A0.bin').write_bytes(flip_last(CHUNK0))
    rep=recover(db,'DRIFT','verified_mosaic',tmp_path/'out')
    # B0 still carries the manifest-matching chunk 0
    assert rep['status']=='published'
    d=next(x for x in rep['chunks'] if (x['chunk'],x['replica'])==(0,'A'))
    assert d['decision']=='rejected' and d['reason']=='shard_drifted_after_freeze'

def test_unknown_contract_rejected(tmp_path):
    db=seed_db(tmp_path)
    with pytest.raises(ValueError,match='unknown_contract'):
        recover(db,'ARCH-1','made_up',tmp_path/'out')

def test_original_plain_ingest_remains_compatible(tmp_path):
    db=tmp_path/'plain.db';initialize(db,FIX/'config.json')
    r1=ingest(FIX/'events.jsonl',db);r2=ingest(FIX/'events.jsonl',db)
    assert r1=={'imported':5,'duplicate_file':False}
    assert r2=={'imported':0,'duplicate_file':True}
