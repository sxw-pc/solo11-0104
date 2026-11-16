import hashlib
import json
from datetime import datetime
from pathlib import Path
from .store import connect
KINDS={'enter','exit','move','cancel','heartbeat'}
def at(s):
    d=datetime.fromisoformat(s.replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('timezone_required')
    return d
def ingest(path,db_path):
    raw=Path(path).read_bytes();filehash=hashlib.sha256(raw).hexdigest();db=connect(db_path)
    if db.execute('SELECT 1 FROM source WHERE sha256=?',(filehash,)).fetchone():db.close();return {'imported':0,'duplicate_file':True}
    count=0
    with db:
        sid=db.execute('INSERT INTO source(name,sha256,raw) VALUES (?,?,?)',(str(path),filehash,raw)).lastrowid
        for line,rawline in enumerate(raw.decode('utf-8-sig').splitlines(),1):
            if not rawline.strip():continue
            try:
                r=json.loads(rawline);kind=r['kind'];device=str(r['device']);seq=int(r['seq']);t=r['at'];at(t)
                if kind not in KINDS:raise ValueError('unknown_kind')
                if kind!='heartbeat' and not r.get('gate'):raise ValueError('gate_required')
                h=hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':')).encode()).hexdigest()
                old=db.execute('SELECT sha256 FROM event WHERE device=? AND seq=?',(device,seq)).fetchone()
                if old:
                    if old['sha256']!=h:db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('identity_conflict',device,f'{device}:{seq}'))
                    continue
                eid=db.execute('INSERT INTO event(source_id,line_no,device,seq,kind,at,gate,ticket,ref_device,ref_seq,payload,sha256) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (sid,line,device,seq,kind,t,r.get('gate'),r.get('ticket'),r.get('ref_device'),r.get('ref_seq'),rawline,h)).lastrowid;db.execute('INSERT INTO evidence(event_id,source_sha256,payload_sha256,role) VALUES (?,?,?,?)',(eid,filehash,h,'gate_event'));count+=1
            except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
                db.execute('INSERT INTO issue(code,subject,detail) VALUES (?,?,?)',('parse_error',str(line),str(exc)))
    db.close();return {'imported':count,'duplicate_file':False}
