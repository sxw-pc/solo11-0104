from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.ticket_refunds import register
from gateflow.ingest import ingest
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_refund_freezes_real_ticket_sales(tmp_path):
    db=tmp_path/'refund.db';initialize(db,FIX/'refund-config.json')
    assert ingest(FIX/'refund-events.jsonl',db)['imported']==2
    got=register(db,'CANCEL-1',FIX/'refund-case.json')
    assert got['claims']==5
    assert register(db,'CANCEL-1',FIX/'refund-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM ticket_sale').fetchone()[0]==6
    finally:c.close()
def test_refund_requires_real_ticket(tmp_path):
    with pytest.raises(ValueError,match='refund_ticket_missing'):
        register(tmp_path/'missing.db','CANCEL-1',FIX/'refund-case.json')
