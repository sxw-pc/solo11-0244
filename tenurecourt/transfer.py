from .store import one,rows
from .tenure import active_lease,frozen

def run(db,x):
 op=x['op']
 if op=='propose':
  lease=active_lease(db,x['namespace'],x['at'])
  if not lease or lease['holder']!=x['from_team'] or frozen(db,x['namespace']) or x['deadline']<=x['at']:
   return {'decision':'transfer_not_eligible'}
  if one(db,"SELECT id FROM transfers WHERE namespace=? AND status IN ('proposed','escrow')",(x['namespace'],)):
   return {'decision':'competing_transfer'}
  db.execute('INSERT INTO transfers VALUES(?,?,?,?,?,?,?)',(x['id'],x['namespace'],x['from_team'],x['to_team'],lease['generation'],'proposed',x['deadline']))
  assets=rows(db,"SELECT id,generation FROM resources WHERE namespace=? AND status='active'",(x['namespace'],))
  db.executemany('INSERT INTO transfer_assets VALUES(?,?,?)',[(x['id'],a['id'],a['generation']) for a in assets])
  return {'decision':'proposed','assets':[a['id'] for a in assets]}
 if op=='liability':
  transfer=one(db,'SELECT * FROM transfers WHERE id=?',(x['transfer'],))
  if not transfer or transfer['status']!='proposed' or x['amount']<0:return {'decision':'invalid_liability'}
  db.execute('INSERT INTO liabilities VALUES(?,?,?,?,?)',(x['id'],x['transfer'],x['kind'],x['amount'],'pending'))
  return {'decision':'recorded'}
 if op=='accept_liability':
  item=one(db,'SELECT * FROM liabilities WHERE id=?',(x['id'],))
  if not item:return {'decision':'unknown_liability'}
  t=one(db,'SELECT * FROM transfers WHERE id=?',(item['transfer'],))
  acceptance=one(db,'SELECT * FROM acceptances WHERE transfer=? AND team=? AND actor=?',(t['id'],t['to_team'],x.get('actor')))
  if x.get('team')!=t['to_team'] or not acceptance or x['at']>t['deadline']:
   return {'decision':'unauthorized_liability_acceptance'}
  db.execute('INSERT INTO liability_receipts VALUES(?,?,?,?,?)',(x['receipt'],item['id'],x['team'],x['actor'],x['at']))
  db.execute("UPDATE liabilities SET status='accepted' WHERE id=?",(x['id'],))
  return {'decision':'accepted'}
 if op=='accept':
  t=one(db,'SELECT * FROM transfers WHERE id=?',(x['id'],))
  if not t or t['status']!='proposed' or x['at']>t['deadline'] or x['team'] not in (t['from_team'],t['to_team'],'platform'):
   return {'decision':'acceptance_invalid'}
  expected_role='platform' if x['team']=='platform' else 'outgoing' if x['team']==t['from_team'] else 'incoming'
  if x['role']!=expected_role:return {'decision':'role_mismatch'}
  old=one(db,'SELECT * FROM acceptances WHERE transfer=? AND team=?',(x['id'],x['team']))
  if old:return {'decision':'reused' if old['actor']==x['actor'] else 'team_acceptance_conflict'}
  db.execute('INSERT INTO acceptances VALUES(?,?,?,?,?,?)',(x['receipt'],x['id'],x['team'],x['actor'],x['role'],x['at']))
  return {'decision':'accepted'}
 if op=='escrow':
  t=one(db,'SELECT * FROM transfers WHERE id=?',(x['id'],))
  if not t or t['status']!='proposed' or x['at']>t['deadline']:return {'decision':'not_escrowable'}
  a=rows(db,'SELECT * FROM acceptances WHERE transfer=?',(x['id'],))
  if {t['from_team'],t['to_team'],'platform'}-{v['team'] for v in a} or len({v['actor'] for v in a})<3:
   return {'decision':'missing_parties'}
  kinds={l['kind'] for l in rows(db,"SELECT * FROM liabilities WHERE transfer=? AND status='accepted'",(x['id'],))}
  if not {'billing','alert'}.issubset(kinds) or one(db,"SELECT id FROM liabilities WHERE transfer=? AND status!='accepted'",(x['id'],)):
   return {'decision':'liability_unaccepted'}
  db.execute('INSERT INTO escrows VALUES(?,?,?,?)',(x['id'],x['id'],t['deadline'],'active'))
  db.execute("UPDATE transfers SET status='escrow' WHERE id=?",(x['id'],))
  return {'decision':'escrowed'}
 if op=='complete':
  t=one(db,"SELECT * FROM transfers WHERE id=? AND status='escrow'",(x['id'],))
  if not t or x['at']>t['deadline']:return {'decision':'escrow_expired'}
  lease=active_lease(db,t['namespace'],x['at'])
  if not lease or lease['generation']!=t['lease_generation'] or frozen(db,t['namespace']):return {'decision':'tenure_changed'}
  snapshot={a['resource']:a['generation'] for a in rows(db,'SELECT * FROM transfer_assets WHERE transfer=?',(x['id'],))}
  assets=rows(db,"SELECT * FROM resources WHERE namespace=? AND status='active'",(t['namespace'],))
  live={a['id']:a['generation'] for a in assets}
  if snapshot!=live or any(a['owner']!=t['from_team'] for a in assets) or one(db,"SELECT id FROM orphans WHERE namespace=? AND status='quarantined'",(t['namespace'],)) or one(db,"SELECT id FROM reappearance_cases WHERE namespace=? AND status IN ('open','verified')",(t['namespace'],)):
   return {'decision':'inventory_changed'}
  db.execute('UPDATE leases SET holder=?,generation=generation+1 WHERE id=?',(t['to_team'],lease['id']))
  db.execute("UPDATE approvals SET status='stale' WHERE lease=? AND status='active'",(lease['id'],))
  db.execute("UPDATE resources SET owner=? WHERE namespace=? AND status='active'",(t['to_team'],t['namespace']))
  db.execute("UPDATE transfers SET status='complete' WHERE id=?",(x['id'],))
  db.execute("UPDATE escrows SET status='closed' WHERE id=?",(x['id'],))
  return {'decision':'transferred','new_holder':t['to_team']}
 if op=='cancel':
  t=one(db,'SELECT * FROM transfers WHERE id=?',(x['id'],))
  if not t or t['status'] not in ('proposed','escrow'):return {'decision':'not_cancellable'}
  db.execute("UPDATE transfers SET status='cancelled' WHERE id=?",(x['id'],))
  db.execute("UPDATE escrows SET status='cancelled' WHERE transfer=?",(x['id'],))
  return {'decision':'cancelled'}
 raise ValueError(op)
