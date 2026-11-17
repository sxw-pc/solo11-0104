"""Freeze a held item, custody log and competing physical claims."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('item') or not doc.get('custody') or not doc.get('claims'):
        raise ValueError('bad_property_case')
    item=doc['item'];db=connect(db_path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM ticket WHERE id=?',(item['deposit_ticket'],)).fetchone():
                raise ValueError('deposit_ticket_missing')
            holder=doc['ticket_holder']
            old=db.execute('SELECT person FROM ticket_holder WHERE ticket=?',(item['deposit_ticket'],)).fetchone()
            if old and old['person']!=holder:raise ValueError('holder_conflict')
            db.execute('INSERT OR IGNORE INTO ticket_holder VALUES (?,?)',(item['deposit_ticket'],holder))
            receipt_digest=hashlib.sha256(doc['receipt_serial'].encode()).hexdigest()
            old=db.execute('SELECT deposit_ticket,receipt_digest,seal FROM property_item WHERE id=?',(item['id'],)).fetchone()
            expected={'deposit_ticket':item['deposit_ticket'],'receipt_digest':receipt_digest,'seal':item['seal']}
            if old and dict(old)!=expected:raise ValueError('property_item_conflict')
            db.execute('INSERT OR IGNORE INTO property_item VALUES (?,?,?,?)',(item['id'],item['deposit_ticket'],receipt_digest,item['seal']))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'receipt_digest':receipt_digest},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM property_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('property_case_conflict')
            db.execute('INSERT OR IGNORE INTO property_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'claims':len(doc['claims'])}
