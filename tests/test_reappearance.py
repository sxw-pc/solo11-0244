import tempfile,unittest
from pathlib import Path
from tenurecourt.store import connect,execute,replay,one,rows
CASE='reappearance-R-d1'
class ReappearanceTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'x.sqlite';self.db=connect(str(self.path))
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def step(self,sub,**x):return execute(self.db,sub,x)
 def scenario(self):
  # R pinned to physical uid A; team-a lease minute 1..30; deletion receipt confirmed at minute 20
  self.step('tenure',op='namespace',id='ns',uid='ns-u1',generation=1)
  self.step('tenure',op='lease',id='L1',namespace='ns',namespace_uid='ns-u1',holder='team-a',generation=1,start=1,expires=30)
  self.step('cleanup',op='resource',id='R',namespace='ns',fingerprint='fp1',generation=1,owner='team-a',at=5)
  self.assertEqual(self.step('reappearance',op='pin',id='pin-1',resource='R',uid='A',digest='fp1',location='loc-1',generation=1,source='console-0',at=5)['decision'],'pinned')
  self.assertEqual(self.step('cleanup',op='delete',id='d1',resource='R',generation=1,at=20)['decision'],'deleted')
 def sight(self,**kw):
  # minute 21: control plane reports R generation 5, same fingerprint, no physical uid
  x=dict(op='sighting',id='s1',resource='R',generation=5,digest='fp1',uid=None,location=None,source='console-1',at=21);x.update(kw)
  return self.step('reappearance',**x)
 def readback_ok(self,at=23,id='rb1'):
  return self.step('reappearance',op='readback',id=id,case=CASE,uid='A',location='loc-1',pages_expected=4,pages_present=4,digest='fp1',rollback_source='backup-1',endpoint='store-2',at=at)
 def verified_case(self):
  self.sight()
  self.step('tenure',op='freeze',id='f1',namespace='ns',generation=1,reason='reappearance-investigation')
  self.assertEqual(self.readback_ok()['decision'],'verified')
  self.step('reappearance',op='authorize',id='auth-r',namespace='ns',resource='R',kind='restore',customer='alice',platform='operator',valid_until=26,at=23)
  self.step('reappearance',op='signoff',id='so1',case=CASE,team='team-a',actor='alice',uid='A',accept=True,at=24)
 def test_higher_generation_does_not_overturn_signed_deletion(self):
  self.scenario()
  r=self.sight()
  self.assertEqual(r['decision'],'case_opened');self.assertEqual(r['old_uid'],'A');self.assertEqual(r['deletion'],'d1')
  res=one(self.db,'SELECT * FROM resources WHERE id=?',('R',))
  self.assertEqual((res['status'],res['owner'],res['generation']),('deleted','team-a',1))
  d=one(self.db,'SELECT * FROM deletions WHERE id=?',('d1',))
  self.assertEqual((d['status'],d['at']),('confirmed',20))
  # a retried delete neither reports a fresh cleanup nor opens writes
  self.assertEqual(self.step('cleanup',op='delete',id='d1',resource='R',generation=1,at=20)['decision'],'stale_resource')
  # the unverified case does not inherit the old team's still-valid lease write rights
  self.assertEqual(self.step('tenure',op='write',id='w1',namespace='ns',team='team-a',generation=1,resource='R',at=22)['decision'],'write_denied')
  # the open case blocks namespace close even with no finalizers
  r=self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=31)
  self.assertEqual(r['decision'],'cleanup_incomplete');self.assertEqual(r['reappearance_cases'],[CASE])
  self.assertTrue(replay(str(self.path))['verified'])
 def test_sighting_requires_deleted_resource(self):
  self.step('tenure',op='namespace',id='ns',uid='ns-u1',generation=1)
  self.step('tenure',op='lease',id='L1',namespace='ns',namespace_uid='ns-u1',holder='team-a',generation=1,start=1,expires=30)
  self.step('cleanup',op='resource',id='R',namespace='ns',fingerprint='fp1',generation=1,owner='team-a',at=5)
  self.assertEqual(self.sight(at=6)['decision'],'not_deleted')
  self.assertEqual(self.sight(id='s9',resource='ZZ',at=6)['decision'],'unknown_resource')
 def test_restore_path_returns_original_entity_with_restricted_lease(self):
  self.scenario();self.verified_case()
  r=self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=30,at=24)
  # the restricted lease is clamped to the pre-authorized window
  self.assertEqual((r['decision'],r['uid'],r['lease'],r['expires']),('restored','A','L2',26))
  res=one(self.db,'SELECT * FROM resources WHERE id=?',('R',))
  self.assertEqual((res['status'],res['owner'],res['generation']),('active','team-a',1))
  old=one(self.db,'SELECT * FROM leases WHERE id=?',('L1',));new=one(self.db,'SELECT * FROM leases WHERE id=?',('L2',))
  self.assertEqual(old['status'],'superseded')
  self.assertEqual((new['holder'],new['start'],new['expires'],new['status']),('team-a',24,26,'active'))
  self.assertEqual(one(self.db,'SELECT lease FROM reappearance_cases WHERE id=?',(CASE,))['lease'],'L2')
  self.assertEqual(self.step('tenure',op='unfreeze',id='f1',generation=2)['decision'],'active')
  # the customer keeps using the original data under the restricted lease
  self.assertEqual(self.step('tenure',op='write',id='w1',namespace='ns',team='team-a',generation=2,resource='R',at=26)['decision'],'written')
  self.assertEqual(self.step('tenure',op='write',id='w2',namespace='ns',team='team-a',generation=1,resource='R',at=26)['decision'],'write_denied')
  # power-failure retries of the restore replay to the same decision
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=30,at=24)['decision'],'reused')
  self.assertEqual(self.step('reappearance',op='restore',id='rs2',case=CASE,lease='L3',expires=30,at=24)['decision'],'case_closed')
  self.assertEqual(len(rows(self.db,"SELECT * FROM leases WHERE namespace='ns' AND status='active'")),1)
  self.assertTrue(replay(str(self.path))['verified'])
 def test_restore_requires_verification_freeze_authorization_and_signoff(self):
  self.scenario();self.sight()
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'unverified')
  self.readback_ok()
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'namespace_not_frozen')
  self.step('tenure',op='freeze',id='f1',namespace='ns',generation=1,reason='reappearance')
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'unauthorized_disposition')
  self.step('reappearance',op='authorize',id='auth-r',namespace='ns',resource='R',kind='restore',customer='alice',platform='operator',valid_until=26,at=23)
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'team_not_signed_off')
  self.step('reappearance',op='signoff',id='so1',case=CASE,team='team-a',actor='alice',uid='A',accept=True,at=24)
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'restored')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_isolate_path_preserves_deletion_and_closes_namespace(self):
  self.scenario();self.sight()
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=22)['decision'],'unauthorized_disposition')
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=21)
  r=self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=22)
  self.assertEqual((r['decision'],r['new_uid'],r['deletion']),('isolated','C','d1'))
  # the minute-20 deletion fact is preserved and the old object is not restored
  d=one(self.db,'SELECT * FROM deletions WHERE id=?',('d1',))
  self.assertEqual((d['status'],d['at']),('confirmed',20))
  self.assertEqual(one(self.db,'SELECT status FROM resources WHERE id=?',('R',))['status'],'deleted')
  # further sightings of the adjudicated deletion are recorded as post-resolution, no new case
  self.assertEqual(self.sight(id='s1b',at=23)['decision'],'case_closed')
  # the isolated object cannot borrow the old ownership proofs and no orphan case exists for it
  self.assertEqual(self.step('cleanup',op='proof',id='p5',resource='R',team='team-b',generation=5,issuer='platform',valid_until=40,at=23)['decision'],'invalid_ownership_attestation')
  self.assertEqual(self.step('cleanup',op='proof',id='p1',resource='R',team='team-b',generation=1,issuer='platform',valid_until=40,at=23)['decision'],'attested')
  self.assertEqual(rows(self.db,'SELECT id FROM orphans WHERE resource=?',('R',)),[])
  # a concurrently added finalizer still blocks closing; the resolved case may not bypass it
  self.step('cleanup',op='finalizer',id='fin2',resource='R')
  r=self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=31)
  self.assertEqual(r['decision'],'cleanup_incomplete');self.assertEqual(r['finalizers'],['fin2']);self.assertEqual(r['reappearance_cases'],[])
  self.step('cleanup',op='release_finalizer',id='fin2')
  self.assertEqual(self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=31)['decision'],'closed')
  # the customer no longer gets R's data: writes and new sightings stay refused after close
  self.assertEqual(self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=31)['decision'],'reused')
  self.assertEqual(self.step('cleanup',op='close',id='c2',namespace='ns',uid='ns-u1',at=32)['decision'],'already_closed')
  self.assertEqual(self.step('tenure',op='write',id='w1',namespace='ns',team='team-a',generation=1,resource='R',at=32)['decision'],'write_denied')
  self.assertEqual(self.sight(id='s2',at=32)['decision'],'namespace_closed')
  self.assertEqual(len(rows(self.db,'SELECT * FROM closures')),1)
  self.assertTrue(replay(str(self.path))['verified'])
 def test_same_content_different_uid_cannot_restore(self):
  self.scenario()
  r=self.sight(uid='B',location='loc-2')
  self.assertEqual(r['decision'],'case_opened')
  # independent readback of the pinned uid A finds the bytes gone: the same-fingerprint object at uid B is a copy
  self.assertEqual(self.step('reappearance',op='readback',id='rb1',case=CASE,uid='A',location='loc-1',pages_expected=4,pages_present=0,digest='',rollback_source='backup-1',endpoint='store-2',at=23)['decision'],'readback_incomplete')
  self.step('tenure',op='freeze',id='f1',namespace='ns',generation=1,reason='r')
  self.step('reappearance',op='authorize',id='auth-r',namespace='ns',resource='R',kind='restore',customer='alice',platform='operator',valid_until=26,at=23)
  self.step('reappearance',op='signoff',id='so1',case=CASE,team='team-a',actor='alice',uid='A',accept=True,at=24)
  # fingerprint equality is not identity: restore stays impossible
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'unverified')
  # the claimed uid B never becomes a pinned identity
  self.assertEqual(rows(self.db,"SELECT id FROM identities WHERE uid='B'"),[])
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=24)
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=25)['decision'],'isolated')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_copied_old_uid_is_not_identity(self):
  self.scenario()
  r=self.sight(uid='A',location='loc-9',source='console-2')
  self.assertEqual(r['decision'],'case_opened')
  # the retired uid A cannot be pinned again: not as-is, not with other content, not for another resource
  self.assertEqual(self.step('reappearance',op='pin',id='p2',resource='R',uid='A',digest='fp1',location='loc-9',generation=5,source='console-2',at=21)['decision'],'uid_retired')
  self.assertEqual(self.step('reappearance',op='pin',id='p3',resource='R',uid='A',digest='fpX',location='loc-9',generation=5,source='console-2',at=21)['decision'],'uid_conflict')
  self.step('cleanup',op='resource',id='R2',namespace='ns',fingerprint='fp2',generation=1,owner='team-a',at=22)
  self.assertEqual(self.step('reappearance',op='pin',id='p4',resource='R2',uid='A',digest='fp1',location='loc-1',generation=1,source='console-2',at=22)['decision'],'uid_conflict')
  # the storage endpoint proves A's bytes are gone; the copied uid claim cannot be verified
  self.assertEqual(self.step('reappearance',op='readback',id='rb1',case=CASE,uid='A',location='loc-1',pages_expected=4,pages_present=0,digest='',rollback_source='backup-1',endpoint='store-2',at=23)['decision'],'readback_incomplete')
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'unverified')
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=23)
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='D',space='inv-1',at=24)['decision'],'isolated')
  self.assertEqual(one(self.db,'SELECT status FROM resources WHERE id=?',('R2',))['status'],'active')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_readback_evidence_rules(self):
  self.scenario();self.sight()
  # evidence must pertain to the pinned uid and location
  self.assertEqual(self.step('reappearance',op='readback',id='rb1',case=CASE,uid='B',location='loc-1',pages_expected=4,pages_present=4,digest='fp1',rollback_source='backup-1',endpoint='store-2',at=22)['decision'],'identity_mismatch')
  # missing pages leave the case unverified
  self.assertEqual(self.step('reappearance',op='readback',id='rb2',case=CASE,uid='A',location='loc-1',pages_expected=4,pages_present=3,digest='fp1',rollback_source='backup-1',endpoint='store-2',at=22)['decision'],'readback_incomplete')
  # content polluted by the new object forbids restore
  self.assertEqual(self.step('reappearance',op='readback',id='rb3',case=CASE,uid='A',location='loc-1',pages_expected=4,pages_present=4,digest='fp-new',rollback_source='backup-1',endpoint='store-2',at=22)['decision'],'content_mismatch')
  self.assertEqual(one(self.db,'SELECT status FROM reappearance_cases WHERE id=?',(CASE,))['status'],'open')
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'unverified')
  # a complete, matching, sourced readback verifies ownership; retry is idempotent
  self.assertEqual(self.readback_ok(id='rb4')['decision'],'verified')
  self.assertEqual(self.readback_ok(id='rb4')['decision'],'reused')
  self.assertEqual(self.readback_ok(id='rb4',at=24)['decision'],'readback_conflict')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_two_consoles_attach_to_single_case(self):
  self.scenario()
  r1=self.sight()
  r2=self.sight(id='s2',source='console-2',uid='B',location='loc-2')
  self.assertEqual(r1['decision'],'case_opened')
  self.assertEqual(r2['decision'],'case_attached')
  self.assertEqual(r1['case'],r2['case'])
  # power-failure retry of the first console's report replays; conflicting reuse of the id is rejected
  self.assertEqual(self.sight()['decision'],'reused')
  self.assertEqual(self.sight(digest='fpX')['decision'],'sighting_conflict')
  self.assertEqual(len(rows(self.db,'SELECT * FROM reappearance_cases')),1)
  self.assertEqual(len(rows(self.db,'SELECT * FROM sightings')),2)
  self.assertEqual({s['source'] for s in rows(self.db,'SELECT * FROM sightings')},{'console-1','console-2'})
  self.assertTrue(replay(str(self.path))['verified'])
 def test_team_refusal_blocks_restore_falls_back_to_isolate(self):
  self.scenario();self.sight()
  self.step('tenure',op='freeze',id='f1',namespace='ns',generation=1,reason='r')
  self.readback_ok()
  self.step('reappearance',op='authorize',id='auth-r',namespace='ns',resource='R',kind='restore',customer='alice',platform='operator',valid_until=26,at=23)
  # sign-off must come from the responsible team and reference the same entity uid
  self.assertEqual(self.step('reappearance',op='signoff',id='so1',case=CASE,team='team-b',actor='bob',uid='A',accept=True,at=24)['decision'],'not_responsible_team')
  self.assertEqual(self.step('reappearance',op='signoff',id='so2',case=CASE,team='team-a',actor='alice',uid='B',accept=True,at=24)['decision'],'entity_mismatch')
  # the team refuses the returned object; refusal is final for the case
  self.assertEqual(self.step('reappearance',op='signoff',id='so3',case=CASE,team='team-a',actor='alice',uid='A',accept=False,at=24)['decision'],'refused')
  self.assertEqual(self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=25,at=24)['decision'],'team_refused')
  self.assertEqual(self.step('reappearance',op='signoff',id='so4',case=CASE,team='team-a',actor='alice',uid='A',accept=True,at=25)['decision'],'already_signed')
  # fall back to the isolate disposition and close the namespace
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=25)
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=26)['decision'],'isolated')
  self.assertEqual(self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=31)['decision'],'closed')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_isolate_requires_fresh_uid_and_is_idempotent(self):
  self.scenario();self.sight()
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=21)
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='A',space='inv-1',at=22)['decision'],'uid_not_fresh')
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=22)['decision'],'isolated')
  self.assertEqual(self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=22)['decision'],'reused')
  self.assertEqual(self.step('reappearance',op='isolate',id='iso2',case=CASE,new_uid='D',space='inv-1',at=23)['decision'],'case_closed')
  # the investigation uid is now pinned and cannot be re-pinned under a different claim
  self.assertEqual(self.step('reappearance',op='pin',id='p9',resource='R',uid='C',digest='fpX',location='loc-9',generation=5,source='console-2',at=23)['decision'],'uid_conflict')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_interrupted_close_leaves_no_partial_state(self):
  self.scenario();self.sight()
  # an interrupted close attempt while the case is open records nothing
  r=self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=31)
  self.assertEqual(r['decision'],'cleanup_incomplete')
  self.assertEqual(rows(self.db,'SELECT * FROM closures'),[])
  self.assertEqual(one(self.db,'SELECT status FROM namespaces WHERE id=?',('ns',))['status'],'open')
  # after resolution the same close id succeeds; a crash-and-retry reports the same outcome
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=21)
  self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=32)
  self.assertEqual(self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=33)['decision'],'closed')
  self.assertEqual(self.step('cleanup',op='close',id='c1',namespace='ns',uid='ns-u1',at=33)['decision'],'reused')
  # the retried delete and late write stay consistent: already cleaned, never writable
  self.assertEqual(self.step('cleanup',op='delete',id='d1',resource='R',generation=1,at=20)['decision'],'stale_resource')
  self.assertEqual(self.step('tenure',op='write',id='w1',namespace='ns',team='team-a',generation=1,resource='R',at=34)['decision'],'write_denied')
  self.assertTrue(replay(str(self.path))['verified'])
 def test_restore_then_new_deletion_starts_fresh_case(self):
  self.scenario();self.verified_case()
  self.step('reappearance',op='restore',id='rs1',case=CASE,lease='L2',expires=26,at=24)
  # the restored entity is later deleted again properly; each receipt gets its own case
  self.assertEqual(self.step('cleanup',op='delete',id='d2',resource='R',generation=1,at=26)['decision'],'deleted')
  r=self.sight(id='s9',at=27)
  self.assertEqual(r['decision'],'case_opened');self.assertEqual(r['case'],'reappearance-R-d2');self.assertEqual(r['deletion'],'d2')
  self.assertEqual(self.sight(id='s8',at=28)['decision'],'case_attached')
  self.assertEqual(len(rows(self.db,'SELECT * FROM reappearance_cases')),2)
  self.assertTrue(replay(str(self.path))['verified'])
 def test_open_case_blocks_transfer_handover(self):
  self.scenario();self.sight()
  self.step('tenure',op='lease',id='L9',namespace='ns',namespace_uid='ns-u1',holder='team-a',generation=2,start=20,expires=40)
  self.step('transfer',op='propose',id='t',namespace='ns',from_team='team-a',to_team='team-b',at=25,deadline=40)
  for team,actor,role in [('team-a','alice','outgoing'),('team-b','bob','incoming'),('platform','op','platform')]:
   self.step('transfer',op='accept',id='t',receipt='a-'+team,team=team,actor=actor,role=role,at=25)
  for kind in ('billing','alert'):
   self.step('transfer',op='liability',id='l-'+kind,transfer='t',kind=kind,amount=1)
   self.step('transfer',op='accept_liability',id='l-'+kind,receipt='lr-'+kind,team='team-b',actor='bob',at=25)
  self.step('transfer',op='escrow',id='t',at=26)
  self.assertEqual(self.step('transfer',op='complete',id='t',at=26)['decision'],'inventory_changed')
  # once the case is resolved the handover can complete
  self.step('reappearance',op='authorize',id='auth-i',namespace='ns',resource='R',kind='isolate',customer='alice',platform='operator',valid_until=40,at=26)
  self.step('reappearance',op='isolate',id='iso1',case=CASE,new_uid='C',space='inv-1',at=27)
  self.assertEqual(self.step('transfer',op='complete',id='t',at=28)['decision'],'transferred')
  self.assertTrue(replay(str(self.path))['verified'])
if __name__=='__main__':unittest.main()
