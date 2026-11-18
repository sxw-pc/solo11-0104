import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .lost_property import register as register_case,claim as verify_claim,handover as counter_handover,audit as property_audit
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('batch',['id','start','end']),('zone',['id','capacity']),('gate_map',['gate','zone','start','end']),('ticket',['id','type'])]:
            for r in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[r[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='gateflow-'));root.mkdir(parents=True,exist_ok=True);fixture=Path(__file__).resolve().parent.parent/'fixtures';db=root/'gateflow.db'
    initialize(db,fixture/'config.json');ingest(fixture/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def main(argv=None):
    p=argparse.ArgumentParser(description='离线闸机客流证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    lp=s.add_parser('lost-property',description='失物放行：冻结登记、合同认领、柜台交接、审计报告');sub=lp.add_subparsers(dest='lp',required=True)
    rg=sub.add_parser('register');rg.add_argument('--db',required=True);rg.add_argument('--case-id',required=True);rg.add_argument('--fixture',required=True)
    cl=sub.add_parser('claim');cl.add_argument('--db',required=True);cl.add_argument('--case-id',required=True);cl.add_argument('--claim-id',required=True);cl.add_argument('--contract',required=True,choices=['registered_owner','receipt_bearer']);cl.add_argument('--ticket');cl.add_argument('--person');cl.add_argument('--receipt-file');cl.add_argument('--at')
    ho=sub.add_parser('handover');ho.add_argument('--db',required=True);ho.add_argument('--release-id',required=True,type=int);ho.add_argument('--deliverer',required=True);ho.add_argument('--at')
    au=sub.add_parser('audit');au.add_argument('--db',required=True);au.add_argument('--case-id',required=True);au.add_argument('--out',required=True)
    q=p.parse_args(argv)
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif q.command=='lost-property':
        if q.lp=='register':print(json.dumps(register_case(q.db,q.case_id,q.fixture),ensure_ascii=False))
        elif q.lp=='claim':
            cred={}
            if q.ticket:cred['ticket']=q.ticket
            if q.person:cred['person']=q.person
            if q.receipt_file:cred['receipt_serial']=Path(q.receipt_file).read_text(encoding='utf-8').strip()
            print(json.dumps(verify_claim(q.db,q.case_id,q.claim_id,q.contract,cred,at=q.at),ensure_ascii=False))
        elif q.lp=='handover':print(json.dumps(counter_handover(q.db,q.release_id,q.deliverer,at=q.at),ensure_ascii=False))
        elif q.lp=='audit':print(property_audit(q.db,q.case_id,q.out))
if __name__=='__main__':main()
