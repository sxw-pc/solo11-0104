"""Local gate collector firmware endpoint simulator; no rollout coordinator."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('devices') or not doc.get('target_version'):
        raise ValueError('bad_gate_firmware_case')
    db=connect(db_path)
    try:
        with db:
            observed=[]
            for device in doc['devices']:
                if not db.execute('SELECT 1 FROM gate_map WHERE gate=?',(device['gate'],)).fetchone():
                    raise ValueError('firmware_gate_missing')
                event=db.execute('SELECT e.id,e.device,e.gate,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.device=? AND e.gate=? ORDER BY e.id LIMIT 1',(device['id'],device['gate'])).fetchone()
                if not event:raise ValueError('firmware_device_evidence_missing')
                observed.append(dict(event))
                old=db.execute('SELECT gate,cell,version,revision FROM gate_firmware_device WHERE id=?',(device['id'],)).fetchone()
                expected={'gate':device['gate'],'cell':device['cell'],'version':device['current_version'],'revision':0}
                if old and dict(old)!=expected:raise ValueError('firmware_device_conflict')
                db.execute('INSERT OR IGNORE INTO gate_firmware_device VALUES (?,?,?,?,0)',(device['id'],device['gate'],device['cell'],device['current_version']))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'observed':observed},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM gate_firmware_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('gate_firmware_case_conflict')
            db.execute('INSERT OR IGNORE INTO gate_firmware_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'devices':len(doc['devices'])}

def state(db_path,device):
    db=connect(db_path)
    try:
        row=db.execute('SELECT id,gate,cell,version,revision FROM gate_firmware_device WHERE id=?',(device,)).fetchone()
        if not row:raise ValueError('unknown_gate_firmware_device')
        return dict(row)
    finally:db.close()

def invoke(db_path,case_id,device,command,version,operation_id,expected_revision):
    if command not in ('install','rollback'):raise ValueError('bad_gate_firmware_command')
    db=connect(db_path)
    try:
        with db:
            prior=db.execute('SELECT result FROM gate_firmware_operation WHERE id=?',(operation_id,)).fetchone()
            if prior:return {'status':'replayed','record':json.loads(prior['result'])}
            row=db.execute('SELECT version,revision FROM gate_firmware_device WHERE id=?',(device,)).fetchone()
            if not row:raise ValueError('unknown_gate_firmware_device')
            if row['revision']!=expected_revision:return {'status':'revision_conflict','current':dict(row)}
            case=db.execute('SELECT payload FROM gate_firmware_case WHERE id=?',(case_id,)).fetchone()
            if not case:raise ValueError('unknown_gate_firmware_case')
            spec=next((x for x in json.loads(case['payload'])['case']['devices'] if x['id']==device),None)
            if not spec:raise ValueError('device_not_in_case')
            if command=='install' and spec.get('failure')=='reject':
                outcome={'status':'rejected','version':row['version'],'revision':row['revision']}
            else:
                db.execute('UPDATE gate_firmware_device SET version=?,revision=revision+1 WHERE id=?',(version,device))
                outcome={'status':'applied','version':version,'revision':row['revision']+1}
            db.execute('INSERT INTO gate_firmware_operation VALUES (?,?,?,?)',(operation_id,device,command,json.dumps(outcome,sort_keys=True)))
            if command=='install' and spec.get('failure')=='commit_then_ack_lost':return {'status':'ack_lost'}
            return outcome
    finally:db.close()
