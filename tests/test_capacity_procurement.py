import tempfile
import threading
import unittest
from pathlib import Path

from tenurecourt.store import connect, execute, replay, one, rows
from tenurecourt.reservation_note import provisional_capacity
from tenurecourt import capacity_procurement as cp


class CapacityProcurementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "procurement.sqlite")
        self.db = connect(self.path)
        self.seq = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def step(self, **x):
        return execute(self.db, "procurement", x)

    def nid(self, prefix):
        self.seq += 1
        return "%s-%d" % (prefix, self.seq)

    # ------------------------------------------------------------------
    # Inventory construction helpers
    # ------------------------------------------------------------------
    def register_device(self, device_id, rack, fd, serial, title, fp, at=1):
        return self.step(
            op="register_device", id=device_id, rack=rack, serial=serial,
            upstream_title=title, failure_domain=fd, fingerprint=fp, at=at,
        )

    def register_slots(self, device_id, prefix, count):
        device = one(self.db, "SELECT * FROM cp_devices WHERE device_id=?", (device_id,))
        ids = []
        for i in range(1, count + 1):
            sid = "%s-%02d" % (prefix, i)
            r = self.step(op="register_slot", id=sid, device_id=device_id,
                          position="p%02d" % i)
            self.assertEqual(r["decision"], "slot_registered")
            ids.append(sid)
        self.assertEqual(len(ids), count)
        return ids

    def verify_offer(self, offer, vendor, rack, ids, title, expiry=1000, at=2):
        return self.step(
            op="verify_offer", offer=offer, vendor=vendor, rack=rack,
            slots=len(ids), expiry=expiry, upstream_title=title,
            slot_ids=ids, at=at,
        )

    def reserve(self, order_id, slot_id, start=10, expires=500, at=10,
                source="new", migration_id=None):
        rid = self.nid("res")
        payload = dict(op="reserve", reservation_id=rid, order_id=order_id,
                       slot_id=slot_id, lease_start=start, lease_expires=expires, at=at,
                       source=source)
        if migration_id:
            payload["migration_id"] = migration_id
        return self.step(**payload), rid

    def deliver(self, rid, slot_id, device_id, fp, at=11, token=None, test_id=None):
        token = token or "write-%s" % rid
        test_id = test_id or "test-%s" % rid
        return self.step(
            op="delivery_test", test_id=test_id, reservation_id=rid,
            slot_id=slot_id, device_id=device_id, measured_fingerprint=fp,
            write_token=token, readback="read-%s" % rid, at=at,
        )

    def build_resold_inventory(self):
        """Two vendors reselling the same 20-position managed rack (fd-1)."""
        # Quotes enter through the event stream so replay reconstructs them.
        self.step(op="record_quote", offer="offer-1", rack="rack-a",
                  slots=20, expiry=1000)
        self.step(op="record_quote", offer="offer-2", rack="rack-a",
                  slots=20, expiry=1000)
        # The reservation note still adds paper quotes: 20 + 20 = 40.
        self.assertEqual(provisional_capacity(self.db, "rack-a"), 40)
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 20)
        return ids

    # ------------------------------------------------------------------
    # Starting point: duplicate resale cannot be counted twice
    # ------------------------------------------------------------------
    def test_duplicate_resale_quotes_collapse_to_physical_slots(self):
        ids = self.build_resold_inventory()

        first = self.verify_offer("offer-1", "vendor-1", "rack-a", ids,
                                  "upstream-landlord")
        self.assertEqual(first["decision"], "offer_verified")
        self.assertEqual(first["distinct_slots"], 20)
        self.assertEqual(cp.effective_capacity(self.db, "rack-a"), 20)

        second = self.verify_offer("offer-2", "vendor-2", "rack-a", ids,
                                   "upstream-landlord")
        self.assertEqual(second["decision"], "resold_capacity_duplicate")
        self.assertEqual(second["duplicate_slots"], ids)
        # Effective capacity stays at the physical number, never the sum (40).
        self.assertEqual(cp.effective_capacity(self.db, "rack-a"), 20)
        self.assertEqual(
            one(self.db, "SELECT decision FROM cp_offer_checks WHERE offer='offer-2'")["decision"],
            "duplicate",
        )

    def test_paper_quote_sum_never_drives_a_three_replica_lease(self):
        ids = self.build_resold_inventory()
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.verify_offer("offer-2", "vendor-2", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-new", customer="new-customer",
                  required_copies=3, at=5)
        # Only one independent failure domain physically exists.
        r, rid = self.reserve("ord-new", ids[0])
        self.assertEqual(r["decision"], "reserved")
        r, _ = self.reserve("ord-new", ids[1])
        self.assertEqual(r["decision"], "failure_domain_not_independent")
        activation = self.step(op="activate_order", order_id="ord-new",
                               proof_id="proof-early", at=12)
        self.assertEqual(activation["decision"], "replicas_not_ready")
        self.assertIn("replica_count_1_of_3", activation["problems"])
        # No lease/activation proof may exist before the hardware is there.
        self.assertIsNone(one(
            self.db,
            "SELECT 1 FROM cp_activation_proofs WHERE order_id='ord-new'",
        ))

    # ------------------------------------------------------------------
    # Contract outcome A: delay activation with agreed compensation
    # ------------------------------------------------------------------
    def test_contract_path_a_delay_activation_pays_only_delivered_capacity(self):
        ids = self.build_resold_inventory()
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.verify_offer("offer-2", "vendor-2", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-delay", customer="new-customer",
                  required_copies=3, at=5)

        deferred = self.step(
            op="defer_order", order_id="ord-delay", liability_id="liab-delay",
            compensation=300, target_date=120, at=6,
        )
        self.assertEqual(deferred["decision"], "order_deferred")
        liab = one(self.db, "SELECT * FROM cp_procurement_liabilities WHERE liability_id='liab-delay'")
        self.assertEqual(liab["kind"], "delay_compensation")
        self.assertEqual(liab["status"], "owed")
        self.assertIsNone(one(
            self.db,
            "SELECT 1 FROM cp_activation_proofs WHERE order_id='ord-delay'",
        ))

        # Vendor cannot be paid on the duplicated, ineffective resale offer,
        # even if a reservation id is supplied that does not back the slot.
        pay = self.step(
            op="pay_vendor", payment_id="pay-bad", offer="offer-2",
            slot_id="rack-a-01", reservation_id="res-never-existed",
            amount=100, at=8,
        )
        self.assertEqual(pay["decision"], "offer_not_effective")
        # A lump-sum refund against the summed over-number is contractually barred.
        self.assertEqual(self.step(op="lump_refund", at=8)["decision"],
                         "lump_sum_refund_rejected")

        # Later the two genuinely independent rooms are delivered and tested.
        self.register_device("dev-b", "rack-b", "fd-2", "SN-B",
                             "upstream-landlord", "fp-B")
        b_ids = self.register_slots("dev-b", "rack-b", 5)
        self.register_device("dev-c", "rack-c", "fd-3", "SN-C",
                             "upstream-landlord", "fp-C")
        c_ids = self.register_slots("dev-c", "rack-c", 5)
        self.verify_offer("offer-3", "vendor-1", "rack-b", b_ids, "upstream-landlord", at=20)
        self.verify_offer("offer-4", "vendor-3", "rack-c", c_ids, "upstream-landlord", at=20)

        picks = [("rack-a-01", "dev-a", "fp-A", "offer-1"),
                 ("rack-b-01", "dev-b", "fp-B", "offer-3"),
                 ("rack-c-01", "dev-c", "fp-C", "offer-4")]
        reservation_ids = []
        for slot_id, device_id, fp, offer in picks:
            r, rid = self.reserve("ord-delay", slot_id, at=30)
            self.assertEqual(r["decision"], "reserved")
            self.assertEqual(self.deliver(rid, slot_id, device_id, fp, at=31)["decision"],
                             "delivery_passed")
            reservation_ids.append((rid, slot_id, offer))
            self.assertEqual(self.step(
                op="pay_vendor", payment_id="pay-%s" % rid, offer=offer,
                slot_id=slot_id, reservation_id=rid, amount=100, at=32,
            )["decision"], "vendor_paid")

        activated = self.step(op="activate_order", order_id="ord-delay",
                              proof_id="proof-delay", at=33)
        self.assertEqual(activated["decision"], "activated")
        # Service cannot start before all three replicas actually persisted.
        self.assertGreaterEqual(activated["service_start"], 31)
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_orders WHERE order_id='ord-delay'")["status"],
            "active",
        )
        self.assertEqual(
            rows(self.db, "SELECT amount FROM cp_vendor_payments WHERE order_id='ord-delay'"),
            [{"amount": 100}, {"amount": 100}, {"amount": 100}],
        )

    # ------------------------------------------------------------------
    # Contract outcome B: verifiable migration, then deliver the freed slot
    # ------------------------------------------------------------------
    def test_contract_path_b_migration_frees_independent_slot_for_new_customer(self):
        ids = self.build_resold_inventory()
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.verify_offer("offer-2", "vendor-2", "rack-a", ids, "upstream-landlord")

        # The low-priority customer already occupies fd-1 and fd-2.
        self.register_device("dev-b", "rack-b", "fd-2", "SN-B",
                             "upstream-landlord", "fp-B")
        b_ids = self.register_slots("dev-b", "rack-b", 5)
        self.register_device("dev-c", "rack-c", "fd-3", "SN-C",
                             "upstream-landlord", "fp-C")
        c_ids = self.register_slots("dev-c", "rack-c", 5)
        self.verify_offer("offer-3", "vendor-1", "rack-b", b_ids, "upstream-landlord", at=3)
        self.verify_offer("offer-4", "vendor-3", "rack-c", c_ids, "upstream-landlord", at=3)

        self.step(op="place_order", order_id="ord-old", customer="low-priority",
                  required_copies=2, at=4)
        old_res = {}
        for slot_id, device_id, fp, offer in [
            ("rack-a-03", "dev-a", "fp-A", "offer-1"),
            ("rack-b-03", "dev-b", "fp-B", "offer-3"),
        ]:
            r, rid = self.reserve("ord-old", slot_id, at=10)
            self.assertEqual(r["decision"], "reserved")
            self.assertEqual(self.deliver(rid, slot_id, device_id, fp)["decision"],
                             "delivery_passed")
            old_res[slot_id] = rid
        self.step(op="activate_order", order_id="ord-old", proof_id="proof-old", at=12)
        self.step(op="record_customer_write", write_id="w-old-before",
                  customer="low-priority", slot_id="rack-b-03", at=13)

        # New customer: fd-1 and fd-3 are available but fd-2 is occupied.
        self.step(op="place_order", order_id="ord-new", customer="new-customer",
                  required_copies=3, at=14)
        self.assertIn(
            "replicas_not_ready",
            self.step(op="activate_order", order_id="ord-new",
                      proof_id="nope", at=15)["decision"],
        )

        # Migration cannot be planned into the same failure domain.
        bad_plan = self.step(
            op="plan_migration", migration_id="mig-bad", order_id="ord-new",
            source_customer="low-priority", source_failure_domain="fd-2",
            target_failure_domain="fd-2", snapshot_id="snap-1",
            restore_deadline=80, liability_id="liab-bad",
            discount_amount=50, accepted_delay=True, at=16,
        )
        self.assertEqual(bad_plan["decision"], "failure_domain_not_independent")

        plan = self.step(
            op="plan_migration", migration_id="mig-1", order_id="ord-new",
            source_customer="low-priority", source_failure_domain="fd-2",
            target_failure_domain="fd-3", snapshot_id="snap-1",
            restore_deadline=80, liability_id="liab-mig",
            discount_amount=50, at=16,
        )
        self.assertEqual(plan["decision"], "migration_planned")

        # The new customer has not accepted the delay yet: readiness is blocked.
        early = self.step(op="migration_ready", migration_id="mig-1", at=30)
        self.assertEqual(early["decision"], "migration_blocked")
        self.assertIn("delay_not_accepted", early["reasons"])
        self.assertIn("snapshot_restore_unverified", early["reasons"])
        self.assertEqual(self.step(
            op="record_customer_delay_acceptance", migration_id="mig-1",
            accepted=False, at=31,
        )["decision"], "delay_declined")
        self.assertEqual(self.step(
            op="record_customer_delay_acceptance", migration_id="mig-1",
            accepted=True, at=32,
        )["decision"], "delay_acceptance_recorded")

        # Cutover stays blocked until snapshot restore SLA is met.
        blocked = self.step(op="migration_ready", migration_id="mig-1", at=40)
        self.assertEqual(blocked["decision"], "migration_blocked")
        self.assertNotIn("delay_not_accepted", blocked["reasons"])
        self.assertIn("snapshot_restore_unverified", blocked["reasons"])

        breach = self.step(op="confirm_snapshot_restore", migration_id="mig-1",
                           restored_at=95)
        self.assertEqual(breach["decision"], "snapshot_restore_sla_breach")
        self.assertEqual(self.step(
            op="confirm_snapshot_restore", migration_id="mig-1", restored_at=45,
        )["decision"], "snapshot_restore_verified")

        # Move the original customer onto an independent fd-3 slot.
        r, target_rid = self.reserve("ord-old", "rack-c-03", at=46,
                                     source="migration", migration_id="mig-1")
        self.assertEqual(r["decision"], "reserved")
        self.assertEqual(self.deliver(target_rid, "rack-c-03", "dev-c", "fp-C", at=47)
                         ["decision"], "delivery_passed")
        ready = self.step(op="migration_ready", migration_id="mig-1", at=48)
        self.assertEqual(ready["decision"], "migration_ready")
        cutover = self.step(op="cutover", migration_id="mig-1", at=50)
        self.assertEqual(cutover["decision"], "migration_cutover_complete")
        self.assertEqual(cutover["freed_slot"], "rack-b-03")
        # Migration discount liability settles only after the verified cutover.
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_procurement_liabilities WHERE liability_id='liab-mig'")["status"],
            "settled",
        )
        # The original customer's service and prior activation proof are retained.
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_orders WHERE order_id='ord-old'")["status"],
            "active",
        )
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_activation_proofs WHERE proof_id='proof-old'")["status"],
            "issued",
        )

        # Writing to the old replica position after migration is a detected stray.
        stray = self.step(
            op="record_customer_write", write_id="w-stray",
            customer="low-priority", slot_id="rack-b-03", at=55,
        )
        self.assertEqual(stray["decision"], "stale_replica_write_detected")
        self.assertEqual(len(cp.stray_writes(self.db, "mig-1")), 1)
        # The new target replica still accepts the original customer's writes.
        self.assertEqual(self.step(
            op="record_customer_write", write_id="w-ok", customer="low-priority",
            slot_id="rack-c-03", at=56,
        )["decision"], "customer_write_recorded")

        # The freed fd-2 slot now serves the new customer's third replica.
        new_picks = [("rack-a-02", "dev-a", "fp-A", "offer-1"),
                     ("rack-b-03", "dev-b", "fp-B", "offer-3"),
                     ("rack-c-02", "dev-c", "fp-C", "offer-4")]
        for slot_id, device_id, fp, offer in new_picks:
            r, rid = self.reserve("ord-new", slot_id, at=60)
            self.assertEqual(r["decision"], "reserved")
            self.assertEqual(self.deliver(rid, slot_id, device_id, fp, at=61)
                             ["decision"], "delivery_passed")
            self.assertEqual(self.step(
                op="pay_vendor", payment_id="pay-new-%s" % rid, offer=offer,
                slot_id=slot_id, reservation_id=rid, amount=100, at=62,
            )["decision"], "vendor_paid")
        activated = self.step(op="activate_order", order_id="ord-new",
                              proof_id="proof-new", at=63)
        self.assertEqual(activated["decision"], "activated")
        self.assertGreaterEqual(activated["service_start"], 61)

        # The two customers have distinct real write positions.
        old_slots = {r["slot_id"] for r in rows(
            self.db,
            "SELECT slot_id FROM cp_reservations WHERE order_id='ord-old' AND status='active'",
        )}
        new_slots = {r["slot_id"] for r in rows(
            self.db,
            "SELECT slot_id FROM cp_reservations WHERE order_id='ord-new' AND status='active'",
        )}
        self.assertTrue(old_slots.isdisjoint(new_slots))
        self.assertEqual(len(new_slots), 3)

        # The whole two-customer migration history reconstructs identically.
        result = replay(self.path)
        self.assertTrue(result["verified"], result["errors"])

    # ------------------------------------------------------------------
    # Same physical device cannot be reintroduced under a new serial label
    # ------------------------------------------------------------------
    def test_same_measurement_under_new_serial_label_is_rejected(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        relabel = self.step(
            op="register_device", id="dev-a2", rack="rack-a2", serial="SN-A-NEW",
            upstream_title="upstream-landlord", failure_domain="fd-9",
            fingerprint="fp-A", at=5,
        )
        self.assertEqual(relabel["decision"], "device_identity_reused")
        # A genuinely different device (different measured identity) is fine.
        self.assertEqual(self.step(
            op="register_device", id="dev-b", rack="rack-b", serial="SN-B",
            upstream_title="upstream-landlord", failure_domain="fd-2",
            fingerprint="fp-B", at=6,
        )["decision"], "device_registered")

    def test_slot_identity_cannot_be_reused(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        self.register_slots("dev-a", "rack-a", 1)
        again = self.step(op="register_slot", id="dup", device_id="dev-a", position="p01")
        self.assertEqual(again["decision"], "slot_identity_reused")

    def test_upstream_title_mismatch_blocks_offer_verification(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "real-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 2)
        r = self.step(
            op="verify_offer", offer="offer-x", vendor="vendor-1", rack="rack-a",
            slots=2, expiry=1000, upstream_title="different-landlord",
            slot_ids=ids, at=3,
        )
        self.assertEqual(r["decision"], "upstream_title_mismatch")
        self.assertEqual(cp.effective_capacity(self.db, "rack-a"), 0)

    # ------------------------------------------------------------------
    # Concurrent orders cannot grab the same physical slot
    # ------------------------------------------------------------------
    def test_concurrent_orders_cannot_preempt_the_same_slot(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 1)
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        for order_id in ("ord-1", "ord-2"):
            self.step(op="place_order", order_id=order_id,
                      customer="c-%s" % order_id, required_copies=1, at=5)

        outcomes = []

        errors = []

        def grab(order_id, rid):
            db = connect(self.path)
            try:
                r = execute(db, "procurement", {
                    "op": "reserve", "reservation_id": rid, "order_id": order_id,
                    "slot_id": "rack-a-01", "lease_start": 10, "lease_expires": 500,
                    "at": 10,
                })
                outcomes.append(r["decision"])
            except BaseException as exc:  # surface threading failures
                errors.append(exc)
            finally:
                db.close()

        t1 = threading.Thread(target=grab, args=("ord-1", "res-1"))
        t2 = threading.Thread(target=grab, args=("ord-2", "res-2"))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), ["reserved", "slot_already_reserved"])
        active = rows(self.db,
                      "SELECT * FROM cp_reservations WHERE slot_id='rack-a-01' AND status='active'")
        self.assertEqual(len(active), 1)

    # ------------------------------------------------------------------
    # Vendor temporarily withdraws an offer before activation
    # ------------------------------------------------------------------
    def test_offer_withdrawal_before_activation_revokes_and_reinstates(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 2)
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-w", customer="cust",
                  required_copies=1, at=5)
        r, rid = self.reserve("ord-w", "rack-a-01", at=10)
        self.assertEqual(r["decision"], "reserved")

        withdrawn = self.step(op="withdraw_offer", offer="offer-1", at=20)
        self.assertEqual(withdrawn["decision"], "offer_withdrawn")
        self.assertEqual(withdrawn["revoked_reservations"], 1)
        # Activation and delivery are blocked while the offer is ineffective.
        self.assertEqual(
            self.step(op="activate_order", order_id="ord-w", proof_id="p", at=21)["decision"],
            "replicas_not_ready",
        )
        self.assertEqual(
            self.deliver(rid, "rack-a-01", "dev-a", "fp-A", at=22)["decision"],
            "reservation_not_active",
        )

        self.assertEqual(self.step(op="reinstate_offer", offer="offer-1", at=25)["decision"],
                         "offer_reinstated")
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_reservations WHERE reservation_id=?", (rid,))["status"],
            "active",
        )
        self.assertEqual(self.deliver(rid, "rack-a-01", "dev-a", "fp-A", at=26)["decision"],
                         "delivery_passed")

    # ------------------------------------------------------------------
    # Down payment already made, then the device fails
    # ------------------------------------------------------------------
    def test_device_failure_after_payment_keeps_proofs_and_opens_liability(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 2)
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-f", customer="cust",
                  required_copies=1, at=5)
        r, rid = self.reserve("ord-f", "rack-a-01", at=10)
        self.deliver(rid, "rack-a-01", "dev-a", "fp-A", at=11)
        self.assertEqual(self.step(
            op="pay_vendor", payment_id="pay-1", offer="offer-1",
            slot_id="rack-a-01", reservation_id=rid, amount=100, at=12,
        )["decision"], "vendor_paid")
        self.step(op="activate_order", order_id="ord-f", proof_id="proof-1", at=13)

        failure = self.step(op="record_device_failure", device_id="dev-a",
                            amount=100, at=40)
        self.assertEqual(failure["decision"], "device_failure_recorded")
        self.assertEqual(failure["impaired"], 1)
        self.assertEqual(failure["liabilities"], 1)
        # Original proof and paid down payment are retained, not recalculated.
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_activation_proofs WHERE proof_id='proof-1'")["status"],
            "issued",
        )
        self.assertEqual(
            one(self.db, "SELECT status, amount FROM cp_vendor_payments WHERE payment_id='pay-1'")["status"],
            "paid",
        )
        # Readiness drops because the replica lost its physical persistence.
        self.assertFalse(cp.readiness(self.db, "ord-f", 41)["ready"])

        # A relabelled same device cannot serve as the replacement.
        self.assertEqual(self.step(
            op="register_device", id="dev-fake", rack="rack-z", serial="SN-FAKE",
            upstream_title="upstream-landlord", failure_domain="fd-1",
            fingerprint="fp-A", at=42,
        )["decision"], "device_identity_reused")

        self.register_device("dev-a2", "rack-a2", "fd-1", "SN-A2",
                             "upstream-landlord", "fp-A2", at=43)
        new_slots = self.register_slots("dev-a2", "rack-a2", 1)
        self.verify_offer("offer-9", "vendor-1", "rack-a2", new_slots,
                          "upstream-landlord", at=44)
        self.assertEqual(self.step(
            op="replace_device", old_device_id="dev-a", new_device_id="dev-a2", at=45,
        )["decision"], "device_replacement_recorded")

        # A second failed device in another domain proves reprovisioning must
        # keep the replica inside its original independent failure domain.
        self.register_device("dev-other", "rack-o", "fd-7", "SN-O",
                             "upstream-landlord", "fp-O", at=47)
        other_slots = self.register_slots("dev-other", "rack-o", 1)
        self.verify_offer("offer-o", "vendor-1", "rack-o", other_slots,
                          "upstream-landlord", at=48)
        self.step(op="place_order", order_id="ord-f2", customer="cust-2",
                  required_copies=1, at=48)
        _, other_rid = self.reserve("ord-f2", "rack-o-01", at=48)
        self.deliver(other_rid, "rack-o-01", "dev-other", "fp-O", at=49,
                     test_id="test-other", token="write-other")
        self.step(op="record_device_failure", device_id="dev-other", at=50)
        wrong = self.step(
            op="reprovision_reservation", reservation_id=other_rid,
            new_reservation_id="res-wrong", new_slot_id="rack-a2-01",
            lease_start=46, lease_expires=900, at=51,
        )
        self.assertEqual(wrong["decision"], "failure_domain_changed")

        repro = self.step(
            op="reprovision_reservation", reservation_id=rid,
            new_reservation_id="res-repro", new_slot_id="rack-a2-01",
            lease_start=46, lease_expires=900, at=46,
        )
        self.assertEqual(repro["decision"], "reservation_reprovisioned")
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_reservations WHERE reservation_id=?", (rid,))["status"],
            "reprovisioned",
        )

        self.assertEqual(self.deliver("res-repro", "rack-a2-01", "dev-a2", "fp-A2",
                                      at=50, token="write-repro",
                                      test_id="test-repro")["decision"],
                         "delivery_passed")
        # No second down payment for the same replica; the rework liability settles.
        self.assertEqual(self.step(
            op="pay_vendor", payment_id="pay-2", offer="offer-9",
            slot_id="rack-a2-01", reservation_id="res-repro", amount=100, at=51,
        )["decision"], "replacement_payment_duplicate")
        liab_id = one(
            self.db,
            "SELECT liability_id FROM cp_procurement_liabilities WHERE kind='device_failure_rework'",
        )["liability_id"]
        self.assertEqual(self.step(
            op="settle_rework", liability_id=liab_id,
            reservation_id="res-repro", at=52,
        )["decision"], "rework_settled")
        self.assertTrue(cp.readiness(self.db, "ord-f", 53)["ready"])

    # ------------------------------------------------------------------
    # Activation proof correction preserves the original document
    # ------------------------------------------------------------------
    def test_activation_proof_correction_keeps_history_and_records_liability(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 1)
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-c", customer="cust",
                  required_copies=1, at=5)
        r, rid = self.reserve("ord-c", "rack-a-01", at=10)
        self.deliver(rid, "rack-a-01", "dev-a", "fp-A", at=11)
        self.step(op="activate_order", order_id="ord-c", proof_id="proof-v1", at=13)

        # Upstream lease expiry before correction removes readiness.
        self.assertEqual(self.step(
            op="record_lease_expiry", reservation_id=rid,
            liability_id="liab-lease", amount=20, at=501,
        )["decision"], "lease_expired_recorded")
        self.assertIn(
            "replicas_not_ready",
            self.step(op="correct_activation_proof", order_id="ord-c",
                      proof_id="proof-v2", liability_id="liab-corr",
                      amount=10, reason="lease-gap", at=502)["decision"],
        )

        # Re-lease and re-test the same physical slot, then correct.
        self.assertEqual(self.step(
            op="renew_lease", reservation_id=rid, lease_expires=999, at=503,
        )["decision"], "lease_renewed")
        self.assertEqual(self.deliver(rid, "rack-a-01", "dev-a", "fp-A",
                                      at=504, token="write-renewed",
                                      test_id="test-renewed")["decision"],
                         "delivery_passed")
        corrected = self.step(
            op="correct_activation_proof", order_id="ord-c", proof_id="proof-v2",
            liability_id="liab-corr", amount=10, reason="lease-gap", at=505,
        )
        self.assertEqual(corrected["decision"], "activation_proof_corrected")
        self.assertEqual(corrected["old_proof_id"], "proof-v1")
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_activation_proofs WHERE proof_id='proof-v1'")["status"],
            "corrected",
        )
        self.assertEqual(
            one(self.db, "SELECT superseded_by FROM cp_activation_proofs WHERE proof_id='proof-v1'")["superseded_by"],
            "proof-v2",
        )
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_activation_proofs WHERE proof_id='proof-v2'")["status"],
            "issued",
        )
        self.assertEqual(
            one(self.db, "SELECT kind FROM cp_procurement_liabilities WHERE liability_id='liab-corr'")["kind"],
            "activation_correction",
        )

    # ------------------------------------------------------------------
    # Vendor withdrawal after activation keeps the proof and opens liability
    # ------------------------------------------------------------------
    def test_offer_withdrawal_after_activation_impairs_and_records_liability(self):
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 1)
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-w", customer="cust",
                  required_copies=1, at=5)
        r, rid = self.reserve("ord-w", "rack-a-01", at=10)
        self.deliver(rid, "rack-a-01", "dev-a", "fp-A", at=11)
        self.step(op="pay_vendor", payment_id="pay-1", offer="offer-1",
                  slot_id="rack-a-01", reservation_id=rid, amount=100, at=12)
        self.step(op="activate_order", order_id="ord-w", proof_id="proof-w", at=13)

        withdrawn = self.step(op="withdraw_offer", offer="offer-1",
                              amount=100, at=40)
        self.assertEqual(withdrawn["decision"], "offer_withdrawn")
        self.assertEqual(withdrawn["liabilities"], 1)
        self.assertEqual(withdrawn["revoked_reservations"], 0)
        # Readiness drops; the original activation proof and payment are retained.
        self.assertFalse(cp.readiness(self.db, "ord-w", 41)["ready"])
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_reservations WHERE reservation_id=?", (rid,))["status"],
            "impaired",
        )
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_activation_proofs WHERE proof_id='proof-w'")["status"],
            "issued",
        )
        self.assertEqual(
            one(self.db, "SELECT status FROM cp_vendor_payments WHERE payment_id='pay-1'")["status"],
            "paid",
        )
        liab = one(
            self.db,
            "SELECT * FROM cp_procurement_liabilities WHERE kind='offer_withdrawal'",
        )
        self.assertEqual(liab["status"], "owed")
        self.assertEqual(liab["amount"], 100)

    # ------------------------------------------------------------------
    # Event sourcing: every procurement decision replays to identical tables
    # ------------------------------------------------------------------
    def test_full_procurement_history_replays(self):
        # Quotes go through the event stream so replay rebuilds them too.
        self.step(op="record_quote", offer="offer-1", rack="rack-a",
                  slots=20, expiry=1000)
        self.step(op="record_quote", offer="offer-2", rack="rack-a",
                  slots=20, expiry=1000)
        self.register_device("dev-a", "rack-a", "fd-1", "SN-A",
                             "upstream-landlord", "fp-A")
        ids = self.register_slots("dev-a", "rack-a", 20)
        self.verify_offer("offer-1", "vendor-1", "rack-a", ids, "upstream-landlord")
        self.verify_offer("offer-2", "vendor-2", "rack-a", ids, "upstream-landlord")
        self.step(op="place_order", order_id="ord-r", customer="cust",
                  required_copies=1, at=5)
        r, rid = self.reserve("ord-r", ids[4], at=10)
        self.deliver(rid, ids[4], "dev-a", "fp-A", at=11)
        self.step(op="pay_vendor", payment_id="pay-r", offer="offer-1",
                  slot_id=ids[4], reservation_id=rid, amount=100, at=12)
        self.step(op="activate_order", order_id="ord-r", proof_id="proof-r", at=13)
        result = replay(self.path)
        self.assertTrue(result["verified"], result["errors"])


if __name__ == "__main__":
    unittest.main()
