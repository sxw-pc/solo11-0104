from pathlib import Path
import pytest
from gateflow.__main__ import initialize
from gateflow.ingest import ingest
from gateflow.gate_firmware import register,state,invoke
FIX=Path(__file__).resolve().parents[1]/'fixtures'
def test_gate_firmware_lab_freezes_real_devices_and_faults(tmp_path):
    db=tmp_path/'firmware.db';initialize(db,FIX/'gate-firmware-config.json')
    assert ingest(FIX/'gate-firmware-events.jsonl',db)['imported']==3
    assert register(db,'FIRM-1',FIX/'gate-firmware-case.json')['devices']==3
    assert state(db,'D1')['version']=='v1'
    assert invoke(db,'FIRM-1','D2','install','v2','OP-D2',0)=={'status':'ack_lost'}
    assert state(db,'D2')['version']=='v2'
    assert invoke(db,'FIRM-1','D3','install','v2','OP-D3',0)['status']=='rejected'
def test_gate_firmware_requires_device_events(tmp_path):
    db=tmp_path/'firmware.db';initialize(db,FIX/'gate-firmware-config.json')
    with pytest.raises(ValueError,match='firmware_device_evidence_missing'):
        register(db,'FIRM-1',FIX/'gate-firmware-case.json')
