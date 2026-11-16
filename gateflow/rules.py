import json
from collections import defaultdict
from datetime import timedelta
from .ingest import at
from .store import connect
VERSION='2'
CONTRACTS={'immediate','sustained'}
DEFAULT_CONTRACT='immediate'
DEFAULT_SUSTAIN_SECONDS=300
def evaluate(path,contract=None,sustain_seconds=None):
    db=connect(path)
    if contract is None:
        row=db.execute("SELECT value FROM setting WHERE key='alert_contract'").fetchone()
        contract=row['value'] if row else DEFAULT_CONTRACT
    if sustain_seconds is None:
        row=db.execute("SELECT value FROM setting WHERE key='sustain_seconds'").fetchone()
        sustain_seconds=int(row['value']) if row else DEFAULT_SUSTAIN_SECONDS
    if contract not in CONTRACTS:raise ValueError(f'unknown_contract:{contract}')
    if not isinstance(sustain_seconds,int) or sustain_seconds<0:raise ValueError('sustain_seconds_required')
    result={}
    with db:
        run_id=db.execute("INSERT INTO run(status,started_at) VALUES ('待接收',CURRENT_TIMESTAMP)").lastrowid
        db.execute("UPDATE run SET status='已校验' WHERE id=?",(run_id,))
        db.execute('DELETE FROM occupancy');db.execute('DELETE FROM decision');db.execute('DELETE FROM over_capacity')
        db.execute("DELETE FROM issue WHERE code NOT IN ('parse_error','identity_conflict')")
        events=[dict(x) for x in db.execute('SELECT * FROM event ORDER BY at,id')]
        zones={x['id']:x['capacity'] for x in db.execute('SELECT * FROM zone')};maps=[dict(x) for x in db.execute('SELECT * FROM gate_map')]
        tickets={x['id']:x['type'] for x in db.execute('SELECT * FROM ticket')}
        # 撤销解析：重复撤销、无效引用不进入重放，绝不产生在场时长
        valid={(e['device'],e['seq']):e for e in events};cancelled=set();issues=defaultdict(list)
        for e in events:
            if e['kind']!='cancel':continue
            key=(e['ref_device'],e['ref_seq']);target=valid.get(key)
            if not target or target['kind'] in {'cancel','heartbeat'} or key in cancelled:
                issues['cancel_invalid'].append((e['gate'] or '',e['id']));continue
            cancelled.add(key)
        # 可信度：序号缺口与心跳缺口先判定，使重放期间每条 occupancy 都可标注分区可信度
        sequences=defaultdict(list)
        for e in events:sequences[e['device']].append(e)
        gap_devices=set();heartbeats=defaultdict(list)
        for e in events:
            if e['kind']=='heartbeat':heartbeats[e['device']].append(at(e['at']))
        for device,items in sequences.items():
            seqs={e['seq'] for e in items}
            if max(seqs)-min(seqs)+1!=len(seqs):
                gap_devices.add(device)
                issues['sequence_gap'].append((device,None))
        for device,items in sequences.items():
            if not any(e['kind']=='heartbeat' for e in items):
                issues['heartbeat_gap'].append((device,None));gap_devices.add(device);continue
            beat=sorted(heartbeats[device]);times=[at(e['at']) for e in items]
            if beat and (times[0]<beat[0]-timedelta(minutes=30) or times[-1]>beat[-1]+timedelta(minutes=30)):
                issues['heartbeat_gap'].append((device,None));gap_devices.add(device)
        # 按事件时间重放，逐事件重建分区在场数与连续超容区间
        presence={};counts={z:0 for z in zones};peak=counts.copy()
        zone_tickets=defaultdict(set);zone_tainted=defaultdict(bool);zone_last_at={}
        open_ep={};closed_eps=[]
        def gate_zone(gate,t):
            mapping=[m for m in maps if m['gate']==gate and at(m['start'])<=t<at(m['end'])]
            return mapping[0]['zone'] if len(mapping)==1 else None
        def close_episode(zone,end_at,clear_event):
            ep=open_ep.pop(zone)
            observed=(at(end_at)-ep['start_dt']).total_seconds()
            ep.update({'ended_at':end_at,'clear_event_id':clear_event['id'],'duration':int(observed)})
            closed_eps.append(ep)
        for e in events:
            if e['kind'] in {'heartbeat','cancel'} or (e['device'],e['seq']) in cancelled:continue
            t=at(e['at']);zone=gate_zone(e['gate'],t)
            if zone is None or zone not in zones:
                issues['map_missing'].append((e['gate'],e['id']));continue
            if e['device'] in gap_devices:
                zone_tainted[zone]=True
            ticket=e['ticket']
            if ticket not in tickets:
                # 未知票据不进入在场状态，但仍留下逐事件占用证据
                issues['ticket_unknown'].append((zone,e['id']))
                for z,n in counts.items():
                    if n<0:issues['negative_occupancy'].append((z,e['id']))
                    peak[z]=max(peak[z],n)
                    db.execute('INSERT INTO occupancy(at,zone,count,confidence,event_id) VALUES (?,?,?,?,?)',(e['at'],z,n,'未知' if zone_tainted[z] else '可信',e['id']))
                zone_last_at[zone]=t
                continue
            current=presence.get(ticket);affected={zone};legal=False
            if e['kind']=='enter':
                if current is not None:issues['duplicate_entry'].append((zone,e['id']))
                else:
                    presence[ticket]=zone;counts[zone]+=1;zone_tickets[zone].add(ticket);legal=True
            elif e['kind']=='exit':
                if current is None:issues['orphan_exit'].append((zone,e['id']))
                else:
                    presence.pop(ticket);counts[current]-=1;zone_tickets[current].discard(ticket);affected={current};legal=True
            elif e['kind']=='move':
                if current is None:issues['orphan_move'].append((zone,e['id']))
                elif current!=zone:
                    counts[current]-=1;counts[zone]+=1
                    zone_tickets[current].discard(ticket);zone_tickets[zone].add(ticket)
                    presence[ticket]=zone;affected={current,zone};legal=True
            if e['device'] in gap_devices:
                for z in affected:
                    zone_tainted[z]=True
            for z,n in counts.items():
                if n<0:issues['negative_occupancy'].append((z,e['id']))
                peak[z]=max(peak[z],n)
                confidence='未知' if zone_tainted[z] else '可信'
                db.execute('INSERT INTO occupancy(at,zone,count,confidence,event_id) VALUES (?,?,?,?,?)',(e['at'],z,n,confidence,e['id']))
            for z in affected:
                zone_last_at[z]=t
            if not legal:continue
            for z in affected:
                if counts[z]>zones[z] and z not in open_ep:
                    open_ep[z]={'zone':z,'started_at':e['at'],'start_dt':t,'peak':counts[z],
                                'tickets':set(zone_tickets[z]),'trigger_event_id':e['id'],'tainted':zone_tainted[z]}
                elif z in open_ep:
                    ep=open_ep[z];ep['peak']=max(ep['peak'],counts[z]);ep['tickets'].update(zone_tickets[z]);ep['tainted']=ep['tainted'] or zone_tainted[z]
                    if counts[z]<=zones[z]:close_episode(z,e['at'],e)
        # 观测窗口结束仍未解除的区间：以该分区最后一条重放事件为证据边界，不虚构解除事件
        for z,ep in list(open_ep.items()):
            boundary=zone_last_at.get(z,ep['start_dt'])
            ep.update({'ended_at':None,'clear_event_id':None,
                       'duration':int((boundary-ep['start_dt']).total_seconds())})
            closed_eps.append(ep)
        open_ep.clear()
        def classify(ep):
            if ep['tainted']:return '证据不足'
            if contract=='immediate':return '超限'
            return '需疏散' if ep['duration']>=sustain_seconds else '待观察'
        eps_by_zone=defaultdict(list)
        for ep in closed_eps:
            ep['classification']=classify(ep);eps_by_zone[ep['zone']].append(ep)
            db.execute('INSERT INTO over_capacity(run_id,zone,started_at,ended_at,peak,duration_seconds,tickets,trigger_event_id,clear_event_id,confidence,classification) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (run_id,ep['zone'],ep['started_at'],ep['ended_at'],ep['peak'],ep['duration'],
                 json.dumps(sorted(ep['tickets']),ensure_ascii=False),ep['trigger_event_id'],ep['clear_event_id'],
                 '未知' if ep['tainted'] else '可信',ep['classification']))
        for code,entries in issues.items():
            for subject,eid in entries:db.execute('INSERT INTO issue(code,subject,event_id,detail) VALUES (?,?,?,?)',(code,subject,eid,code))
        priority={'需疏散':4,'超限':3,'证据不足':2,'待观察':1}
        for zone,limit in zones.items():
            codes=[c for c,entries in issues.items() if any(s==zone for s,_ in entries)]
            untrusted=zone_tainted[zone] or any(ep['classification']=='证据不足' for ep in eps_by_zone[zone])
            if eps_by_zone[zone]:
                status=max((ep['classification'] for ep in eps_by_zone[zone]),key=lambda s:priority[s])
            elif untrusted or codes:
                status='证据不足'
            else:
                status='可信'
            detail=json.dumps({'issues':codes,'capacity':limit,'ticket_types':tickets,'contract':contract,
                               'sustain_seconds':sustain_seconds,
                               'episodes':[{'id':ep['trigger_event_id'],'started_at':ep['started_at'],'ended_at':ep['ended_at'],
                                            'peak':ep['peak'],'duration_seconds':ep['duration'],
                                            'tickets':sorted(ep['tickets']),'classification':ep['classification'],
                                            'confidence':'未知' if ep['tainted'] else '可信'} for ep in eps_by_zone[zone]]},
                              ensure_ascii=False)
            db.execute('INSERT INTO decision VALUES (?,?,?,?,?)',(zone,status,peak[zone],detail,VERSION));result[zone]=status
        db.execute("UPDATE run SET status=CASE WHEN EXISTS(SELECT 1 FROM decision WHERE status<>'可信') THEN '待核' ELSE '已判定' END,ended_at=CURRENT_TIMESTAMP WHERE id=?",(run_id,))
    db.close();return result
