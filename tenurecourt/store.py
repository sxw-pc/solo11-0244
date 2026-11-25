import json,sqlite3
from contextlib import contextmanager
SCHEMA="""
CREATE TABLE IF NOT EXISTS namespaces(id TEXT PRIMARY KEY,uid TEXT,generation INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS leases(id TEXT PRIMARY KEY,namespace TEXT,holder TEXT,generation INTEGER,start INTEGER,expires INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,lease TEXT,actor TEXT,role TEXT,until_ts INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS freezes(id TEXT PRIMARY KEY,namespace TEXT,generation INTEGER,reason TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS writes(id TEXT PRIMARY KEY,namespace TEXT,lease_generation INTEGER,resource TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,namespace TEXT,from_team TEXT,to_team TEXT,lease_generation INTEGER,status TEXT,deadline INTEGER);
CREATE TABLE IF NOT EXISTS transfer_assets(transfer TEXT,resource TEXT,generation INTEGER,PRIMARY KEY(transfer,resource));
CREATE TABLE IF NOT EXISTS liabilities(id TEXT PRIMARY KEY,transfer TEXT,kind TEXT,amount INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS acceptances(id TEXT PRIMARY KEY,transfer TEXT,team TEXT,actor TEXT,role TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS escrows(id TEXT PRIMARY KEY,transfer TEXT,until_ts INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,namespace TEXT,fingerprint TEXT,generation INTEGER,owner TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS orphans(id TEXT PRIMARY KEY,resource TEXT,namespace TEXT,status TEXT,proof TEXT);
CREATE TABLE IF NOT EXISTS finalizers(id TEXT PRIMARY KEY,namespace TEXT,resource TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS deletions(id TEXT PRIMARY KEY,resource TEXT,generation INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS closures(id TEXT PRIMARY KEY,namespace TEXT,uid TEXT,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,subsystem TEXT,payload TEXT,result TEXT);
"""
def connect(path):
 db=sqlite3.connect(path,isolation_level=None);db.row_factory=sqlite3.Row
 db.execute('PRAGMA busy_timeout=3000');db.executescript(SCHEMA);return db
@contextmanager
def tx(db):
 db.execute('BEGIN IMMEDIATE')
 try:
  yield
  db.execute('COMMIT')
 except BaseException:
  db.execute('ROLLBACK');raise
def one(db,q,a=()):
 x=db.execute(q,a).fetchone();return dict(x) if x else None
def rows(db,q,a=()):return [dict(x) for x in db.execute(q,a)]
def execute(db,subsystem,payload):
 from . import tenure,transfer,cleanup
 fn={'tenure':tenure.run,'transfer':transfer.run,'cleanup':cleanup.run}[subsystem]
 with tx(db):
  result=fn(db,payload)
  db.execute('INSERT INTO events(subsystem,payload,result) VALUES(?,?,?)',(subsystem,json.dumps(payload,sort_keys=True,ensure_ascii=False),json.dumps(result,sort_keys=True,ensure_ascii=False)))
 return result
def replay(path):
 source=connect(path);target=connect(':memory:');errors=[]
 for i,e in enumerate(rows(source,'SELECT * FROM events ORDER BY seq'),1):
  if execute(target,e['subsystem'],json.loads(e['payload']))!=json.loads(e['result']):errors.append({'step':i})
 names=[x[0] for x in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('events','sqlite_sequence') ORDER BY name")]
 for name in names:
  if [tuple(x) for x in source.execute(f'SELECT * FROM {name} ORDER BY rowid')]!=[tuple(x) for x in target.execute(f'SELECT * FROM {name} ORDER BY rowid')]:errors.append({'table':name})
 count=source.execute('SELECT COUNT(*) FROM events').fetchone()[0]
 source.close();target.close();return {'verified':not errors,'decisions':count,'errors':errors}
