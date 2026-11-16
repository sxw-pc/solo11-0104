import json
from collections import defaultdict
from datetime import timedelta
from .ingest import at
from .store import connect
VERSION='1'
def evaluate(path):
    db=connect(path);result={}
    with db:
        db.execute('DELETE FROM occupancy');db.execute('DELETE FROM decision')
        db.execute("DELETE FROM issue WHERE code NOT IN ('parse_error','identity_conflict')")
        events=[dict(x) for x in db.execute('SELECT * FROM event ORDER BY at,id')]
        zones={x['id']:x['capacity'] for x in db.execute('SELECT * FROM zone')};maps=[dict(x) for x in db.execute('SELECT * FROM gate_map')]
        tickets={x['id']:x['type'] for x in db.execute('SELECT * FROM ticket')}
        valid={(e['device'],e['seq']):e for e in events};cancelled=set();issues=defaultdict(list);unknown=set()
        for e in events:
            if e['kind']!='cancel':continue
            key=(e['ref_device'],e['ref_seq']);target=valid.get(key)
            if not target or target['kind'] in {'cancel','heartbeat'} or key in cancelled:
                issues['cancel_invalid'].append((e['gate'] or '',e['id']));continue
            cancelled.add(key)
        sequences=defaultdict(list)
        for e in events:sequences[e['device']].append(e)
        for device,items in sequences.items():
            seq={e['seq'] for e in items}
            if max(seq)-min(seq)+1!=len(seq):
                for e in items:
                    if e['gate']:unknown.add(e['gate'])
                issues['sequence_gap'].append((device,None))
        presence={};counts={z:0 for z in zones};peak=counts.copy();by_zone=defaultdict(list)
        heartbeats=defaultdict(list)
        for e in events:
            if e['kind']=='heartbeat':heartbeats[e['device']].append(at(e['at']))
        for e in events:
            if e['kind'] in {'heartbeat','cancel'} or (e['device'],e['seq']) in cancelled:continue
            t=at(e['at']);mapping=[m for m in maps if m['gate']==e['gate'] and at(m['start'])<=t<at(m['end'])]
            if len(mapping)!=1:
                issues['map_missing'].append((e['gate'],e['id']));unknown.add(e['gate']);continue
            zone=mapping[0]['zone'];ticket=e['ticket']
            if ticket not in tickets:issues['ticket_unknown'].append((zone,e['id']));continue
            current=presence.get(ticket)
            if e['kind']=='enter':
                if current is not None:issues['duplicate_entry'].append((zone,e['id']));continue
                presence[ticket]=zone;counts[zone]+=1
            elif e['kind']=='exit':
                if current is None:issues['orphan_exit'].append((zone,e['id']));continue
                presence.pop(ticket);counts[current]-=1
            elif e['kind']=='move':
                if current is None:issues['orphan_move'].append((zone,e['id']));continue
                if current!=zone:counts[current]-=1;counts[zone]+=1;presence[ticket]=zone
            for z,n in counts.items():
                if n<0:issues['negative_occupancy'].append((z,e['id']))
                peak[z]=max(peak[z],n)
                db.execute('INSERT INTO occupancy(at,zone,count,confidence,event_id) VALUES (?,?,?,?,?)',(e['at'],z,n,'未知' if unknown else '可信',e['id']))
        for device,items in sequences.items():
            if not any(e['kind']=='heartbeat' for e in items):
                issues['heartbeat_gap'].append((device,None));unknown.update(e['gate'] for e in items if e['gate']);continue
            beat=sorted(heartbeats[device]);times=[at(e['at']) for e in items]
            if beat and (times[0]<beat[0]-timedelta(minutes=30) or times[-1]>beat[-1]+timedelta(minutes=30)):
                issues['heartbeat_gap'].append((device,None));unknown.update(e['gate'] for e in items if e['gate'])
        for code,entries in issues.items():
            for subject,eid in entries:db.execute('INSERT INTO issue(code,subject,event_id,detail) VALUES (?,?,?,?)',(code,subject,eid,code))
        for zone,limit in zones.items():
            codes=[c for c,entries in issues.items() if any(s==zone for s,_ in entries)]
            status='超限' if peak[zone]>limit else ('证据不足' if unknown or codes else '可信')
            detail=json.dumps({'issues':codes,'capacity':limit,'ticket_types':tickets},ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(zone,status,peak[zone],detail,VERSION));result[zone]=status
    db.close();return result
