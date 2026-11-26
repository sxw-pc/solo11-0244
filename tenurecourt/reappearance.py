"""Identity and disposition flow for resources sighted again after deletion.

A confirmed deletion receipt is never overturned by a higher generation: a later
observation is recorded only as a sighting bound to a reappearance case that pins
the retired physical UID, content digest, storage location, the deletion receipt
and the observation source.  Ownership is proven by an independent storage
readback of the pinned UID, and the case is resolved through a customer/platform
pre-authorized disposition: restore the verified original entity to its team with
a restricted lease, or isolate the name-reusing object under a fresh UID while
preserving the trusted deletion history.  Every op is idempotent so a retried
step after a power failure replays to the same decision.
"""
from .store import one,rows
from .tenure import frozen

OPEN=('open','verified')

def _case(db,cid):return one(db,'SELECT * FROM reappearance_cases WHERE id=?',(cid,))

def pin(db,x):
 old=one(db,'SELECT * FROM identities WHERE id=?',(x['id'],))
 if old:
  same=all(old[k]==x.get(k) for k in('resource','uid','digest','location','source'))and old['generation']==x.get('generation')and old['at']==x.get('at')
  return {'decision':'reused' if same else 'pin_conflict'}
 if not x.get('uid'):return {'decision':'invalid_pin'}
 if not one(db,'SELECT id FROM resources WHERE id=?',(x['resource'],)):return {'decision':'unknown_resource'}
 holders=rows(db,'SELECT * FROM identities WHERE uid=?',(x['uid'],))
 if any(h['resource']!=x['resource'] or h['digest']!=x['digest'] for h in holders):return {'decision':'uid_conflict'}
 if holders and one(db,"SELECT id FROM deletions WHERE resource=? AND status='confirmed' AND at>=?",(x['resource'],min(h['at'] for h in holders))):
  return {'decision':'uid_retired'}
 if holders:return {'decision':'reused'}
 db.execute('INSERT INTO identities VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['resource'],x['uid'],x['digest'],x.get('location'),x.get('generation'),x.get('source'),x['at']))
 return {'decision':'pinned'}

def sighting(db,x):
 old=one(db,'SELECT * FROM sightings WHERE id=?',(x['id'],))
 if old:
  same=all(old[k]==x.get(k) for k in('resource','generation','digest','uid','location','source'))and old['at']==x.get('at')
  return {'decision':'reused' if same else 'sighting_conflict','case':old['case_id']}
 r=one(db,'SELECT * FROM resources WHERE id=?',(x['resource'],))
 if not r:return {'decision':'unknown_resource'}
 n=one(db,'SELECT status FROM namespaces WHERE id=?',(r['namespace'],))
 if n and n['status']=='closed':return {'decision':'namespace_closed'}
 deletion=one(db,"SELECT * FROM deletions WHERE resource=? AND status='confirmed' ORDER BY at DESC,rowid DESC LIMIT 1",(r['id'],))
 if r['status']!='deleted' or not deletion:return {'decision':'not_deleted'}
 cid=f"reappearance-{r['id']}-{deletion['id']}"
 case=_case(db,cid)
 if case and case['status'] not in OPEN:
  db.execute('INSERT INTO sightings VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],r['id'],r['namespace'],x.get('generation'),x.get('digest'),x.get('uid'),x.get('location'),x.get('source'),x['at'],cid,'post_resolution'))
  return {'decision':'case_closed','case':cid}
 if not case:
  idn=one(db,'SELECT * FROM identities WHERE resource=? AND at<=? ORDER BY at DESC,rowid DESC LIMIT 1',(r['id'],deletion['at']))or one(db,'SELECT * FROM identities WHERE resource=? ORDER BY at DESC,rowid DESC LIMIT 1',(r['id'],))
  db.execute('INSERT INTO reappearance_cases VALUES(?,?,?,?,?,?,?,?,?,?,?)',(cid,r['id'],r['namespace'],deletion['id'],idn['uid'] if idn else None,idn['digest'] if idn else None,idn['location'] if idn else None,'open',None,None,None))
  case=_case(db,cid);decision='case_opened'
 else:decision='case_attached'
 db.execute('INSERT INTO sightings VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],r['id'],r['namespace'],x.get('generation'),x.get('digest'),x.get('uid'),x.get('location'),x.get('source'),x['at'],cid,'recorded'))
 return {'decision':decision,'case':cid,'old_uid':case['old_uid'],'deletion':case['deletion']}

def readback(db,x):
 old=one(db,'SELECT * FROM readbacks WHERE id=?',(x['id'],))
 if old:
  same=old['case_id']==x['case']and all(old[k]==x.get(k) for k in('uid','location','digest','rollback_source','endpoint'))and old['pages_expected']==x.get('pages_expected')and old['pages_present']==x.get('pages_present')and old['at']==x.get('at')
  return {'decision':'reused' if same else 'readback_conflict'}
 case=_case(db,x['case'])
 if not case:return {'decision':'unknown_case'}
 if case['status'] not in OPEN:return {'decision':'case_closed'}
 if not case['old_uid'] or x.get('uid')!=case['old_uid'] or x.get('location')!=case['old_location']:
  status,decision='mismatch','identity_mismatch'
 elif not x.get('rollback_source') or not x.get('endpoint'):
  status,decision='invalid','invalid_readback'
 elif x['pages_present']!=x['pages_expected']:
  status,decision='incomplete','readback_incomplete'
 elif x.get('digest')!=case['old_digest']:
  status,decision='polluted','content_mismatch'
 else:status,decision='complete','verified'
 db.execute('INSERT INTO readbacks VALUES(?,?,?,?,?,?,?,?,?,?,?)',(x['id'],x['case'],x.get('uid'),x.get('location'),x['pages_expected'],x['pages_present'],x.get('digest'),x.get('rollback_source'),x.get('endpoint'),x['at'],status))
 if status=='complete':db.execute("UPDATE reappearance_cases SET status='verified' WHERE id=?",(x['case'],))
 return {'decision':decision}

def authorize(db,x):
 old=one(db,'SELECT * FROM dispositions WHERE id=?',(x['id'],))
 if old:
  same=all(old[k]==x.get(k) for k in('namespace','resource','kind','customer','platform'))and old['valid_until']==x.get('valid_until')
  return {'decision':'reused' if same else 'authorization_conflict'}
 if x['kind'] not in('restore','isolate'):return {'decision':'invalid_disposition'}
 if not one(db,'SELECT id FROM namespaces WHERE id=?',(x['namespace'],)):return {'decision':'unknown_namespace'}
 if not x.get('customer') or not x.get('platform'):return {'decision':'missing_party'}
 if x['valid_until']<x['at']:return {'decision':'expired_authorization'}
 db.execute('INSERT INTO dispositions VALUES(?,?,?,?,?,?,?,?)',(x['id'],x['namespace'],x['resource'],x['kind'],x['customer'],x['platform'],x['valid_until'],x['at']))
 return {'decision':'authorized'}

def signoff(db,x):
 decision='accepted' if x.get('accept') else 'refused'
 old=one(db,'SELECT * FROM signoffs WHERE id=?',(x['id'],))
 if old:
  same=old['case_id']==x['case']and old['team']==x['team']and old['actor']==x['actor']and old['uid']==x.get('uid')and old['decision']==decision
  return {'decision':'reused' if same else 'signoff_conflict'}
 case=_case(db,x['case'])
 if not case:return {'decision':'unknown_case'}
 if case['status'] not in OPEN:return {'decision':'case_closed'}
 r=one(db,'SELECT owner FROM resources WHERE id=?',(case['resource'],))
 if x['team']!=r['owner']:return {'decision':'not_responsible_team'}
 if x.get('uid')!=case['old_uid']:return {'decision':'entity_mismatch'}
 if one(db,'SELECT id FROM signoffs WHERE case_id=? AND team=?',(x['case'],x['team'])):return {'decision':'already_signed'}
 db.execute('INSERT INTO signoffs VALUES(?,?,?,?,?,?,?)',(x['id'],x['case'],x['team'],x['actor'],x.get('uid'),decision,x['at']))
 return {'decision':decision}

def restore(db,x):
 old=one(db,'SELECT * FROM resolutions WHERE id=?',(x['id'],))
 if old:return {'decision':'reused' if old['case_id']==x['case']and old['kind']=='restore' else 'restore_conflict'}
 case=_case(db,x['case'])
 if not case:return {'decision':'unknown_case'}
 if case['status'] not in OPEN:return {'decision':'case_closed'}
 if case['status']!='verified':return {'decision':'unverified'}
 if not frozen(db,case['namespace']):return {'decision':'namespace_not_frozen'}
 auth=one(db,"SELECT * FROM dispositions WHERE namespace=? AND resource=? AND kind='restore' AND valid_until>=? ORDER BY valid_until DESC LIMIT 1",(case['namespace'],case['resource'],x['at']))
 if not auth:return {'decision':'unauthorized_disposition'}
 if one(db,"SELECT id FROM signoffs WHERE case_id=? AND decision='refused'",(x['case'],)):return {'decision':'team_refused'}
 r=one(db,'SELECT * FROM resources WHERE id=?',(case['resource'],))
 if not one(db,"SELECT id FROM signoffs WHERE case_id=? AND team=? AND uid=? AND decision='accepted'",(x['case'],r['owner'],case['old_uid'])):
  return {'decision':'team_not_signed_off'}
 if not x.get('lease')or not x.get('expires'):return {'decision':'invalid_lease'}
 if one(db,'SELECT id FROM leases WHERE id=?',(x['lease'],)):return {'decision':'lease_conflict'}
 expires=min(x['expires'],auth['valid_until'])
 if expires<=x['at']:return {'decision':'invalid_lease'}
 g=one(db,'SELECT MAX(generation) AS g FROM leases WHERE namespace=?',(case['namespace'],))['g']or 0
 db.execute("UPDATE leases SET status='superseded' WHERE namespace=? AND status='active'",(case['namespace'],))
 db.execute('INSERT INTO leases VALUES(?,?,?,?,?,?,?)',(x['lease'],case['namespace'],r['owner'],g+1,x['at'],expires,'active'))
 db.execute("UPDATE resources SET status='active' WHERE id=?",(r['id'],))
 db.execute("UPDATE sightings SET status='explained' WHERE case_id=?",(x['case'],))
 db.execute("UPDATE reappearance_cases SET status='restored',disposition=?,lease=?,resolution='restored-original-entity' WHERE id=?",(auth['id'],x['lease'],x['case']))
 db.execute('INSERT INTO resolutions VALUES(?,?,?,?,?,?,?)',(x['id'],x['case'],'restore',case['old_uid'],x['lease'],None,x['at']))
 return {'decision':'restored','uid':case['old_uid'],'lease':x['lease'],'expires':expires}

def isolate(db,x):
 old=one(db,'SELECT * FROM resolutions WHERE id=?',(x['id'],))
 if old:return {'decision':'reused' if old['case_id']==x['case']and old['kind']=='isolate' else 'isolation_conflict'}
 case=_case(db,x['case'])
 if not case:return {'decision':'unknown_case'}
 if case['status'] not in OPEN:return {'decision':'case_closed'}
 auth=one(db,"SELECT * FROM dispositions WHERE namespace=? AND resource=? AND kind='isolate' AND valid_until>=? ORDER BY valid_until DESC LIMIT 1",(case['namespace'],case['resource'],x['at']))
 if not auth:return {'decision':'unauthorized_disposition'}
 if not x.get('new_uid')or not x.get('space'):return {'decision':'invalid_isolation'}
 if x['new_uid']==case['old_uid']or one(db,'SELECT id FROM identities WHERE uid=?',(x['new_uid'],))or one(db,'SELECT id FROM resolutions WHERE uid=?',(x['new_uid'],)):
  return {'decision':'uid_not_fresh'}
 if not one(db,"SELECT id FROM deletions WHERE id=? AND status='confirmed'",(case['deletion'],)):
  return {'decision':'deletion_history_lost'}
 s=one(db,"SELECT * FROM sightings WHERE case_id=? AND status='recorded' ORDER BY at,rowid LIMIT 1",(x['case'],))
 db.execute('INSERT INTO identities VALUES(?,?,?,?,?,?,?,?)',(f"{x['case']}-isolation",case['resource'],x['new_uid'],s['digest'] if s else None,x['space'],s['generation'] if s else None,'isolation',x['at']))
 db.execute("UPDATE sightings SET status='isolated' WHERE case_id=?",(x['case'],))
 db.execute("UPDATE reappearance_cases SET status='isolated',disposition=?,resolution='name-reuse-isolated' WHERE id=?",(auth['id'],x['case']))
 db.execute('INSERT INTO resolutions VALUES(?,?,?,?,?,?,?)',(x['id'],x['case'],'isolate',x['new_uid'],None,x['space'],x['at']))
 return {'decision':'isolated','new_uid':x['new_uid'],'space':x['space'],'deletion':case['deletion']}

def run(db,x):
 op=x['op']
 if op=='pin':return pin(db,x)
 if op=='sighting':return sighting(db,x)
 if op=='readback':return readback(db,x)
 if op=='authorize':return authorize(db,x)
 if op=='signoff':return signoff(db,x)
 if op=='restore':return restore(db,x)
 if op=='isolate':return isolate(db,x)
 raise ValueError(op)
