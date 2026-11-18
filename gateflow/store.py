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
CREATE TABLE IF NOT EXISTS climate_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS climate_plan(
 id INTEGER PRIMARY KEY,
 case_id TEXT NOT NULL REFERENCES climate_case(id),
 contract TEXT NOT NULL,
 version INTEGER NOT NULL,
 status TEXT NOT NULL,
 feasible INTEGER NOT NULL,
 energy_cents INTEGER,
 total_exposure REAL,
 max_zone_exposure REAL,
 input_digest TEXT NOT NULL,
 digest TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 supersedes INTEGER REFERENCES climate_plan(id),
 created_at TEXT NOT NULL,
 payload TEXT NOT NULL,
 UNIQUE(case_id,contract,version)
);
CREATE TABLE IF NOT EXISTS climate_execution(
 id INTEGER PRIMARY KEY,
 case_id TEXT NOT NULL REFERENCES climate_case(id),
 plan_id INTEGER NOT NULL REFERENCES climate_plan(id),
 slot INTEGER NOT NULL,
 zone TEXT,
 command TEXT NOT NULL,
 executed_at TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 UNIQUE(case_id,slot)
);
CREATE TABLE IF NOT EXISTS climate_slot_plan(
 id INTEGER PRIMARY KEY,
 plan_id INTEGER NOT NULL REFERENCES climate_plan(id),
 slot INTEGER NOT NULL,
 start TEXT NOT NULL,
 energy_cents INTEGER NOT NULL,
 source_readings TEXT NOT NULL DEFAULT '[]',
 start_temps TEXT NOT NULL,
 end_temps TEXT NOT NULL,
 exposure TEXT NOT NULL,
 cumulative_exposure TEXT NOT NULL,
 action_type TEXT NOT NULL,
 action_zones TEXT NOT NULL,
 cooling_c REAL NOT NULL,
 device_occupied INTEGER NOT NULL,
 device_used INTEGER NOT NULL,
 source_basis TEXT NOT NULL,
 executed INTEGER NOT NULL DEFAULT 0,
 execution_id INTEGER REFERENCES climate_execution(id),
 UNIQUE(plan_id,slot)
);
CREATE TABLE IF NOT EXISTS climate_feedback(
 id INTEGER PRIMARY KEY,
 case_id TEXT NOT NULL REFERENCES climate_case(id),
 plan_id INTEGER REFERENCES climate_plan(id),
 slot INTEGER NOT NULL,
 zone TEXT NOT NULL,
 at TEXT NOT NULL,
 temp_c REAL NOT NULL,
 sensor TEXT,
 role TEXT NOT NULL,
 source_sha256 TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(case_id,slot,zone,at)
);
CREATE TABLE IF NOT EXISTS climate_issue(
 id INTEGER PRIMARY KEY,
 plan_id INTEGER NOT NULL REFERENCES climate_plan(id),
 code TEXT NOT NULL,
 subject TEXT NOT NULL,
 detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS climate_plan_case_idx ON climate_plan(case_id,contract,version);
CREATE INDEX IF NOT EXISTS climate_slot_plan_plan_idx ON climate_slot_plan(plan_id,slot);
CREATE INDEX IF NOT EXISTS climate_feedback_plan_idx ON climate_feedback(plan_id);
CREATE INDEX IF NOT EXISTS climate_feedback_case_idx ON climate_feedback(case_id,slot,zone);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
