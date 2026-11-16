from gateflow.__main__ import demo
from gateflow.store import connect
import json
def test_evidence_and_run(tmp_path):
    p=demo(tmp_path);data=json.loads(p.read_text(encoding='utf-8'))
    assert len(data['evidence'])==5
    assert len(data['run'])==1 and data['run'][0]['status'] in ('已判定','待核')
    db=connect(tmp_path/'gateflow.db')
    assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
    assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==5
    db.close()
def test_rerun_keeps_evidence_and_run_history(tmp_path):
    demo(tmp_path)
    from gateflow.rules import evaluate
    evaluate(tmp_path/'gateflow.db')
    db=connect(tmp_path/'gateflow.db')
    assert db.execute('SELECT count(*) FROM evidence').fetchone()[0]==5
    assert db.execute('SELECT count(*) FROM run').fetchone()[0]==2
    db.close()
