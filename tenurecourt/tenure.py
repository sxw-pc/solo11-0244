from .store import one,rows

def active_lease(db,namespace,at):
 return one(db,"SELECT * FROM leases WHERE namespace=? AND status='active' AND start<=? AND expires>=? ORDER BY generation DESC LIMIT 1",(namespace,at,at))
def frozen(db,namespace):return bool(one(db,"SELECT id FROM freezes WHERE namespace=? AND status='active'",(namespace,)))
def run(db,x):
 op=x['op']
 if op=='namespace':
  old=one(db,'SELECT * FROM namespaces WHERE id=?',(x['id'],))
  if old and x['generation']<=old['generation']:return {'decision':'stale_namespace'}
  db.execute('INSERT INTO namespaces VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET uid=excluded.uid,generation=excluded.generation,status=excluded.status',(x['id'],x['uid'],x['generation'],'open'))
  if old and old['uid']!=x['uid']:
   db.execute("UPDATE leases SET status='stale' WHERE namespace=? AND status='active'",(x['id'],))
  return {'decision':'registered'}
 if op=='lease':
  n=one(db,'SELECT * FROM namespaces WHERE id=?',(x['namespace'],))
  if not n or n['uid']!=x['namespace_uid'] or frozen(db,x['namespace']) or x['expires']<=x['start']:return {'decision':'invalid_tenure'}
  current=one(db,'SELECT MAX(generation) AS g FROM leases WHERE namespace=?',(x['namespace'],))
  if x['generation']<=(current['g'] or 0):return {'decision':'stale_tenure'}
  existing=one(db,"SELECT * FROM leases WHERE namespace=? AND status='active'",(x['namespace'],))
  if existing and existing['holder']!=x['holder']:
   return {'decision':'holder_takeover_denied'}
  db.execute("UPDATE leases SET status='superseded' WHERE namespace=? AND status='active'",(x['namespace'],))
  db.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,?)',(x['id'],x['namespace'],x['holder'],x['generation'],x['start'],x['expires'],'active'))
  return {'decision':'active'}
 if op=='approval':
  lease=one(db,'SELECT * FROM leases WHERE id=?',(x['lease'],))
  if not lease:return {'decision':'unknown_lease'}
  db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)',(x['id'],x['lease'],x['actor'],x['role'],x['until'],'active'))
  return {'decision':'recorded'}
 if op=='renew':
  lease=one(db,'SELECT * FROM leases WHERE id=?',(x['id'],))
  if not lease or lease['status']!='active' or frozen(db,lease['namespace']):return {'decision':'not_renewable'}
  if x['generation']!=lease['generation']+1 or x['at']>lease['expires'] or x['expires']<=lease['expires']:return {'decision':'stale_or_invalid_renewal'}
  approvals=rows(db,"SELECT * FROM approvals WHERE lease=? AND status='active' AND until_ts>=?",(x['id'],x['at']))
  if {'holder','budget'}-{a['role'] for a in approvals} or len({a['actor'] for a in approvals})<2:return {'decision':'missing_two_approvals'}
  db.execute('UPDATE leases SET generation=?,expires=? WHERE id=?',(x['generation'],x['expires'],x['id']))
  return {'decision':'renewed'}
 if op=='freeze':
  n=one(db,'SELECT * FROM namespaces WHERE id=?',(x['namespace'],))
  if not n:return {'decision':'unknown_namespace'}
  latest=one(db,'SELECT MAX(generation) AS g FROM freezes WHERE namespace=?',(x['namespace'],))
  if x['generation']<=(latest['g'] or 0):return {'decision':'stale_freeze'}
  db.execute('INSERT INTO freezes VALUES(?,?,?,?,?)',(x['id'],x['namespace'],x['generation'],x['reason'],'active'))
  return {'decision':'frozen'}
 if op=='unfreeze':
  f=one(db,"SELECT * FROM freezes WHERE id=? AND status='active'",(x['id'],))
  if not f or x['generation']<=f['generation']:return {'decision':'stale_unfreeze'}
  db.execute("UPDATE freezes SET status='released' WHERE id=?",(x['id'],))
  return {'decision':'active'}
 if op=='write':
  lease=active_lease(db,x['namespace'],x['at'])
  if not lease or frozen(db,x['namespace']) or lease['holder']!=x['team'] or lease['generation']!=x['generation']:
   return {'decision':'write_denied'}
  r=one(db,'SELECT status FROM resources WHERE id=?',(x['resource'],))
  if r and r['status']!='active':return {'decision':'write_denied'}
  if one(db,"SELECT id FROM reappearance_cases WHERE resource=? AND status IN ('open','verified')",(x['resource'],)):
   return {'decision':'write_denied'}
  old=one(db,'SELECT * FROM writes WHERE id=?',(x['id'],))
  if old:return {'decision':'reused' if old['resource']==x['resource'] else 'write_conflict'}
  db.execute('INSERT INTO writes VALUES(?,?,?,?,?)',(x['id'],x['namespace'],lease['generation'],x['resource'],x['at']))
  return {'decision':'written'}
 raise ValueError(op)
