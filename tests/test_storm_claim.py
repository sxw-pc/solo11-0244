import unittest

from tenurecourt.store import connect, one
from tenurecourt import tenure
from tenurecourt.damage_report import quote as legacy_quote
from tenurecourt.claim_estimate import provisional_claim
from tenurecourt.storm_claim import run, claim_estimate, ledger, balances, dossier


class StormClaimTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(':memory:')

    def tearDown(self):
        self.db.close()

    def site(self, site='site-1', storm_ded=10, neglect_ded=25, maintained=1, pre='sound', weather=100, damage_at=101, filed=100):
        run(self.db, {'op': 'policy', 'id': 'p-' + site, 'site': site, 'storm_deductible': storm_ded, 'neglect_deductible': neglect_ded})
        run(self.db, {'op': 'component', 'id': 'c-' + site, 'site': site, 'name': 'roof-north', 'pre_condition': pre, 'maintained': maintained, 'at': 90})
        if weather is not None:
            run(self.db, {'op': 'weather', 'id': 'w-' + site, 'site': site, 'kind': 'rainstorm', 'at': weather, 'filed': filed})
        run(self.db, {'op': 'damage', 'id': 'd-' + site, 'component': 'c-' + site, 'scope': 'roof membrane 20m2', 'at': damage_at})

    def lease(self, lease='L1', holder='cust-a', start=90, expires=200):
        tenure.run(self.db, {'op': 'namespace', 'id': 'ns-' + lease, 'uid': 'u-' + lease, 'generation': 1})
        tenure.run(self.db, {'op': 'lease', 'id': lease, 'namespace': 'ns-' + lease, 'namespace_uid': 'u-' + lease,
                             'holder': holder, 'generation': 1, 'start': start, 'expires': expires})

    def award(self, item, damage, kind, contractor, amount, **extra):
        x = {'op': 'item', 'id': item, 'damage': damage, 'kind': kind}
        x.update(extra)
        self.assertEqual(run(self.db, x)['decision'], 'created')
        self.assertEqual(run(self.db, {'op': 'quote', 'id': 'q-' + item, 'item': item, 'contractor': contractor,
                                       'amount': amount, 'at': 102})['decision'], 'quoted')
        self.assertEqual(run(self.db, {'op': 'award', 'id': 'q-' + item, 'at': 102})['decision'], 'awarded')

    def finish(self, item, contractor, at=103):
        self.assertEqual(run(self.db, {'op': 'start', 'item': item, 'contractor': contractor, 'at': at})['decision'], 'started')
        self.assertEqual(run(self.db, {'op': 'complete', 'item': item, 'contractor': contractor, 'at': at + 1})['decision'], 'completed')
        self.assertEqual(run(self.db, {'op': 'accept', 'id': 'ac-' + item, 'item': item, 'result': 'passed', 'at': at + 2})['decision'], 'accepted')

    def test_competing_quotes_do_not_inflate_claim(self):
        # legacy estimator: a second quote for the same roof doubles the claim
        legacy_quote(self.db, 'q1', 'site-1', 'contractor-a', 31)
        self.assertEqual(provisional_claim(self.db, 'site-1'), 31)
        legacy_quote(self.db, 'q2', 'site-1', 'contractor-b', 31)
        self.assertEqual(provisional_claim(self.db, 'site-1'), 62)
        # adjudicated model: competing quotes select a contractor, never add up
        self.site()
        run(self.db, {'op': 'item', 'id': 'i-rep', 'damage': 'd-site-1', 'kind': 'repair'})
        run(self.db, {'op': 'quote', 'id': 'sq1', 'item': 'i-rep', 'contractor': 'contractor-a', 'amount': 31, 'at': 102})
        self.assertEqual(claim_estimate(self.db, 'site-1'), 31)
        run(self.db, {'op': 'quote', 'id': 'sq2', 'item': 'i-rep', 'contractor': 'contractor-b', 'amount': 31, 'at': 102})
        self.assertEqual(claim_estimate(self.db, 'site-1'), 31)
        run(self.db, {'op': 'award', 'id': 'sq1', 'at': 103})
        self.assertEqual(claim_estimate(self.db, 'site-1'), 31)
        self.assertEqual(one(self.db, 'SELECT status FROM storm_quotes WHERE id=?', ('sq2',))['status'], 'rejected')
        self.assertEqual(ledger(self.db, 'site-1'), {'repair': 31})

    def test_temporary_protection_then_inspected_repair(self):
        self.site()
        self.lease()
        self.assertEqual(run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})['decision'], 'assessed')
        # emergency tarping is allowed before any inspection as loss mitigation
        self.award('i-tarp', 'd-site-1', 'tarping', 'tarp-co', 5)
        self.finish('i-tarp', 'tarp-co')
        # affected customer relocated for the lease period proven by tenure
        self.award('i-reloc', 'd-site-1', 'relocation', 'move-co', 8, lease='L1', customer='cust-a', from_ts=101, to_ts=120)
        self.finish('i-reloc', 'move-co')
        # permanent repair is the irreversible step: blocked before inspection
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        self.assertEqual(run(self.db, {'op': 'start', 'item': 'i-rep', 'contractor': 'roof-co', 'at': 104})['decision'],
                         'inspection_or_notarization_required')
        self.assertEqual(run(self.db, {'op': 'inspect', 'id': 'insp-1', 'site': 'site-1', 'adjuster': 'adj',
                                       'verdict': 'confirmed', 'at': 105})['decision'], 'inspected')
        # after inspection the repair must go to an insurer-approved contractor
        self.assertEqual(run(self.db, {'op': 'start', 'item': 'i-rep', 'contractor': 'roof-co', 'at': 106})['decision'],
                         'contractor_not_approved')
        run(self.db, {'op': 'approve_contractor', 'id': 'ap-1', 'site': 'site-1', 'contractor': 'roof-co', 'at': 106})
        self.finish('i-rep', 'roof-co', at=107)
        # equipment salvage value is its own ledger and offsets the loss
        self.award('i-salv', 'd-site-1', 'salvage', 'salv-co', 2)
        self.finish('i-salv', 'salv-co')
        r = run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 110})
        # basis = tarp 5 + repair 31 + relocation 8 - salvage 2 = 42, storm deductible 10
        self.assertEqual(r, {'decision': 'settled', 'insurer': 32, 'operator': 10})
        self.assertEqual(run(self.db, {'op': 'settle', 'id': 's2', 'site': 'site-1', 'at': 111})['decision'], 'already_settled')
        # every party gets its own voucher
        self.assertEqual(run(self.db, {'op': 'advance', 'id': 'adv-1', 'item': 'i-tarp', 'at': 111})['decision'], 'advanced')
        self.assertEqual(run(self.db, {'op': 'advance', 'id': 'adv-2', 'item': 'i-rep', 'at': 111})['decision'], 'advanced')
        self.assertEqual(run(self.db, {'op': 'receipt', 'id': 'rc-1', 'advance': 'adv-1', 'at': 112})['decision'], 'received')
        self.assertEqual(run(self.db, {'op': 'receipt', 'id': 'rc-2', 'advance': 'adv-2', 'at': 112})['decision'], 'received')
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-1', 'site': 'site-1', 'amount': 32, 'at': 113})['decision'], 'paid')
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-2', 'site': 'site-1', 'amount': 1, 'at': 113})['decision'],
                         'exceeds_settlement')
        self.assertEqual(run(self.db, {'op': 'compensate', 'id': 'cp-1', 'item': 'i-reloc', 'amount': 8, 'at': 114})['decision'],
                         'compensated')
        b = balances(self.db, 'site-1')
        self.assertEqual(b['insurer_net'], 32)
        self.assertEqual(b['operator_advance'], 36)
        self.assertEqual(b['contractor_net'], 36)
        self.assertEqual(b['customer_compensation'], 8)
        # operator keeps the salvage 2, so its uncovered loss is the deductible: 36 + 8 - 32 - 2 = 10
        self.assertEqual(b['operator_advance'] + b['customer_compensation'] - b['insurer_net'] - 2, 10)
        self.assertEqual(ledger(self.db, 'site-1'), {'tarping': 5, 'repair': 31, 'relocation': 8, 'salvage': 2})
        d = dossier(self.db, 'd-site-1')
        self.assertEqual(d['component']['pre_condition'], 'sound')
        self.assertEqual(d['damage']['cause'], 'storm')
        self.assertEqual(len(d['weather']), 1)
        self.assertTrue(all(i['status'] == 'accepted' for i in d['items']))

    def test_notarized_emergency_repair_before_inspection(self):
        self.site()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        # no adjuster visit: notarize the damaged originals, then repair at once
        self.assertEqual(run(self.db, {'op': 'notarize', 'id': 'n1', 'damage': 'd-site-1', 'notary': 'notary-a', 'at': 102})['decision'],
                         'notarized')
        self.finish('i-rep', 'roof-co')
        # the scene is gone: neither inspection nor notarization can be redone
        self.assertEqual(run(self.db, {'op': 'inspect', 'id': 'insp-late', 'site': 'site-1', 'adjuster': 'adj',
                                       'verdict': 'confirmed', 'at': 110})['decision'], 'scene_altered')
        self.assertEqual(run(self.db, {'op': 'notarize', 'id': 'n2', 'damage': 'd-site-1', 'notary': 'notary-b', 'at': 110})['decision'],
                         'scene_altered')
        r = run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 111})
        self.assertEqual(r, {'decision': 'settled', 'insurer': 21, 'operator': 10})
        run(self.db, {'op': 'advance', 'id': 'adv-1', 'item': 'i-rep', 'at': 112})
        run(self.db, {'op': 'receipt', 'id': 'rc-1', 'advance': 'adv-1', 'at': 112})
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-1', 'site': 'site-1', 'amount': 21, 'at': 113})['decision'], 'paid')
        b = balances(self.db, 'site-1')
        self.assertEqual(b['insurer_net'], 21)
        self.assertEqual(b['contractor_net'], 31)

    def test_contractor_cannot_swap_work_item_midjob(self):
        self.site()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        self.award('i-a', 'd-site-1', 'repair', 'roof-co', 31)
        self.award('i-b', 'd-site-1', 'repair', 'other-co', 20)
        run(self.db, {'op': 'inspect', 'id': 'insp-1', 'site': 'site-1', 'adjuster': 'adj', 'verdict': 'confirmed', 'at': 105})
        for c in ('roof-co', 'other-co'):
            run(self.db, {'op': 'approve_contractor', 'id': 'ap-' + c, 'site': 'site-1', 'contractor': c, 'at': 105})
        # the awarded contractor drifts to a different work item
        self.assertEqual(run(self.db, {'op': 'start', 'item': 'i-b', 'contractor': 'roof-co', 'at': 106})['decision'],
                         'unauthorized_contractor')
        self.assertEqual(run(self.db, {'op': 'quote', 'id': 'q-extra', 'item': 'i-a', 'contractor': 'roof-co',
                                       'amount': 35, 'at': 106})['decision'], 'item_already_awarded')
        self.finish('i-a', 'roof-co', at=107)
        self.assertEqual(run(self.db, {'op': 'complete', 'item': 'i-b', 'contractor': 'roof-co', 'at': 108})['decision'],
                         'item_not_started')
        # money only moves against the awarded, accepted item
        self.assertEqual(run(self.db, {'op': 'advance', 'id': 'adv-b', 'item': 'i-b', 'at': 108})['decision'], 'item_not_accepted')
        self.assertEqual(run(self.db, {'op': 'advance', 'id': 'adv-a', 'item': 'i-a', 'at': 108})['decision'], 'advanced')
        self.assertEqual(balances(self.db, 'site-1')['operator_advance'], 31)

    def test_late_weather_evidence_forms_recovery_not_rewrite(self):
        self.site(weather=None)
        # no met record yet: causation cannot be established
        self.assertEqual(run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})['decision'],
                         'insufficient_causal_evidence')
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        run(self.db, {'op': 'inspect', 'id': 'insp-1', 'site': 'site-1', 'adjuster': 'adj', 'verdict': 'confirmed', 'at': 105})
        run(self.db, {'op': 'approve_contractor', 'id': 'ap-1', 'site': 'site-1', 'contractor': 'roof-co', 'at': 105})
        self.finish('i-rep', 'roof-co', at=106)
        r = run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 110})
        # cause unknown: none of the repair cost goes to the insurer
        self.assertEqual(r, {'decision': 'settled', 'insurer': 0, 'operator': 31})
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-0', 'site': 'site-1', 'amount': 1, 'at': 110})['decision'],
                         'exceeds_settlement')
        # the met record arrives late: it can only ground a recovery
        run(self.db, {'op': 'weather', 'id': 'w-late', 'site': 'site-1', 'kind': 'rainstorm', 'at': 100, 'filed': 111})
        self.assertEqual(run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 112})['decision'], 'assessed')
        self.assertEqual(run(self.db, {'op': 'recover', 'id': 'rec-1', 'damage': 'd-site-1', 'at': 113}),
                         {'decision': 'recovered', 'amount': 21})
        # the original settlement is untouched and no second payment can be drawn
        self.assertEqual(one(self.db, 'SELECT insurer FROM storm_settlements WHERE id=?', ('s1',))['insurer'], 0)
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-1', 'site': 'site-1', 'amount': 1, 'at': 114})['decision'],
                         'exceeds_settlement')
        self.assertEqual(run(self.db, {'op': 'recover', 'id': 'rec-2', 'damage': 'd-site-1', 'at': 115})['decision'],
                         'nothing_to_recover')
        b = balances(self.db, 'site-1')
        self.assertEqual(b['recovery'], 21)
        self.assertEqual(b['insurer_net'], 21)

    def test_adjuster_denial_leaves_costs_with_operator(self):
        self.site()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        self.assertEqual(run(self.db, {'op': 'inspect', 'id': 'insp-1', 'site': 'site-1', 'adjuster': 'adj',
                                       'verdict': 'denied', 'at': 105})['decision'], 'denied')
        # denied scene: no inspection route remains; operator repairs at its own cost
        self.assertEqual(run(self.db, {'op': 'start', 'item': 'i-rep', 'contractor': 'roof-co', 'at': 106})['decision'],
                         'inspection_or_notarization_required')
        run(self.db, {'op': 'notarize', 'id': 'n1', 'damage': 'd-site-1', 'notary': 'notary-a', 'at': 106})
        self.finish('i-rep', 'roof-co', at=107)
        r = run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 110})
        self.assertEqual(r, {'decision': 'settled', 'insurer': 0, 'operator': 31})
        run(self.db, {'op': 'advance', 'id': 'adv-1', 'item': 'i-rep', 'at': 111})
        run(self.db, {'op': 'receipt', 'id': 'rc-1', 'advance': 'adv-1', 'at': 112})
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-1', 'site': 'site-1', 'amount': 1, 'at': 113})['decision'],
                         'exceeds_settlement')
        b = balances(self.db, 'site-1')
        self.assertEqual(b['insurer_net'], 0)
        self.assertEqual(b['contractor_net'], 31)

    def test_post_settlement_denial_triggers_refund(self):
        self.site()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        run(self.db, {'op': 'notarize', 'id': 'n1', 'damage': 'd-site-1', 'notary': 'notary-a', 'at': 102})
        self.finish('i-rep', 'roof-co')
        run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 110})
        run(self.db, {'op': 'insurer_pay', 'id': 'pay-1', 'site': 'site-1', 'amount': 21, 'at': 111})
        # late review disputes the emergency necessity: paid money must come back
        self.assertEqual(run(self.db, {'op': 'deny', 'id': 'dn-1', 'damage': 'd-site-1', 'adjuster': 'adj',
                                       'reason': 'emergency_necessity_disputed', 'at': 112})['decision'], 'denied')
        self.assertEqual(run(self.db, {'op': 'recover', 'id': 'rec-1', 'damage': 'd-site-1', 'at': 113}),
                         {'decision': 'refund_due', 'amount': 21})
        self.assertEqual(run(self.db, {'op': 'refund', 'id': 'rf-1', 'damage': 'd-site-1', 'amount': 21, 'at': 114})['decision'],
                         'refunded')
        self.assertEqual(run(self.db, {'op': 'refund', 'id': 'rf-2', 'damage': 'd-site-1', 'amount': 1, 'at': 115})['decision'],
                         'refund_exceeds_payments')
        self.assertEqual(run(self.db, {'op': 'insurer_pay', 'id': 'pay-2', 'site': 'site-1', 'amount': 1, 'at': 116})['decision'],
                         'exceeds_settlement')
        self.assertEqual(balances(self.db, 'site-1')['insurer_net'], 0)

    def test_old_and_new_damage_keep_separate_causes_and_deductibles(self):
        self.site(maintained=0, pre='prior_damage', neglect_ded=25)
        # damage reported before the storm: the old neglected leak
        run(self.db, {'op': 'damage', 'id': 'd-old', 'component': 'c-site-1', 'scope': 'old seam leak 5m2', 'at': 90})
        self.assertEqual(run(self.db, {'op': 'assess', 'id': 'd-old', 'cause': 'storm', 'at': 101})['decision'],
                         'insufficient_causal_evidence')
        self.assertEqual(run(self.db, {'op': 'assess', 'id': 'd-old', 'cause': 'neglect', 'at': 101})['decision'], 'assessed')
        self.assertEqual(run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})['decision'], 'assessed')
        self.award('i-old', 'd-old', 'repair', 'roof-co', 40)
        self.award('i-new', 'd-site-1', 'repair', 'roof-co', 60)
        run(self.db, {'op': 'inspect', 'id': 'insp-1', 'site': 'site-1', 'adjuster': 'adj', 'verdict': 'confirmed', 'at': 105})
        run(self.db, {'op': 'approve_contractor', 'id': 'ap-1', 'site': 'site-1', 'contractor': 'roof-co', 'at': 105})
        self.finish('i-old', 'roof-co', at=106)
        self.finish('i-new', 'roof-co', at=106)
        r = run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 110})
        # old leak: 40 - neglect deductible 25 = 15; storm damage: 60 - storm deductible 10 = 50
        self.assertEqual(r, {'decision': 'settled', 'insurer': 65, 'operator': 35})
        causes = {l['damage']: l['cause'] for l in self.db.execute('SELECT * FROM storm_settlement_lines').fetchall()}
        self.assertEqual(causes, {'d-old': 'neglect', 'd-site-1': 'storm'})

    def test_customer_credit_is_not_claimed_twice(self):
        self.site()
        self.lease()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        self.award('i-reloc', 'd-site-1', 'relocation', 'move-co', 50, lease='L1', customer='cust-a', from_ts=101, to_ts=120)
        run(self.db, {'op': 'inspect', 'id': 'insp-1', 'site': 'site-1', 'adjuster': 'adj', 'verdict': 'confirmed', 'at': 105})
        run(self.db, {'op': 'approve_contractor', 'id': 'ap-1', 'site': 'site-1', 'contractor': 'roof-co', 'at': 105})
        self.finish('i-rep', 'roof-co', at=106)
        self.finish('i-reloc', 'move-co', at=106)
        # the customer already received a service credit for the same outage
        self.assertEqual(run(self.db, {'op': 'credit', 'id': 'cr-1', 'lease': 'L1', 'customer': 'cust-a',
                                       'amount': 20, 'from_ts': 101, 'to_ts': 120, 'at': 107})['decision'], 'credited')
        self.assertEqual(run(self.db, {'op': 'compensate', 'id': 'cp-1', 'item': 'i-reloc', 'amount': 35, 'at': 108})['decision'],
                         'credit_already_applied')
        self.assertEqual(run(self.db, {'op': 'compensate', 'id': 'cp-2', 'item': 'i-reloc', 'amount': 30, 'at': 108})['decision'],
                         'compensated')
        r = run(self.db, {'op': 'settle', 'id': 's1', 'site': 'site-1', 'at': 110})
        # customer side: 50 relocation - 20 credit already given = 30 claimable
        self.assertEqual(r, {'decision': 'settled', 'insurer': 51, 'operator': 10})

    def test_contractor_refund_reduces_contractor_balance(self):
        self.site()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        self.award('i-rep', 'd-site-1', 'repair', 'roof-co', 31)
        run(self.db, {'op': 'notarize', 'id': 'n1', 'damage': 'd-site-1', 'notary': 'notary-a', 'at': 102})
        self.finish('i-rep', 'roof-co')
        run(self.db, {'op': 'advance', 'id': 'adv-1', 'item': 'i-rep', 'at': 110})
        run(self.db, {'op': 'receipt', 'id': 'rc-1', 'advance': 'adv-1', 'at': 111})
        # overquote found after payment: the contractor returns the difference
        self.assertEqual(run(self.db, {'op': 'contractor_refund', 'id': 'crf-1', 'item': 'i-rep', 'amount': 6, 'at': 112})['decision'],
                         'refunded')
        self.assertEqual(run(self.db, {'op': 'contractor_refund', 'id': 'crf-2', 'item': 'i-rep', 'amount': 26, 'at': 113})['decision'],
                         'refund_exceeds_receipts')
        b = balances(self.db, 'site-1')
        self.assertEqual(b['contractor_receipt'], 31)
        self.assertEqual(b['contractor_net'], 25)

    def test_relocation_requires_active_customer_lease(self):
        self.site()
        run(self.db, {'op': 'assess', 'id': 'd-site-1', 'cause': 'storm', 'at': 101})
        # a lease proves who occupied the namespace when, not the rain damage
        r = run(self.db, {'op': 'item', 'id': 'i-reloc', 'damage': 'd-site-1', 'kind': 'relocation',
                          'lease': 'L-none', 'customer': 'cust-a', 'from_ts': 101, 'to_ts': 120})
        self.assertEqual(r['decision'], 'lease_not_active')


if __name__ == '__main__':
    unittest.main()
