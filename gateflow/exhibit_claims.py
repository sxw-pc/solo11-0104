"""Freeze insured exhibit, signed damage inspection and paid advance; settle and reconcile exhibit damage claims."""
import hashlib,json
from pathlib import Path
from .store import connect

def _direction(net):
    return '承保方补付' if net>0 else ('业主返还' if net<0 else '持平')

def _load(fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not doc.get('asset') or not doc.get('inspection') or not doc.get('advance'):
        raise ValueError('bad_claim_case')
    return path,raw,doc

def _verify_attachment(path,doc):
    inspection=doc['inspection']
    attachment=(path.parent/inspection['attachment']).resolve()
    if hashlib.sha256(attachment.read_bytes()).hexdigest()!=inspection['attachment_sha256']:
        raise ValueError('inspection_attachment_mismatch')

def _freeze(db,doc):
    asset=doc['asset'];advance=doc['advance']
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

def _payload(path,doc):
    return json.dumps({'fixture':str(path),'case':doc},sort_keys=True,separators=(',',':'))

def register(db_path,case_id,fixture):
    path,raw,doc=_load(fixture)
    if not case_id:raise ValueError('bad_claim_case')
    _verify_attachment(path,doc)
    digest=hashlib.sha256(raw).hexdigest()
    db=connect(db_path)
    try:
        with db:
            _freeze(db,doc)
            old=db.execute('SELECT digest FROM exhibit_claim_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('claim_case_conflict')
            db.execute('INSERT OR IGNORE INTO exhibit_claim_case(id,payload,digest,version) VALUES (?,?,?,1)',(case_id,_payload(path,doc),digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'advance':doc['advance']['id']}

def revise(db_path,case_id,fixture):
    path,raw,doc=_load(fixture)
    if not case_id:raise ValueError('bad_claim_case')
    _verify_attachment(path,doc)
    digest=hashlib.sha256(raw).hexdigest()
    db=connect(db_path)
    try:
        with db:
            cur=db.execute('SELECT payload,digest,version FROM exhibit_claim_case WHERE id=?',(case_id,)).fetchone()
            if not cur:raise ValueError('claim_case_missing')
            if cur['digest']==digest:
                return {'case_id':case_id,'digest':digest,'version':cur['version'],'advance':doc['advance']['id']}
            _freeze(db,doc)
            db.execute('INSERT INTO exhibit_claim_case_archive(case_id,version,payload,digest) VALUES (?,?,?,?)',(case_id,cur['version'],cur['payload'],cur['digest']))
            db.execute('UPDATE exhibit_claim_case SET payload=?,digest=?,version=? WHERE id=?',(_payload(path,doc),digest,cur['version']+1,case_id))
            version=cur['version']+1
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'version':version,'advance':doc['advance']['id']}

def settle(db_path,case_id,contract):
    db=connect(db_path)
    try:
        row=db.execute('SELECT payload,digest FROM exhibit_claim_case WHERE id=?',(case_id,)).fetchone()
        if not row:raise ValueError('claim_case_missing')
        payload=json.loads(row['payload']);doc=payload['case']
        if contract not in doc.get('contracts',[]):raise ValueError('contract_not_authorized')
        asset=doc['asset'];inspection=doc['inspection'];advance=doc['advance']
        reasons=[]
        if not inspection.get('inspector') or not inspection.get('signed_at'):reasons.append('inspection_unsigned')
        quote=inspection.get('restoration_quote_cents')
        if isinstance(quote,bool) or not isinstance(quote,int) or quote<=0:reasons.append('invalid_quote')
        deductible=doc.get('deductible_cents')
        if isinstance(deductible,bool) or not isinstance(deductible,int) or deductible<0:reasons.append('invalid_deductible')
        try:
            att_raw=(Path(payload['fixture']).parent/inspection['attachment']).resolve().read_bytes()
            att=json.loads(att_raw) if hashlib.sha256(att_raw).hexdigest()==inspection['attachment_sha256'] else None
        except (OSError,json.JSONDecodeError,UnicodeDecodeError):att=None
        if att is None or att.get('exhibit')!=asset['id'] or att.get('restoration_quote_cents')!=quote or att.get('signed_by')!=inspection.get('inspector'):
            reasons.append('attachment_mismatch')
        exhibit=db.execute('SELECT * FROM insured_exhibit WHERE id=?',(asset['id'],)).fetchone()
        if not exhibit:reasons.append('asset_mismatch')
        else:
            if exhibit['owner']!=asset['owner']:reasons.append('owner_mismatch')
            if exhibit['zone']!=asset['zone']:reasons.append('gallery_mismatch')
            if exhibit['agreed_value_cents']!=asset['agreed_value_cents']:reasons.append('asset_mismatch')
        paid=advance.get('paid_cents')
        adv=db.execute('SELECT * FROM claim_advance WHERE id=?',(advance['id'],)).fetchone()
        if isinstance(paid,bool) or not isinstance(paid,int) or paid<=0 or not adv or adv['paid_cents']!=paid or adv['exhibit']!=asset['id']:
            reasons.append('advance_unpaid')
        for other in db.execute('SELECT payload FROM exhibit_claim_case WHERE id<>?',(case_id,)):
            odoc=json.loads(other['payload'])['case']
            if odoc['asset']['id']==asset['id'] and odoc['inspection'].get('report_id')==inspection.get('report_id'):
                reasons.append('duplicate_incident');break
        if reasons:
            with db:
                db.execute('INSERT INTO claim_review(case_id,contract,reasons,detail) VALUES (?,?,?,?)',
                    (case_id,contract,json.dumps(reasons,ensure_ascii=False),json.dumps({'case_digest':row['digest']},sort_keys=True)))
            return {'case_id':case_id,'status':'待核','reasons':reasons}
        gross=quote if contract=='restoration_cost' else exhibit['agreed_value_cents']
        due=max(0,gross-deductible);net=due-paid
        latest=db.execute('SELECT * FROM claim_settlement WHERE case_id=? ORDER BY revision DESC LIMIT 1',(case_id,)).fetchone()
        if latest and latest['case_digest']==row['digest'] and latest['contract']==contract:
            return {'case_id':case_id,'status':'已结算','revision':latest['revision'],'contract':contract,'due_cents':latest['due_cents'],
                'advance_paid_cents':latest['advance_paid_cents'],'net_cents':latest['net_cents'],'direction':_direction(latest['net_cents'])}
        revision=(latest['revision'] if latest else 0)+1
        with db:
            sid=db.execute('INSERT INTO claim_settlement(case_id,revision,contract,exhibit,owner,zone,inspection_report,attachment_sha256,quote_cents,agreed_value_cents,deductible_cents,due_cents,advance_id,advance_paid_cents,net_cents,case_digest,reversal_of,status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (case_id,revision,contract,asset['id'],asset['owner'],asset['zone'],inspection.get('report_id',''),inspection['attachment_sha256'],quote,
                 exhibit['agreed_value_cents'],deductible,due,advance['id'],paid,net,row['digest'],latest['id'] if latest else None,'已结算')).lastrowid
            if latest:
                db.execute('INSERT INTO claim_ledger(case_id,settlement_id,kind,revision,amount_cents,exhibit,owner,contract,advance_id,attachment_sha256,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                    (case_id,latest['id'],'reversal',revision,-latest['net_cents'],latest['exhibit'],latest['owner'],latest['contract'],latest['advance_id'],latest['attachment_sha256'],f"冲回结算{latest['id']}"))
            db.execute('INSERT INTO claim_ledger(case_id,settlement_id,kind,revision,amount_cents,exhibit,owner,contract,advance_id,attachment_sha256,note) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (case_id,sid,'settlement',revision,net,asset['id'],asset['owner'],contract,advance['id'],inspection['attachment_sha256'],f"结算修订{revision}"))
            balance=db.execute('SELECT COALESCE(SUM(amount_cents),0) FROM claim_ledger WHERE case_id=?',(case_id,)).fetchone()[0]
            if balance!=net:raise ValueError('ledger_imbalance')
        return {'case_id':case_id,'status':'已结算','revision':revision,'contract':contract,'due_cents':due,'advance_paid_cents':paid,'net_cents':net,'direction':_direction(net)}
    finally:db.close()

def reconcile(db_path,out=None):
    db=connect(db_path)
    try:
        cases=[]
        for row in db.execute('SELECT * FROM exhibit_claim_case ORDER BY id'):
            doc=json.loads(row['payload'])['case']
            settlements=[dict(r) for r in db.execute('SELECT * FROM claim_settlement WHERE case_id=? ORDER BY revision',(row['id'],))]
            ledger=[dict(r) for r in db.execute('SELECT * FROM claim_ledger WHERE case_id=? ORDER BY id',(row['id'],))]
            reviews=[dict(r) for r in db.execute('SELECT * FROM claim_review WHERE case_id=? ORDER BY id',(row['id'],))]
            balance=sum(e['amount_cents'] for e in ledger)
            latest=settlements[-1] if settlements else None
            cases.append({'case_id':row['id'],'case_version':row['version'],'exhibit':doc['asset']['id'],'owner':doc['asset']['owner'],'zone':doc['asset']['zone'],
                'advance_id':doc['advance']['id'],'advance_paid_cents':doc['advance']['paid_cents'],
                'attachment_sha256':doc['inspection']['attachment_sha256'],'case_digest':row['digest'],
                'status':'已结算' if latest else ('待核' if reviews else '未结算'),
                'revision':latest['revision'] if latest else 0,'contract':latest['contract'] if latest else None,
                'due_cents':latest['due_cents'] if latest else None,'net_cents':latest['net_cents'] if latest else None,
                'balance_cents':balance,'direction':_direction(balance) if ledger else '未结算',
                'settlements':settlements,'ledger':ledger,'reviews':reviews})
        insurer_owes=sum(c['balance_cents'] for c in cases if c['balance_cents']>0)
        owner_owes=-sum(c['balance_cents'] for c in cases if c['balance_cents']<0)
        data={'cases':cases,'totals':{'insurer_owes_owner_cents':insurer_owes,'owner_owes_insurer_cents':owner_owes,'net_cents':insurer_owes-owner_owes}}
        if not out:return data
        p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');return p
    finally:db.close()
