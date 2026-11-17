"""Freeze venue zone requirements and external vendor offers."""
import csv,hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('zone_requirements') or not doc.get('quotes_file'):
        raise ValueError('bad_venue_procurement_case')
    quote_path=(path.parent/doc['quotes_file']).resolve();quote_raw=quote_path.read_bytes()
    quotes=list(csv.DictReader(quote_raw.decode('utf-8-sig').splitlines()))
    if not quotes:raise ValueError('quotes_empty')
    db=connect(db_path)
    try:
        with db:
            zones=[]
            for item in doc['zone_requirements']:
                if not db.execute('SELECT 1 FROM zone WHERE id=?',(item['zone'],)).fetchone():
                    raise ValueError('venue_zone_missing')
                zones.append(item['zone'])
            digest=hashlib.sha256(raw+quote_raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'zones':zones,'quotes':quotes,'quote_sha256':hashlib.sha256(quote_raw).hexdigest()},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM venue_procurement_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('venue_procurement_conflict')
            db.execute('INSERT OR IGNORE INTO venue_procurement_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'quotes':len(quotes)}
