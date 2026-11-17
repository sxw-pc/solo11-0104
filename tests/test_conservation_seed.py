from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.conservation import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_conservation_freezes_real_gallery_exhibits(tmp_path):
    db=tmp_path/'conservation.db';initialize(db,FIX/'conservation-config.json')
    got=register(db,'CONS-1',FIX/'conservation-case.json')
    assert got['exhibits']==4
    assert register(db,'CONS-1',FIX/'conservation-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM exhibit_registry').fetchone()[0]==4
    finally:c.close()
def test_conservation_requires_real_zone(tmp_path):
    with pytest.raises(ValueError,match='conservation_zone_missing'):
        register(tmp_path/'missing.db','CONS-1',FIX/'conservation-case.json')
