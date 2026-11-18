import sqlite3
from pathlib import Path
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS batch(id TEXT PRIMARY KEY,start TEXT NOT NULL,end TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS zone(id TEXT PRIMARY KEY,capacity INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS gate_map(gate TEXT NOT NULL,zone TEXT NOT NULL REFERENCES zone(id),start TEXT NOT NULL,end TEXT NOT NULL,PRIMARY KEY(gate,start));
CREATE TABLE IF NOT EXISTS ticket(id TEXT PRIMARY KEY,type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,name TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,device TEXT NOT NULL,seq INTEGER NOT NULL,kind TEXT NOT NULL,at TEXT NOT NULL,gate TEXT,ticket TEXT,ref_device TEXT,ref_seq INTEGER,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(device,seq));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,code TEXT NOT NULL,subject TEXT NOT NULL,event_id INTEGER REFERENCES event(id),detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS occupancy(id INTEGER PRIMARY KEY,at TEXT NOT NULL,zone TEXT NOT NULL,count INTEGER NOT NULL,confidence TEXT NOT NULL,event_id INTEGER REFERENCES event(id));
CREATE TABLE IF NOT EXISTS decision(zone TEXT PRIMARY KEY,status TEXT NOT NULL,peak INTEGER NOT NULL,detail TEXT NOT NULL,rule_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS evidence(id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL REFERENCES event(id), source_sha256 TEXT NOT NULL, payload_sha256 TEXT NOT NULL, role TEXT NOT NULL, UNIQUE(event_id,role));
CREATE TABLE IF NOT EXISTS insured_exhibit(id TEXT PRIMARY KEY,owner TEXT NOT NULL,zone TEXT NOT NULL REFERENCES zone(id),agreed_value_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS claim_advance(id TEXT PRIMARY KEY,exhibit TEXT NOT NULL REFERENCES insured_exhibit(id),paid_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS exhibit_claim_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS exhibit_claim_case_archive(case_id TEXT NOT NULL,version INTEGER NOT NULL,payload TEXT NOT NULL,digest TEXT NOT NULL,archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,PRIMARY KEY(case_id,version));
CREATE TABLE IF NOT EXISTS claim_settlement(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL REFERENCES exhibit_claim_case(id),revision INTEGER NOT NULL,contract TEXT NOT NULL,exhibit TEXT NOT NULL REFERENCES insured_exhibit(id),owner TEXT NOT NULL,zone TEXT NOT NULL,inspection_report TEXT NOT NULL,attachment_sha256 TEXT NOT NULL,quote_cents INTEGER NOT NULL,agreed_value_cents INTEGER NOT NULL,deductible_cents INTEGER NOT NULL,due_cents INTEGER NOT NULL,advance_id TEXT NOT NULL REFERENCES claim_advance(id),advance_paid_cents INTEGER NOT NULL,net_cents INTEGER NOT NULL,case_digest TEXT NOT NULL,reversal_of INTEGER REFERENCES claim_settlement(id),status TEXT NOT NULL CHECK(status IN ('已结算')),created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(case_id,revision));
CREATE TABLE IF NOT EXISTS claim_ledger(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL REFERENCES exhibit_claim_case(id),settlement_id INTEGER NOT NULL REFERENCES claim_settlement(id),kind TEXT NOT NULL CHECK(kind IN ('settlement','reversal')),revision INTEGER NOT NULL,amount_cents INTEGER NOT NULL,exhibit TEXT NOT NULL,owner TEXT NOT NULL,contract TEXT NOT NULL,advance_id TEXT NOT NULL,attachment_sha256 TEXT NOT NULL,note TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS claim_review(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL REFERENCES exhibit_claim_case(id),contract TEXT NOT NULL,reasons TEXT NOT NULL,detail TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
"""
def _migrate(db):
    cols={r['name'] for r in db.execute('PRAGMA table_info(exhibit_claim_case)')}
    if cols and 'version' not in cols:
        db.execute('ALTER TABLE exhibit_claim_case ADD COLUMN version INTEGER NOT NULL DEFAULT 1')
        db.commit()
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);_migrate(db);return db
