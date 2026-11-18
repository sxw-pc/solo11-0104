"""Freeze a held item, custody log and competing physical claims; release the item exactly once."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from .store import connect

CONTRACTS=('registered_owner','receipt_bearer')

class _Halt(Exception):
    """Evidence integrity failure: stop release to everyone for this item."""

def _digest(kind,value):return hashlib.sha256(f'{kind}:{value}'.encode()).hexdigest()
def _receipt_digest(serial):return hashlib.sha256(serial.encode()).hexdigest()
def _now():return datetime.now(timezone.utc).isoformat()
def _at(s):
    d=datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if d.tzinfo is None:raise ValueError('timezone_required')
    return d

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
            receipt_digest=_receipt_digest(doc['receipt_serial'])
            old=db.execute('SELECT deposit_ticket,receipt_digest,seal FROM property_item WHERE id=?',(item['id'],)).fetchone()
            expected={'deposit_ticket':item['deposit_ticket'],'receipt_digest':receipt_digest,'seal':item['seal']}
            if old and dict(old)!=expected:raise ValueError('property_item_conflict')
            db.execute('INSERT OR IGNORE INTO property_item(id,deposit_ticket,receipt_digest,seal) VALUES (?,?,?,?)',(item['id'],item['deposit_ticket'],receipt_digest,item['seal']))
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'receipt_digest':receipt_digest},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM property_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('property_case_conflict')
            db.execute('INSERT OR IGNORE INTO property_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'claims':len(doc['claims'])}

def _load_case(db,case_id):
    row=db.execute('SELECT * FROM property_case WHERE id=?',(case_id,)).fetchone()
    if not row:raise ValueError('case_missing')
    return row,json.loads(row['payload'])

def _custody_digest(doc):
    return hashlib.sha256(json.dumps(doc['custody'],sort_keys=True,separators=(',',':')).encode()).hexdigest()

def _chain(doc,item):
    """Rebuild the custody chain for one item and return the current custodian.

    Every event must point at the same item and seal, times must not regress,
    transfers need both signatures, and the chain must end with a seal check
    by the current custodian. Any break raises _Halt.
    """
    events=doc.get('custody') or []
    if not events:raise _Halt('custody_chain_empty')
    custodian=None;prev=None
    for i,e in enumerate(events):
        if e.get('item',item['id'])!=item['id']:raise _Halt('custody_item_mismatch')
        if e.get('seal')!=item['seal']:raise _Halt('seal_changed')
        try:t=_at(e.get('at'))
        except (TypeError,ValueError):raise _Halt('custody_order')
        if prev is not None and t<prev:raise _Halt('custody_order')
        kind=e.get('kind')
        if i==0:
            if kind!='deposit':raise _Halt('custody_chain_empty')
            if not e.get('staff') or e.get('signed_by')!=e.get('staff'):raise _Halt('custodian_unknown')
            custodian=e['staff']
        elif kind=='transfer':
            frm,to=e.get('from'),e.get('to')
            if not frm or not to or frm!=custodian:raise _Halt('custodian_unknown')
            if e.get('signed_by')!=frm or e.get('accepted_by')!=to:raise _Halt('acceptance_missing')
            custodian=to
        elif kind=='seal_check':
            if not custodian or e.get('staff')!=custodian or e.get('signed_by')!=custodian:raise _Halt('custodian_unknown')
        else:raise _Halt('custody_kind_unknown')
        prev=t
    if events[-1].get('kind')!='seal_check':raise _Halt('final_seal_check_missing')
    return custodian

def _presented_digest(contract,credential):
    if contract=='registered_owner' and credential.get('ticket'):return _digest('ticket',credential['ticket'])
    if contract=='receipt_bearer' and credential.get('receipt_serial'):return _receipt_digest(credential['receipt_serial'])
    return None

def _verify(contract,frozen,credential,item,db):
    """Check the presented credential against the frozen claim and the contract.

    Returns (credential_digest, None) on success or (None, reason) on refusal.
    Only digests leave this function; raw credentials never do.
    """
    if contract=='registered_owner':
        ticket=credential.get('ticket');person=credential.get('person')
        if not ticket or not person:return None,'credential_missing'
        if frozen.get('ticket')!=ticket or frozen.get('person')!=person:return None,'claim_mismatch'
        if ticket!=item['deposit_ticket']:return None,'ticket_mismatch'
        holder=db.execute('SELECT person FROM ticket_holder WHERE ticket=?',(ticket,)).fetchone()
        if not holder or holder['person']!=person:return None,'holder_mismatch'
        return _digest('ticket',ticket),None
    if contract=='receipt_bearer':
        serial=credential.get('receipt_serial')
        if not serial:return None,'credential_missing'
        if _receipt_digest(frozen.get('receipt_serial') or '')!=item['receipt_digest']:return None,'claim_mismatch'
        digest=_receipt_digest(serial)
        if digest!=item['receipt_digest']:return None,'receipt_mismatch'
        return digest,None
    return None,'contract_unknown'

def claim(db_path,case_id,claim_id,contract,credential,at=None):
    """Verify one frozen claim under one frozen contract.

    On success atomically consumes the item's one-time state and records a
    single authorization bound to the current custodian, the claimant, the
    credential digest and the custody chain digest. Arrival order decides
    nothing: only a claim that satisfies the contract consumes the item.
    """
    credential=credential or {};ts=at or _now()
    db=connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            case,payload=_load_case(db,case_id)
            doc=payload['case'];item_id=doc['item']['id']
            item=db.execute('SELECT * FROM property_item WHERE id=?',(item_id,)).fetchone()
            if not item:raise ValueError('item_missing')
            frozen={c.get('id'):c for c in doc.get('claims',[])}.get(claim_id)
            cred_digest=_presented_digest(contract,credential)
            claimant=(frozen or {}).get('person') or credential.get('person')
            def finish(verdict,reason,extra=None):
                db.execute('INSERT INTO property_attempt(case_id,claim_id,contract,claimant,credential_digest,verdict,reason,at) VALUES (?,?,?,?,?,?,?,?)',
                    (case_id,claim_id,contract,claimant,cred_digest,verdict,reason,ts))
                db.commit()
                out={'verdict':verdict,'case_id':case_id,'claim_id':claim_id,'contract':contract}
                if reason:out['reason']=reason
                if extra:out.update(extra)
                return out
            try:
                if not frozen:return finish('refused','claim_unknown')
                if contract not in CONTRACTS:return finish('refused','contract_unknown')
                if contract not in doc.get('contracts',[]):return finish('refused','contract_not_frozen')
                if item['released']:return finish('refused','item_released')
                if db.execute("SELECT 1 FROM property_release WHERE item_id=? AND status IN ('authorized','released')",(item_id,)).fetchone():
                    return finish('refused','item_committed')
                custodian=_chain(doc,item)
                if payload.get('receipt_digest')!=item['receipt_digest']:raise _Halt('receipt_digest_mismatch')
                holder=db.execute('SELECT person FROM ticket_holder WHERE ticket=?',(item['deposit_ticket'],)).fetchone()
                if not holder or holder['person']!=doc.get('ticket_holder'):raise _Halt('holder_disputed')
                verified,reason=_verify(contract,frozen,credential,item,db)
                if reason:return finish('refused',reason)
                cred_digest=verified
                version=item['version']
                cur=db.execute('UPDATE property_item SET version=version+1 WHERE id=? AND version=? AND released=0',(item_id,version))
                if cur.rowcount!=1:raise _Halt('version_conflict')
                custody_digest=_custody_digest(doc)
                release_id=db.execute('INSERT INTO property_release(case_id,item_id,claim_id,contract,claimant,credential_digest,custodian,deliverer,item_version,custody_digest,status,authorized_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (case_id,item_id,claim_id,contract,claimant,cred_digest,custodian,custodian,version+1,custody_digest,'authorized',ts)).lastrowid
                return finish('authorized',None,{'release_id':release_id,'item_id':item_id,'claimant':claimant,'deliverer':custodian,'credential_digest':cred_digest,'custody_digest':custody_digest,'item_version':version+1,'authorized_at':ts})
            except _Halt as exc:
                return finish('halted',str(exc))
        except Exception:
            db.rollback();raise
    finally:db.close()

def handover(db_path,release_id,deliverer,at=None):
    """Perform the counter handover for one authorization, atomically.

    The same item version, custody evidence and authorized deliverer must
    still hold; a shift change or any drift rejects the handover. A failed
    handover rolls back completely so the same authorization can be retried,
    but a completed handover can never be replayed.
    """
    ts=at or _now();db=connect(db_path)
    try:
        db.execute('BEGIN IMMEDIATE')
        try:
            rel=db.execute('SELECT * FROM property_release WHERE id=?',(release_id,)).fetchone()
            if not rel:raise ValueError('release_missing')
            item=db.execute('SELECT * FROM property_item WHERE id=?',(rel['item_id'],)).fetchone()
            reason=None
            if rel['status']=='released' or item['released']:reason='already_released'
            elif rel['status']!='authorized':reason='not_authorized'
            elif deliverer!=rel['deliverer']:reason='deliverer_mismatch'
            else:
                _case,payload=_load_case(db,rel['case_id'])
                if _custody_digest(payload['case'])!=rel['custody_digest']:reason='custody_evidence_changed'
                elif item['version']!=rel['item_version']:reason='version_conflict'
            if reason is None:
                cur=db.execute('UPDATE property_item SET version=version+1,released=1 WHERE id=? AND version=? AND released=0',(item['id'],rel['item_version']))
                if cur.rowcount!=1:reason='version_conflict'
            if reason is not None:
                db.execute('INSERT INTO property_handover(release_id,deliverer,claimant,at,result,reason) VALUES (?,?,?,?,?,?)',(release_id,deliverer,rel['claimant'],ts,'rejected',reason))
                db.commit();return {'verdict':'rejected','reason':reason,'release_id':release_id,'item_id':rel['item_id']}
            db.execute("UPDATE property_release SET status='released',released_at=? WHERE id=? AND status='authorized'",(ts,release_id))
            db.execute('INSERT INTO property_handover(release_id,deliverer,claimant,at,result,reason) VALUES (?,?,?,?,?,?)',(release_id,deliverer,rel['claimant'],ts,'released',None))
            db.commit()
            return {'verdict':'released','release_id':release_id,'item_id':rel['item_id'],'claimant':rel['claimant'],'deliverer':deliverer,'released_at':ts}
        except Exception:
            db.rollback();raise
    finally:db.close()

def audit(db_path,case_id,out=None):
    """Build the audit report for a case: digests and verdicts only, never raw credentials."""
    db=connect(db_path)
    try:
        case,payload=_load_case(db,case_id)
        doc=payload['case'];item_id=doc['item']['id']
        item=db.execute('SELECT * FROM property_item WHERE id=?',(item_id,)).fetchone()
        chain=[{'seq':i,'kind':e.get('kind'),'staff':e.get('staff'),'from':e.get('from'),'to':e.get('to'),
                'at':e.get('at'),'seal':e.get('seal'),'seal_intact':e.get('seal')==item['seal'],
                'signed_by':e.get('signed_by'),'accepted_by':e.get('accepted_by')}
               for i,e in enumerate(doc.get('custody',[]))]
        claims=[]
        for c in doc.get('claims',[]):
            if c.get('receipt_serial'):kind,digest='receipt',_receipt_digest(c['receipt_serial'])
            elif c.get('ticket'):kind,digest='ticket',_digest('ticket',c['ticket'])
            else:kind,digest=None,None
            claims.append({'id':c.get('id'),'person':c.get('person'),'at':c.get('at'),'credential':kind,'credential_digest':digest})
        data={'case_id':case_id,'case_digest':case['digest'],'contracts':doc.get('contracts',[]),
              'item':{'id':item_id,'seal':item['seal'],'deposit_ticket_digest':_digest('ticket',item['deposit_ticket']),
                      'receipt_digest':item['receipt_digest'],'released':item['released'],'version':item['version']},
              'ticket_holder':doc.get('ticket_holder'),
              'custody_digest':_custody_digest(doc),'custody_chain':chain,'claims':claims,
              'attempts':[dict(r) for r in db.execute('SELECT id,case_id,claim_id,contract,claimant,credential_digest,verdict,reason,at FROM property_attempt WHERE case_id=? ORDER BY id',(case_id,))],
              'releases':[dict(r) for r in db.execute('SELECT * FROM property_release WHERE case_id=? ORDER BY id',(case_id,))],
              'handovers':[dict(r) for r in db.execute('SELECT h.* FROM property_handover h JOIN property_release r ON r.id=h.release_id WHERE r.case_id=? ORDER BY h.id',(case_id,))]}
        if out:
            p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            return p
        return data
    finally:db.close()
