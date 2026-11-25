import json,tempfile,unittest
from pathlib import Path
import tenurecourt
from tenurecourt.store import connect,execute,replay,one
ROOT=Path(__file__).resolve().parents[1]
assert Path(tenurecourt.__file__).resolve().is_relative_to(ROOT)
print('MODULE_PATH',tenurecourt.__file__)
class TenureTests(unittest.TestCase):
 def setUp(self):self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'x.sqlite';self.db=connect(str(self.path))
 def tearDown(self):self.db.close();self.tmp.cleanup()
 def step(self,sub,**x):return execute(self.db,sub,x)
 def fixture(self,sub,name):
  xs=json.loads((ROOT/'fixtures'/name).read_text(encoding='utf-8'))
  return [execute(self.db,sub,x) for x in xs]
 def base(self):self.fixture('tenure','tenure_normal.json');self.fixture('cleanup','cleanup_normal.json')
 def test_namespace_recreation_invalidates_lease(self):
  self.base();self.step('tenure',op='namespace',id='ns',uid='ns-u2',generation=2)
  self.assertEqual(self.step('tenure',op='write',id='w2',namespace='ns',team='team-a',generation=2,resource='r2',at=20)['decision'],'write_denied')
 def test_unapproved_holder_takeover_is_rejected(self):
  self.fixture('tenure','tenure_normal.json')
  self.assertEqual(self.step('tenure',op='lease',id='stolen',namespace='ns',namespace_uid='ns-u1',holder='team-b',generation=3,start=20,expires=100)['decision'],'holder_takeover_denied')
 def test_two_role_renewal_and_freeze(self):
  self.fixture('tenure','tenure_normal.json')
  self.assertEqual(self.step('tenure',op='renew',id='lease-a',generation=3,at=20,expires=90)['decision'],'renewed')
  self.step('tenure',op='freeze',id='f1',namespace='ns',generation=1,reason='owner-left')
  self.assertEqual(self.step('tenure',op='write',id='w2',namespace='ns',team='team-a',generation=3,resource='r2',at=21)['decision'],'write_denied')
 def test_transfer_snapshot_blocks_new_resource(self):
  self.base();self.step('transfer',op='propose',id='t',namespace='ns',from_team='team-a',to_team='team-b',at=12,deadline=40)
  self.step('cleanup',op='resource',id='r2',namespace='ns',fingerprint='fp2',generation=1,owner='team-a',at=13)
  self.step('transfer',op='accept',id='t',receipt='a1',team='team-a',actor='alice',role='outgoing',at=13)
  self.step('transfer',op='accept',id='t',receipt='a2',team='team-b',actor='bob',role='incoming',at=14)
  self.step('transfer',op='accept',id='t',receipt='a3',team='platform',actor='operator',role='platform',at=14)
  for kind in ('billing','alert'):
   self.step('transfer',op='liability',id=kind,transfer='t',kind=kind,amount=1)
   self.step('transfer',op='accept_liability',id=kind,receipt='lr-'+kind,team='team-b',actor='bob',at=15)
  self.step('transfer',op='escrow',id='t',at=15)
  self.assertEqual(self.step('transfer',op='complete',id='t',at=16)['decision'],'inventory_changed')
 def test_liability_and_distinct_acceptors(self):
  self.base();self.step('transfer',op='propose',id='t',namespace='ns',from_team='team-a',to_team='team-b',at=12,deadline=40)
  self.step('transfer',op='liability',id='l',transfer='t',kind='billing',amount=100)
  self.step('transfer',op='accept',id='t',receipt='a1',team='team-a',actor='same',role='outgoing',at=13)
  self.step('transfer',op='accept',id='t',receipt='a2',team='team-b',actor='same',role='incoming',at=14)
  self.assertEqual(self.step('transfer',op='escrow',id='t',at=15)['decision'],'missing_parties')
  self.assertEqual(one(self.db,'SELECT status FROM liabilities WHERE id=?',('l',))['status'],'pending')
 def test_transfer_changes_lease_holder_and_resource_owner(self):
  self.base();self.assertEqual(self.fixture('transfer','transfer_normal.json')[-1]['decision'],'transferred')
  self.assertEqual(one(self.db,'SELECT owner FROM resources WHERE id=?',('r1',))['owner'],'team-b')
  self.assertEqual(self.step('tenure',op='write',id='w2',namespace='ns',team='team-a',generation=3,resource='r2',at=20)['decision'],'write_denied')
  self.assertEqual(self.step('tenure',op='write',id='w3',namespace='ns',team='team-b',generation=3,resource='r2',at=20)['decision'],'written')
  self.assertEqual(self.step('tenure',op='renew',id='lease-a',generation=4,at=21,expires=90)['decision'],'missing_two_approvals')
 def test_platform_and_liability_receipts_are_required(self):
  self.base();self.step('transfer',op='propose',id='t',namespace='ns',from_team='team-a',to_team='team-b',at=12,deadline=40)
  self.step('transfer',op='accept',id='t',receipt='a1',team='team-a',actor='alice',role='outgoing',at=13)
  self.step('transfer',op='accept',id='t',receipt='a2',team='team-b',actor='bob',role='incoming',at=14)
  self.step('transfer',op='liability',id='l',transfer='t',kind='billing',amount=100)
  self.assertEqual(self.step('transfer',op='accept_liability',id='l',receipt='bad',team='team-a',actor='alice',at=15)['decision'],'unauthorized_liability_acceptance')
  self.assertEqual(self.step('transfer',op='escrow',id='t',at=16)['decision'],'missing_parties')
 def test_third_team_orphan_claim_cannot_be_overwritten_by_transfer(self):
  self.base()
  self.step('transfer',op='propose',id='t',namespace='ns',from_team='team-a',to_team='team-b',at=12,deadline=40)
  for team,actor,role in [('team-a','alice','outgoing'),('team-b','bob','incoming'),('platform','operator','platform')]:
   self.step('transfer',op='accept',id='t',receipt='a-'+team,team=team,actor=actor,role=role,at=13)
  for kind in ('billing','alert'):
   self.step('transfer',op='liability',id=kind,transfer='t',kind=kind,amount=1)
   self.step('transfer',op='accept_liability',id=kind,receipt='lr-'+kind,team='team-b',actor='bob',at=14)
  self.step('transfer',op='escrow',id='t',at=15)
  self.step('cleanup',op='discover',id='o',resource='r1',namespace='ns')
  self.step('cleanup',op='proof',id='proof-c',resource='r1',team='team-c',generation=1,issuer='platform',valid_until=30,at=15)
  self.step('cleanup',op='claim',id='o',proof='proof-c',owner='team-c',at=16)
  self.assertEqual(self.step('transfer',op='complete',id='t',at=16)['decision'],'inventory_changed')
 def test_orphan_and_finalizer_block_closure(self):
  self.base();self.fixture('transfer','transfer_normal.json')
  self.step('cleanup',op='discover',id='o',resource='r1',namespace='ns')
  self.assertEqual(self.step('cleanup',op='close',id='c',namespace='ns',uid='ns-u1',at=80)['decision'],'cleanup_incomplete')
  self.assertEqual(self.step('cleanup',op='claim',id='o',proof='wrong',owner='team-b',at=70)['decision'],'invalid_ownership_proof')
  self.step('cleanup',op='proof',id='proof-b',resource='r1',team='team-b',generation=1,issuer='platform',valid_until=100,at=70)
  self.assertEqual(self.step('cleanup',op='claim',id='o',proof='proof-b',owner='team-b',at=71)['decision'],'claimed')
  self.assertEqual(self.step('cleanup',op='delete',id='d',resource='r1',generation=1,at=81)['decision'],'deleted')
  self.assertEqual(self.step('cleanup',op='close',id='c',namespace='ns',uid='ns-u1',at=82)['decision'],'closed')
 def test_all_fixtures_and_replay(self):
  for sub,name in [('tenure','tenure_normal.json'),('cleanup','cleanup_normal.json'),('transfer','transfer_normal.json'),('tenure','tenure_failure.json'),('transfer','transfer_failure.json'),('cleanup','cleanup_failure.json')]:self.fixture(sub,name)
  self.assertTrue(replay(str(self.path))['verified'])
 def test_deleted_resource_reappearance_blocks_close(self):
  self.base();self.fixture('transfer','transfer_normal.json')
  self.step('cleanup',op='delete',id='d',resource='r1',generation=1,at=70)
  self.assertEqual(self.step('cleanup',op='reappear',id='r1',generation=2,fingerprint='fp2')['decision'],'quarantined_reappearance')
  self.assertEqual(self.step('cleanup',op='close',id='c',namespace='ns',uid='ns-u1',at=80)['decision'],'cleanup_incomplete')
if __name__=='__main__':unittest.main()
