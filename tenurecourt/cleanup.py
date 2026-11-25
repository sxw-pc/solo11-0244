from .store import one,rows
from .tenure import active_lease,frozen

def run(db,x):
 op=x['op']
 if op=='resource':
  n=one(db,'SELECT * FROM namespaces WHERE id=?',(x['namespace'],))
  if not n or n['status']!='open':return {'decision':'namespace_unavailable'}
  lease=active_lease(db,x['namespace'],x['at'])
  if not lease or frozen(db,x['namespace']) or lease['holder']!=x['owner']:
   return {'decision':'owner_without_tenure'}
  old=one(db,'SELECT * FROM resources WHERE id=?',(x['id'],))
  if old:return {'decision':'reused' if old['fingerprint']==x['fingerprint'] and old['generation']==x['generation'] else 'resource_conflict'}
  db.execute('INSERT INTO resources VALUES(?,?,?,?,?,?)',(x['id'],x['namespace'],x['fingerprint'],x['generation'],x['owner'],'active'))
  return {'decision':'registered'}
 if op=='discover':
  resource=one(db,'SELECT * FROM resources WHERE id=?',(x['resource'],))
  if not resource or resource['namespace']!=x['namespace']:return {'decision':'unknown_resource'}
  db.execute('INSERT INTO orphans VALUES(?,?,?,?,?)',(x['id'],x['resource'],x['namespace'],'quarantined',None))
  return {'decision':'quarantined'}
 if op=='claim':
  o=one(db,"SELECT * FROM orphans WHERE id=? AND status='quarantined'",(x['id'],))
  r=one(db,'SELECT * FROM resources WHERE id=?',(o['resource'],)) if o else None
  if not r or x['proof']!=r['fingerprint'] or not x['owner']:return {'decision':'invalid_ownership_proof'}
  db.execute("UPDATE orphans SET status='claimed',proof=? WHERE id=?",(x['proof'],x['id']))
  db.execute('UPDATE resources SET owner=? WHERE id=?',(x['owner'],r['id']))
  return {'decision':'claimed'}
 if op=='finalizer':
  r=one(db,'SELECT * FROM resources WHERE id=?',(x['resource'],))
  if not r:return {'decision':'unknown_resource'}
  db.execute('INSERT INTO finalizers VALUES(?,?,?,?)',(x['id'],r['namespace'],r['id'],'open'))
  return {'decision':'registered'}
 if op=='release_finalizer':
  f=one(db,'SELECT * FROM finalizers WHERE id=?',(x['id'],))
  if not f or f['status']!='open':return {'decision':'not_open'}
  db.execute("UPDATE finalizers SET status='released' WHERE id=?",(x['id'],))
  return {'decision':'released'}
 if op=='delete':
  r=one(db,'SELECT * FROM resources WHERE id=?',(x['resource'],))
  if not r or r['generation']!=x['generation'] or r['status']!='active':return {'decision':'stale_resource'}
  if one(db,"SELECT id FROM finalizers WHERE resource=? AND status='open'",(r['id'],)):
   return {'decision':'finalizer_pending'}
  old=one(db,'SELECT * FROM deletions WHERE id=?',(x['id'],))
  if old:return {'decision':'reused' if old['resource']==r['id'] else 'deletion_conflict'}
  db.execute('INSERT INTO deletions VALUES(?,?,?,?,?)',(x['id'],r['id'],r['generation'],x['at'],'confirmed'))
  db.execute("UPDATE resources SET status='deleted' WHERE id=?",(r['id'],))
  return {'decision':'deleted'}
 if op=='close':
  n=one(db,'SELECT * FROM namespaces WHERE id=?',(x['namespace'],))
  if not n or n['uid']!=x['uid']:return {'decision':'namespace_identity_conflict'}
  lease=active_lease(db,x['namespace'],x['at'])
  if lease:return {'decision':'tenure_still_active'}
  if one(db,"SELECT id FROM transfers WHERE namespace=? AND status IN ('proposed','escrow')",(x['namespace'],)):
   return {'decision':'transfer_still_open'}
  pending=rows(db,"SELECT id FROM resources WHERE namespace=? AND status='active'",(x['namespace'],))
  orphans=rows(db,"SELECT id FROM orphans WHERE namespace=? AND status='quarantined'",(x['namespace'],))
  finals=rows(db,"SELECT id FROM finalizers WHERE namespace=? AND status='open'",(x['namespace'],))
  if pending or orphans or finals:return {'decision':'cleanup_incomplete','resources':[r['id'] for r in pending],'orphans':[o['id'] for o in orphans],'finalizers':[f['id'] for f in finals]}
  db.execute('INSERT INTO closures VALUES(?,?,?,?,?)',(x['id'],x['namespace'],x['uid'],x['at'],'closed'))
  db.execute("UPDATE namespaces SET status='closed' WHERE id=?",(x['namespace'],))
  db.execute("UPDATE leases SET status='expired' WHERE namespace=? AND status='active'",(x['namespace'],))
  return {'decision':'closed'}
 raise ValueError(op)
