"""Freeze gallery climate observations and a finite HVAC shift."""
import csv,hashlib,json
from pathlib import Path
from .store import connect

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('zones') or not doc.get('sensors_file') or not doc.get('slots'):
        raise ValueError('bad_climate_case')
    sensor_path=(path.parent/doc['sensors_file']).resolve();sensor_raw=sensor_path.read_bytes()
    readings=list(csv.DictReader(sensor_raw.decode('utf-8-sig').splitlines()))
    if not readings:raise ValueError('climate_readings_missing')
    db=connect(db_path)
    try:
        with db:
            for zone in doc['zones']:
                if not db.execute('SELECT 1 FROM zone WHERE id=?',(zone['id'],)).fetchone():
                    raise ValueError('climate_zone_missing')
            digest=hashlib.sha256(raw+sensor_raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'readings':readings,'sensor_sha256':hashlib.sha256(sensor_raw).hexdigest()},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM climate_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('climate_case_conflict')
            db.execute('INSERT OR IGNORE INTO climate_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'readings':len(readings)}
