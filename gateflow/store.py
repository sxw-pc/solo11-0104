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
CREATE TABLE IF NOT EXISTS gate_firmware_device(id TEXT PRIMARY KEY,gate TEXT NOT NULL,cell TEXT NOT NULL,version TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS gate_firmware_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gate_firmware_operation(id TEXT PRIMARY KEY,device TEXT NOT NULL,command TEXT NOT NULL,result TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS gate_firmware_evidence(
  case_id TEXT NOT NULL,
  device TEXT NOT NULL,
  gate TEXT NOT NULL,
  cell TEXT NOT NULL,
  event_id INTEGER NOT NULL,
  event_sha256 TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  frozen_version TEXT NOT NULL,
  frozen_revision INTEGER NOT NULL DEFAULT 0,
  target_version TEXT NOT NULL,
  PRIMARY KEY(case_id,device)
);
CREATE TABLE IF NOT EXISTS gate_firmware_run(
  id INTEGER PRIMARY KEY,
  case_id TEXT NOT NULL,
  contract_id TEXT NOT NULL,
  contract_authorized_by TEXT NOT NULL,
  status TEXT NOT NULL,
  outcome TEXT,
  frozen_digest TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '{}',
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  ended_at TEXT,
  UNIQUE(case_id,contract_id)
);
CREATE TABLE IF NOT EXISTS gate_firmware_step(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES gate_firmware_run(id),
  device TEXT NOT NULL,
  phase TEXT NOT NULL,
  ordinal INTEGER NOT NULL,
  command TEXT NOT NULL,
  operation_id TEXT,
  prior_version TEXT,
  target_version TEXT,
  expected_revision INTEGER,
  status TEXT NOT NULL,
  endpoint_response TEXT,
  status_recheck TEXT,
  observed_version TEXT,
  observed_revision INTEGER,
  attempt INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id,phase,device)
);
CREATE TABLE IF NOT EXISTS gate_firmware_manual(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES gate_firmware_run(id),
  device TEXT NOT NULL,
  reason TEXT NOT NULL,
  observed_version TEXT,
  observed_revision INTEGER,
  detail TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  UNIQUE(run_id,device,reason)
);
CREATE TABLE IF NOT EXISTS gate_firmware_observation(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES gate_firmware_run(id),
  device TEXT NOT NULL,
  phase TEXT NOT NULL,
  operation_id TEXT,
  version TEXT NOT NULL,
  revision INTEGER NOT NULL,
  source TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_gate_firmware_run_case ON gate_firmware_run(case_id,contract_id);
CREATE INDEX IF NOT EXISTS idx_gate_firmware_step_run ON gate_firmware_step(run_id);
CREATE INDEX IF NOT EXISTS idx_gate_firmware_observation_run ON gate_firmware_observation(run_id,device);
"""
# Additive migrations for databases created by earlier versions of this tool.
MIGRATIONS={
  'gate_firmware_evidence':{
    'frozen_revision':"INTEGER NOT NULL DEFAULT 0",
    'target_version':"TEXT NOT NULL DEFAULT ''",
  },
  'gate_firmware_run':{
    'contract_authorized_by':"TEXT NOT NULL DEFAULT ''",
    'outcome':"TEXT",
    'frozen_digest':"TEXT NOT NULL DEFAULT ''",
    'detail':"TEXT NOT NULL DEFAULT '{}'",
    'updated_at':"TEXT NOT NULL DEFAULT ''",
  },
  'gate_firmware_step':{
    'endpoint_response':"TEXT",
    'status_recheck':"TEXT",
    'observed_version':"TEXT",
    'observed_revision':"INTEGER",
    'attempt':"INTEGER NOT NULL DEFAULT 0",
    'detail':"TEXT NOT NULL DEFAULT '{}'",
    'updated_at':"TEXT NOT NULL DEFAULT ''",
  },
  'gate_firmware_manual':{
    'detail':"TEXT NOT NULL DEFAULT '{}'",
  },
  'gate_firmware_observation':{
    'note':"TEXT NOT NULL DEFAULT ''",
  },
}
def _migrate(db):
    for table,columns in MIGRATIONS.items():
        existing={row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        if not existing:continue
        for name,ddl in columns.items():
            if name not in existing:db.execute(f'ALTER TABLE {table} ADD COLUMN {name} {ddl}')
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA)
    with db:_migrate(db)
    return db
