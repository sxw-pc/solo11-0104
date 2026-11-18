import hashlib,json,sqlite3
from pathlib import Path
import pytest
from gateflow.__main__ import initialize,main
from gateflow.exhibit_claims import register,revise,settle,reconcile
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'

def setup_case(tmp_path):
    db=tmp_path/'claim.db';initialize(db,FIX/'claim-config.json');register(db,'CASE-1',FIX/'exhibit-claim.json');return db

def make_case(root,*,quote=30000,paid=60000,signed=True,report_id='IR-1',owner='OWNER-1',deductible=5000,
              contracts=('restoration_cost','agreed_value'),advance_id='ADV-1',exhibit='EX-1'):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    att={'exhibit':exhibit,'inspection':'cracked display case; exhibit body intact','restoration_quote_cents':quote,
         'signed_by':'EXAMINER-1' if signed else ''}
    (root/'damage-inspection.json').write_text(json.dumps(att,indent=2),encoding='utf-8')
    sha=hashlib.sha256((root/'damage-inspection.json').read_bytes()).hexdigest()
    doc={'asset':{'id':exhibit,'owner':owner,'zone':'A','agreed_value_cents':100000},
         'inspection':{'report_id':report_id,'inspector':'EXAMINER-1' if signed else '',
             'signed_at':'2026-01-01T10:00:00+00:00' if signed else '','damage':'cracked_case','repairable':True,
             'restoration_quote_cents':quote,'attachment':'damage-inspection.json','attachment_sha256':sha},
         'advance':{'id':advance_id,'paid_cents':paid},'deductible_cents':deductible,'contracts':list(contracts)}
    (root/'exhibit-claim.json').write_text(json.dumps(doc,indent=2),encoding='utf-8')
    return root/'exhibit-claim.json'

def fresh(tmp_path,case):
    db=tmp_path/'claim.db';initialize(db,FIX/'claim-config.json');register(db,'CASE-1',case);return db

def test_settle_restoration_cost_owner_returns(tmp_path):
    r=settle(setup_case(tmp_path),'CASE-1','restoration_cost')
    assert r['status']=='已结算' and r['revision']==1 and r['due_cents']==25000
    assert r['advance_paid_cents']==60000 and r['net_cents']==-35000 and r['direction']=='业主返还'

def test_settle_agreed_value_insurer_topup(tmp_path):
    r=settle(setup_case(tmp_path),'CASE-1','agreed_value')
    assert r['status']=='已结算' and r['due_cents']==95000 and r['net_cents']==35000 and r['direction']=='承保方补付'

def test_settle_is_idempotent(tmp_path):
    db=setup_case(tmp_path)
    assert settle(db,'CASE-1','restoration_cost')==settle(db,'CASE-1','restoration_cost')
    c=connect(db)
    try:
        assert c.execute('SELECT count(*) FROM claim_settlement').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM claim_ledger').fetchone()[0]==1
    finally:c.close()

def test_contract_switch_reverses_and_flips_direction(tmp_path):
    db=setup_case(tmp_path)
    settle(db,'CASE-1','restoration_cost')
    r=settle(db,'CASE-1','agreed_value')
    assert r['revision']==2 and r['net_cents']==35000 and r['direction']=='承保方补付'
    c=connect(db)
    try:
        entries=[(e['kind'],e['amount_cents']) for e in c.execute('SELECT * FROM claim_ledger ORDER BY id')]
        assert entries==[('settlement',-35000),('reversal',35000),('settlement',35000)]
        assert c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM claim_ledger').fetchone()[0]==35000
        assert c.execute('SELECT net_cents FROM claim_settlement WHERE revision=2').fetchone()[0]==95000-60000
        assert c.execute('SELECT reversal_of FROM claim_settlement WHERE revision=2').fetchone()[0]==1
    finally:c.close()

def test_two_consecutive_revisions_keep_chain(tmp_path):
    db=setup_case(tmp_path)
    settle(db,'CASE-1','restoration_cost')
    revise(db,'CASE-1',make_case(tmp_path/'v2',quote=40000,report_id='IR-2'))
    r2=settle(db,'CASE-1','restoration_cost')
    assert r2['revision']==2 and r2['due_cents']==35000 and r2['net_cents']==-25000
    revise(db,'CASE-1',make_case(tmp_path/'v3',quote=20000,report_id='IR-3'))
    r3=settle(db,'CASE-1','restoration_cost')
    assert r3['revision']==3 and r3['due_cents']==15000 and r3['net_cents']==-45000
    c=connect(db)
    try:
        assert c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM claim_ledger').fetchone()[0]==-45000
        assert [r[0] for r in c.execute('SELECT quote_cents FROM claim_settlement ORDER BY revision')]==[30000,40000,20000]
        assert c.execute('SELECT count(*) FROM exhibit_claim_case_archive').fetchone()[0]==2
        archived=json.loads(c.execute('SELECT payload FROM exhibit_claim_case_archive WHERE version=1').fetchone()[0])
        assert archived['case']['inspection']['restoration_quote_cents']==30000
        assert c.execute('SELECT paid_cents FROM claim_advance WHERE id="ADV-1"').fetchone()[0]==60000
    finally:c.close()

def test_revise_is_idempotent_and_requires_case(tmp_path):
    db=setup_case(tmp_path)
    f=make_case(tmp_path/'v2',quote=40000,report_id='IR-2')
    assert revise(db,'CASE-1',f)==revise(db,'CASE-1',f)
    c=connect(db)
    try:assert c.execute('SELECT count(*) FROM exhibit_claim_case_archive').fetchone()[0]==1
    finally:c.close()
    with pytest.raises(ValueError,match='claim_case_missing'):
        revise(db,'CASE-9',f)

def test_tampered_attachment_goes_pending(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'tampered'))
    att=tmp_path/'tampered'/'damage-inspection.json'
    doc=json.loads(att.read_text(encoding='utf-8'));doc['restoration_quote_cents']=99999
    att.write_text(json.dumps(doc,indent=2),encoding='utf-8')
    r=settle(db,'CASE-1','restoration_cost')
    assert r['status']=='待核' and 'attachment_mismatch' in r['reasons']
    c=connect(db)
    try:
        assert c.execute('SELECT count(*) FROM claim_settlement').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM claim_ledger').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM claim_review WHERE reasons LIKE '%attachment_mismatch%'").fetchone()[0]==1
    finally:c.close()

def test_owner_mismatch_goes_pending(tmp_path):
    db=setup_case(tmp_path)
    c=connect(db);c.execute("UPDATE insured_exhibit SET owner='OWNER-2' WHERE id='EX-1'");c.commit();c.close()
    r=settle(db,'CASE-1','agreed_value')
    assert r['status']=='待核' and 'owner_mismatch' in r['reasons']

def test_same_exhibit_other_owner_rejected_at_register(tmp_path):
    db=setup_case(tmp_path)
    with pytest.raises(ValueError,match='claim_asset_conflict'):
        register(db,'CASE-2',make_case(tmp_path/'other',owner='OWNER-2',report_id='IR-9',advance_id='ADV-2'))

def test_duplicate_incident_goes_pending(tmp_path):
    db=tmp_path/'claim.db';initialize(db,FIX/'claim-config.json')
    register(db,'CASE-1',FIX/'exhibit-claim.json');register(db,'CASE-2',FIX/'exhibit-claim.json')
    assert 'duplicate_incident' in settle(db,'CASE-1','restoration_cost')['reasons']
    assert 'duplicate_incident' in settle(db,'CASE-2','restoration_cost')['reasons']
    c=connect(db)
    try:assert c.execute('SELECT count(*) FROM claim_settlement').fetchone()[0]==0
    finally:c.close()

def test_unsigned_inspection_goes_pending(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'unsigned',signed=False))
    r=settle(db,'CASE-1','restoration_cost')
    assert r['status']=='待核' and 'inspection_unsigned' in r['reasons']

def test_invalid_quote_goes_pending(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'badquote',quote=0))
    r=settle(db,'CASE-1','restoration_cost')
    assert r['status']=='待核' and 'invalid_quote' in r['reasons']

def test_unpaid_advance_goes_pending(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'unpaid',paid=0))
    r=settle(db,'CASE-1','restoration_cost')
    assert r['status']=='待核' and 'advance_unpaid' in r['reasons']

def test_advance_amount_tampered_goes_pending(tmp_path):
    db=setup_case(tmp_path)
    c=connect(db);c.execute("UPDATE claim_advance SET paid_cents=50000 WHERE id='ADV-1'");c.commit();c.close()
    r=settle(db,'CASE-1','restoration_cost')
    assert r['status']=='待核' and 'advance_unpaid' in r['reasons']

@pytest.mark.parametrize('quote,due',[(4999,0),(5000,0),(5001,1),(30000,25000)])
def test_quote_crossing_deductible(tmp_path,quote,due):
    db=fresh(tmp_path,make_case(tmp_path/'q',quote=quote))
    r=settle(db,'CASE-1','restoration_cost')
    assert r['due_cents']==due and r['net_cents']==due-60000

def test_advance_exactly_equals_due(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'exact',paid=25000))
    r=settle(db,'CASE-1','restoration_cost')
    assert r['net_cents']==0 and r['direction']=='持平'

def test_conservation_and_bindings(tmp_path):
    db=setup_case(tmp_path)
    settle(db,'CASE-1','restoration_cost')
    revise(db,'CASE-1',make_case(tmp_path/'v2',quote=40000,report_id='IR-2'))
    settle(db,'CASE-1','agreed_value')
    c=connect(db)
    try:
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        rows=c.execute('SELECT * FROM claim_settlement ORDER BY revision').fetchall()
        top=c.execute('SELECT max(revision) FROM claim_settlement').fetchone()[0]
        for row in rows:
            assert row['due_cents']-row['advance_paid_cents']==row['net_cents']
            own=c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM claim_ledger WHERE settlement_id=?',(row['id'],)).fetchone()[0]
            assert own==(row['net_cents'] if row['revision']==top else 0)
        assert c.execute('SELECT COALESCE(SUM(amount_cents),0) FROM claim_ledger').fetchone()[0]==rows[-1]['net_cents']
        for e in c.execute('SELECT * FROM claim_ledger').fetchall():
            assert e['exhibit']=='EX-1' and e['owner']=='OWNER-1' and e['advance_id']=='ADV-1'
            assert e['revision']>=1 and len(e['attachment_sha256'])==64 and e['contract'] in ('restoration_cost','agreed_value')
        assert c.execute('SELECT count(*) FROM claim_advance').fetchone()[0]==1
    finally:c.close()

def test_unauthorized_contract_rejected(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'one',contracts=('agreed_value',)))
    with pytest.raises(ValueError,match='contract_not_authorized'):
        settle(db,'CASE-1','restoration_cost')

def test_settle_unknown_case(tmp_path):
    db=tmp_path/'claim.db';initialize(db,FIX/'claim-config.json')
    with pytest.raises(ValueError,match='claim_case_missing'):
        settle(db,'CASE-9','restoration_cost')

def test_reconcile_report(tmp_path):
    db=setup_case(tmp_path)
    settle(db,'CASE-1','restoration_cost')
    out=tmp_path/'reconcile.json';reconcile(db,out)
    data=json.loads(out.read_text(encoding='utf-8'))
    case=data['cases'][0]
    assert case['status']=='已结算' and case['revision']==1 and case['contract']=='restoration_cost'
    assert case['net_cents']==-35000 and case['balance_cents']==-35000 and case['direction']=='业主返还'
    assert case['advance_id']=='ADV-1' and case['advance_paid_cents']==60000
    assert case['ledger'][0]['attachment_sha256']==case['attachment_sha256']
    assert data['totals']=={'insurer_owes_owner_cents':0,'owner_owes_insurer_cents':35000,'net_cents':-35000}

def test_pending_case_in_report(tmp_path):
    db=fresh(tmp_path,make_case(tmp_path/'tampered'))
    att=tmp_path/'tampered'/'damage-inspection.json';att.write_text(att.read_text(encoding='utf-8')+' ',encoding='utf-8')
    settle(db,'CASE-1','restoration_cost')
    data=reconcile(db)
    assert data['cases'][0]['status']=='待核' and data['cases'][0]['reviews'] and data['cases'][0]['balance_cents']==0

def test_cli_claim_flow(tmp_path,monkeypatch):
    db=tmp_path/'claim.db';initialize(db,FIX/'claim-config.json')
    monkeypatch.setattr('sys.argv',['gateflow','claim-register','--db',str(db),'--case','CASE-1','--fixture',str(FIX/'exhibit-claim.json')]);main()
    monkeypatch.setattr('sys.argv',['gateflow','claim-settle','--db',str(db),'--case','CASE-1','--contract','restoration_cost']);main()
    out=tmp_path/'report.json'
    monkeypatch.setattr('sys.argv',['gateflow','claim-report','--db',str(db),'--out',str(out)]);main()
    data=json.loads(out.read_text(encoding='utf-8'))
    assert data['cases'][0]['net_cents']==-35000 and data['cases'][0]['direction']=='业主返还'

def test_migrate_old_case_table(tmp_path):
    db=tmp_path/'old.db'
    raw=sqlite3.connect(db)
    raw.executescript("CREATE TABLE exhibit_claim_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);INSERT INTO exhibit_claim_case VALUES ('CASE-0','{}','d0');")
    raw.commit();raw.close()
    c=connect(db)
    try:
        cols={r['name'] for r in c.execute('PRAGMA table_info(exhibit_claim_case)')}
        assert 'version' in cols
        assert c.execute('SELECT version FROM exhibit_claim_case WHERE id="CASE-0"').fetchone()[0]==1
    finally:c.close()
