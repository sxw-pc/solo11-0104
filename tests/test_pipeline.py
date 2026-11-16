import json
from pathlib import Path
from gateflow.__main__ import demo,initialize
from gateflow.ingest import ingest
from gateflow.rules import evaluate
from gateflow.store import connect
FIX=Path(__file__).resolve().parent.parent/'fixtures'
def setup(tmp_path):
    db=tmp_path/'x.db';initialize(db,FIX/'config.json');return db
def event(**changes):
    r={'kind':'enter','device':'D','seq':1,'at':'2026-01-01T08:00:00+00:00','gate':'G1','ticket':'T1'};r.update(changes);return r
def add(tmp_path,db,items):
    p=tmp_path/'extra.jsonl';p.write_text('\n'.join(json.dumps(x) for x in items),encoding='utf-8');return ingest(p,db)
def test_demo(tmp_path):
    p=demo(tmp_path);j=json.loads(p.read_text(encoding='utf-8'));assert j['decision'][0]['status']=='可信';assert p.with_suffix('.md').exists()
def test_duplicate_file(tmp_path):
    db=setup(tmp_path);assert ingest(FIX/'events.jsonl',db)['imported']==5;assert ingest(FIX/'events.jsonl',db)['duplicate_file']
def test_duplicate_entry(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(),event(seq=2,at='2026-01-01T08:01:00+00:00')]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='duplicate_entry'").fetchone()[0]==1
def test_orphan_exit(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(kind='exit')]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='orphan_exit'").fetchone()[0]==1
def test_mapping_version(tmp_path):
    db=setup(tmp_path);c=connect(db);c.execute('DELETE FROM gate_map');c.commit();c.close();add(tmp_path,db,[event()]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='map_missing'").fetchone()[0]==1
def test_capacity(tmp_path):
    db=setup(tmp_path);c=connect(db);c.execute('UPDATE zone SET capacity=0 WHERE id="A"');c.commit();c.close()
    add(tmp_path,db,[event(),event(seq=2,kind='heartbeat',gate=None,at='2026-01-01T08:02:00+00:00')]);assert evaluate(db)['A']=='超限'
def test_sequence_gap(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(),event(seq=3,kind='exit',at='2026-01-01T09:00:00+00:00')]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='sequence_gap'").fetchone()[0]>=1
def test_cancel(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(),event(seq=2,kind='cancel',ref_device='D',ref_seq=1,at='2026-01-01T09:00:00+00:00')]);evaluate(db)
    c=connect(db);assert c.execute('SELECT max(peak) FROM decision').fetchone()[0]==0
def test_cancel_twice(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(),event(seq=2,kind='cancel',ref_device='D',ref_seq=1,at='2026-01-01T09:00:00+00:00'),event(seq=3,kind='cancel',ref_device='D',ref_seq=1,at='2026-01-01T10:00:00+00:00')]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='cancel_invalid'").fetchone()[0]==1
