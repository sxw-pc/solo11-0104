from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.lost_property import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_property_freezes_item_and_two_claims(tmp_path):
    db=tmp_path/'property.db';initialize(db,FIX/'property-config.json')
    got=register(db,'PROP-1',FIX/'property-case.json')
    assert got['claims']==2
    assert register(db,'PROP-1',FIX/'property-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM property_item').fetchone()[0]==1
    finally:c.close()
def test_property_requires_real_ticket(tmp_path):
    with pytest.raises(ValueError,match='deposit_ticket_missing'):
        register(tmp_path/'missing.db','PROP-1',FIX/'property-case.json')
