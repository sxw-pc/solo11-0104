import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .archive_lab import register,recover,recovery_report
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('batch',['id','start','end']),('zone',['id','capacity']),('gate_map',['gate','zone','start','end']),('ticket',['id','type'])]:
            for r in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[r[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='gateflow-'));root.mkdir(parents=True,exist_ok=True);fixture=Path(__file__).resolve().parent.parent/'fixtures';db=root/'gateflow.db'
    initialize(db,fixture/'config.json');ingest(fixture/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def main():
    p=argparse.ArgumentParser(description='离线闸机客流证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    ar=s.add_parser('archive-register');ar.add_argument('--db',required=True);ar.add_argument('--case-id',required=True);ar.add_argument('--case',required=True)
    rc=s.add_parser('archive-recover');rc.add_argument('--db',required=True);rc.add_argument('--case-id',required=True);rc.add_argument('--contract',required=True,choices=('two_copy_quorum','verified_mosaic'));rc.add_argument('--recover-dir',required=True);rc.add_argument('--report')
    rr=s.add_parser('archive-report');rr.add_argument('--db',required=True);rr.add_argument('--case-id')
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='archive-register':print(register(q.db,q.case_id,q.case))
    elif q.command=='archive-recover':print(json.dumps(recover(q.db,q.case_id,q.contract,q.recover_dir,q.report),ensure_ascii=False,indent=2,sort_keys=True))
    elif q.command=='archive-report':print(json.dumps(recovery_report(q.db,q.case_id),ensure_ascii=False,indent=2,sort_keys=True))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()
