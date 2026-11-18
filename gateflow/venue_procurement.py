"""Freeze venue zone requirements and external vendor offers, then issue purchase contracts."""
import copy,csv,hashlib,json
from datetime import date
from pathlib import Path
from .store import connect

OBJECTIVES=('min_total_cost','min_max_lateness')
# 本地案例上界：最多 8 种 SKU、3 家供应商；求解器必须精确，枚举量超过该上界即显式报错，不做近似。
MAX_SKUS=8
MAX_VENDORS=3
MAX_EXHAUSTIVE_LEAVES=2_000_000


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
            if not db.execute('SELECT 1 FROM zone LIMIT 1').fetchone():
                raise ValueError('venue_zone_missing')
            zones=[];unknown_zones=[]
            for item in doc['zone_requirements']:
                zone=str(item.get('zone',''))
                if zone and db.execute('SELECT 1 FROM zone WHERE id=?',(zone,)).fetchone():
                    zones.append(zone)
                else:
                    # 展区身份无效不阻断冻结：规划期统一诊断并使整单待核，保留原始证据。
                    unknown_zones.append(zone)
            digest=hashlib.sha256(raw+quote_raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'zones':zones,
                                'unknown_zones':unknown_zones,'quotes':quotes,
                                'quote_sha256':hashlib.sha256(quote_raw).hexdigest()},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM venue_procurement_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('venue_procurement_conflict')
            db.execute('INSERT OR IGNORE INTO venue_procurement_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'quotes':len(quotes)}


def _parse_int(value,code):
    try:
        text=str(value).strip()
        if not text:raise ValueError
        n=int(text)
        if str(n)!=text:raise ValueError
        return n
    except (ValueError,TypeError):
        raise ValueError(code)


def _load_frozen_case(db,case_id):
    row=db.execute('SELECT payload,digest FROM venue_procurement_case WHERE id=?',(case_id,)).fetchone()
    if not row:raise ValueError('venue_procurement_case_missing')
    frozen=json.loads(row['payload'])
    return frozen['case'],frozen['quotes'],frozen['quote_sha256'],row['digest']


def _validate_input(case_doc,quotes,known_zones):
    """返回 (需求列表, 报价索引, 诊断列表)。诊断非空时整份开馆准备订单待核。"""
    diagnostics=[]
    requirements=[]
    seen_req=set()
    for raw in case_doc.get('zone_requirements',[]):
        zone=str(raw.get('zone',''));sku=str(raw.get('sku',''))
        if not zone or not sku:
            diagnostics.append('requirement_identity_invalid');continue
        if zone not in known_zones:
            diagnostics.append('venue_zone_missing');continue
        try:
            qty=_parse_int(raw.get('qty'),'requirement_qty_invalid')
            deadline=_parse_int(raw.get('deadline_day'),'deadline_invalid')
        except ValueError as exc:
            diagnostics.append(str(exc));continue
        if qty<=0:diagnostics.append('requirement_qty_invalid')
        if deadline<=0:diagnostics.append('deadline_invalid')
        key=(zone,sku)
        if key in seen_req:
            diagnostics.append('requirement_duplicate')
        seen_req.add(key)
        requirements.append({'zone':zone,'sku':sku,'qty':qty,'deadline_day':deadline})
    if not requirements:diagnostics.append('requirement_empty')

    parsed=[]
    seen_quote=set()
    for i,row in enumerate(quotes,1):
        try:
            vendor=str(row.get('vendor','')).strip();sku=str(row.get('sku','')).strip()
            if not vendor or not sku:
                diagnostics.append('quote_identity_invalid');continue
            unit=_parse_int(row.get('unit_cents'),'unit_price_invalid')
            capacity=_parse_int(row.get('available_qty'),'capacity_invalid')
            arrival=_parse_int(row.get('arrival_day'),'arrival_day_invalid')
            moq=_parse_int(row.get('min_order_cents'),'min_order_invalid')
            if unit<=0:diagnostics.append('unit_price_not_positive')
            if capacity<0:diagnostics.append('capacity_negative')
            if arrival<=0:diagnostics.append('arrival_day_invalid')
            if moq<0:diagnostics.append('min_order_negative')
            key=(vendor,sku)
            if key in seen_quote:
                diagnostics.append('quote_duplicate_conflict')
            seen_quote.add(key)
            parsed.append({'vendor':vendor,'sku':sku,'unit_cents':unit,'available_qty':capacity,'arrival_day':arrival,'min_order_cents':moq,'line_no':i})
        except ValueError as exc:
            diagnostics.append(str(exc))

    # 同一供应商的起订额在各报价行间必须一致，否则报价相互矛盾。
    moq_by_vendor={}
    for q in parsed:
        old=moq_by_vendor.setdefault(q['vendor'],q['min_order_cents'])
        if old!=q['min_order_cents']:
            diagnostics.append('min_order_conflict')

    skus=sorted({r['sku'] for r in requirements})
    vendors=sorted({q['vendor'] for q in parsed})
    if len(skus)>MAX_SKUS or len(vendors)>MAX_VENDORS:
        diagnostics.append('case_outside_exhaustive_bound')

    quote_index={}
    for q in parsed:quote_index[(q['vendor'],q['sku'])]=q
    for r in requirements:
        if not any(q['sku']==r['sku'] for q in parsed):
            diagnostics.append('quote_missing')
        if sum(q['available_qty'] for q in parsed if q['sku']==r['sku'])<r['qty']:
            diagnostics.append('capacity_insufficient')

    requirements.sort(key=lambda r:(r['zone'],r['sku']))
    return requirements,quote_index,diagnostics,vendors,skus


def _compositions(need,parts):
    """整数拆分：把 need 按 parts 顺序拆成非负整数分量，每个分量不超过对应容量。"""
    if not parts:
        if need==0:yield ()
        return
    cap=parts[0]
    if len(parts)==1:
        if 0<=need<=cap:yield (need,)
        return
    for head in range(min(cap,need)+1):
        for tail in _compositions(need-head,parts[1:]):
            yield (head,)+tail


def _solve(requirements,quote_index,vendors,skus):
    """对每个 SKU 做 DFS 整数分配，叶节点统一校验跨 SKU 起订额与到货，精确穷举。"""
    sku_need={}
    for r in requirements:sku_need[r['sku']]=sku_need.get(r['sku'],0)+r['qty']
    ordered_skus=sorted(sku_need)

    # 每个 SKU 仅保留报价该 SKU 的供应商，顺序固定，保证同一输入重复规划稳定。
    sku_vendors={s:tuple(v for v in vendors if (v,s) in quote_index) for s in ordered_skus}
    sku_caps={s:tuple(quote_index[(v,s)]['available_qty'] for v in sku_vendors[s]) for s in ordered_skus}

    feasible=[]
    rejected={'moq_not_satisfied':0,'incomplete_allocation':0}
    leaves=0
    alloc={}

    def dfs(idx):
        nonlocal leaves
        if idx==len(ordered_skus):
            leaves+=1
            if leaves>MAX_EXHAUSTIVE_LEAVES:
                raise RuntimeError('exhaustive_search_limit_exceeded')
            # 回溯会复用并改写 alloc；候选内含多层可变结构，深拷贝保证各可行解完全独立。
            candidate=_evaluate(copy.deepcopy(alloc),requirements,quote_index,vendors,sku_vendors)
            if candidate is None:
                rejected['moq_not_satisfied']+=1
            else:
                feasible.append(candidate)
            return
        sku=ordered_skus[idx];need=sku_need[sku]
        any_capacity=False
        for comp in _compositions(need,sku_caps[sku]):
            any_capacity=True
            alloc[sku]=comp
            dfs(idx+1)
        if not any_capacity:
            leaves+=1
            rejected['incomplete_allocation']+=1

    dfs(0)
    return feasible,rejected,sku_vendors,leaves


def _evaluate(alloc,requirements,quote_index,vendors,sku_vendors):
    lines=[]
    vendor_totals={v:0 for v in vendors}
    vendor_arrival={}
    for sku,comp in alloc.items():
        for vendor,qty in zip(sku_vendors[sku],comp):
            if qty<=0:continue
            q=quote_index[(vendor,sku)]
            line_cents=qty*q['unit_cents']
            lines.append({'vendor':vendor,'sku':sku,'qty':qty,'unit_cents':q['unit_cents'],
                          'line_cents':line_cents,'arrival_day':q['arrival_day'],'available_qty':q['available_qty']})
            vendor_totals[vendor]+=line_cents
            vendor_arrival[vendor]=max(vendor_arrival.get(vendor,q['arrival_day']),q['arrival_day'])
    # 起订额按本次向该供应商购买的全部 SKU 合计计算；只买一部分时该供应商整单不合格。
    used=[v for v in vendors if vendor_totals[v]>0]
    for v in used:
        moq=quote_index[(v,next(x['sku'] for x in lines if x['vendor']==v))]['min_order_cents']
        if vendor_totals[v]<moq:
            return None
    req_rows=[];max_lateness=0
    for r in requirements:
        vlist=sku_vendors[r['sku']]
        arrivals=[quote_index[(v,r['sku'])]['arrival_day'] for v,qty in zip(vlist,alloc[r['sku']]) if qty>0]
        effective=max(arrivals)
        lateness=max(0,effective-r['deadline_day'])
        max_lateness=max(max_lateness,lateness)
        req_rows.append({'zone':r['zone'],'sku':r['sku'],'qty':r['qty'],'deadline_day':r['deadline_day'],
                         'effective_arrival_day':effective,'lateness':lateness,'covered':1})
    total=sum(x['line_cents'] for x in lines)
    # 不得原地排序：各候选的行列表在 DFS 期间并存，原地改写会污染先前候选。
    ordered_lines=sorted(lines,key=lambda x:(x['vendor'],x['sku']))
    vendor_summary={}
    for v in used:
        moq=quote_index[(v,next(x['sku'] for x in lines if x['vendor']==v))]['min_order_cents']
        vendor_summary[v]={'total_cents':vendor_totals[v],'min_order_cents':moq,
                           'moq_satisfied':1,'promised_arrival_day':vendor_arrival[v]}
    return {'total_cents':total,'max_lateness':max_lateness,'lines':ordered_lines,
            'vendor_totals':vendor_summary,'requirements':req_rows,
            'signature':tuple((x['vendor'],x['sku'],x['qty']) for x in ordered_lines)}


def _pick(feasible,objective):
    # 字典序：成本目标先总价后逾期；逾期目标先最大正逾期后总价。signature 仅作稳定平局裁决。
    if objective=='min_total_cost':
        return min(feasible,key=lambda c:(c['total_cents'],c['max_lateness'],c['signature']))
    return min(feasible,key=lambda c:(c['max_lateness'],c['total_cents'],c['signature']))


def _pareto(feasible):
    frontier=[]
    for c in sorted(feasible,key=lambda x:(x['total_cents'],x['max_lateness'])):
        if any(o['total_cents']<=c['total_cents'] and o['max_lateness']<=c['max_lateness']
               and (o['total_cents'],o['max_lateness'])!=(c['total_cents'],c['max_lateness']) for o in feasible):
            continue
        if not any(o['total_cents']==c['total_cents'] and o['max_lateness']==c['max_lateness'] for o in frontier):
            frontier.append({'total_cents':c['total_cents'],'max_lateness':c['max_lateness']})
    frontier.sort(key=lambda x:(x['total_cents'],x['max_lateness']))
    return frontier


def plan_purchases(db_path,case_id,plan_id=None):
    """基于已冻结案例签发两份合同；数据阻断或无完整可行解时整单待核，不签发任何部分订单。"""
    db=connect(db_path)
    try:
        case_doc,quotes,quote_sha,case_digest=_load_frozen_case(db,case_id)
        zones={r['id'] for r in db.execute('SELECT id FROM zone')}
        requirements,quote_index,diagnostics,vendors,skus=_validate_input(case_doc,quotes,zones)

        existing=db.execute('SELECT id,status,proof FROM purchase_plan WHERE case_id=?',(case_id,)).fetchone()
        created_at=(case_doc.get('planning_day') or date.today().isoformat())
        pid=plan_id or ('PLAN-'+case_digest[:12])

        if diagnostics:
            proof={'case_id':case_id,'diagnostics':sorted(set(diagnostics)),'feasible_count':0,
                   'note':'诊断阻断：整份开馆准备订单待核，不签发部分采购。'}
            with db:
                if not existing:
                    db.execute('INSERT INTO purchase_plan VALUES (?,?,?,?,?,?,?,?,?)',
                               (pid,case_id,case_digest,quote_sha,'待核',len(vendors),len(skus),
                                json.dumps(proof,ensure_ascii=False,sort_keys=True),created_at))
            return _plan_payload(db,pid,case_id,'待核',proof,created_at)

        feasible,rejected,sku_vendors,leaves=_solve(requirements,quote_index,vendors,skus)
        if not feasible:
            proof={'case_id':case_id,'diagnostics':['no_complete_feasible_order'],
                   'rejected':rejected,'leaves':leaves,
                   'note':'无完整可行订单：所有整数分配均被起订额或容量阻断，整单待核。'}
            with db:
                if not existing:
                    db.execute('INSERT INTO purchase_plan VALUES (?,?,?,?,?,?,?,?,?)',
                               (pid,case_id,case_digest,quote_sha,'待核',len(vendors),len(skus),
                                json.dumps(proof,ensure_ascii=False,sort_keys=True),created_at))
            return _plan_payload(db,pid,case_id,'待核',proof,created_at)

        proof={'case_id':case_id,'solver':'exhaustive_integer_enumeration',
               'bounds':{'max_skus':MAX_SKUS,'max_vendors':MAX_VENDORS},
               'sku_vendors':{s:list(vs) for s,vs in sorted(sku_vendors.items())},
               'leaves':leaves,'feasible_count':len(feasible),'rejected':rejected,
               'pareto_frontier':_pareto(feasible),
               'all_feasible_orders':[{'total_cents':c['total_cents'],'max_lateness':c['max_lateness'],
                                       'lines':c['lines']} for c in
                                      sorted(feasible,key=lambda c:(c['total_cents'],c['max_lateness'],c['signature']))],
               'no_better_order_note':'精确穷举全部整数分配并逐一核对容量、跨 SKU 起订额与到货；按目标字典序取最小者，frontier 之外不存在更优完整订单。'}
        chosen={obj:_pick(feasible,obj) for obj in OBJECTIVES}

        with db:
            if existing:
                # 同一冻结案例重复规划：旧单不可静默覆写，原样返回以保持稳定。
                return _plan_payload(db,existing['id'],case_id,existing['status'],
                                     json.loads(existing['proof']),created_at)
            db.execute('INSERT INTO purchase_plan VALUES (?,?,?,?,?,?,?,?,?)',
                       (pid,case_id,case_digest,quote_sha,'可签发',len(vendors),len(skus),
                        json.dumps(proof,ensure_ascii=False,sort_keys=True),created_at))
            for obj in OBJECTIVES:
                c=chosen[obj]
                cid='PO-{}-{}'.format(case_id,obj)
                cproof={'objective':obj,'optimal_order':(c['total_cents'],c['max_lateness']),
                        'feasible_count':len(feasible),'leaves':leaves,
                        'no_better_order_note':proof['no_better_order_note']}
                db.execute('INSERT INTO purchase_contract VALUES (?,?,?,?,?,?,?,?,?)',
                           (cid,pid,case_id,obj,'有效',c['total_cents'],c['max_lateness'],
                            json.dumps(cproof,ensure_ascii=False,sort_keys=True),created_at))
                for x in c['lines']:
                    db.execute('INSERT INTO purchase_order_line(contract_id,vendor,sku,qty,unit_cents,line_cents,arrival_day,available_qty) VALUES (?,?,?,?,?,?,?,?)',
                               (cid,x['vendor'],x['sku'],x['qty'],x['unit_cents'],x['line_cents'],x['arrival_day'],x['available_qty']))
                for v,vt in sorted(c['vendor_totals'].items()):
                    db.execute('INSERT INTO purchase_vendor_total VALUES (?,?,?,?,?,?)',
                               (cid,v,vt['total_cents'],vt['min_order_cents'],1,vt['promised_arrival_day']))
                for r in c['requirements']:
                    db.execute('INSERT INTO purchase_requirement VALUES (?,?,?,?,?,?,?,?)',
                               (cid,r['zone'],r['sku'],r['qty'],r['deadline_day'],r['effective_arrival_day'],r['lateness'],1))
            # 新合同落库后再登记替代/补购，满足外键约束并显式取消旧单。
            _supersede_overlapping(db,case_id,requirements,chosen,created_at)
        return _plan_payload(db,pid,case_id,'可签发',proof,created_at)
    finally:
        db.close()


def _supersede_overlapping(db,case_id,requirements,chosen,created_at):
    """新案例覆盖既有有效订单的同一分区需求：旧单显式取消/补购，禁止两版叠加收费。"""
    new_keys={(r['zone'],r['sku']) for r in requirements}
    olds=db.execute("SELECT id,objective,total_cents FROM purchase_contract WHERE status='有效' AND case_id<>?",(case_id,)).fetchall()
    for old in olds:
        covered={(r['zone'],r['sku']) for r in db.execute(
            'SELECT zone,sku FROM purchase_requirement WHERE contract_id=?',(old['id'],))}
        overlap=covered & new_keys
        if not overlap:continue
        new=chosen[old['objective']]
        # 旧需求被新案例完全覆盖（相等或为其真子集）→替代；旧单仍含新案例未覆盖的独有需求→补购差额。
        relation='补购' if covered-new_keys else '替代'
        delta=new['total_cents']-old['total_cents']
        note='新案例 {} 覆盖旧单 {} 的需求 {}；旧单已显式取消并{}，差额 {} 分，不对同一需求叠加收费。'.format(
            case_id,old['id'],sorted(overlap),relation,delta)
        db.execute("UPDATE purchase_contract SET status='取消' WHERE id=?",(old['id'],))
        db.execute('INSERT INTO contract_supersession(old_contract_id,new_contract_id,relation,delta_cents,note) VALUES (?,?,?,?,?)',
                   (old['id'],'PO-{}-{}'.format(case_id,old['objective']),relation,delta,note))


def _plan_payload(db,pid,case_id,status,proof,created_at):
    return {'plan_id':pid,'case_id':case_id,'status':status,'created_at':created_at,
            'contracts':[dict(r) for r in db.execute(
                'SELECT id,objective,status,total_cents,max_lateness FROM purchase_contract WHERE plan_id=? ORDER BY objective',(pid,))],
            'proof':proof}


def readiness_report(db_path,case_id,out_path):
    """导出 JSON 订单与布展准备报告（附 Markdown 摘要），数据全部来自已冻结/已签发行。"""
    db=connect(db_path)
    try:
        plan=db.execute('SELECT * FROM purchase_plan WHERE case_id=?',(case_id,)).fetchone()
        if not plan:raise ValueError('purchase_plan_missing')
        case_doc,quotes,quote_sha,_=_load_frozen_case(db,case_id)
        contracts=[]
        for c in db.execute('SELECT * FROM purchase_contract WHERE plan_id=? ORDER BY objective',(plan['id'],)):
            contracts.append({
                'contract_id':c['id'],'objective':c['objective'],'status':c['status'],
                'total_cents':c['total_cents'],'max_lateness':c['max_lateness'],
                'created_at':c['created_at'],'contract_proof':json.loads(c['proof']),
                'lines':[dict(x) for x in db.execute(
                    'SELECT vendor,sku,qty,unit_cents,line_cents,arrival_day,available_qty FROM purchase_order_line WHERE contract_id=? ORDER BY vendor,sku',(c['id'],))],
                'vendor_totals':[dict(x) for x in db.execute(
                    'SELECT vendor,total_cents,min_order_cents,moq_satisfied,promised_arrival_day FROM purchase_vendor_total WHERE contract_id=? ORDER BY vendor',(c['id'],))],
                'requirements':[dict(x) for x in db.execute(
                    'SELECT zone,sku,qty,deadline_day,effective_arrival_day,lateness,covered FROM purchase_requirement WHERE contract_id=? ORDER BY zone,sku',(c['id'],))],
            })
        data={'plan_id':plan['id'],'case_id':case_id,'status':plan['status'],'created_at':plan['created_at'],
              'quote_sha256':plan['quote_sha256'],'case_digest':plan['case_digest'],
              'planning_day':case_doc.get('planning_day'),
              'zone_requirements':case_doc['zone_requirements'],
              'contracts':contracts,'optimality_proof':json.loads(plan['proof']),
              'supersessions':[dict(x) for x in db.execute(
                  'SELECT old_contract_id,new_contract_id,relation,delta_cents,note FROM contract_supersession WHERE new_contract_id IN (SELECT id FROM purchase_contract WHERE plan_id=?) ORDER BY id',(plan['id'],))]}
        out=Path(out_path);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
        md=['# 布展物资采购准备报告','',f"- 案例：{case_id}；计划状态：{plan['status']}；报价原文 SHA-256：{plan['quote_sha256']}",
            f"- 规划基准日：{data['planning_day']}"]
        for c in contracts:
            md += ['',f"## {c['contract_id']}（{c['objective']}，状态 {c['status']}）",
                   f"- 总价 {c['total_cents']} 分；最大正逾期 {c['max_lateness']} 天"]
            for x in c['lines']:
                md.append(f"- 向 {x['vendor']} 采购 {x['sku']} ×{x['qty']}，单价 {x['unit_cents']} 分，分行价 {x['line_cents']} 分，第 {x['arrival_day']} 日到（可供 {x['available_qty']} 件）")
            for v in c['vendor_totals']:
                md.append(f"- 供应商 {v['vendor']} 合计 {v['total_cents']} 分，起订额 {v['min_order_cents']} 分，满足：{v['moq_satisfied']==1}，承诺第 {v['promised_arrival_day']} 日到货")
            for r in c['requirements']:
                md.append(f"- 展区 {r['zone']} 的 {r['sku']} ×{r['qty']}：期限第 {r['deadline_day']} 日，第 {r['effective_arrival_day']} 日齐套，延误 {r['lateness']} 天")
        for s in data['supersessions']:
            md += ['',f"- 订单修订：{s['note']}"]
        out.with_suffix('.md').write_text('\n'.join(md),encoding='utf-8')
        return out
    finally:
        db.close()
