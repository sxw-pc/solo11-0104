"""Freeze actual exhibit register and condition inspection for opening readiness."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('exhibits') or not doc.get('inspection') or not doc.get('repair_slots'):
        raise ValueError('bad_conservation_case')
    db=connect(db_path)
    try:
        with db:
            for item in doc['exhibits']:
                if not db.execute('SELECT 1 FROM zone WHERE id=?',(item['zone'],)).fetchone():
                    raise ValueError('conservation_zone_missing')
                old=db.execute('SELECT zone,name FROM exhibit_registry WHERE id=?',(item['id'],)).fetchone()
                if old and dict(old)!={'zone':item['zone'],'name':item['name']}:
                    raise ValueError('exhibit_registry_conflict')
                db.execute('INSERT OR IGNORE INTO exhibit_registry VALUES (?,?,?)',(item['id'],item['zone'],item['name']))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM conservation_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('conservation_case_conflict')
            db.execute('INSERT OR IGNORE INTO conservation_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'exhibits':len(doc['exhibits'])}
