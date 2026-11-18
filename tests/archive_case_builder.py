"""Builders for synthetic damaged-archive recovery cases in isolated dirs.

Untrusted fixture material lives under tmp_path; these helpers only emit
fresh bytes there, never import anything from the fixture directory.
"""
import hashlib,json
from pathlib import Path
from gateflow.archive_lab import register
from gateflow.store import connect

def sha256(data):return hashlib.sha256(data).hexdigest()

def split_at(data,boundary):
    return data[:boundary],data[boundary:]

def make_case(root,name,source_bytes,boundary,manifest_over=None,shard_mut=None,
              source_id='CAPTURE-X',revision=1,gate='G1',expected=None,
              shard_order=None,extra_shards=None,contracts=None):
    """Write a two-replica, two-shard case under root/<name> and register it.

    shard_mut maps (replica, chunk) -> mutating function(bytes)->bytes.
    """
    root=Path(root);case_dir=root/name;arch=case_dir/'archive';arch.mkdir(parents=True)
    c0,c1=split_at(source_bytes,boundary)
    chunks=(c0,c1)
    doc_shards=[]
    for replica in ('A','B'):
        for chunk in (0,1):
            rel='archive/%s%d.bin'%(replica,chunk)
            data=chunks[chunk]
            fn=(shard_mut or {}).get((replica,chunk))
            if fn is not None:data=fn(data)
            (case_dir/rel).write_bytes(data)
            doc_shards.append({'path':rel,'replica':replica,'chunk':chunk})
    if extra_shards:
        for extra in extra_shards:
            (case_dir/extra['path']).parent.mkdir(parents=True,exist_ok=True)
            (case_dir/extra['path']).write_bytes(extra['data'])
            doc_shards.append({k:v for k,v in extra.items() if k!='data'})
    if shard_order is not None:
        idx={ (s['replica'],s['chunk']):s for s in doc_shards }
        doc_shards=[idx[k] for k in shard_order]
    manifest={'source_id':source_id,'revision':revision,
              'chunk_sha256':[sha256(c0),sha256(c1)],
              'full_sha256':sha256(source_bytes),'length':len(source_bytes),
              'authorized_by':'VENUE-EVIDENCE-OPS'}
    if manifest_over:manifest.update(manifest_over)
    doc={'gate':gate,'manifest':manifest,'shards':doc_shards,
         'expected_event_count':expected if expected is not None else source_bytes.count(b'\n'),
         'contracts':contracts or [{'id':'two_copy_quorum','authorized_by':'VENUE-EVIDENCE-OPS'},
                                   {'id':'verified_mosaic','authorized_by':'VENUE-EVIDENCE-OPS'}]}
    case_file=case_dir/'case.json';case_file.write_text(json.dumps(doc,ensure_ascii=False,indent=2),encoding='utf-8')
    return case_file,manifest,doc

def register_case(db,case_id,case_file):
    return register(db,case_id,case_file)

def rows(db,sql,params=()):
    c=connect(db)
    try:return [dict(r) for r in c.execute(sql,params)]
    finally:c.close()

def flip_last(data):
    b=bytearray(data);b[-1]^=0xFF;return bytes(b)

def truncate_last(data):
    return data[:-1]
