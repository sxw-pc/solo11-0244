import json
from .store import one, rows
from .tenure import active_lease, frozen

# Carrier/server hand-off, device delivery, human acknowledgement and on-site
# action are four distinct facts that must never collapse into one "notified".
STAGES = ('queued', 'delivered', 'no_answer', 'acknowledged', 'onsite')
# A platform emergency seat may only issue a bounded set of recovery commands;
# it is an independent authorization, never a shortcut around tenure.
RESTRICTED_COMMANDS = ('restart', 'failover', 'quarantine', 'rollback')


def open_incident(db, namespace, resource):
 return one(db, "SELECT * FROM incidents WHERE namespace=? AND resource=? AND status='open'", (namespace, resource))


def active_seat(db, seat_id, at):
 return one(db, "SELECT * FROM duty_seats WHERE id=? AND status='active' AND valid_from<=? AND valid_until>=?", (seat_id, at, at))


def seat_covers(db, seat, namespace, resource):
 if one(db, 'SELECT 1 FROM duty_scopes WHERE seat=? AND namespace=? AND resource=?', (seat['id'], namespace, '*')):
  return True
 return bool(one(db, 'SELECT 1 FROM duty_scopes WHERE seat=? AND namespace=? AND resource=?', (seat['id'], namespace, resource)))


def active_grant(db, incident_id, at):
 return one(db, "SELECT * FROM emergency_grants WHERE incident=? AND status='active' AND valid_until>=?", (incident_id, at))


def add_notification(db, x, stage, at, status, address_stale=0, out_of_order=0, corrects=None):
 db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (x['id'], x['incident'], x.get('seat'), x.get('team'), x.get('person'), stage,
             x.get('channel'), x.get('address'), int(address_stale), x.get('receipt'),
             x.get('corr'), at, int(out_of_order), corrects, status))


def add_customer_message(db, msg_id, incident_id, kind, team, at, eta=None, body=None, corrects=None):
 db.execute('INSERT INTO customer_messages VALUES(?,?,?,?,?,?,?,?)', (msg_id, incident_id, kind, team, eta, body, at, corrects))


def run(db, x):
 op = x['op']
 if op == 'seat':
  if one(db, 'SELECT id FROM duty_seats WHERE id=?', (x['id'],)):
   return {'decision': 'reused'}
  if not x['valid_until'] > x['valid_from']:
   return {'decision': 'invalid_seat_window'}
  db.execute('INSERT INTO duty_seats VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
             (x['id'], x['team'], x['person'], x['channel'], x['address'],
              x['backup_person'], x['backup_channel'], x['backup_address'],
              x['valid_from'], x['valid_until'], 'active', None))
  scopes = x.get('scopes', [])
  db.executemany('INSERT INTO duty_scopes VALUES(?,?,?)',
                 [(x['id'], s['namespace'], s['resource']) for s in scopes])
  return {'decision': 'seat_active', 'scopes': len(scopes)}
 if op == 'revoke_seat':
  s = one(db, "SELECT * FROM duty_seats WHERE id=? AND status='active'", (x['id'],))
  if not s:
   return {'decision': 'unknown_seat'}
  db.execute("UPDATE duty_seats SET status='revoked',successor=? WHERE id=?", (x.get('successor'), x['id']))
  # Prior notifications stay intact as evidence; only future routing changes.
  return {'decision': 'seat_revoked'}
 if op == 'ticket':
  n = one(db, 'SELECT * FROM namespaces WHERE id=?', (x['namespace'],))
  if not n or n['status'] != 'open':
   return {'decision': 'namespace_unavailable'}
  # One handover window, one failure: a single customer-trackable ticket.
  existing = open_incident(db, x['namespace'], x['resource'])
  if existing:
   return {'decision': 'ticket_existing', 'incident': existing['id']}
  db.execute('INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?)',
             (x['id'], x['namespace'], x['resource'], x['at'], 'open', None, None, None))
  add_customer_message(db, 'cm-open-' + x['id'], x['id'], 'ticket_opened', x.get('team', 'platform'), x['at'],
                       body=x.get('body'))
  return {'decision': 'ticket_opened', 'incident': x['id']}
 if op == 'alert':
  inc = one(db, 'SELECT * FROM incidents WHERE id=?', (x['incident'],))
  if not inc or inc['status'] != 'open':
   return {'decision': 'unknown_incident'}
  if x['stage'] not in STAGES:
   return {'decision': 'unknown_stage'}
  seat = active_seat(db, x['seat'], x['at']) if x.get('seat') else None
  if not seat:
   return {'decision': 'no_active_seat'}
  if not seat_covers(db, seat, inc['namespace'], inc['resource']):
   return {'decision': 'resource_outside_seat_scope'}
  if x.get('corr') and one(db, 'SELECT id FROM notifications WHERE corr=?', (x['corr'],)):
   # Repeated carrier delivery is evidence, never a second ticket or a new fact.
   return {'decision': 'duplicate_delivery'}
  # Carrier receipts arriving late must be ordered against the real timeline.
  out_of_order = bool(one(db, 'SELECT id FROM notifications WHERE incident=? AND stage IN (?,?)',
                          (x['incident'], 'acknowledged', 'onsite'))) and x['stage'] in ('queued', 'delivered', 'no_answer')
  stale = 0
  if x['stage'] in ('delivered', 'no_answer'):
   # A transport receipt only proves a device/line accepted the message. Number
   # reuse means that line may no longer belong to the duty person at all.
   if x.get('address') is not None and x['address'] != seat['address']:
    stale = 1
   add_notification(db, x, x['stage'], x['at'], 'recorded', stale, out_of_order)
   decision = 'delivery_recorded' if x['stage'] == 'delivered' else 'no_answer_recorded'
   return {'decision': decision, 'address_stale': bool(stale), 'out_of_order': bool(out_of_order)}
  if x['stage'] == 'queued':
   add_notification(db, x, 'queued', x['at'], 'recorded', 0, out_of_order)
   return {'decision': 'queued', 'out_of_order': bool(out_of_order)}
  # acknowledged / onsite require the human themselves: transport receipts
  # cannot be promoted into personal acknowledgement.
  if x.get('person') != seat['person']:
   return {'decision': 'acknowledgement_person_mismatch'}
  if not x.get('evidence'):
   return {'decision': 'missing_acknowledgement_evidence'}
  add_notification(db, x, x['stage'], x['at'], 'recorded', 0, out_of_order)
  return {'decision': 'acknowledged' if x['stage'] == 'acknowledged' else 'onsite_recorded',
          'out_of_order': bool(out_of_order)}
 if op == 'correct':
  # Corrections never overwrite the original: both the original notice and the
  # customer-visible correction are retained and linked.
  old = one(db, 'SELECT * FROM notifications WHERE id=?', (x['notification'],))
  if not old:
   return {'decision': 'unknown_notification'}
  db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (x['id'], old['incident'], old['seat'], old['team'], old['person'], old['stage'],
             x.get('channel', old['channel']), x.get('address', old['address']),
             int(x.get('address_stale', old['address_stale'])), old['receipt'], None,
             x['at'], 0, old['id'], 'corrected'))
  add_customer_message(db, x['message_id'], old['incident'], 'correction', old['team'], x['at'],
                       body=x.get('body'), corrects=old['id'])
  return {'decision': 'correction_issued', 'corrects': old['id']}
 if op == 'diagnose':
  inc = one(db, 'SELECT * FROM incidents WHERE id=?', (x['incident'],))
  if not inc or inc['status'] != 'open':
   return {'decision': 'unknown_incident'}
  seat = active_seat(db, x['seat'], x['at'])
  if not seat or not seat_covers(db, seat, inc['namespace'], inc['resource']):
   return {'decision': 'seat_not_authorized_for_resource'}
  if one(db, 'SELECT id FROM diagnoses WHERE id=?', (x['id'],)):
   return {'decision': 'reused'}
  # Read-only diagnosis needs no write tenure: the ex-holder on site can still
  # observe, even after losing the lease.
  db.execute('INSERT INTO diagnoses VALUES(?,?,?,?,?,?,?)',
             (x['id'], x['incident'], x['seat'], seat['team'], x['actor'], x['at'], x['findings']))
  return {'decision': 'diagnosis_recorded'}
 if op == 'signoff':
  # The new holder personally signs the recovery plan on the backup channel.
  inc = one(db, 'SELECT * FROM incidents WHERE id=?', (x['incident'],))
  if not inc or inc['status'] != 'open':
   return {'decision': 'unknown_incident'}
  seat = active_seat(db, x['seat'], x['at'])
  if not seat or not seat_covers(db, seat, inc['namespace'], inc['resource']):
   return {'decision': 'seat_not_authorized_for_resource'}
  if x.get('person') != seat['person']:
   return {'decision': 'acknowledgement_person_mismatch'}
  if not x.get('evidence'):
   return {'decision': 'missing_acknowledgement_evidence'}
  db.execute('INSERT INTO notifications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (x['id'], x['incident'], x['seat'], seat['team'], seat['person'], 'acknowledged',
             seat['backup_channel'], seat['backup_address'], 0, x.get('receipt'), x.get('corr'),
             x['at'], 0, None, 'recorded'))
  return {'decision': 'backup_channel_signoff', 'person': seat['person']}
 if op == 'respond':
  inc = one(db, "SELECT * FROM incidents WHERE id=? AND status='open'", (x['incident'],))
  if not inc:
   return {'decision': 'unknown_incident'}
  if active_grant(db, x['incident'], x['at']):
   return {'decision': 'emergency_path_active'}
  lease = active_lease(db, inc['namespace'], x['at'])
  if not lease or lease['holder'] != x['team']:
   return {'decision': 'team_without_write_tenure'}
  if not x.get('eta') or x['eta'] <= x['at']:
   return {'decision': 'invalid_eta'}
  prior = one(db, "SELECT * FROM customer_messages WHERE incident=? AND kind='eta'", (x['incident'],))
  kind = 'eta_correction' if prior else 'eta'
  if prior:
   add_customer_message(db, x['message_id'], x['incident'], kind, x['team'], x['at'], eta=x['eta'],
                        body=x.get('body'), corrects=prior['id'])
  else:
   add_customer_message(db, x['message_id'], x['incident'], kind, x['team'], x['at'], eta=x['eta'],
                        body=x.get('body'))
  db.execute("UPDATE incidents SET owner_team=?,eta=?,response_path='holder' WHERE id=?",
             (x['team'], x['eta'], x['incident']))
  return {'decision': 'eta_promised' if kind == 'eta' else 'eta_corrected', 'eta': x['eta']}
 if op == 'emergency':
  inc = one(db, "SELECT * FROM incidents WHERE id=? AND status='open'", (x['incident'],))
  if not inc:
   return {'decision': 'unknown_incident'}
  seat = active_seat(db, x['seat'], x['at'])
  if not seat or seat['team'] != 'platform':
   return {'decision': 'not_platform_emergency_seat'}
  if not seat_covers(db, seat, inc['namespace'], inc['resource']):
   return {'decision': 'resource_outside_seat_scope'}
  if one(db, "SELECT id FROM emergency_grants WHERE incident=? AND status='active'", (x['incident'],)):
   return {'decision': 'emergency_grant_exists'}
  commands = x.get('commands', [])
  if not commands or any(c not in RESTRICTED_COMMANDS for c in commands):
   return {'decision': 'command_outside_restricted_set'}
  if not x.get('valid_until') or x['valid_until'] <= x['at']:
   return {'decision': 'invalid_grant_window'}
  # Independent verification that the real holder is unreachable through every
  # listed channel; alert-side claims are not enough to take over.
  ack = one(db, "SELECT id FROM notifications WHERE incident=? AND stage='acknowledged'", (x['incident'],))
  contact = rows(db, "SELECT DISTINCT stage FROM notifications WHERE incident=? AND stage IN ('delivered','no_answer')",
                 (x['incident'],))
  if ack or not contact or not x.get('evidence'):
   return {'decision': 'holder_unreachability_unproven'}
  gid = x['id']
  db.execute('INSERT INTO emergency_grants VALUES(?,?,?,?,?,?,?,?,?,?)',
             (gid, x['incident'], inc['namespace'], inc['resource'], x['responder'],
              json.dumps(commands), x['at'], x['valid_until'], 'active', x['evidence']))
  add_customer_message(db, x['message_id'], x['incident'], 'platform_takeover', 'platform', x['at'],
                       body=x.get('body'))
  db.execute("UPDATE incidents SET owner_team='platform',response_path='emergency' WHERE id=?", (x['incident'],))
  return {'decision': 'emergency_granted', 'grant': gid, 'commands': commands}
 if op == 'revoke_emergency':
  g = one(db, "SELECT * FROM emergency_grants WHERE id=? AND status='active'", (x['id'],))
  if not g:
   return {'decision': 'unknown_grant'}
  db.execute("UPDATE emergency_grants SET status='revoked' WHERE id=?", (x['id'],))
  add_customer_message(db, x['message_id'], g['incident'], 'takeover_revoked', 'platform', x['at'],
                       body=x.get('body'))
  return {'decision': 'emergency_revoked'}
 if op == 'repair':
  inc = one(db, "SELECT * FROM incidents WHERE id=? AND status='open'", (x['incident'],))
  if not inc:
   return {'decision': 'unknown_incident'}
  if one(db, 'SELECT id FROM repairs WHERE id=?', (x['id'],)):
   return {'decision': 'reused'}
  if frozen(db, inc['namespace']):
   # A freeze overrides every chain: neither the holder nor an emergency seat
   # may write while the namespace is frozen.
   return {'decision': 'write_denied_frozen'}
  grant = one(db, "SELECT * FROM emergency_grants WHERE incident=? ORDER BY issued_at DESC LIMIT 1", (x['incident'],))
  grant_active = bool(grant and grant['status'] == 'active' and grant['valid_until'] >= x['at'])
  lease = active_lease(db, inc['namespace'], x['at'])
  if x['team'] == 'platform':
   # Platform actors may only write under a live, independent emergency grant.
   if not grant_active:
    if grant and grant['status'] == 'revoked':
     return {'decision': 'emergency_grant_revoked'}
    if grant and grant['valid_until'] < x['at']:
     return {'decision': 'emergency_grant_expired'}
    return {'decision': 'write_denied_no_tenure'}
   if x['actor'] != grant['responder']:
    return {'decision': 'not_grant_responder'}
   if x['command'] not in json.loads(grant['commands']):
    return {'decision': 'command_outside_grant'}
   authority, gen, gid = 'emergency_grant', (lease['generation'] if lease else None), grant['id']
  else:
   # While a takeover is live the emergency path is exclusive; once revoked or
   # expired the real lease holder resumes under normal tenure rules.
   if grant_active:
    return {'decision': 'emergency_path_active'}
   # The alert acknowledgement never confers write power: the real lease at
   # the time of the command is the only holder-path authority.
   if not lease or lease['holder'] != x['team'] or lease['generation'] != x['generation']:
    return {'decision': 'write_denied_no_tenure'}
   authority, gen, gid = 'lease_holder', lease['generation'], None
  # The executing chain must carry an explicit sign-off for the repair.
  if not x.get('signoff'):
   return {'decision': 'missing_repair_signoff'}
  # Orphaned/quarantined resources need independent platform ownership proof;
  # neither lease writes nor restricted recovery commands may touch them.
  if one(db, "SELECT id FROM orphans WHERE resource=? AND namespace=? AND status='quarantined'",
         (inc['resource'], inc['namespace'])):
   return {'decision': 'orphan_requires_platform_proof'}
  db.execute('INSERT INTO repairs VALUES(?,?,?,?,?,?,?,?,?,?,?)',
             (x['id'], x['incident'], inc['namespace'], inc['resource'], x['team'], x['actor'],
              x['command'], authority, gen, gid, x['at']))
  return {'decision': 'repair_executed', 'authority': authority}
 if op == 'resolve':
  inc = one(db, "SELECT * FROM incidents WHERE id=? AND status='open'", (x['incident'],))
  if not inc:
   return {'decision': 'unknown_incident'}
  repair = one(db, 'SELECT * FROM repairs WHERE incident=? ORDER BY at DESC LIMIT 1', (x['incident'],))
  if not repair:
   return {'decision': 'no_repair_evidence'}
  db.execute("UPDATE incidents SET status='resolved' WHERE id=?", (x['incident'],))
  add_customer_message(db, x['message_id'], x['incident'], 'resolved', repair['team'], x['at'],
                       body=x.get('body'))
  return {'decision': 'resolved'}
 if op == 'view':
  inc = one(db, 'SELECT * FROM incidents WHERE id=?', (x['incident'],))
  if not inc:
   return {'decision': 'unknown_incident'}
  notes = rows(db, 'SELECT stage,team,person,channel,address,address_stale,out_of_order,status,at,corrects FROM notifications WHERE incident=? ORDER BY at,rowid', (x['incident'],))
  return {'decision': 'view',
          'incident': dict(inc),
          'notifications': notes,
          'customer_messages': rows(db, 'SELECT id,kind,team,eta,at,corrects FROM customer_messages WHERE incident=? ORDER BY at,rowid', (x['incident'],)),
          'repairs': rows(db, 'SELECT id,team,actor,command,authority,lease_generation,grant_id,at FROM repairs WHERE incident=? ORDER BY at', (x['incident'],)),
          'grants': rows(db, 'SELECT id,responder,commands,issued_at,valid_until,status FROM emergency_grants WHERE incident=? ORDER BY issued_at', (x['incident'],))}
 raise ValueError(op)
