from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.exhibit_move import load_catalog,register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_exhibit_move_freezes_real_inventory(tmp_path):
    db=tmp_path/'move.db';initialize(db,FIX/'exhibit-config.json')
    assert load_catalog(db,FIX/'exhibit-catalog.json')=={'exhibits':2}
    got=register(db,'M-1',FIX/'exhibit-move-case.json')
    assert got['moves']==2
    assert register(db,'M-1',FIX/'exhibit-move-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM exhibit_move_case').fetchone()[0]==1
    finally:c.close()
def test_move_requires_real_inventory(tmp_path):
    db=tmp_path/'move.db';initialize(db,FIX/'exhibit-config.json')
    with pytest.raises(ValueError,match='exhibit_missing'):
        register(db,'M-1',FIX/'exhibit-move-case.json')
