import ast,json,sys
from pathlib import Path
import pytest
from gateflow.__main__ import main
from gateflow.archive_lab import recovery_report
from tests.archive_case_builder import sha256

FIX=Path(__file__).resolve().parents[1]/'fixtures'
TRUE_FULL=sha256((FIX/'archive/A0.bin').read_bytes()+(FIX/'archive/B1.bin').read_bytes())

def run_cli(argv,capfd):
    old=sys.argv;sys.argv=['gateflow']+argv
    try:main()
    finally:sys.argv=old
    return capfd.readouterr().out.strip()

def parse(out):
    if not out:return None
    try:return json.loads(out)
    except json.JSONDecodeError:return ast.literal_eval(out)

def test_cli_archive_lifecycle(capfd,tmp_path):
    db=tmp_path/'cli.db';rdir=tmp_path/'recovered';rep_file=tmp_path/'evidence.json'
    run_cli(['init','--db',str(db),'--config',str(FIX/'archive-config.json')],capfd)
    reg=parse(run_cli(['archive-register','--db',str(db),'--case-id','ARCH-1','--case',str(FIX/'archive-case.json')],capfd))
    assert reg['shards']==4
    blocked=parse(run_cli(['archive-recover','--db',str(db),'--case-id','ARCH-1',
                           '--contract','two_copy_quorum','--recover-dir',str(rdir),
                           '--report',str(rep_file)],capfd))
    assert blocked['status']=='blocked'
    published=parse(run_cli(['archive-recover','--db',str(db),'--case-id','ARCH-1',
                             '--contract','verified_mosaic','--recover-dir',str(rdir)],capfd))
    assert published['status']=='published'
    assert published['assembly']['full_sha256']==TRUE_FULL
    assert json.loads(rep_file.read_text(encoding='utf-8'))['status']=='blocked'
    ledger=parse(run_cli(['archive-report','--db',str(db),'--case-id','ARCH-1'],capfd))
    assert [a['attempt']['status'] for a in ledger]==['blocked','published']
    assert len(ledger[0]['chunks'])==4 and len(ledger[1]['chunks'])==4
    assert len(recovery_report(db))==2 and len(recovery_report(db,'ARCH-1'))==2

def test_cli_rejects_unknown_contract(capfd,tmp_path):
    db=tmp_path/'cli.db'
    run_cli(['init','--db',str(db),'--config',str(FIX/'archive-config.json')],capfd)
    run_cli(['archive-register','--db',str(db),'--case-id','ARCH-1','--case',str(FIX/'archive-case.json')],capfd)
    with pytest.raises(SystemExit):
        run_cli(['archive-recover','--db',str(db),'--case-id','ARCH-1',
                 '--contract','nope','--recover-dir',str(tmp_path/'x')],capfd)

def test_cli_demo_still_runs(capfd,tmp_path):
    run_cli(['demo','--workdir',str(tmp_path/'demo')],capfd)
    data=json.loads((tmp_path/'demo/report.json').read_text(encoding='utf-8'))
    assert len(data['event'])==5 and data['decision']
