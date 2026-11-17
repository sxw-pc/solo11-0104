"""Freeze sold ticket identities and grouped refund claims for review."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('sales') or not doc.get('claims') or not doc.get('review_slots'):
        raise ValueError('bad_refund_case')
    db=connect(db_path)
    try:
        with db:
            for sale in doc['sales']:
                if not db.execute('SELECT 1 FROM ticket WHERE id=?',(sale['ticket'],)).fetchone():
                    raise ValueError('refund_ticket_missing')
                old=db.execute('SELECT customer,paid_cents FROM ticket_sale WHERE id=?',(sale['id'],)).fetchone()
                if old and (old['customer']!=sale['customer'] or old['paid_cents']!=sale['paid_cents']):
                    raise ValueError('sale_conflict')
                db.execute('INSERT OR IGNORE INTO ticket_sale VALUES (?,?,?,?)',(sale['id'],sale['ticket'],sale['customer'],sale['paid_cents']))
            ticket_evidence=[dict(e) for e in db.execute('SELECT e.id,e.ticket,e.kind,e.sha256,s.sha256 AS source_sha256 FROM event e JOIN source s ON s.id=e.source_id WHERE e.ticket IS NOT NULL ORDER BY e.id')]
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'ticket_events':ticket_evidence},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM refund_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('refund_case_conflict')
            db.execute('INSERT OR IGNORE INTO refund_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'claims':len(doc['claims'])}
