import argparse,json,tempfile
from pathlib import Path
from .store import connect
from .ingest import ingest
from .rules import evaluate
from .report import report
from .gate_firmware import register,run_rollout,state,operation_status,evidence_report
def initialize(db_path,config):
    c=json.loads(Path(config).read_text(encoding='utf-8'));db=connect(db_path)
    with db:
        for table,cols in [('batch',['id','start','end']),('zone',['id','capacity']),('gate_map',['gate','zone','start','end']),('ticket',['id','type'])]:
            for r in c[table]:db.execute(f'INSERT OR IGNORE INTO {table} VALUES ({",".join("?" for _ in cols)})',[r[k] for k in cols])
    db.close()
def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='gateflow-'));root.mkdir(parents=True,exist_ok=True);fixture=Path(__file__).resolve().parent.parent/'fixtures';db=root/'gateflow.db'
    initialize(db,fixture/'config.json');ingest(fixture/'events.jsonl',db);evaluate(db);return report(db,root/'report.json')
def firmware_demo(workdir=None):
    """Run both authorized contracts on the frozen case and emit version evidence."""
    root=Path(workdir or tempfile.mkdtemp(prefix='gateflow-firmware-'));root.mkdir(parents=True,exist_ok=True);fixture=Path(__file__).resolve().parent.parent/'fixtures'
    out={}
    for contract in ('atomic_all_gates','zone_isolated'):
        db=root/f'{contract}.db'
        initialize(db,fixture/'gate-firmware-config.json');ingest(fixture/'gate-firmware-events.jsonl',db)
        register(db,'FIRM-1',fixture/'gate-firmware-case.json')
        summary=run_rollout(db,'FIRM-1',contract)
        p=evidence_report(db,'FIRM-1',contract,root/f'firmware-{contract}.json')
        out[contract]={'outcome':summary['outcome'],
                       'final_versions':{d['id']:d['version'] for d in summary['devices']},
                       'report':str(p)}
    return out
def main():
    p=argparse.ArgumentParser(description='离线闸机客流证据台');s=p.add_subparsers(dest='command',required=True)
    d=s.add_parser('demo');d.add_argument('--workdir')
    i=s.add_parser('init');i.add_argument('--db',required=True);i.add_argument('--config',required=True)
    a=s.add_parser('ingest');a.add_argument('file');a.add_argument('--db',required=True)
    e=s.add_parser('evaluate');e.add_argument('--db',required=True)
    r=s.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    x=s.add_parser('inspect');x.add_argument('--db',required=True)
    fd=s.add_parser('firmware-demo');fd.add_argument('--workdir')
    fr=s.add_parser('firmware-register');fr.add_argument('--db',required=True);fr.add_argument('--case-id',required=True);fr.add_argument('--fixture',required=True)
    fo=s.add_parser('firmware-run');fo.add_argument('--db',required=True);fo.add_argument('--case-id',required=True);fo.add_argument('--contract',required=True,choices=('atomic_all_gates','zone_isolated'));fo.add_argument('--max-attempts',type=int,default=3)
    fp=s.add_parser('firmware-report');fp.add_argument('--db',required=True);fp.add_argument('--case-id',required=True);fp.add_argument('--contract',required=True,choices=('atomic_all_gates','zone_isolated'));fp.add_argument('--out',required=True)
    fs=s.add_parser('firmware-state');fs.add_argument('--db',required=True);fs.add_argument('--device',required=True)
    fq=s.add_parser('firmware-operation');fq.add_argument('--db',required=True);fq.add_argument('--operation-id',required=True)
    q=p.parse_args()
    if q.command=='demo':print(demo(q.workdir))
    elif q.command=='firmware-demo':print(json.dumps(firmware_demo(q.workdir),ensure_ascii=False))
    elif q.command=='init':initialize(q.db,q.config)
    elif q.command=='ingest':print(ingest(q.file,q.db))
    elif q.command=='evaluate':print(evaluate(q.db))
    elif q.command=='report':print(report(q.db,q.out))
    elif q.command=='firmware-register':print(register(q.db,q.case_id,q.fixture))
    elif q.command=='firmware-run':print(json.dumps(run_rollout(q.db,q.case_id,q.contract,max_attempts=q.max_attempts),ensure_ascii=False,default=str))
    elif q.command=='firmware-report':print(evidence_report(q.db,q.case_id,q.contract,q.out))
    elif q.command=='firmware-state':print(json.dumps(state(q.db,q.device),ensure_ascii=False))
    elif q.command=='firmware-operation':print(json.dumps(operation_status(q.db,q.operation_id),ensure_ascii=False))
    elif q.command=='inspect':
        db=connect(q.db);print(json.dumps([dict(z) for z in db.execute('SELECT * FROM decision')],ensure_ascii=False));db.close()
if __name__=='__main__':main()
