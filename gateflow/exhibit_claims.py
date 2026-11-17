"""Freeze insured exhibit, signed damage inspection, and issued advance."""
import hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('asset') or not doc.get('inspection') or not doc.get('advance'):
        raise ValueError('bad_claim_case')
    asset=doc['asset']; advance=doc['advance']
    attachment=(path.parent/doc['inspection']['attachment']).resolve()
    if hashlib.sha256(attachment.read_bytes()).hexdigest()!=doc['inspection']['attachment_sha256']:
        raise ValueError('inspection_attachment_mismatch')
    db=connect(db_path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM zone WHERE id=?',(asset['zone'],)).fetchone():
                raise ValueError('claim_zone_missing')
            old=db.execute('SELECT owner,zone,agreed_value_cents FROM insured_exhibit WHERE id=?',(asset['id'],)).fetchone()
            if old and dict(old)!={'owner':asset['owner'],'zone':asset['zone'],'agreed_value_cents':asset['agreed_value_cents']}:
                raise ValueError('claim_asset_conflict')
            db.execute('INSERT OR IGNORE INTO insured_exhibit VALUES (?,?,?,?)',(asset['id'],asset['owner'],asset['zone'],asset['agreed_value_cents']))
            old=db.execute('SELECT exhibit,paid_cents FROM claim_advance WHERE id=?',(advance['id'],)).fetchone()
            if old and dict(old)!={'exhibit':asset['id'],'paid_cents':advance['paid_cents']}:
                raise ValueError('advance_conflict')
            db.execute('INSERT OR IGNORE INTO claim_advance VALUES (?,?,?)',(advance['id'],asset['id'],advance['paid_cents']))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM exhibit_claim_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('claim_case_conflict')
            db.execute('INSERT OR IGNORE INTO exhibit_claim_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'advance':advance['id']}
