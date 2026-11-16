import json
from pathlib import Path
from gateflow.__main__ import initialize,demo
from gateflow.ingest import ingest
from gateflow.rules import evaluate
from gateflow.store import connect
FIX=Path(__file__).resolve().parent.parent/'fixtures'
def setup(tmp):
    db=tmp/'x.db';initialize(db,FIX/'config.json');return db
def add(tmp,db,rows):
    p=tmp/'e.jsonl';p.write_text('\n'.join(json.dumps(r) for r in rows),encoding='utf-8');ingest(p,db)
def event(**changes):
    r={'kind':'enter','device':'D','seq':1,'at':'2026-01-01T08:00:00+00:00','gate':'G1','ticket':'T1'};r.update(changes);return r
def test_no_heartbeat_marks_uncertain(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event()]);assert evaluate(db)['A']=='证据不足'
def test_unknown_ticket_is_issue(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(ticket='UNKNOWN')]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='ticket_unknown'").fetchone()[0]==1
def test_orphan_move_does_not_increase_count(tmp_path):
    db=setup(tmp_path);add(tmp_path,db,[event(kind='move')]);evaluate(db)
    c=connect(db);assert c.execute('SELECT peak FROM decision WHERE zone="A"').fetchone()[0]==0
def test_conflicting_mapping_is_issue(tmp_path):
    db=setup(tmp_path);c=connect(db);c.execute("INSERT INTO gate_map VALUES ('G1','B','2026-01-01T00:00:00+00:00','2027-01-01T00:00:00+00:00')");c.commit();c.close();add(tmp_path,db,[event()]);evaluate(db)
    c=connect(db);assert c.execute("SELECT count(*) FROM issue WHERE code='map_missing'").fetchone()[0]==1
def test_report_contains_ticket_and_occupancy(tmp_path):
    d=json.loads(demo(tmp_path).read_text(encoding='utf-8'));assert {x['type'] for x in d['ticket']}=={'adult','child'};assert d['occupancy']
