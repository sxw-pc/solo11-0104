import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from . import climate_control
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('batch',['id','start','end']),('zone',['id','capacity']),('gate_map',['gate','zone','start','end']),('ticket',['id','type'])]:
            for r in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[r[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='gateflow-'));root.mkdir(parents=True,exist_ok=True);fixture=Path(__file__).resolve().parent.parent/'fixtures';db=root/'gateflow.db'
    initialize(db,fixture/'config.json');ingest(fixture/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def climate_demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='gateflow-climate-'));root.mkdir(parents=True,exist_ok=True)
    fixture=Path(__file__).resolve().parent.parent/'fixtures';db=root/'climate.db'
    initialize(db,fixture/'climate-config.json')
    climate_control.register(db,'CLIMATE-1',fixture/'climate-case.json')
    plans=climate_control.plan_all(db,'CLIMATE-1')
    path=climate_control.report(db,'CLIMATE-1',root/'climate-report.json')
    return {'db':str(db),'report':str(path),
            'plans':{contract:{k:payload.get(k) for k in ('contract','status','energy_cents','total_exposure','max_zone_exposure','commands')}
                     for contract,payload in plans.items()}}
def _dump(obj):
    return json.dumps(obj,ensure_ascii=False,indent=2,sort_keys=True)
def main():
    p=argparse.ArgumentParser(description='离线闸机客流与展馆保存环境证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    cd=s.add_parser('climate-demo');cd.add_argument('--workdir')
    cr=s.add_parser('climate-register');cr.add_argument('--db',required=True);cr.add_argument('--case-id',required=True);cr.add_argument('--fixture',required=True)
    cp=s.add_parser('climate-plan');cp.add_argument('--db',required=True);cp.add_argument('--case-id',required=True);cp.add_argument('--contract',default='min_energy');cp.add_argument('--all-contracts',action='store_true');cp.add_argument('--out')
    cx=s.add_parser('climate-execute');cx.add_argument('--db',required=True);cx.add_argument('--case-id',required=True);cx.add_argument('--contract',default='min_energy');cx.add_argument('--plan-id',type=int);cx.add_argument('--slots',type=int,nargs='*')
    cf=s.add_parser('climate-feedback');cf.add_argument('--db',required=True);cf.add_argument('--case-id',required=True);cf.add_argument('--contract',default='min_energy');cf.add_argument('--feedback',required=True)
    cq=s.add_parser('climate-report');cq.add_argument('--db',required=True);cq.add_argument('--case-id',required=True);cq.add_argument('--out',required=True)
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
    elif q.command=='climate-demo':print(_dump(climate_demo(q.workdir)))
    elif q.command=='climate-register':print(_dump(climate_control.register(q.db,q.case_id,q.fixture)))
    elif q.command=='climate-plan':
        out=climate_control.plan_all(q.db,q.case_id) if q.all_contracts else {q.contract:climate_control.plan(q.db,q.case_id,q.contract)}
        if q.out:
            Path(q.out).parent.mkdir(parents=True,exist_ok=True)
            Path(q.out).write_text(_dump(out),encoding='utf-8')
        print(_dump(out))
    elif q.command=='climate-execute':print(_dump(climate_control.execute(q.db,q.case_id,q.contract,q.plan_id,q.slots)))
    elif q.command=='climate-feedback':print(_dump(climate_control.apply_feedback(q.db,q.case_id,q.contract,q.feedback)))
    elif q.command=='climate-report':print(climate_control.report(q.db,q.case_id,q.out))
if __name__=='__main__':main()
