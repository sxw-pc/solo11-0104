"""Freeze damaged archive shards and manifest; no reconstruction implementation."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('manifest') or not doc.get('shards') or not doc.get('contracts'):
        raise ValueError('bad_archive_case')
    root=path.parent
    observed=[]
    for shard in doc['shards']:
        candidate=(root/shard['path']).resolve()
        if not candidate.is_relative_to(root):raise ValueError('shard_path_escape')
        data=candidate.read_bytes()
        observed.append({'path':shard['path'],'chunk':shard['chunk'],'replica':shard['replica'],'sha256':hashlib.sha256(data).hexdigest(),'byte_length':len(data)})
    db=connect(db_path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM gate_map WHERE gate=?',(doc['gate'],)).fetchone():
                raise ValueError('archive_gate_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'observed_shards':observed},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM archive_recovery_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('archive_case_conflict')
            db.execute('INSERT OR IGNORE INTO archive_recovery_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'shards':len(observed)}
