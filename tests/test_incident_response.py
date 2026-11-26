import json,tempfile,unittest
from pathlib import Path
from tenurecourt.store import connect,execute,replay,one,rows

class IncidentResponseTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'x.sqlite';self.db=connect(str(self.path))
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def step(self,sub,**x):return execute(self.db,sub,x)
 def base(self):
  for sub,name in [('tenure','tenure_normal.json'),('cleanup','cleanup_normal.json'),('transfer','transfer_normal.json')]:
   for payload in json.loads((Path(__file__).resolve().parents[1]/'fixtures'/name).read_text(encoding='utf-8')):
    execute(self.db,sub,payload)
  # Transfer completes at t=16: lease holder is now team-b (generation 3);
  # resource r1 fails at t=19.
  self.step('incident',op='seat',id='seat-a',team='team-a',person='alice',channel='radio',address='radio-a',
            backup_person='alice2',backup_channel='sat',backup_address='sat-a',valid_from=1,valid_until=60,
            scopes=[{'namespace':'ns','resource':'r1'}])
  self.step('incident',op='seat',id='seat-b',team='team-b',person='carol',channel='sms',address='new-num',
            backup_person='carol2',backup_channel='backup-radio',backup_address='backup-b',valid_from=1,valid_until=60,
            scopes=[{'namespace':'ns','resource':'r1'}])
  self.step('incident',op='seat',id='seat-platform',team='platform',person='operator',channel='hotline',address='hotline-1',
            backup_person='operator2',backup_channel='sat',backup_address='sat-p',valid_from=1,valid_until=60,
            scopes=[{'namespace':'ns','resource':'*'}])
 def open_ticket(self,at=19):
  r=self.step('incident',op='ticket',id='inc-1',namespace='ns',resource='r1',at=at,team='monitor')
  self.assertEqual(r['decision'],'ticket_opened')
  return r
 def test_carrier_receipt_to_recycled_number_is_not_personal_ack(self):
  self.base();self.open_ticket()
  # Escrow proves team-b accepted the alert *liability*; it must not count as
  # a human acknowledgement of this incident.
  self.assertEqual(one(self.db,"SELECT status FROM liabilities WHERE kind='alert'")['status'],'accepted')
  self.assertIsNone(one(self.db,"SELECT id FROM notifications WHERE stage='acknowledged'"))
  # Mail server enqueue and handset delivery are separate, retained facts.
  self.assertEqual(self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',team='team-b',
                             stage='queued',channel='mail',address='mail-b',receipt='r-q',corr='c-q',at=19)['decision'],'queued')
  d=self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',team='team-b',
              stage='delivered',channel='sms',address='old-num',receipt='r-d',corr='c-d',at=19)
  self.assertEqual(d['decision'],'delivery_recorded');self.assertTrue(d['address_stale'])
  # Without person + evidence the transport receipt cannot be promoted.
  self.assertEqual(self.step('incident',op='alert',id='n3',incident='inc-1',seat='seat-b',team='team-b',person='carol',
                             stage='acknowledged',channel='sms',address='old-num',corr='c-a',at=20)['decision'],
                   'missing_acknowledgement_evidence')
  self.assertEqual(self.step('incident',op='alert',id='n4',incident='inc-1',seat='seat-b',team='team-b',person='stranger',
                             evidence='pin',stage='acknowledged',corr='c-a2',at=20)['decision'],
                   'acknowledgement_person_mismatch')
  self.assertIsNone(one(self.db,"SELECT id FROM notifications WHERE stage='acknowledged'"))
  # A is on site but has lost the lease: observation allowed, repair denied.
  self.assertEqual(self.step('incident',op='alert',id='n5',incident='inc-1',seat='seat-a',team='team-a',person='alice',
                             evidence='radio-voice',stage='onsite',corr='c-o',at=19)['decision'],'onsite_recorded')
  self.assertEqual(self.step('incident',op='repair',id='rep-x',incident='inc-1',team='team-a',actor='alice',
                             command='restart',generation=3,signoff='s',at=20)['decision'],'write_denied_no_tenure')
  self.assertEqual(self.step('tenure',op='write',id='w-a',namespace='ns',team='team-a',generation=3,resource='r1',at=20)['decision'],'write_denied')
 def test_holder_path_diagnosis_backup_signoff_holder_repair_single_customer_eta(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',channel='mail',address='mail-b',corr='c-q',at=19)
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',stage='delivered',channel='sms',address='old-num',corr='c-d',at=19)
  # A performs read-only on-site diagnosis with no write tenure.
  self.assertEqual(self.step('incident',op='diagnose',id='dg1',incident='inc-1',seat='seat-a',actor='alice',at=20,
                             findings='disk-readonly')['decision'],'diagnosis_recorded')
  # B personally signs the plan on the backup channel.
  self.assertEqual(self.step('incident',op='signoff',id='sg1',incident='inc-1',seat='seat-b',person='carol',
                             evidence='backup-pin',receipt='r-sg',corr='c-sg',at=21)['decision'],'backup_channel_signoff')
  # A cannot promise a competing recovery time: only the real lease holder responds.
  self.assertEqual(self.step('incident',op='respond',incident='inc-1',team='team-a',eta=30,message_id='m-a',at=21)['decision'],
                   'team_without_write_tenure')
  self.assertEqual(self.step('incident',op='respond',incident='inc-1',team='team-b',eta=30,message_id='m-b',at=21)['decision'],
                   'eta_promised')
  # B holds the new lease and executes; wrong generation is rejected.
  self.assertEqual(self.step('incident',op='repair',id='rep-bad',incident='inc-1',team='team-b',actor='carol',
                             command='restart',generation=2,signoff='sg1',at=22)['decision'],'write_denied_no_tenure')
  self.assertEqual(self.step('incident',op='repair',id='rep1',incident='inc-1',team='team-b',actor='carol',
                             command='restart',generation=3,signoff='sg1',at=22)['decision'],'repair_executed')
  self.assertEqual(one(self.db,"SELECT authority FROM repairs WHERE id='rep1'")['authority'],'lease_holder')
  # While the ticket is open a repeated failure alert reuses the single ticket.
  self.assertEqual(self.step('incident',op='ticket',id='inc-dup',namespace='ns',resource='r1',at=23,team='monitor')['decision'],
                   'ticket_existing')
  self.assertEqual(self.step('incident',op='resolve',incident='inc-1',message_id='m-r',at=25)['decision'],'resolved')
  msgs=rows(self.db,"SELECT kind FROM customer_messages WHERE incident='inc-1' ORDER BY at")
  self.assertEqual([m['kind'] for m in msgs],['ticket_opened','eta','resolved'])
 def test_emergency_path_platform_takeover_restricted_commands_and_customer_notice(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',channel='mail',address='mail-b',corr='c-q',at=19)
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',stage='delivered',channel='sms',address='old-num',corr='c-d',at=19)
  self.assertEqual(self.step('incident',op='alert',id='n3',incident='inc-1',seat='seat-b',stage='no_answer',
                             channel='sms',address='new-num',corr='c-n',at=20)['decision'],'no_answer_recorded')
  # Takeover needs unreachability evidence and contact attempts.
  self.assertEqual(self.step('incident',op='emergency',id='g1',incident='inc-1',seat='seat-platform',responder='operator',
                             commands=['restart'],valid_until=40,message_id='m-g',at=21)['decision'],'holder_unreachability_unproven')
  self.assertEqual(self.step('incident',op='emergency',id='g1',incident='inc-1',seat='seat-platform',responder='operator',
                             commands=['restart','rm-rf'],valid_until=40,evidence='call-log',message_id='m-g',at=21)['decision'],
                   'command_outside_restricted_set')
  # A non-platform seat cannot claim emergency powers.
  self.assertEqual(self.step('incident',op='emergency',id='g2',incident='inc-1',seat='seat-a',responder='alice',
                             commands=['restart'],valid_until=40,evidence='call-log',message_id='m-g2',at=21)['decision'],
                   'not_platform_emergency_seat')
  r=self.step('incident',op='emergency',id='g1',incident='inc-1',seat='seat-platform',responder='operator',
              commands=['restart','failover'],valid_until=40,evidence='call-log',message_id='m-g',at=21)
  self.assertEqual(r['decision'],'emergency_granted')
  self.assertEqual(one(self.db,"SELECT owner_team FROM incidents WHERE id='inc-1'")['owner_team'],'platform')
  # Once the emergency path is active, B cannot attach a competing ETA.
  self.assertEqual(self.step('incident',op='respond',incident='inc-1',team='team-b',eta=30,message_id='m-b',at=22)['decision'],
                   'emergency_path_active')
  # Only the named responder, only granted commands; A still cannot write.
  self.assertEqual(self.step('incident',op='repair',id='rep-a',incident='inc-1',team='team-a',actor='alice',
                             command='restart',generation=3,signoff='s',at=22)['decision'],'emergency_path_active')
  self.assertEqual(self.step('incident',op='repair',id='rep-x',incident='inc-1',team='platform',actor='other',
                             command='restart',signoff='g1',at=22)['decision'],'not_grant_responder')
  self.assertEqual(self.step('incident',op='repair',id='rep-x',incident='inc-1',team='platform',actor='operator',
                             command='quarantine',signoff='g1',at=22)['decision'],'command_outside_grant')
  self.assertEqual(self.step('incident',op='repair',id='rep1',incident='inc-1',team='platform',actor='operator',
                             command='restart',signoff='g1',at=22)['decision'],'repair_executed')
  self.assertEqual(one(self.db,"SELECT authority FROM repairs WHERE id='rep1'")['authority'],'emergency_grant')
  self.assertEqual(self.step('incident',op='resolve',incident='inc-1',message_id='m-r',at=25)['decision'],'resolved')
  kinds=[m['kind'] for m in rows(self.db,"SELECT kind FROM customer_messages WHERE incident='inc-1' ORDER BY at")]
  self.assertEqual(kinds,['ticket_opened','platform_takeover','resolved'])
 def test_platform_revocation_and_grant_expiry_stop_writes(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c-q',at=19)
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',stage='delivered',address='old-num',corr='c-d',at=19)
  self.step('incident',op='alert',id='n3',incident='inc-1',seat='seat-b',stage='no_answer',corr='c-n',at=20)
  self.step('incident',op='emergency',id='g1',incident='inc-1',seat='seat-platform',responder='operator',
            commands=['restart'],valid_until=40,evidence='call-log',message_id='m-g',at=21)
  self.step('incident',op='revoke_emergency',id='g1',message_id='m-rev',at=23,body='false-alarm')
  self.assertEqual(self.step('incident',op='repair',id='rep1',incident='inc-1',team='platform',actor='operator',
                             command='restart',signoff='g1',at=24)['decision'],'emergency_grant_revoked')
  # Expiry path on a fresh grant.
  self.step('incident',op='emergency',id='g2',incident='inc-1',seat='seat-platform',responder='operator',
            commands=['restart'],valid_until=30,evidence='call-log-2',message_id='m-g2',at=25)
  self.assertEqual(self.step('incident',op='repair',id='rep2',incident='inc-1',team='platform',actor='operator',
                             command='restart',signoff='g2',at=31)['decision'],'emergency_grant_expired')
  self.assertTrue(any(m['kind']=='takeover_revoked' for m in rows(self.db,"SELECT kind FROM customer_messages WHERE incident='inc-1'")))
 def test_freeze_blocks_holder_and_emergency_writes_alike(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c-q',at=19)
  self.step('incident',op='signoff',id='sg1',incident='inc-1',seat='seat-b',person='carol',evidence='pin',corr='c-sg',at=20)
  self.step('tenure',op='freeze',id='fz1',namespace='ns',generation=1,reason='audit')
  self.assertEqual(self.step('incident',op='repair',id='rep1',incident='inc-1',team='team-b',actor='carol',
                             command='restart',generation=3,signoff='sg1',at=21)['decision'],'write_denied_frozen')
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',stage='delivered',address='old-num',corr='c-d',at=19)
  self.step('incident',op='alert',id='n3',incident='inc-1',seat='seat-b',stage='no_answer',corr='c-n',at=20)
  self.step('incident',op='emergency',id='g1',incident='inc-1',seat='seat-platform',responder='operator',
            commands=['restart'],valid_until=40,evidence='log',message_id='m-g',at=22)
  self.assertEqual(self.step('incident',op='repair',id='rep2',incident='inc-1',team='platform',actor='operator',
                             command='restart',signoff='g1',at=23)['decision'],'write_denied_frozen')
  self.step('tenure',op='unfreeze',id='fz1',generation=2)
  self.assertEqual(self.step('incident',op='repair',id='rep3',incident='inc-1',team='team-b',actor='carol',
                             command='restart',generation=3,signoff='sg1',at=24)['decision'],'repair_executed')
 def test_orphaned_resource_requires_independent_platform_proof(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c-q',at=19)
  self.step('cleanup',op='discover',id='o1',resource='r1',namespace='ns')
  # Holder path: a quarantined resource blocks even a signed-off lease repair.
  self.step('incident',op='signoff',id='sg1',incident='inc-1',seat='seat-b',person='carol',evidence='pin',corr='c-sg',at=20)
  self.assertEqual(self.step('incident',op='repair',id='rep1',incident='inc-1',team='team-b',actor='carol',
                             command='restart',generation=3,signoff='sg1',at=21)['decision'],'orphan_requires_platform_proof')
  # Independent platform attestation + claim clears the quarantine.
  self.step('cleanup',op='proof',id='p1',resource='r1',team='team-b',generation=1,issuer='platform',valid_until=60,at=24)
  self.step('cleanup',op='claim',id='o1',proof='p1',owner='team-b',at=25)
  self.assertEqual(self.step('incident',op='repair',id='rep3',incident='inc-1',team='team-b',actor='carol',
                             command='restart',generation=3,signoff='sg1',at=26)['decision'],'repair_executed')
 def test_orphaned_resource_blocks_emergency_recovery_until_platform_claim(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c-q',at=19)
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',stage='delivered',address='old-num',corr='c-d',at=19)
  self.step('incident',op='alert',id='n3',incident='inc-1',seat='seat-b',stage='no_answer',corr='c-n',at=20)
  self.step('cleanup',op='discover',id='o1',resource='r1',namespace='ns')
  self.step('incident',op='emergency',id='g1',incident='inc-1',seat='seat-platform',responder='operator',
            commands=['restart'],valid_until=40,evidence='log',message_id='m-g',at=22)
  self.assertEqual(one(self.db,"SELECT status FROM emergency_grants WHERE id='g1'")['status'],'active')
  # The emergency seat is also blocked by the quarantine, independent of tenure.
  self.assertEqual(self.step('incident',op='repair',id='rep2',incident='inc-1',team='platform',actor='operator',
                             command='restart',signoff='g1',at=23)['decision'],'orphan_requires_platform_proof')
  self.step('cleanup',op='proof',id='p1',resource='r1',team='team-b',generation=1,issuer='platform',valid_until=60,at=24)
  self.step('cleanup',op='claim',id='o1',proof='p1',owner='team-b',at=25)
  self.assertEqual(self.step('incident',op='repair',id='rep3',incident='inc-1',team='platform',actor='operator',
                             command='restart',signoff='g1',at=26)['decision'],'repair_executed')
 def test_duplicate_delivery_out_of_order_receipts_and_corrections_are_retained(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c1',at=19)
  self.assertEqual(self.step('incident',op='alert',id='n1b',incident='inc-1',seat='seat-b',stage='queued',corr='c1',at=19)['decision'],
                   'duplicate_delivery')
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',person='carol',evidence='pin',
            stage='acknowledged',corr='c2',at=20)
  # A late carrier receipt after human ack is marked out of order but kept.
  late=self.step('incident',op='alert',id='n3',incident='inc-1',seat='seat-b',stage='delivered',address='new-num',corr='c3',at=22)
  self.assertTrue(late['out_of_order'])
  self.assertEqual(one(self.db,"SELECT status FROM notifications WHERE id='n1'")['status'],'recorded')
  # Correction preserves the original and emits a customer-visible correction.
  c=self.step('incident',op='correct',id='n2c',notification='n2',channel='voice',address='hotline-2',at=23,
              message_id='m-c',body='ack was by voice, not sms')
  self.assertEqual(c['decision'],'correction_issued')
  self.assertEqual(one(self.db,"SELECT status FROM notifications WHERE id='n2'")['status'],'recorded')
  self.assertEqual(one(self.db,"SELECT corrects,status FROM notifications WHERE id='n2c'")['corrects'],'n2')
  self.assertEqual(one(self.db,"SELECT kind,corrects FROM customer_messages WHERE id='m-c'")['kind'],'correction')
 def test_seat_rotation_window_and_scope_are_enforced(self):
  self.base();self.open_ticket()
  # A seat outside its validity window cannot receive.
  self.step('incident',op='seat',id='seat-old',team='team-c',person='zoe',channel='sms',address='z1',
            backup_person='z2',backup_channel='sat',backup_address='z3',valid_from=1,valid_until=10,
            scopes=[{'namespace':'ns','resource':'r1'}])
  self.assertEqual(self.step('incident',op='alert',id='x1',incident='inc-1',seat='seat-old',stage='queued',corr='cx',at=19)['decision'],
                   'no_active_seat')
  # Scope mismatch.
  self.step('incident',op='seat',id='seat-narrow',team='team-b',person='carol',channel='sms',address='new-num',
            backup_person='carol2',backup_channel='b',backup_address='bb',valid_from=1,valid_until=60,
            scopes=[{'namespace':'ns','resource':'other'}])
  self.assertEqual(self.step('incident',op='alert',id='x2',incident='inc-1',seat='seat-narrow',stage='queued',corr='cx2',at=19)['decision'],
                   'resource_outside_seat_scope')
  # Rotation: revoke with a successor; the old seat stops routing, evidence stays.
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c1',at=19)
  self.step('incident',op='revoke_seat',id='seat-b',successor='seat-b2')
  self.assertEqual(self.step('incident',op='alert',id='x3',incident='inc-1',seat='seat-b',stage='queued',corr='cx3',at=20)['decision'],
                   'no_active_seat')
  self.assertEqual(one(self.db,"SELECT status FROM notifications WHERE id='n1'")['status'],'recorded')
  self.step('incident',op='seat',id='seat-b2',team='team-b',person='dave',channel='sms',address='dave-num',
            backup_person='dave2',backup_channel='radio',backup_address='dave-b',valid_from=20,valid_until=60,
            scopes=[{'namespace':'ns','resource':'r1'}])
  self.assertEqual(self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b2',person='dave',evidence='pin',
                             stage='acknowledged',corr='c2',at=20)['decision'],'acknowledged')
  self.assertEqual(one(self.db,"SELECT status,successor FROM duty_seats WHERE id='seat-b'")['status'],'revoked')
 def test_eta_corrections_form_one_lineage_and_view_exposes_evidence(self):
  self.base();self.open_ticket()
  self.step('incident',op='respond',incident='inc-1',team='team-b',eta=30,message_id='m1',at=20)
  self.assertEqual(self.step('incident',op='respond',incident='inc-1',team='team-b',eta=45,message_id='m2',at=22,
                             body='parts delayed')['decision'],'eta_corrected')
  v=self.step('incident',op='view',incident='inc-1')
  self.assertEqual(v['decision'],'view')
  self.assertEqual(v['incident']['eta'],45)
  eta=[m for m in v['customer_messages'] if m['kind'].startswith('eta')]
  self.assertEqual([m['id'] for m in eta],['m1','m2'])
  self.assertEqual(eta[1]['corrects'],'m1')
 def test_full_incident_journal_replays(self):
  self.base();self.open_ticket()
  self.step('incident',op='alert',id='n1',incident='inc-1',seat='seat-b',stage='queued',corr='c-q',at=19)
  self.step('incident',op='alert',id='n2',incident='inc-1',seat='seat-b',stage='delivered',address='old-num',corr='c-d',at=19)
  self.step('incident',op='diagnose',id='dg1',incident='inc-1',seat='seat-a',actor='alice',at=20,findings='disk')
  self.step('incident',op='signoff',id='sg1',incident='inc-1',seat='seat-b',person='carol',evidence='pin',corr='c-sg',at=21)
  self.step('incident',op='respond',incident='inc-1',team='team-b',eta=30,message_id='m1',at=21)
  self.step('incident',op='repair',id='rep1',incident='inc-1',team='team-b',actor='carol',command='restart',
            generation=3,signoff='sg1',at=22)
  self.step('incident',op='resolve',incident='inc-1',message_id='m2',at=25)
  r=replay(str(self.path))
  self.assertTrue(r['verified'],r['errors'])

if __name__=='__main__':unittest.main()
