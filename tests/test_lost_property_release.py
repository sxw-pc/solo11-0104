import hashlib,json,sqlite3,threading
from pathlib import Path
import pytest
from gateflow.__main__ import initialize,main
from gateflow.lost_property import register,claim,handover,audit
from gateflow.store import connect
FIX=Path(__file__).resolve().parents[1]/'fixtures'
SERIAL='SYNTHETIC-RECEIPT-R1'
OWNER={'ticket':'T1','person':'ALICE'}
BEARER={'receipt_serial':SERIAL}
T='2026-01-01T09:30:00+00:00'

def setup(tmp_path,mut=None):
    db=tmp_path/'prop.db';initialize(db,FIX/'property-config.json')
    fixture=FIX/'property-case.json'
    if mut:
        doc=json.loads(fixture.read_text(encoding='utf-8'));mut(doc)
        fixture=tmp_path/'case.json';fixture.write_text(json.dumps(doc),encoding='utf-8')
    register(db,'PROP-1',fixture)
    return db

def one(db,sql):
    c=connect(db)
    try:return c.execute(sql).fetchone()[0]
    finally:c.close()

def test_registered_owner_releases_to_alice_and_refuses_bob(tmp_path):
    db=setup(tmp_path)
    bob=claim(db,'PROP-1','C-BEARER','registered_owner',BEARER,at=T)
    assert bob['verdict']=='refused' and bob['reason']=='credential_missing'
    alice=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    assert alice['verdict']=='authorized' and alice['claimant']=='ALICE' and alice['deliverer']=='S2'
    assert one(db,'SELECT released FROM property_item')==0  # 授权不等于实物交接
    done=handover(db,alice['release_id'],'S2',at='2026-01-01T09:40:00+00:00')
    assert done['verdict']=='released' and done['claimant']=='ALICE'
    assert one(db,'SELECT released FROM property_item')==1
    # 认领结果不得反写客流记录
    for table in ('event','occupancy','decision','issue'):
        assert one(db,f'SELECT count(*) FROM {table}')==0

def test_receipt_bearer_releases_to_bob_and_refuses_alice(tmp_path):
    db=setup(tmp_path)
    alice=claim(db,'PROP-1','C-OWNER','receipt_bearer',OWNER,at=T)
    assert alice['verdict']=='refused' and alice['reason']=='credential_missing'
    bob=claim(db,'PROP-1','C-BEARER','receipt_bearer',BEARER,at=T)
    assert bob['verdict']=='authorized' and bob['claimant']=='BOB'
    assert handover(db,bob['release_id'],'S2',at=T)['verdict']=='released'

def test_missing_acceptance_halts_both_contracts(tmp_path):
    db=setup(tmp_path,lambda d:d['custody'][1].pop('accepted_by'))
    for contract,cid,cred in [('registered_owner','C-OWNER',OWNER),('receipt_bearer','C-BEARER',BEARER)]:
        v=claim(db,'PROP-1',cid,contract,cred,at=T)
        assert v['verdict']=='halted' and v['reason']=='acceptance_missing'
    assert one(db,'SELECT count(*) FROM property_release')==0

def test_seal_change_halts_both_contracts(tmp_path):
    db=setup(tmp_path,lambda d:d['custody'][2].update(seal='SEALED-2'))
    for contract,cid,cred in [('registered_owner','C-OWNER',OWNER),('receipt_bearer','C-BEARER',BEARER)]:
        v=claim(db,'PROP-1',cid,contract,cred,at=T)
        assert v['verdict']=='halted' and v['reason']=='seal_changed'
    assert one(db,'SELECT count(*) FROM property_release')==0

def test_custody_time_must_not_regress(tmp_path):
    db=setup(tmp_path,lambda d:d['custody'][1].update(at='2026-01-01T07:00:00+00:00'))
    v=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    assert v['verdict']=='halted' and v['reason']=='custody_order'

def test_final_seal_check_required(tmp_path):
    db=setup(tmp_path,lambda d:d.update(custody=d['custody'][:2]))
    v=claim(db,'PROP-1','C-BEARER','receipt_bearer',BEARER,at=T)
    assert v['verdict']=='halted' and v['reason']=='final_seal_check_missing'

def test_receipt_digest_variant_halts(tmp_path):
    db=setup(tmp_path)
    c=connect(db);c.execute("UPDATE property_item SET receipt_digest='variant'");c.commit();c.close()
    for contract,cid,cred in [('registered_owner','C-OWNER',OWNER),('receipt_bearer','C-BEARER',BEARER)]:
        v=claim(db,'PROP-1',cid,contract,cred,at=T)
        assert v['verdict']=='halted' and v['reason']=='receipt_digest_mismatch'

def test_holder_dispute_halts_everyone(tmp_path):
    db=setup(tmp_path)
    c=connect(db);c.execute("UPDATE ticket_holder SET person='CAROL'");c.commit();c.close()
    for contract,cid,cred in [('registered_owner','C-OWNER',OWNER),('receipt_bearer','C-BEARER',BEARER)]:
        v=claim(db,'PROP-1',cid,contract,cred,at=T)
        assert v['verdict']=='halted' and v['reason']=='holder_disputed'

def test_duplicate_receipt_yields_exactly_one_release(tmp_path):
    def mut(d):d['claims'].append({'id':'C-BEARER-2','person':'CAROL','receipt_serial':SERIAL,'at':'2026-01-01T09:13:00+00:00'})
    db=setup(tmp_path,mut)
    first=claim(db,'PROP-1','C-BEARER','receipt_bearer',BEARER,at=T)
    second=claim(db,'PROP-1','C-BEARER-2','receipt_bearer',BEARER,at=T)
    assert first['verdict']=='authorized'
    assert second['verdict']=='refused' and second['reason']=='item_committed'
    again=claim(db,'PROP-1','C-BEARER','receipt_bearer',BEARER,at=T)
    assert again['verdict']=='refused' and again['reason']=='item_committed'
    assert one(db,'SELECT count(*) FROM property_release')==1

def test_wrong_ticket_refused_and_item_stays_releasable(tmp_path):
    db=setup(tmp_path)
    bad=claim(db,'PROP-1','C-OWNER','registered_owner',{'ticket':'T2','person':'ALICE'},at=T)
    assert bad['verdict']=='refused' and bad['reason']=='claim_mismatch'
    stranger=claim(db,'PROP-1','C-OWNER','registered_owner',{'ticket':'T1','person':'BOB'},at=T)
    assert stranger['verdict']=='refused' and stranger['reason']=='claim_mismatch'
    assert one(db,'SELECT count(*) FROM property_release')==0
    good=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    assert good['verdict']=='authorized'

def test_frozen_claim_with_wrong_ticket_refused(tmp_path):
    db=setup(tmp_path,lambda d:d['claims'][0].update(ticket='T2'))
    v=claim(db,'PROP-1','C-OWNER','registered_owner',{'ticket':'T2','person':'ALICE'},at=T)
    assert v['verdict']=='refused' and v['reason']=='ticket_mismatch'

def test_competing_claims_have_exactly_one_winner(tmp_path):
    db=setup(tmp_path);results=[]
    def race(cid,contract,cred):results.append(claim(db,'PROP-1',cid,contract,cred,at=T))
    threads=[threading.Thread(target=race,args=('C-OWNER','registered_owner',OWNER)),
             threading.Thread(target=race,args=('C-BEARER','receipt_bearer',BEARER))]
    for t in threads:t.start()
    for t in threads:t.join()
    assert sorted(r['verdict'] for r in results)==['authorized','refused']
    assert one(db,'SELECT count(*) FROM property_release')==1
    c=connect(db)
    rel=c.execute('SELECT * FROM property_release').fetchone();item=c.execute('SELECT * FROM property_item').fetchone()
    assert item['version']==rel['item_version'] and item['released']==0
    c.close()

def test_shift_change_blocks_old_authorization(tmp_path):
    db=setup(tmp_path)
    alice=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    assert alice['deliverer']=='S2'
    stale=handover(db,alice['release_id'],'S3',at=T)
    assert stale['verdict']=='rejected' and stale['reason']=='deliverer_mismatch'
    assert one(db,'SELECT released FROM property_item')==0
    assert handover(db,alice['release_id'],'S2',at=T)['verdict']=='released'

def test_failed_handover_retries_but_never_replays(tmp_path):
    db=setup(tmp_path)
    alice=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    c=connect(db)
    c.execute("CREATE TRIGGER reject_handover BEFORE INSERT ON property_handover BEGIN SELECT RAISE(ABORT,'injected'); END")
    c.commit();c.close()
    with pytest.raises(sqlite3.IntegrityError):
        handover(db,alice['release_id'],'S2',at=T)
    assert one(db,"SELECT status FROM property_release")=='authorized'
    assert one(db,'SELECT released FROM property_item')==0
    c=connect(db);c.execute('DROP TRIGGER reject_handover');c.commit();c.close()
    assert handover(db,alice['release_id'],'S2',at=T)['verdict']=='released'
    replay=handover(db,alice['release_id'],'S2',at=T)
    assert replay['verdict']=='rejected' and replay['reason']=='already_released'
    assert one(db,"SELECT count(*) FROM property_handover WHERE result='released'")==1

def test_released_item_blocks_all_further_claims(tmp_path):
    db=setup(tmp_path)
    alice=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    handover(db,alice['release_id'],'S2',at=T)
    for cid,contract,cred in [('C-BEARER','receipt_bearer',BEARER),('C-OWNER','registered_owner',OWNER)]:
        v=claim(db,'PROP-1',cid,contract,cred,at=T)
        assert v['verdict']=='refused' and v['reason']=='item_released'

def test_contract_must_be_frozen_and_claim_must_exist(tmp_path):
    db=setup(tmp_path,lambda d:d.update(contracts=['registered_owner']))
    v=claim(db,'PROP-1','C-BEARER','receipt_bearer',BEARER,at=T)
    assert v['verdict']=='refused' and v['reason']=='contract_not_frozen'
    v=claim(db,'PROP-1','C-NOPE','registered_owner',OWNER,at=T)
    assert v['verdict']=='refused' and v['reason']=='claim_unknown'

def test_audit_report_keeps_digests_only(tmp_path):
    db=setup(tmp_path)
    claim(db,'PROP-1','C-BEARER','registered_owner',BEARER,at=T)
    alice=claim(db,'PROP-1','C-OWNER','registered_owner',OWNER,at=T)
    handover(db,alice['release_id'],'S2',at='2026-01-01T09:40:00+00:00')
    out=tmp_path/'audit.json';audit(db,'PROP-1',out)
    text=out.read_text(encoding='utf-8')
    assert SERIAL not in text and 'T1' not in text
    data=json.loads(text)
    assert data['item']['receipt_digest']==hashlib.sha256(SERIAL.encode()).hexdigest()
    assert data['item']['deposit_ticket_digest']==hashlib.sha256(b'ticket:T1').hexdigest()
    assert data['releases'][0]['claimant']=='ALICE' and data['releases'][0]['deliverer']=='S2'
    assert data['releases'][0]['custody_digest']==data['custody_digest']
    assert data['handovers'][0]['result']=='released'
    assert {a['verdict'] for a in data['attempts']}=={'refused','authorized'}
    assert [e['kind'] for e in data['custody_chain']]==['deposit','transfer','seal_check']
    assert all(e['seal_intact'] for e in data['custody_chain'])

def test_cli_offline_claim_handover_audit(tmp_path,capsys):
    db=tmp_path/'cli.db'
    main(['init','--db',str(db),'--config',str(FIX/'property-config.json')])
    main(['lost-property','register','--db',str(db),'--case-id','PROP-1','--fixture',str(FIX/'property-case.json')])
    receipt=tmp_path/'receipt.txt';receipt.write_text(SERIAL,encoding='utf-8')
    main(['lost-property','claim','--db',str(db),'--case-id','PROP-1','--claim-id','C-BEARER','--contract','receipt_bearer','--receipt-file',str(receipt),'--at',T])
    verdict=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert verdict['verdict']=='authorized' and verdict['claimant']=='BOB'
    main(['lost-property','handover','--db',str(db),'--release-id',str(verdict['release_id']),'--deliverer','S2','--at',T])
    done=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert done['verdict']=='released'
    out=tmp_path/'audit.json'
    main(['lost-property','audit','--db',str(db),'--case-id','PROP-1','--out',str(out)])
    data=json.loads(out.read_text(encoding='utf-8'))
    assert data['releases'][0]['status']=='released' and SERIAL not in out.read_text(encoding='utf-8')

def test_connect_migrates_pre_release_schema(tmp_path):
    db_path=tmp_path/'old.db'
    db=sqlite3.connect(db_path)
    db.execute('CREATE TABLE property_item(id TEXT PRIMARY KEY,deposit_ticket TEXT NOT NULL,receipt_digest TEXT NOT NULL,seal TEXT NOT NULL)')
    db.execute("INSERT INTO property_item VALUES ('L-1','T1','d','SEALED-1')")
    db.commit();db.close()
    c=connect(db_path)
    cols={r['name'] for r in c.execute('PRAGMA table_info(property_item)')}
    assert {'released','version'}<=cols
    row=c.execute('SELECT * FROM property_item').fetchone()
    assert row['released']==0 and row['version']==0
    c.close()
