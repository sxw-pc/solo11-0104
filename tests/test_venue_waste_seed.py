from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.venue_waste import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_venue_waste_freezes_zone_containers_and_routes(tmp_path):
    db=tmp_path/'waste.db';initialize(db,FIX/'venue-waste-config.json')
    got=register(db,'VW-1',FIX/'venue-waste-case.json')
    assert got['containers']==2
    assert register(db,'VW-1',FIX/'venue-waste-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM venue_waste_container').fetchone()[0]==2
    finally:c.close()
def test_venue_waste_requires_real_zone(tmp_path):
    with pytest.raises(ValueError,match='venue_waste_zone_missing'):
        register(tmp_path/'missing.db','VW-1',FIX/'venue-waste-case.json')
