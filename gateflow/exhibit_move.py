"""Freeze exhibition inventory and movement requests; routing is deliberately absent."""
import hashlib,json
from pathlib import Path
from .store import connect

def load_catalog(db_path,fixture):
    doc=json.loads(Path(fixture).read_text(encoding='utf-8'))
    db=connect(db_path)
    try:
        with db:
            for item in doc['exhibits']:
                if not db.execute('SELECT 1 FROM zone WHERE id=?',(item['target_zone'],)).fetchone():
                    raise ValueError('exhibit_zone_missing')
                db.execute('INSERT OR IGNORE INTO exhibit VALUES (?,?,?,?,?,?)',(item['id'],item['name'],item['current_node'],item['target_node'],item['target_zone'],item['crate_units']))
    finally:db.close()
    return {'exhibits':len(doc['exhibits'])}

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('moves') or not doc.get('arcs') or not doc.get('crews'):
        raise ValueError('bad_move_case')
    db=connect(db_path)
    try:
        with db:
            items=[]
            for move in doc['moves']:
                row=db.execute('SELECT * FROM exhibit WHERE id=?',(move['exhibit'],)).fetchone()
                if not row:raise ValueError('exhibit_missing')
                items.append(dict(row))
            if len({x['id'] for x in items})!=len(items):raise ValueError('duplicate_exhibit')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'exhibits':items},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM exhibit_move_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('move_case_conflict')
            db.execute('INSERT OR IGNORE INTO exhibit_move_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'moves':len(items)}
