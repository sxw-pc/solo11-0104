"""Damaged-archive recovery lab.

``register`` freezes the authorized manifest and the observed shard bytes.
``recover`` reconstructs a capture dossier only under a pre-authorized
integrity contract: every chosen shard must match the manifest digest for its
chunk, bytes are concatenated first and decoded as one UTF-8 stream, and the
assembled file must match the manifest length, full-file SHA-256 and parse as
the complete JSONL dossier before any ``source``/``event`` row is written.
"""
import hashlib,json,os,uuid
from datetime import datetime,timezone
from pathlib import Path
from .store import connect
from .ingest import KINDS,at,import_raw

KNOWN_CONTRACTS=('two_copy_quorum','verified_mosaic')

def register(db_path,case_id,fixture):
    path=Path(fixture).resolve();raw=path.read_bytes();doc=json.loads(raw)
    if not case_id or not doc.get('manifest') or not doc.get('shards') or not doc.get('contracts'):
        raise ValueError('bad_archive_case')
    root=path.parent
    observed=[]
    for shard in doc['shards']:
        candidate=(root/shard['path']).resolve()
        if not candidate.is_relative_to(root):raise ValueError('shard_path_escape')
        data=candidate.read_bytes()
        observed.append({'path':shard['path'],'chunk':shard['chunk'],'replica':shard['replica'],'sha256':hashlib.sha256(data).hexdigest(),'byte_length':len(data)})
    db=connect(db_path)
    try:
        with db:
            if not db.execute('SELECT 1 FROM gate_map WHERE gate=?',(doc['gate'],)).fetchone():
                raise ValueError('archive_gate_missing')
            digest=hashlib.sha256(raw).hexdigest()
            payload=json.dumps({'fixture':str(path),'case':doc,'observed_shards':observed},sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT digest FROM archive_recovery_case WHERE id=?',(case_id,)).fetchone()
            if old and old['digest']!=digest:raise ValueError('archive_case_conflict')
            db.execute('INSERT OR IGNORE INTO archive_recovery_case VALUES (?,?,?)',(case_id,payload,digest))
    finally:db.close()
    return {'case_id':case_id,'digest':digest,'shards':len(observed)}

class _Block(Exception):
    def __init__(self,reason):super().__init__(reason);self.reason=reason

def _sha(data):return hashlib.sha256(data).hexdigest()

def _read_live_shards(doc,root):
    """Re-read every shard from disk; a missing/unreadable file is evidence."""
    live=[]
    for shard in doc['shards']:
        entry={'path':shard['path'],'chunk':shard['chunk'],'replica':shard['replica'],
               'data':None,'sha256':None,'byte_length':None,'error':None}
        try:
            candidate=(root/shard['path']).resolve()
            if not candidate.is_relative_to(root):raise OSError('shard_path_escape')
            data=candidate.read_bytes()
            entry['data']=data;entry['sha256']=_sha(data);entry['byte_length']=len(data)
        except OSError as exc:
            entry['error']=type(exc).__name__
        live.append(entry)
    return live

def _valid_manifest(manifest):
    return (isinstance(manifest,dict) and isinstance(manifest.get('source_id'),str)
        and isinstance(manifest.get('revision'),int) and isinstance(manifest.get('full_sha256'),str)
        and isinstance(manifest.get('length'),int)
        and isinstance(manifest.get('chunk_sha256'),list) and bool(manifest['chunk_sha256'])
        and all(isinstance(x,str) for x in manifest['chunk_sha256']))

def _plan(doc,frozen_obs,live,contract_id):
    """Decide select/witness/reject/blocked for every physical shard copy.

    Returns (decisions, selected_by_chunk, block_reason). No bytes are
    assembled here and nothing outside the report structures is mutated.
    """
    manifest=doc['manifest'];expected=manifest['chunk_sha256'];n=len(expected)
    frozen={(o['replica'],o['chunk']):o for o in frozen_obs}
    decisions=[]
    def dec(s,decision,reason,sha=None):
        return {'chunk':s['chunk'],'replica':s['replica'],'shard_path':s['path'],
                'observed_sha256':s['sha256'],'expected_sha256':expected[s['chunk']] if 0<=s['chunk']<n else None,
                'byte_length':s['byte_length'],'decision':decision,'reason':reason}
    eligible={}
    block_reason=None
    seen={}
    for s in live:
        key=(s['replica'],s['chunk'])
        if key in seen:
            decisions.append(dec(s,'blocked','duplicate_chunk'));block_reason=block_reason or 'duplicate_chunk';continue
        seen[key]=True
        if not isinstance(s['chunk'],int) or not (0<=s['chunk']<n):
            decisions.append(dec(s,'blocked','chunk_out_of_range'));block_reason=block_reason or 'chunk_out_of_range';continue
        fr=frozen.get(key)
        if s['error'] is not None:
            decisions.append(dec(s,'blocked','shard_unreadable'));continue
        if fr is None:
            decisions.append(dec(s,'rejected','unknown_shard_copy'));continue
        if s['sha256']==expected[s['chunk']]:
            reason='shard_digest_match' if s['sha256']==fr['sha256'] else 'repaired_after_freeze'
            decisions.append(dec(s,'witness',reason));eligible.setdefault(s['chunk'],[]).append((s['replica'],s,reason))
        else:
            reason='shard_drifted_after_freeze' if s['sha256']!=fr['sha256'] else 'shard_digest_mismatch'
            decisions.append(dec(s,'rejected',reason))
    structural={d['reason'] for d in decisions if d['decision']=='blocked'}-{'shard_unreadable'}
    if {s.get('chunk') for s in doc['shards']}!=set(range(n)):
        structural.add('chunk_coverage_invalid')
    if structural:return decisions,{},sorted(structural)[0]
    selected={}
    if contract_id=='two_copy_quorum':
        for i in range(n):
            copies=eligible.get(i,[])
            replicas=sorted({r for r,_,_ in copies})
            if len(replicas)<2:
                block_reason='quorum_not_met';break
            chosen=replicas[0]
            for replica,s,reason in sorted(copies,key=lambda x:x[0]):
                d=next(d for d in decisions if d['chunk']==i and d['replica']==replica)
                if replica==chosen and i not in selected:
                    d['decision']='selected';d['reason']=reason+':quorum_selected';selected[i]=s
                else:
                    d['decision']='witness';d['reason']=reason+':quorum_witness'
    elif contract_id=='verified_mosaic':
        replicas=sorted({s['replica'] for s in live})
        complete=[r for r in replicas if all(any(rep==r for rep,_,_ in eligible.get(i,[])) for i in range(n))]
        if complete:
            chosen=complete[0]
            for i in range(n):
                s=next(s for rep,s,_ in eligible[i] if rep==chosen)
                d=next(d for d in decisions if d['chunk']==i and d['replica']==chosen)
                d['decision']='selected';d['reason']='complete_replica_preferred';selected[i]=s
            for d in decisions:
                if d['decision']=='witness':d['reason']='mosaic_alternative'
        else:
            for i in range(n):
                copies=sorted(eligible.get(i,[]),key=lambda x:x[0])
                if not copies:
                    block_reason='shard_missing';break
                replica,s,reason=copies[0]
                d=next(d for d in decisions if d['chunk']==i and d['replica']==replica)
                d['decision']='selected';d['reason']=reason+':mosaic_selected';selected[i]=s
            for d in decisions:
                if d['decision']=='witness':d['reason']=d['reason']+':mosaic_alternative'
    if block_reason:
        return decisions,{},block_reason
    if len(selected)!=n:
        return decisions,{},('quorum_not_met' if contract_id=='two_copy_quorum' else 'shard_missing')
    return decisions,selected,None

def _validate_jsonl(text,expected_count):
    """Strict whole-dossier check; any invalid line blocks the recovery."""
    count=0
    for line,rawline in enumerate(text.splitlines(),1):
        if not rawline.strip():continue
        count+=1
        try:
            r=json.loads(rawline)
            if not isinstance(r,dict):raise ValueError('not_an_object')
            kind=r['kind']
            if kind not in KINDS:raise ValueError('unknown_kind')
            device=str(r['device']);int(r['seq']);at(r['at'])
            if kind!='heartbeat' and not r.get('gate'):raise ValueError('gate_required')
        except (ValueError,KeyError,TypeError,json.JSONDecodeError) as exc:
            raise _Block('event_structure_invalid:'+type(exc).__name__)
    if expected_count is not None and count!=expected_count:
        raise _Block('event_count_mismatch')
    return count

def _fsync_dir(path):
    fd=os.open(str(path),os.O_RDONLY)
    try:os.fsync(fd)
    finally:os.close(fd)

def _durable_write(target,data):
    """Temp file + atomic replace + directory fsync (write-ahead logging)."""
    target.parent.mkdir(parents=True,exist_ok=True)
    tmp=target.with_name('.%s.tmp.%s'%(target.name,uuid.uuid4().hex))
    try:
        with open(tmp,'wb') as fh:
            fh.write(data);fh.flush();os.fsync(fh.fileno())
        os.replace(tmp,target);_fsync_dir(target.parent)
    finally:
        try:
            if tmp.exists():tmp.unlink()
        except FileNotFoundError:pass

def _record_attempt(db,case_id,contract_id,manifest,digest,status,reason,decisions,
                    file_path,full_sha256,length,source_row,event_count):
    now=datetime.now(timezone.utc).isoformat()
    cur=db.execute('INSERT INTO archive_recovery_attempt(case_id,contract_id,source_id,revision,manifest_digest,status,full_sha256,length,file_path,source_row,event_count,reason,report_json,attempted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (case_id,contract_id,manifest['source_id'],manifest['revision'],digest,status,full_sha256,length,file_path,source_row,event_count,reason,'{}',now))
    attempt_id=cur.lastrowid
    for d in decisions:
        db.execute('INSERT INTO archive_chunk_decision(attempt_id,case_id,chunk,replica,shard_path,observed_sha256,expected_sha256,byte_length,decision,reason) VALUES (?,?,?,?,?,?,?,?,?,?)',
            (attempt_id,case_id,d['chunk'],d['replica'],d['shard_path'],d['observed_sha256'],d['expected_sha256'],d['byte_length'],d['decision'],d['reason']))
    return attempt_id

def _build_report(case_id,contract_id,doc,digest,status,reason,decisions,assembled,
                  full_sha256,length,target,import_result,attempt_id):
    return {'case_id':case_id,'source_id':doc['manifest']['source_id'],'revision':doc['manifest']['revision'],
            'gate':doc.get('gate'),'contract_id':contract_id,'manifest_digest':digest,'status':status,'reason':reason,
            'manifest':{'length':doc['manifest']['length'],'full_sha256':doc['manifest']['full_sha256'],'chunk_sha256':doc['manifest']['chunk_sha256']},
            'chunks':decisions,
            'assembly':{'order':list(range(len(doc['manifest']['chunk_sha256']))),'length':length,'full_sha256':full_sha256},
            'recovered_file':str(target) if target else None,
            'import':import_result,'attempt_id':attempt_id}

def recover(db_path,case_id,contract_id,recover_dir,report_path=None,crash_hook=None):
    """Reconstruct and import one registered archive case under a contract.

    Returns an evidence report dict. Either the exact manifest bytes are
    restored and imported exactly once, or the case stays blocked with no
    ``source``/``event`` rows and per-shard rejection reasons.
    """
    if contract_id not in KNOWN_CONTRACTS:raise ValueError('unknown_contract')
    hook=crash_hook or (lambda stage:None)
    db=connect(db_path);db.isolation_level=None
    try:
        row=db.execute('SELECT payload,digest FROM archive_recovery_case WHERE id=?',(case_id,)).fetchone()
        if not row:raise ValueError('archive_case_not_registered')
        frozen=json.loads(row['payload']);doc=frozen['case'];digest=row['digest'];manifest=doc['manifest']
        root=Path(frozen['fixture']).resolve().parent
        recover_dir=Path(recover_dir).resolve()
        target=recover_dir/case_id/("%s.rev%s.jsonl"%(manifest['source_id'],manifest['revision']))
        if not _valid_manifest(manifest):
            live=_read_live_shards(doc,root)
            decisions=[{'chunk':s['chunk'],'replica':s['replica'],'shard_path':s['path'],'observed_sha256':s['sha256'],
                        'expected_sha256':None,'byte_length':s['byte_length'],'decision':'blocked','reason':'manifest_invalid'} for s in live]
            return _finish_blocked(db,case_id,contract_id,manifest,digest,'manifest_invalid',decisions,doc,report_path,target)
        live=_read_live_shards(doc,root)
        decisions,selected,block_reason=_plan(doc,frozen['observed_shards'],live,contract_id)
        assembled=full_sha256=length=None
        if not block_reason:
            try:
                assembled=b''.join(selected[i]['data'] for i in range(len(manifest['chunk_sha256'])))
                length=len(assembled)
                if length!=manifest['length']:raise _Block('length_mismatch')
                full_sha256=_sha(assembled)
                if full_sha256!=manifest['full_sha256']:raise _Block('full_digest_mismatch')
                try:text=assembled.decode('utf-8')
                except UnicodeDecodeError:raise _Block('utf8_decode_failed')
                _validate_jsonl(text,doc.get('expected_event_count'))
            except _Block as exc:
                block_reason=exc.reason
        if block_reason:
            return _finish_blocked(db,case_id,contract_id,manifest,digest,block_reason,decisions,doc,report_path,target)
        prior=db.execute('SELECT * FROM archive_publish WHERE case_id=?',(case_id,)).fetchone()
        if prior is not None and (prior['source_id']!=manifest['source_id'] or prior['revision']!=manifest['revision']
                                  or prior['full_sha256']!=full_sha256):
            return _finish_blocked(db,case_id,contract_id,manifest,digest,'publish_identity_conflict',decisions,doc,report_path,target)
        db.execute('BEGIN IMMEDIATE')
        try:
            # holding the write lock: only our own crashed temps can exist now
            for stale in recover_dir.glob('**/.%s.tmp.*'%target.name):
                try:stale.unlink()
                except FileNotFoundError:pass
            pub=db.execute('SELECT * FROM archive_publish WHERE case_id=?',(case_id,)).fetchone()
            if pub is not None:
                existing=target.exists() and _sha(target.read_bytes())==full_sha256
                report_reason='already_published' if existing else 'file_reconciled'
                if not existing:_durable_write(target,assembled);hook('after_file_replace')
                event_count=db.execute('SELECT count(*) FROM event WHERE source_id=?',(pub['source_row'],)).fetchone()[0]
                attempt_id=_record_attempt(db,case_id,contract_id,manifest,digest,'published',report_reason,decisions,
                                           str(target),full_sha256,length,pub['source_row'],event_count)
                import_result={'source_row':pub['source_row'],'source_sha256':full_sha256,
                               'events_imported':0,'duplicate_file':True}
                db.execute('COMMIT')
            else:
                _durable_write(target,assembled);hook('after_file_replace')
                raw_import=import_raw(db,assembled,str(target),full_sha256)
                import_result={'source_row':raw_import['source_id'],'source_sha256':full_sha256,
                               'events_imported':raw_import['imported'],'duplicate_file':raw_import['duplicate_file']}
                event_count=raw_import['imported']
                now=datetime.now(timezone.utc).isoformat()
                db.execute('INSERT INTO archive_publish(case_id,source_id,revision,contract_id,file_path,full_sha256,length,source_row,event_count,status,reason,published_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (case_id,manifest['source_id'],manifest['revision'],contract_id,str(target),full_sha256,length,
                     import_result['source_row'],event_count,'published','manifest_bytes_verified',now))
                attempt_id=_record_attempt(db,case_id,contract_id,manifest,digest,'published','published',decisions,
                                           str(target),full_sha256,length,import_result['source_row'],event_count)
                report_reason='published'
                hook('before_commit')
                db.execute('COMMIT')
        except BaseException:
            db.execute('ROLLBACK');raise
        hook('after_commit')
        report=_build_report(case_id,contract_id,doc,digest,'published',report_reason,
                             decisions,assembled,full_sha256,length,target,import_result,attempt_id)
        return _write_report(report,report_path)
    finally:db.close()

def _finish_blocked(db,case_id,contract_id,manifest,digest,reason,decisions,doc,report_path,target):
    with db:
        attempt_id=_record_attempt(db,case_id,contract_id,manifest,digest,'blocked',reason,decisions,None,None,None,None,None)
    report=_build_report(case_id,contract_id,doc,digest,'blocked',reason,decisions,None,None,None,None,
                         {'source_row':None,'source_sha256':None,'events_imported':0,'duplicate_file':False},attempt_id)
    return _write_report(report,report_path)

def _write_report(report,report_path):
    if report_path:
        p=Path(report_path);p.parent.mkdir(parents=True,exist_ok=True)
        p.write_text(json.dumps(report,ensure_ascii=False,indent=2,sort_keys=True),encoding='utf-8')
    return report

def recovery_report(db_path,case_id=None):
    """Read back the recovery ledger as evidence (hashes and IDs only)."""
    db=connect(db_path)
    try:
        where='WHERE case_id=?' if case_id else ''
        params=(case_id,) if case_id else ()
        out=[]
        for a in db.execute('SELECT * FROM archive_recovery_attempt %s ORDER BY id'%where,params):
            d={'attempt':dict(a),'chunks':[dict(x) for x in db.execute(
                'SELECT chunk,replica,shard_path,observed_sha256,expected_sha256,byte_length,decision,reason FROM archive_chunk_decision WHERE attempt_id=? ORDER BY chunk,replica',(a['id'],))]}
            out.append(d)
        return out
    finally:db.close()
