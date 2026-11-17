from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.archive_lab import register
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_archive_freezes_damaged_shards_without_plain_source(tmp_path):
    db=tmp_path/'archive.db';initialize(db,FIX/'archive-config.json')
    got=register(db,'ARCH-1',FIX/'archive-case.json')
    assert got['shards']==4
    assert register(db,'ARCH-1',FIX/'archive-case.json')==got
    c=connect(db)
    try:assert c.execute('SELECT COUNT(*) FROM archive_recovery_case').fetchone()[0]==1
    finally:c.close()
def test_archive_requires_real_gate(tmp_path):
    with pytest.raises(ValueError,match='archive_gate_missing'):
        register(tmp_path/'missing.db','ARCH-1',FIX/'archive-case.json')
