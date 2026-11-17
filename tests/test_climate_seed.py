from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.climate_control import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_climate_freezes_two_gallery_observations(tmp_path):
    db=tmp_path/'climate.db';initialize(db,FIX/'climate-config.json')
    got=register(db,'CLIMATE-1',FIX/'climate-case.json')
    assert got['readings']==2
    assert register(db,'CLIMATE-1',FIX/'climate-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM climate_case').fetchone()[0]==1
    finally:c.close()
def test_climate_requires_real_zones(tmp_path):
    with pytest.raises(ValueError,match='climate_zone_missing'):
        register(tmp_path/'missing.db','CLIMATE-1',FIX/'climate-case.json')
