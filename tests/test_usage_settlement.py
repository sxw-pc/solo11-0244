import sqlite3
import tempfile
import unittest
from pathlib import Path

from tenurecourt.store import one
from tenurecourt.usage_settlement import connect, report, replay, run


def seed_dual_usage(db):
    """Create the 600/600/400 fact pattern from the settlement contract."""
    run(db, {
        "op": "bind_namespace", "event_id": "ns-1",
        "namespace": "N", "uid": "uid-N-1", "generation": 1, "at": 1,
    })
    run(db, {
        "op": "register_resource", "event_id": "r-1",
        "resource": "r-main", "namespace": "N", "uid": "uid-N-1",
        "generation": 1, "owner": "platform", "at": 1,
    })

    common = {
        "namespace": "N", "uid": "uid-N-1", "resource": "r-main",
        "period": "2026-11", "counter_id": "counter-7",
        "counter_generation": 3, "persisted_from": 10, "persisted_to": 20,
        "vendor": "storage-vendor",
    }
    facts = [
        ("a", "seq-a", 600),
        ("b", "seq-b", 600),
        ("x", "seq-reused", 400),
    ]
    for key, seq, units in facts:
        usage_id = f"u-{key}"
        run(db, {
            **common, "op": "vendor_proof", "event_id": f"proof-{key}",
            "proof_id": f"proof-{key}", "usage_id": usage_id,
            "request_seq": seq, "units": units, "at": 21,
        })

    run(db, {
        **common, "op": "customer_signoff", "event_id": "sign-a-own",
        "signoff_id": "sign-a-own", "usage_id": "u-a",
        "request_seq": "seq-a", "customer": "A", "units": 600, "at": 22,
    })
    run(db, {
        **common, "op": "customer_signoff", "event_id": "sign-b-own",
        "signoff_id": "sign-b-own", "usage_id": "u-b",
        "request_seq": "seq-b", "customer": "B", "units": 600, "at": 22,
    })
    shared_a = {
        **common, "op": "customer_signoff", "event_id": "sign-x-a",
        "signoff_id": "sign-x-a", "usage_id": "u-x",
        "request_seq": "seq-reused", "customer": "A", "units": 400, "at": 22,
    }
    run(db, shared_a)
    # Exact gateway redelivery of A's candidate is the same journal command.
    run(db, dict(shared_a))
    run(db, {
        **common, "op": "customer_signoff", "event_id": "sign-x-b",
        "signoff_id": "sign-x-b", "usage_id": "u-x",
        "request_seq": "seq-reused", "customer": "B", "units": 400, "at": 22,
    })
    return run(db, {
        "op": "reconcile", "event_id": "reconcile-1", "case_id": "case-1",
        "usage_ids": ["u-a", "u-b", "u-x"], "at": 23,
    })


def customer_invoice_id(rep, customer, doc_type=None):
    for inv in rep["customers"][customer]["invoices"]:
        if doc_type is None or inv["type"] == doc_type:
            return inv["invoice_id"]
    raise AssertionError(f"no {doc_type} invoice for {customer}: {rep}")


def pay_open_debits(db, rep, prefix):
    payments = []
    for customer, view in rep["customers"].items():
        for inv in view["invoices"]:
            if inv["direction"] == "debit" and inv["balance"] > 0:
                pid = f"{prefix}-{customer}-{inv['invoice_id']}"
                result = run(db, {
                    "op": "pay_customer", "event_id": f"evt-{pid}",
                    "payment_id": pid, "customer": customer,
                    "invoice_id": inv["invoice_id"], "amount": inv["balance"],
                    "at": 50,
                })
                payments.append(result)
    return payments


class UsageSettlementTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "settlement.sqlite"
        self.db = connect(str(self.path))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_reconciliation_refuses_to_add_competing_claims(self):
        result = seed_dual_usage(self.db)
        self.assertEqual(result["decision"], "reconciled")
        self.assertEqual(result["physical_units"], 1600)
        self.assertEqual(result["undisputed_units"], 1200)
        self.assertEqual(result["disputed_units"], 400)
        self.assertEqual(result["claim_line_units"], 2000)
        self.assertEqual(result["extra_claim_units"], 400)
        self.assertTrue(report(self.db, "case-1")["verification"]["ok"])

    def test_platform_reserve_route_then_late_attribution(self):
        seed_dual_usage(self.db)
        settled = run(self.db, {
            "op": "settle_reserve", "event_id": "reserve-1",
            "settlement_id": "stl-reserve", "case_id": "case-1", "at": 30,
        })
        self.assertEqual(settled["decision"], "reserve_settled")
        self.assertEqual(settled["vendor_paid"], 1600)
        self.assertEqual(settled["invoiced_total"], 1200)
        self.assertEqual(settled["pending_recovery_amount"], 400)

        rep = report(self.db, "case-1")
        self.assertEqual(rep["vendor"]["paid"], 1600)
        self.assertEqual(rep["vendor"]["outstanding"], 0)
        self.assertEqual(rep["customers"]["A"]["invoiced"], 600)
        self.assertEqual(rep["customers"]["B"]["invoiced"], 600)
        self.assertEqual(len(rep["recovery_items"]), 1)
        self.assertFalse(rep["recovery_items"][0]["invoiced"])

        attributed = run(self.db, {
            "op": "attribute", "event_id": "proof-late-a",
            "proof_id": "proof-late-a", "case_id": "case-1",
            "usage_id": "u-x", "customer": "A", "evidence": "gateway metadata",
            "at": 40,
        })
        self.assertEqual(attributed["decision"], "attributed")
        final = run(self.db, {
            "op": "finalize_attribution", "event_id": "final-reserve",
            "finalization_id": "final-reserve", "case_id": "case-1", "at": 41,
        })
        self.assertEqual(final["decision"], "supplemental_charges_issued")
        self.assertEqual(final["amount"], 400)

        rep = report(self.db, "case-1")
        self.assertEqual(rep["customers"]["A"]["invoiced"], 1000)
        self.assertEqual(rep["customers"]["B"]["invoiced"], 600)
        self.assertTrue(rep["recovery_items"][0]["invoiced"])
        self.assertFalse(rep["recovery_items"][0]["collected"])

        pay_open_debits(self.db, rep, "pay-reserve")
        rep = report(self.db, "case-1")
        self.assertEqual(rep["customers"]["A"]["balance"], 0)
        self.assertEqual(rep["customers"]["B"]["balance"], 0)
        self.assertTrue(rep["recovery_items"][0]["collected"])
        self.assertEqual(rep["vendor"]["paid"], 1600)
        self.assertTrue(rep["verification"]["ok"])

        replayed = replay(self.db)
        self.assertTrue(replayed["verified"], replayed["errors"])

    def test_guarantee_route_issues_notices_then_bills_and_single_refund(self):
        seed_dual_usage(self.db)
        settled = run(self.db, {
            "op": "settle_guarantee", "event_id": "guarantee-settle",
            "settlement_id": "stl-guarantee", "case_id": "case-1",
            "guarantor": "surety-co", "at": 30,
        })
        self.assertEqual(settled["vendor_paid"], 1600)
        self.assertEqual(settled["platform_paid"], 1200)
        self.assertEqual(settled["guarantor_paid"], 400)
        self.assertFalse(settled["formal_invoices_issued"])

        rep = report(self.db, "case-1")
        self.assertEqual(rep["vendor"]["paid"], 1600)
        self.assertEqual(rep["customers"]["A"]["invoiced"], 0)
        self.assertEqual(rep["customers"]["B"]["invoiced"], 0)
        self.assertEqual(rep["customers"]["A"]["notices"][0]["confirmed_amount"], 600)
        self.assertEqual(rep["customers"]["A"]["notices"][0]["disputed_memo_amount"], 400)
        self.assertEqual(rep["guarantee"]["paid"], 400)
        self.assertEqual(rep["guarantee"]["outstanding"], 400)

        run(self.db, {
            "op": "attribute", "event_id": "attribution-b-event",
            "proof_id": "attribution-b", "case_id": "case-1",
            "usage_id": "u-x", "customer": "B", "at": 40,
        })
        final = run(self.db, {
            "op": "finalize_attribution", "event_id": "final-guarantee",
            "finalization_id": "final-guarantee", "case_id": "case-1", "at": 41,
        })
        self.assertEqual(final["decision"], "actual_bills_issued")
        rep = report(self.db, "case-1")
        self.assertEqual(rep["customers"]["A"]["invoiced"], 600)
        self.assertEqual(rep["customers"]["B"]["invoiced"], 1000)

        run(self.db, {
            "op": "pay_customer", "event_id": "pay-a", "payment_id": "pay-a",
            "customer": "A",
            "invoice_id": customer_invoice_id(rep, "A", "invoice"),
            "amount": 600, "at": 42,
        })
        run(self.db, {
            "op": "pay_customer", "event_id": "pay-b", "payment_id": "pay-b",
            "customer": "B",
            "invoice_id": customer_invoice_id(rep, "B", "invoice"),
            "amount": 1000, "at": 42,
        })
        refund = run(self.db, {
            "op": "refund_guarantee", "event_id": "refund-1",
            "refund_id": "refund-1", "case_id": "case-1", "at": 43,
        })
        self.assertEqual(refund["decision"], "guarantee_refunded")
        self.assertEqual(refund["amount"], 400)

        second = run(self.db, {
            "op": "refund_guarantee", "event_id": "refund-2",
            "refund_id": "refund-2", "case_id": "case-1", "at": 44,
        })
        self.assertEqual(second["decision"], "guarantee_refund_exceeded")

        rep = report(self.db, "case-1")
        self.assertEqual(rep["guarantee"]["refunded"], 400)
        self.assertEqual(rep["guarantee"]["outstanding"], 0)
        self.assertEqual(rep["customers"]["A"]["balance"], 0)
        self.assertEqual(rep["customers"]["B"]["balance"], 0)
        self.assertTrue(rep["verification"]["ok"])
        self.assertTrue(replay(self.db)["verified"])

    def test_namespace_rebuild_counter_reset_and_request_retry_do_not_duplicate_claim(self):
        run(self.db, {"op": "bind_namespace", "event_id": "ns1",
                      "namespace": "N", "uid": "uid-N-1"})
        fact = {
            "op": "vendor_proof", "event_id": "proof-1", "proof_id": "proof-1",
            "namespace": "N", "uid": "uid-N-1", "resource": "r1",
            "usage_id": "u1", "request_seq": "req-1",
            "counter_id": "c", "counter_generation": 1,
            "persisted_from": 100, "persisted_to": 101, "units": 10,
        }
        run(self.db, fact)
        sign = {
            "op": "customer_signoff", "event_id": "sign-1", "signoff_id": "sign-1",
            "namespace": "N", "uid": "uid-N-1", "resource": "r1",
            "usage_id": "u1", "request_seq": "req-1", "customer": "A", "units": 10,
        }
        run(self.db, sign)
        run(self.db, dict(sign))

        # Counter returns to zero in generation 2 and the gateway retries.
        reset_proof = {
            **fact, "event_id": "proof-1-reset", "proof_id": "proof-1-reset",
            "counter_generation": 2,
        }
        self.assertEqual(run(self.db, reset_proof)["decision"], "duplicate_proof")
        reset_sign = {
            **sign, "event_id": "sign-1-reset", "signoff_id": "sign-1-reset",
        }
        self.assertEqual(run(self.db, reset_sign)["decision"], "reused_customer_request")
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM us_usage")["n"], 1)
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM us_signoffs")["n"], 1)

        # Same namespace name, new immutable UID: ambiguity blocks name-only retry.
        run(self.db, {"op": "bind_namespace", "event_id": "ns2",
                      "namespace": "N", "uid": "uid-N-2", "generation": 2})
        ambiguous = run(self.db, {
            "op": "vendor_proof", "event_id": "proof-ambiguous",
            "proof_id": "proof-ambiguous", "namespace": "N", "resource": "r1",
            "request_seq": "req-1", "persisted_from": 100, "persisted_to": 101,
            "units": 10,
        })
        self.assertEqual(ambiguous["decision"], "namespace_uid_ambiguous_after_rebuild")
        uid_reuse = run(self.db, {"op": "bind_namespace", "event_id": "ns-other",
                                  "namespace": "OTHER", "uid": "uid-N-1"})
        self.assertEqual(uid_reuse["decision"], "uid_not_reusable")

        # The old resource entity cannot be claimed by the replacement UID.
        rebuilt_resource = run(self.db, {
            "op": "vendor_proof", "event_id": "proof-rebuilt-same-resource",
            "proof_id": "proof-rebuilt-same-resource", "namespace": "N", "uid": "uid-N-2",
            "resource": "r1", "request_seq": "req-2", "counter_generation": 1,
            "persisted_from": 200, "persisted_to": 201, "units": 7,
        })
        self.assertEqual(rebuilt_resource["decision"], "resource_identity_conflict")

        # A new resource entity under the new UID/intervals is distinct; old debt is unchanged.
        new_ns_usage = run(self.db, {
            "op": "vendor_proof", "event_id": "proof-new-uid",
            "proof_id": "proof-new-uid", "namespace": "N", "uid": "uid-N-2",
            "resource": "r2", "request_seq": "req-2", "counter_generation": 1,
            "persisted_from": 200, "persisted_to": 201, "units": 7,
        })
        self.assertEqual(new_ns_usage["decision"], "vendor_usage_proven")
        self.assertEqual(one(self.db, "SELECT COUNT(*) n FROM us_usage")["n"], 2)

    def test_deletion_handoff_and_restore_keep_usage_with_contemporary_owner(self):
        run(self.db, {"op": "bind_namespace", "event_id": "ns",
                      "namespace": "N", "uid": "uid-N-1"})
        run(self.db, {"op": "register_resource", "event_id": "r",
                      "resource": "r", "namespace": "N", "uid": "uid-N-1",
                      "owner": "old-holder", "generation": 1, "at": 0})

        def settle_period(period, start, end, units, customer):
            uid = f"u-{period}"
            run(self.db, {
                "op": "vendor_proof", "event_id": f"proof-{period}",
                "proof_id": f"proof-{period}", "usage_id": uid,
                "namespace": "N", "uid": "uid-N-1", "resource": "r",
                "request_seq": f"seq-{period}", "period": period,
                "persisted_from": start, "persisted_to": end, "units": units,
            })
            run(self.db, {
                "op": "customer_signoff", "event_id": f"sign-{period}",
                "signoff_id": f"sign-{period}", "usage_id": uid,
                "namespace": "N", "uid": "uid-N-1", "resource": "r",
                "request_seq": f"seq-{period}", "customer": customer,
                "units": units, "at": end,
            })
            run(self.db, {
                "op": "reconcile", "event_id": f"case-{period}",
                "case_id": f"case-{period}", "period": period, "at": end + 1,
            })
            run(self.db, {
                "op": "settle_reserve", "event_id": f"settle-{period}",
                "settlement_id": f"stl-{period}", "case_id": f"case-{period}",
                "at": end + 2,
            })

        settle_period("p1", 1, 4, 5, "old-holder")
        run(self.db, {"op": "handoff", "event_id": "handoff", "resource": "r",
                      "to_owner": "new-holder", "at": 5})
        settle_period("p2", 6, 8, 3, "new-holder")
        run(self.db, {"op": "delete_resource", "event_id": "delete",
                      "resource": "r", "generation": 1, "at": 9})
        restored = run(self.db, {"op": "restore_resource", "event_id": "restore",
                                "resource": "r", "generation": 2,
                                "fingerprint": "fp2", "owner": "new-holder", "at": 11})
        self.assertEqual(restored["decision"], "restored")
        settle_period("p3", 12, 13, 2, "new-holder")
        # A later handoff cannot rewrite earlier invoice lines.
        run(self.db, {"op": "handoff", "event_id": "handoff-2", "resource": "r",
                      "to_owner": "later-holder", "at": 14})

        p1 = report(self.db, "case-p1")
        p2 = report(self.db, "case-p2")
        p3 = report(self.db, "case-p3")
        self.assertEqual(p1["customers"]["old-holder"]["invoiced"], 5)
        self.assertEqual(p2["customers"]["new-holder"]["invoiced"], 3)
        self.assertEqual(p3["customers"]["new-holder"]["invoiced"], 2)
        line = one(self.db, """SELECT responsible_party FROM us_invoice_lines
                               WHERE usage_id='u-p1'""")
        self.assertEqual(line["responsible_party"], "old-holder")
        self.assertEqual(one(self.db, "SELECT owner_at_use FROM us_usage WHERE usage_id='u-p2'")["owner_at_use"],
                         "new-holder")
        self.assertEqual(one(self.db, "SELECT owner_at_use FROM us_usage WHERE usage_id='u-p3'")["owner_at_use"],
                         "new-holder")
        self.assertTrue(replay(self.db)["verified"])

    def test_double_payment_reversal_and_append_only_receipts(self):
        run(self.db, {"op": "bind_namespace", "event_id": "ns",
                      "namespace": "N", "uid": "u"})
        run(self.db, {"op": "vendor_proof", "event_id": "vp", "proof_id": "vp",
                      "namespace": "N", "uid": "u", "resource": "r",
                      "usage_id": "u1", "request_seq": "s1",
                      "persisted_from": 1, "persisted_to": 2, "units": 10})
        run(self.db, {"op": "customer_signoff", "event_id": "cs", "signoff_id": "cs",
                      "namespace": "N", "uid": "u", "resource": "r",
                      "usage_id": "u1", "request_seq": "s1",
                      "customer": "A", "units": 10})
        run(self.db, {"op": "reconcile", "event_id": "case", "case_id": "case",
                      "usage_ids": ["u1"]})
        run(self.db, {"op": "settle_reserve", "event_id": "stl",
                      "settlement_id": "stl", "case_id": "case"})
        inv = customer_invoice_id(report(self.db, "case"), "A")

        first = run(self.db, {"op": "pay_customer", "event_id": "pay-1",
                              "payment_id": "pay-1", "customer": "A",
                              "invoice_id": inv, "amount": 10})
        self.assertEqual(first["decision"], "payment_applied")
        self.assertEqual(first["status"], "paid")

        same_id = run(self.db, {"op": "pay_customer", "event_id": "pay-1-other-delivery",
                                "payment_id": "pay-1", "customer": "A",
                                "invoice_id": inv, "amount": 10})
        self.assertEqual(same_id["decision"], "payment_already_recorded")
        duplicate = run(self.db, {"op": "pay_customer", "event_id": "pay-2",
                                  "payment_id": "pay-2", "customer": "A",
                                  "invoice_id": inv, "amount": 10})
        self.assertEqual(duplicate["decision"], "duplicate_payment_held_as_credit")
        self.assertEqual(duplicate["unapplied_amount"], 10)

        reversed_pay = run(self.db, {"op": "reverse_payment", "event_id": "rev-1",
                                     "reversal_id": "rev-1", "payment_id": "pay-1",
                                     "invoice_id": inv, "amount": 10, "reason": "chargeback"})
        self.assertEqual(reversed_pay["decision"], "payment_reversed")
        rep = report(self.db, "case")
        invoice_view = rep["customers"]["A"]["invoices"][0]
        self.assertEqual(invoice_view["paid"], 0)
        self.assertEqual(invoice_view["balance"], 10)

        # The original receipt and invoice amount survive; correction is append-only.
        self.assertEqual(one(self.db, "SELECT amount n FROM us_payments WHERE payment_id='pay-1'")["n"], 10)
        self.assertEqual(one(self.db, "SELECT total_amount n FROM us_invoices WHERE invoice_id=?",
                             (inv,))["n"], 10)
        with self.assertRaises(sqlite3.Error):
            self.db.execute("UPDATE us_payments SET amount=1 WHERE payment_id='pay-1'")
        with self.assertRaises(sqlite3.Error):
            self.db.execute("DELETE FROM us_payments WHERE payment_id='pay-1'")

        # A fresh payment satisfies the reopened invoice exactly once.
        second_good = run(self.db, {"op": "pay_customer", "event_id": "pay-3",
                                    "payment_id": "pay-3", "customer": "A",
                                    "invoice_id": inv, "amount": 10})
        self.assertEqual(second_good["status"], "paid")
        rep = report(self.db, "case")
        self.assertEqual(rep["customers"]["A"]["invoices"][0]["balance"], 0)
        self.assertTrue(rep["verification"]["ok"])
        self.assertTrue(replay(self.db)["verified"])

    def test_contract_free_bytes_reduce_receivable_but_not_vendor_payment(self):
        run(self.db, {"op": "bind_namespace", "event_id": "ns",
                      "namespace": "N", "uid": "u"})
        run(self.db, {"op": "vendor_proof", "event_id": "vp", "proof_id": "vp",
                      "namespace": "N", "uid": "u", "resource": "r",
                      "usage_id": "u1", "request_seq": "s1",
                      "persisted_from": 1, "persisted_to": 2, "units": 10})
        run(self.db, {"op": "customer_signoff", "event_id": "cs", "signoff_id": "cs",
                      "namespace": "N", "uid": "u", "resource": "r",
                      "usage_id": "u1", "request_seq": "s1", "customer": "A",
                      "claimed_units": 12, "units": 10, "free_units": 2})
        reconciled = run(self.db, {"op": "reconcile", "event_id": "case",
                                   "case_id": "case", "usage_ids": ["u1"]})
        self.assertEqual(reconciled["physical_units"], 10)
        self.assertEqual(reconciled["undisputed_units"], 8)
        self.assertEqual(reconciled["nonbillable_units"], 2)
        settled = run(self.db, {"op": "settle_reserve", "event_id": "stl",
                                "settlement_id": "stl", "case_id": "case"})
        self.assertEqual(settled["vendor_paid"], 10)
        self.assertEqual(settled["invoiced_total"], 8)
        rep = report(self.db, "case")
        self.assertEqual(rep["vendor"]["paid"], 10)
        self.assertEqual(rep["customers"]["A"]["invoiced"], 8)
        self.assertTrue(rep["verification"]["ok"])

    def test_store_execute_replays_settlement_subsystem(self):
        from tenurecourt.store import execute as store_execute
        from tenurecourt.store import replay as store_replay

        path = self.path.parent / "store-events.sqlite"
        db = connect(str(path))
        store_execute(db, "settlement", {
            "op": "bind_namespace", "event_id": "ns",
            "namespace": "N", "uid": "u",
        })
        store_execute(db, "settlement", {
            "op": "vendor_proof", "event_id": "vp", "proof_id": "vp",
            "namespace": "N", "uid": "u", "resource": "r", "usage_id": "u1",
            "request_seq": "s1", "persisted_from": 1, "persisted_to": 2,
            "units": 3,
        })
        db.close()
        result = store_replay(str(path))
        self.assertTrue(result["verified"], result.get("errors"))

        direct_path = self.path.parent / "direct-settlement.sqlite"
        direct_db = connect(str(direct_path))
        run(direct_db, {"op": "bind_namespace", "event_id": "direct-ns",
                        "namespace": "N", "uid": "direct-u"})
        direct_db.close()
        direct_result = store_replay(str(direct_path))
        self.assertTrue(direct_result["verified"], direct_result.get("errors"))

    def test_late_overturning_proof_uses_credit_refund_and_supplement(self):
        seed_dual_usage(self.db)
        run(self.db, {"op": "settle_guarantee", "event_id": "stl",
                      "settlement_id": "stl", "case_id": "case-1", "at": 30})
        run(self.db, {"op": "attribute", "event_id": "proof-a-first",
                      "proof_id": "proof-a-first", "case_id": "case-1",
                      "usage_id": "u-x", "customer": "A", "at": 40})
        run(self.db, {"op": "finalize_attribution", "event_id": "final",
                      "finalization_id": "final", "case_id": "case-1", "at": 41})
        rep = report(self.db, "case-1")
        a_inv = customer_invoice_id(rep, "A", "invoice")
        b_inv = customer_invoice_id(rep, "B", "invoice")
        run(self.db, {"op": "pay_customer", "event_id": "pay-a", "payment_id": "pay-a",
                      "customer": "A", "invoice_id": a_inv, "amount": 1000, "at": 42})
        run(self.db, {"op": "pay_customer", "event_id": "pay-b", "payment_id": "pay-b",
                      "customer": "B", "invoice_id": b_inv, "amount": 600, "at": 42})
        run(self.db, {"op": "refund_guarantee", "event_id": "refund",
                      "refund_id": "refund", "case_id": "case-1", "at": 43})

        # Later original evidence shows the reused sequence actually belonged to B.
        run(self.db, {"op": "attribute", "event_id": "proof-b-late",
                      "proof_id": "proof-b-late", "case_id": "case-1",
                      "usage_id": "u-x", "customer": "B",
                      "evidence": "original B idempotency key", "at": 60})
        note = run(self.db, {"op": "issue_credit_note", "event_id": "cn-a",
                             "credit_note_id": "cn-a",
                             "original_invoice_id": a_inv, "amount": 400,
                             "usage_id": "u-x", "reason": "late attribution reversal", "at": 61})
        self.assertEqual(note["decision"], "credit_note_issued")
        charge = run(self.db, {"op": "issue_charge", "event_id": "ch-b",
                               "invoice_id": "ch-b-400", "case_id": "case-1",
                               "customer": "B", "usage_id": "u-x",
                               "units": 400, "unit_price": 1, "at": 61})
        self.assertEqual(charge["decision"], "supplemental_charge_issued")
        run(self.db, {"op": "reverse_payment", "event_id": "rev-a",
                      "reversal_id": "rev-a", "payment_id": "pay-a",
                      "invoice_id": a_inv, "amount": 400, "at": 62})
        run(self.db, {"op": "pay_customer", "event_id": "pay-b-400",
                      "payment_id": "pay-b-400", "customer": "B",
                      "invoice_id": "ch-b-400", "amount": 400, "at": 62})

        rep = report(self.db, "case-1")
        # A's formal ledger position is back to the undisputed 600 and is paid.
        self.assertEqual(rep["customers"]["A"]["invoiced"], 600)
        self.assertEqual(rep["customers"]["A"]["balance"], 0)
        # B finally pays 1000, not an averaged 800.
        self.assertEqual(rep["customers"]["B"]["invoiced"], 1000)
        self.assertEqual(rep["customers"]["B"]["balance"], 0)
        self.assertEqual(rep["vendor"]["paid"], 1600)
        self.assertEqual(rep["guarantee"]["refunded"], 400)
        self.assertTrue(rep["verification"]["ok"], rep["verification"])
        self.assertTrue(replay(self.db)["verified"])


if __name__ == "__main__":
    unittest.main()
