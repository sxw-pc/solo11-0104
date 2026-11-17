from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.exhibit_claims import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_claim_freezes_inspection_and_advance(tmp_path):
    db=tmp_path/'claim.db';initialize(db,FIX/'claim-config.json')
    got=register(db,'CASE-1',FIX/'exhibit-claim.json')
    assert got['advance']=='ADV-1'
    assert register(db,'CASE-1',FIX/'exhibit-claim.json')==got
    c=connect(db)
    try:assert c.execute('SELECT paid_cents FROM claim_advance').fetchone()[0]==60000
    finally:c.close()
def test_claim_requires_real_gallery(tmp_path):
    with pytest.raises(ValueError,match='claim_zone_missing'):
        register(tmp_path/'missing.db','CASE-1',FIX/'exhibit-claim.json')
