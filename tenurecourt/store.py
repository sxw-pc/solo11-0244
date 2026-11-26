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
CREATE TABLE IF NOT EXISTS liability_receipts(id TEXT PRIMARY KEY,liability TEXT,team TEXT,actor TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS acceptances(id TEXT PRIMARY KEY,transfer TEXT,team TEXT,actor TEXT,role TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS escrows(id TEXT PRIMARY KEY,transfer TEXT,until_ts INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS resources(id TEXT PRIMARY KEY,namespace TEXT,fingerprint TEXT,generation INTEGER,owner TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS orphans(id TEXT PRIMARY KEY,resource TEXT,namespace TEXT,status TEXT,proof TEXT);
CREATE TABLE IF NOT EXISTS ownership_proofs(id TEXT PRIMARY KEY,resource TEXT,team TEXT,generation INTEGER,issuer TEXT,valid_until INTEGER);
CREATE TABLE IF NOT EXISTS finalizers(id TEXT PRIMARY KEY,namespace TEXT,resource TEXT,status TEXT);
CREATE TABLE IF NOT EXISTS deletions(id TEXT PRIMARY KEY,resource TEXT,generation INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS closures(id TEXT PRIMARY KEY,namespace TEXT,uid TEXT,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS duty_seats(id TEXT PRIMARY KEY,team TEXT,person TEXT,channel TEXT,address TEXT,backup_person TEXT,backup_channel TEXT,backup_address TEXT,valid_from INTEGER,valid_until INTEGER,status TEXT,successor TEXT);
CREATE TABLE IF NOT EXISTS duty_scopes(seat TEXT,namespace TEXT,resource TEXT,PRIMARY KEY(seat,namespace,resource));
CREATE TABLE IF NOT EXISTS incidents(id TEXT PRIMARY KEY,namespace TEXT,resource TEXT,opened_at INTEGER,status TEXT,owner_team TEXT,eta INTEGER,response_path TEXT);
CREATE TABLE IF NOT EXISTS notifications(id TEXT PRIMARY KEY,incident TEXT,seat TEXT,team TEXT,person TEXT,stage TEXT,channel TEXT,address TEXT,address_stale INTEGER,receipt TEXT,corr TEXT,at INTEGER,out_of_order INTEGER,corrects TEXT,status TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_corr ON notifications(corr) WHERE corr IS NOT NULL;
CREATE TABLE IF NOT EXISTS customer_messages(id TEXT PRIMARY KEY,incident TEXT,kind TEXT,team TEXT,eta INTEGER,body TEXT,at INTEGER,corrects TEXT);
CREATE TABLE IF NOT EXISTS diagnoses(id TEXT PRIMARY KEY,incident TEXT,seat TEXT,team TEXT,actor TEXT,at INTEGER,findings TEXT);
CREATE TABLE IF NOT EXISTS emergency_grants(id TEXT PRIMARY KEY,incident TEXT,namespace TEXT,resource TEXT,responder TEXT,commands TEXT,issued_at INTEGER,valid_until INTEGER,status TEXT,evidence TEXT);
CREATE TABLE IF NOT EXISTS repairs(id TEXT PRIMARY KEY,incident TEXT,namespace TEXT,resource TEXT,team TEXT,actor TEXT,command TEXT,authority TEXT,lease_generation INTEGER,grant_id TEXT,at INTEGER);
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
 from . import tenure,transfer,cleanup,incident_response
 fn={'tenure':tenure.run,'transfer':transfer.run,'cleanup':cleanup.run,'incident':incident_response.run}[subsystem]
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
