from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.venue_procurement import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_venue_buy_freezes_zones_and_quotes(tmp_path):
    db=tmp_path/'venue.db';initialize(db,FIX/'venue-buy-config.json')
    got=register(db,'BUY-1',FIX/'venue-buy-case.json')
    assert got['quotes']==4
    assert register(db,'BUY-1',FIX/'venue-buy-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM venue_procurement_case').fetchone()[0]==1
    finally:c.close()
def test_venue_buy_requires_real_zones(tmp_path):
    with pytest.raises(ValueError,match='venue_zone_missing'):
        register(tmp_path/'missing.db','BUY-1',FIX/'venue-buy-case.json')
