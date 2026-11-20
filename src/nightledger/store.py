"""Raw observation evidence and reversible archive decisions."""
import hashlib,json,sqlite3
from pathlib import Path
SCHEMA="""
CREATE TABLE IF NOT EXISTS source(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT,UNIQUE(system,source_id,digest));
CREATE TABLE IF NOT EXISTS source_conflict(id INTEGER PRIMARY KEY,system TEXT,source_id TEXT,digest TEXT,payload TEXT);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,system TEXT,object_id TEXT,status TEXT,reason TEXT,evidence TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS slot_allocation(id TEXT PRIMARY KEY,telescope TEXT,starts INTEGER,ends INTEGER,plan TEXT,priority INTEGER,status TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS instrument_setup(id TEXT PRIMARY KEY,slot_id TEXT,filter TEXT,detector TEXT,temperature REAL,exposure_ms INTEGER,version INTEGER,signed_at INTEGER);
CREATE TABLE IF NOT EXISTS downtime(id INTEGER PRIMARY KEY,telescope TEXT,starts INTEGER,ends INTEGER,source_id TEXT);
CREATE TABLE IF NOT EXISTS calibration_frame(id TEXT PRIMARY KEY,set_id TEXT,kind TEXT,digest TEXT,filter TEXT,detector TEXT,temperature REAL,exposure_ms INTEGER,expires_at INTEGER,healthy INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS calibration_set(id TEXT PRIMARY KEY,slot_id TEXT,dark_id TEXT,flat_id TEXT,status TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS science_frame(id TEXT PRIMARY KEY,slot_id TEXT,at INTEGER,digest TEXT,target TEXT,pointing_arcsec REAL,status TEXT,replaces TEXT);
CREATE TABLE IF NOT EXISTS archive_case(frame_id TEXT PRIMARY KEY,status TEXT,reason TEXT,calibration_version INTEGER,version INTEGER);
CREATE TABLE IF NOT EXISTS archive_event(id INTEGER PRIMARY KEY,frame_id TEXT,status TEXT,reason TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS photometry_session(id TEXT PRIMARY KEY,slot_id TEXT,target TEXT,comparison TEXT,drop_fraction REAL,status TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS photometry_measurement(frame_id TEXT PRIMARY KEY,session_id TEXT,target_aperture REAL,target_sky REAL,comparison_aperture REAL,comparison_sky REAL,pixels INTEGER,ratio REAL,status TEXT);
CREATE TABLE IF NOT EXISTS photometry_event(id INTEGER PRIMARY KEY,session_id TEXT,status TEXT,reason TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS photometry_product(session_id TEXT,purpose TEXT,status TEXT,reason TEXT,version INTEGER,PRIMARY KEY(session_id,purpose));
CREATE TABLE IF NOT EXISTS photometry_product_event(id INTEGER PRIMARY KEY,session_id TEXT,purpose TEXT,status TEXT,reason TEXT,previous INTEGER);
CREATE TABLE IF NOT EXISTS photometry_intake(session_id TEXT PRIMARY KEY,source_id TEXT,digest TEXT,kind TEXT);
CREATE TABLE IF NOT EXISTS photometry_comparison(session_id TEXT,star TEXT,catalog_mag REAL,catalog_err REAL,designated INTEGER,status TEXT,reason TEXT,version INTEGER,PRIMARY KEY(session_id,star));
CREATE TABLE IF NOT EXISTS photometry_frame_star(frame_id TEXT,session_id TEXT,star TEXT,aperture REAL,sky REAL,pixels INTEGER,missing INTEGER,saturated INTEGER,flux REAL,status TEXT,reason TEXT,PRIMARY KEY(frame_id,star));
CREATE TABLE IF NOT EXISTS photometry_frame_result(frame_id TEXT PRIMARY KEY,session_id TEXT,target_missing INTEGER,target_saturated INTEGER,relative_flux REAL,flux_err REAL,refs TEXT,status TEXT,reason TEXT,version INTEGER);
CREATE TABLE IF NOT EXISTS photometry_product_detail(session_id TEXT,purpose TEXT,version INTEGER,detail TEXT,PRIMARY KEY(session_id,purpose));
"""
def connect(path):
    db=sqlite3.connect(path);db.row_factory=sqlite3.Row;db.executescript(SCHEMA);return db
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def digest_of(data):
    raw=json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(raw.encode()).hexdigest()
def ingest(db,system,data):
    raw=json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':'))
    digest=digest_of(data);sid=data['source_id']
    old=db.execute('SELECT digest FROM source WHERE system=? AND source_id=?',(system,sid)).fetchall()
    if any(r['digest']!=digest for r in old):
        db.execute('INSERT INTO source_conflict(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw))
        decide(db,system,sid,'conflict','same source ID changed bytes',[digest]);return 'conflict'
    if old:return 'duplicate'
    db.execute('INSERT INTO source(system,source_id,digest,payload) VALUES(?,?,?,?)',(system,sid,digest,raw));return 'new'
def decide(db,system,key,status,reason,evidence):
    ev=json.dumps(evidence,sort_keys=True,ensure_ascii=False)
    old=db.execute('SELECT * FROM decision WHERE system=? AND object_id=? ORDER BY id DESC LIMIT 1',(system,key)).fetchone()
    if old and (old['status'],old['reason'],old['evidence'])==(status,reason,ev):return old['id']
    cur=db.execute('INSERT INTO decision(system,object_id,status,reason,evidence,previous) VALUES(?,?,?,?,?,?)',
                   (system,key,status,reason,ev,old['id'] if old else None));return cur.lastrowid
