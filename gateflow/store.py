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
CREATE TABLE IF NOT EXISTS archive_recovery_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_publish(case_id TEXT PRIMARY KEY,source_id TEXT NOT NULL,revision INTEGER NOT NULL,contract_id TEXT NOT NULL,file_path TEXT NOT NULL,full_sha256 TEXT NOT NULL,length INTEGER NOT NULL,source_row INTEGER REFERENCES source(id),event_count INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('published','blocked')),reason TEXT NOT NULL,published_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_recovery_attempt(id INTEGER PRIMARY KEY,case_id TEXT NOT NULL,contract_id TEXT NOT NULL,source_id TEXT NOT NULL,revision INTEGER NOT NULL,manifest_digest TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('published','blocked')),full_sha256 TEXT,length INTEGER,file_path TEXT,source_row INTEGER,event_count INTEGER,reason TEXT NOT NULL,report_json TEXT NOT NULL,attempted_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS archive_chunk_decision(id INTEGER PRIMARY KEY,attempt_id INTEGER NOT NULL REFERENCES archive_recovery_attempt(id),case_id TEXT NOT NULL,chunk INTEGER NOT NULL,replica TEXT NOT NULL,shard_path TEXT,observed_sha256 TEXT,expected_sha256 TEXT,byte_length INTEGER,decision TEXT NOT NULL CHECK(decision IN ('selected','witness','rejected','blocked')),reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row
    db.execute('PRAGMA busy_timeout=15000');db.execute('PRAGMA foreign_keys=ON')
    db.executescript(SCHEMA);return db
