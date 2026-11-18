import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .exhibit_move import load_catalog,register,plan,verify
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
    xc=s.add_parser('exhibit-catalog');xc.add_argument('--db',required=True);xc.add_argument('--fixture',required=True)
    xr=s.add_parser('exhibit-register');xr.add_argument('--db',required=True);xr.add_argument('--case',required=True);xr.add_argument('--fixture',required=True)
    xp=s.add_parser('exhibit-plan');xp.add_argument('--db',required=True);xp.add_argument('--case',required=True);xp.add_argument('--out')
    xv=s.add_parser('exhibit-verify');xv.add_argument('--db',required=True);xv.add_argument('--case',required=True)
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif q.command=='exhibit-catalog':print(json.dumps(load_catalog(q.db,q.fixture),ensure_ascii=False))
    elif q.command=='exhibit-register':print(json.dumps(register(q.db,q.case,q.fixture),ensure_ascii=False))
    elif q.command=='exhibit-plan':print(json.dumps(plan(q.db,q.case,q.out),ensure_ascii=False))
    elif q.command=='exhibit-verify':print(json.dumps(verify(q.db,q.case),ensure_ascii=False))
if __name__=='__main__':main()
