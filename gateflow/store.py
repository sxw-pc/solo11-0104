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
CREATE TABLE IF NOT EXISTS venue_procurement_case(id TEXT PRIMARY KEY,payload TEXT NOT NULL,digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchase_plan(id TEXT PRIMARY KEY,case_id TEXT NOT NULL UNIQUE,case_digest TEXT NOT NULL,quote_sha256 TEXT NOT NULL,status TEXT NOT NULL CHECK(status IN ('可签发','待核')),vendor_count INTEGER NOT NULL,sku_count INTEGER NOT NULL,proof TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS purchase_contract(id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES purchase_plan(id),case_id TEXT NOT NULL,objective TEXT NOT NULL CHECK(objective IN ('min_total_cost','min_max_lateness')),status TEXT NOT NULL CHECK(status IN ('有效','取消')),total_cents INTEGER NOT NULL,max_lateness INTEGER NOT NULL,proof TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(case_id,objective));
CREATE TABLE IF NOT EXISTS purchase_order_line(id INTEGER PRIMARY KEY,contract_id TEXT NOT NULL REFERENCES purchase_contract(id),vendor TEXT NOT NULL,sku TEXT NOT NULL,qty INTEGER NOT NULL CHECK(qty>0),unit_cents INTEGER NOT NULL CHECK(unit_cents>0),line_cents INTEGER NOT NULL CHECK(line_cents>0),arrival_day INTEGER NOT NULL,available_qty INTEGER NOT NULL,UNIQUE(contract_id,vendor,sku));
CREATE TABLE IF NOT EXISTS purchase_vendor_total(contract_id TEXT NOT NULL REFERENCES purchase_contract(id),vendor TEXT NOT NULL,total_cents INTEGER NOT NULL,min_order_cents INTEGER NOT NULL,moq_satisfied INTEGER NOT NULL CHECK(moq_satisfied IN (0,1)),promised_arrival_day INTEGER NOT NULL,PRIMARY KEY(contract_id,vendor));
CREATE TABLE IF NOT EXISTS purchase_requirement(contract_id TEXT NOT NULL REFERENCES purchase_contract(id),zone TEXT NOT NULL,sku TEXT NOT NULL,qty INTEGER NOT NULL,deadline_day INTEGER NOT NULL,effective_arrival_day INTEGER NOT NULL,lateness INTEGER NOT NULL,covered INTEGER NOT NULL CHECK(covered IN (0,1)),PRIMARY KEY(contract_id,zone,sku));
CREATE TABLE IF NOT EXISTS contract_supersession(id INTEGER PRIMARY KEY,old_contract_id TEXT NOT NULL REFERENCES purchase_contract(id),new_contract_id TEXT NOT NULL REFERENCES purchase_contract(id),relation TEXT NOT NULL CHECK(relation IN ('替代','补购','取消')),delta_cents INTEGER NOT NULL,note TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run(id INTEGER PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('待接收','已校验','待核','已判定','作废')), started_at TEXT NOT NULL, ended_at TEXT);
"""
def connect(path):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
