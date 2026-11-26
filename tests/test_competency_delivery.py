import unittest

from tenurecourt import tenure
from tenurecourt.competency_delivery import (
    CORE_ITEMS,
    CompetencyDelivery,
    accept_maintenance_report,
    active_qualification,
    adjudicate_independent_work,
    assign_mentored_delivery,
    attend,
    authorize_independent_work,
    can_work_independently,
    certify_by_practical_exam,
    complete_mentorship,
    complete_review,
    create_maintenance_fee,
    create_training_fee,
    ensure_schema,
    issue_maintenance_report,
    open_assessment,
    open_work_order,
    receive_assessment_result,
    recognize_equipment_scope,
    record_apprentice_evidence,
    record_operation,
    refund_fee,
    retry_assessment_upload,
    revoke_assessment,
    save_evidence,
    senior_departure,
    senior_return,
    sign_assessment,
    record_item_result,
    submit_assessment,
    verify_delivery_chain,
    delivery_dossier,
)
from tenurecourt.qualification_note import provisional_qualified
from tenurecourt.store import connect

MODEL = "HV-SWITCH-220"
OLD_MODEL = "HV-SWITCH-110"
NEW_MODEL = "HV-SWITCH-220-G2"
COURSE = "hv-switching-theory"
ITEMS = list(CORE_ITEMS)


class CompetencyDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")
        ensure_schema(self.db)
        self.addCleanup(self.db.close)

    # ------------------------------------------------------------------
    # Shared story helpers
    # ------------------------------------------------------------------

    def seed_attendance(self, person="new-engineer", course=COURSE):
        attend(self.db, f"att-{person}-1", person, course, "lecture-1")
        attend(self.db, f"att-{person}-2", person, course, "lecture-2")

    def seed_lease(self, namespace="ns-production"):
        tenure.run(
            self.db,
            {"op": "namespace", "id": namespace, "uid": f"uid-{namespace}", "generation": 1},
        )
        result = tenure.run(
            self.db,
            {
                "op": "lease",
                "id": f"lease-{namespace}",
                "namespace": namespace,
                "namespace_uid": f"uid-{namespace}",
                "holder": "hv-team",
                "generation": 1,
                "start": 1,
                "expires": 500,
            },
        )
        self.assertEqual(result["decision"], "active")
        return namespace

    def certify_senior(self, person="senior-engineer", model=MODEL, suffix="senior"):
        result = certify_by_practical_exam(
            self.db,
            f"assessment-{suffix}",
            person=person,
            proctor="proctor-voss",
            issuer="national-grid-certifier",
            equipment_model=model,
            position="hv_switching_technician",
            operation_level="independent_hv_switching",
            valid_from=1,
            valid_until=300,
            at=20,
            video_id=f"video-{suffix}",
            video_sha256=f"sha-{suffix}",
        )
        self.assertEqual(result["decision"], "technician_certified", result)
        return result["qualification"]

    def open_production_order(self, suffix, *, model=MODEL, mentored=False):
        namespace = self.seed_lease(f"ns-{suffix}")
        wo = f"wo-{suffix}"
        opened = open_work_order(
            self.db,
            wo,
            customer=f"customer-{suffix}",
            namespace=namespace,
            lease_id=f"lease-{namespace}",
            team="hv-team",
            generation=1,
            equipment_model=model,
            items=ITEMS,
            scheduled_at=60,
            require_lease=True,
        )
        self.assertEqual(opened["decision"], "work_order_opened", opened)
        return wo

    def complete_independent_story(self, suffix="path-a"):
        person = f"engineer-{suffix}"
        self.seed_attendance(person)
        wo = self.open_production_order(suffix)

        before = authorize_independent_work(self.db, wo, person, at=61, course=COURSE)
        self.assertEqual(before["decision"], "independent_work_denied")
        self.assertTrue(before["provisional_qualified"])

        exam = certify_by_practical_exam(
            self.db,
            f"assessment-{suffix}",
            person=person,
            proctor="proctor-voss",
            issuer="external-certification-body",
            equipment_model=MODEL,
            valid_from=1,
            valid_until=300,
            at=40,
            video_id=f"video-{suffix}",
            fee_id=f"fee-assessment-{suffix}",
            fee_amount=1200,
        )
        self.assertEqual(exam["decision"], "technician_certified", exam)

        authorized = authorize_independent_work(self.db, wo, person, at=62, course=COURSE)
        self.assertEqual(authorized["decision"], "independent_authorized", authorized)

        for index, item in enumerate(ITEMS, 1):
            op = record_operation(
                self.db,
                f"op-{suffix}-{index}",
                wo,
                item,
                person,
                at=70 + index,
                evidence_id=f"video-{suffix}",
            )
            self.assertEqual(op["decision"], "operation_recorded", op)

        report = issue_maintenance_report(self.db, f"report-{suffix}", wo, at=80)
        self.assertEqual(report["decision"], "report_issued", report)
        accepted = accept_maintenance_report(
            self.db,
            f"accept-{suffix}",
            f"report-{suffix}",
            at=81,
            accepted_by="customer-zhang",
        )
        self.assertEqual(accepted["decision"], "report_accepted", accepted)

        assessment_fee = create_training_fee(
            self.db,
            f"fee-assessment-{suffix}",
            f"assessment-{suffix}",
            1200,
            at=46,
            paid=True,
        )
        self.assertEqual(assessment_fee["decision"], "fee_reused", assessment_fee)

        maintenance_fee = create_maintenance_fee(
            self.db,
            f"fee-maintenance-{suffix}",
            amount=5000,
            acceptance_id=f"accept-{suffix}",
            at=82,
            paid=True,
        )
        self.assertEqual(maintenance_fee["decision"], "fee_paid", maintenance_fee)
        return {
            "person": person,
            "wo": wo,
            "assessment": f"assessment-{suffix}",
            "report": f"report-{suffix}",
            "acceptance": f"accept-{suffix}",
        }

    def complete_mentored_story(self, suffix="path-b", *, leave_temporarily=False):
        senior = f"senior-{suffix}"
        apprentice = f"apprentice-{suffix}"
        self.certify_senior(senior, suffix=f"senior-{suffix}")
        self.seed_attendance(apprentice)
        wo = self.open_production_order(suffix)

        assigned = assign_mentored_delivery(self.db, wo, senior, apprentice, at=61)
        self.assertEqual(assigned["decision"], "mentored_delivery_authorized", assigned)
        mentorship_id = assigned["mentorship_id"]

        bad = record_operation(
            self.db, f"op-bad-{suffix}", wo, "live_isolation", apprentice, at=62
        )
        self.assertEqual(bad["decision"], "apprentice_operation_denied", bad)

        if leave_temporarily:
            self.assertEqual(
                senior_departure(self.db, work_order_id=wo, at=63)["decision"],
                "senior_departure_recorded",
            )
            absent_senior = record_operation(
                self.db, f"op-absent-senior-{suffix}", wo, "live_isolation", senior, at=64
            )
            self.assertEqual(absent_senior["decision"], "senior_absent_operation_paused")
            absent_apprentice = record_operation(
                self.db, f"op-absent-apprentice-{suffix}", wo, "interlock_verification", apprentice, at=65
            )
            self.assertEqual(absent_apprentice["decision"], "apprentice_operation_denied")
            self.assertEqual(
                senior_return(self.db, work_order_id=wo, at=66)["decision"],
                "senior_return_recorded",
            )
            first_at = 67
        else:
            first_at = 63

        for index, item in enumerate(ITEMS):
            op = record_operation(
                self.db,
                f"op-{suffix}-{index + 1}",
                wo,
                item,
                senior,
                at=first_at + index,
                evidence_id=f"video-senior-{suffix}",
            )
            self.assertEqual(op["decision"], "operation_recorded", op)
            evidence = record_apprentice_evidence(
                self.db,
                f"observe-{suffix}-{index + 1}",
                mentorship_id,
                item,
                at=first_at + index,
                method="observed",
                note="Apprentice observed and asked procedural questions.",
            )
            self.assertEqual(evidence["decision"], "apprentice_evidence_recorded", evidence)

        completed = complete_mentorship(self.db, mentorship_id, at=first_at + 4)
        self.assertEqual(completed["decision"], "mentorship_completed", completed)

        report = issue_maintenance_report(self.db, f"report-{suffix}", wo, at=80)
        self.assertEqual(report["decision"], "report_issued", report)
        accepted = accept_maintenance_report(
            self.db, f"accept-{suffix}", f"report-{suffix}", at=81, accepted_by="customer-li"
        )
        self.assertEqual(accepted["decision"], "report_accepted", accepted)
        maintenance_fee = create_maintenance_fee(
            self.db,
            f"fee-maintenance-{suffix}",
            amount=5000,
            acceptance_id=f"accept-{suffix}",
            at=82,
            paid=True,
        )
        self.assertEqual(maintenance_fee["decision"], "fee_paid")
        return {
            "senior": senior,
            "apprentice": apprentice,
            "wo": wo,
            "mentorship": mentorship_id,
            "report": f"report-{suffix}",
            "acceptance": f"accept-{suffix}",
        }

    # ------------------------------------------------------------------
    # Required scenarios
    # ------------------------------------------------------------------

    def test_two_attendances_are_provisional_only_and_deny_independent_work(self):
        self.seed_attendance()
        self.assertTrue(provisional_qualified(self.db, "new-engineer", COURSE))

        decision = adjudicate_independent_work(
            self.db,
            "new-engineer",
            equipment_model=MODEL,
            at=50,
            course=COURSE,
        )
        self.assertFalse(decision["allowed"])
        self.assertEqual(decision["decision"], "independent_work_denied")
        self.assertTrue(decision["provisional_qualified"])
        self.assertIn("no_scoped_qualification", decision["blockers"])
        self.assertFalse(
            can_work_independently(
                self.db, "new-engineer", equipment_model=MODEL, at=50
            )
        )

        wo = open_work_order(self.db, "wo-theory-only", customer="c", equipment_model=MODEL)
        denied = authorize_independent_work(self.db, "wo-theory-only", "new-engineer", at=51)
        self.assertEqual(denied["decision"], "independent_work_denied")
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cd_operations").fetchone()[0], 0)

    def test_external_practical_path_produces_independent_accepted_delivery(self):
        story = self.complete_independent_story("independent")
        chain = verify_delivery_chain(self.db, story["wo"])
        self.assertTrue(chain["verified"], chain["errors"])

        report = self.db.execute(
            "SELECT * FROM cd_reports_maintenance WHERE id=?", (story["report"],)
        ).fetchone()
        self.assertEqual(report["actual_operator_id"], story["person"])
        self.assertEqual(report["delivery_mode"], "independent_certified")
        self.assertEqual(report["status"], "accepted")

        qual = active_qualification(self.db, story["person"], equipment_model=MODEL, at=70)
        self.assertIsNotNone(qual)
        self.assertEqual(set(qual["allowed_items"]), set(ITEMS))
        self.assertEqual(qual["position"], "hv_switching_technician")
        self.assertEqual(qual["equipment_model"], MODEL)
        self.assertEqual(qual["operation_level"], "independent_hv_switching")
        self.assertEqual(qual["valid_from"], 1)
        self.assertEqual(qual["valid_until"], 300)
        self.assertEqual(qual["proctor"], "proctor-voss")
        self.assertEqual(qual["issuer"], "external-certification-body")

        fees = [dict(r) for r in self.db.execute("SELECT * FROM cd_fees").fetchall()]
        sources = {(f["kind"], f["source_type"]) for f in fees}
        self.assertIn(("external_assessment_fee", "assessment"), sources)
        self.assertIn(("customer_maintenance_service_fee", "acceptance"), sources)
        self.assertFalse(any(f["source_type"] == "attendance" for f in fees))

    def test_senior_actual_operator_with_real_apprentice_evidence_path(self):
        story = self.complete_mentored_story("mentored")
        chain = verify_delivery_chain(self.db, story["wo"])
        self.assertTrue(chain["verified"], chain["errors"])

        report = self.db.execute(
            "SELECT * FROM cd_reports_maintenance WHERE id=?", (story["report"],)
        ).fetchone()
        self.assertEqual(report["actual_operator_id"], story["senior"])
        self.assertEqual(report["delivery_mode"], "senior_operator_with_apprentice")

        ops = self.db.execute(
            "SELECT DISTINCT actor_id, actor_role FROM cd_operations WHERE work_order_id=?",
            (story["wo"],),
        ).fetchall()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["actor_id"], story["senior"])
        self.assertEqual(ops[0]["actor_role"], "senior_actual_operator")

        evidence_count = self.db.execute(
            "SELECT COUNT(*) FROM cd_mentorship_evidence WHERE mentorship_id=?",
            (story["mentorship"],),
        ).fetchone()[0]
        self.assertEqual(evidence_count, len(ITEMS))
        self.assertIsNone(
            active_qualification(self.db, story["apprentice"], equipment_model=MODEL, at=80)
        )

        # The mentored arrangement has no new-engineer external exam fee.
        assessment_fees = self.db.execute(
            "SELECT COUNT(*) FROM cd_fees WHERE kind='external_assessment_fee' AND payer='platform'"
        ).fetchone()[0]
        self.assertEqual(assessment_fees, 0)
        cost = self.db.execute(
            """INSERT INTO cd_fees
               (id,kind,direction,payer,payee,amount,currency,source_type,source_id,work_order_id,
                basis,status,created_at,due_at,paid_at,refunded_amount)
               VALUES('fee-senior-cost','senior_delivery_cost','payable','platform','hv-operations',
                800,'CNY','work_order',?,?,?,'paid',60,60,60,0)""",
            (story["wo"], story["wo"], '{"actual_operator":"senior"}'),
        )
        self.db.commit()
        self.assertIsNotNone(cost)

    def test_two_lawful_completions_from_same_provisional_starting_point(self):
        independent = self.complete_independent_story("choice-a")
        mentored = self.complete_mentored_story("choice-b")

        independent_report = self.db.execute(
            "SELECT actual_operator_id,delivery_mode FROM cd_reports_maintenance WHERE id=?",
            (independent["report"],),
        ).fetchone()
        mentored_report = self.db.execute(
            "SELECT actual_operator_id,delivery_mode FROM cd_reports_maintenance WHERE id=?",
            (mentored["report"],),
        ).fetchone()
        self.assertEqual(independent_report["actual_operator_id"], independent["person"])
        self.assertEqual(independent_report["delivery_mode"], "independent_certified")
        self.assertEqual(mentored_report["actual_operator_id"], mentored["senior"])
        self.assertEqual(mentored_report["delivery_mode"], "senior_operator_with_apprentice")
        self.assertIsNotNone(
            self.db.execute(
                "SELECT id FROM cd_fees WHERE kind='external_assessment_fee' AND source_id=?",
                (independent["assessment"],),
            ).fetchone()
        )
        self.assertIsNone(
            self.db.execute(
                "SELECT id FROM cd_fees WHERE kind='external_assessment_fee' AND source_id LIKE 'assessment-choice-b%'"
            ).fetchone()
        )
        self.assertTrue(verify_delivery_chain(self.db, independent["wo"])["verified"])
        self.assertTrue(verify_delivery_chain(self.db, mentored["wo"])["verified"])

    def test_equipment_model_change_only_recognizes_proven_mapped_scope(self):
        person = "model-changing-engineer"
        self.certify_senior(person, model=OLD_MODEL, suffix="old-model")

        old_decision = adjudicate_independent_work(
            self.db, person, equipment_model=OLD_MODEL, at=50
        )
        self.assertTrue(old_decision["allowed"])
        new_decision = adjudicate_independent_work(
            self.db, person, equipment_model=NEW_MODEL, at=50
        )
        self.assertFalse(new_decision["allowed"])
        self.assertTrue(
            any("equipment_model" in b or "exact_equipment_model" in b for b in new_decision["blockers"])
        )

        recognized = recognize_equipment_scope(
            self.db,
            "qual-model-transition",
            person=person,
            from_model=OLD_MODEL,
            to_model=NEW_MODEL,
            at=51,
            mapped_items=["live_isolation", "restoration_drill"],
            requested_items=ITEMS,
            recognition_reference="issuer-model-delta-map-7",
        )
        self.assertEqual(recognized["decision"], "model_scope_recognized", recognized)
        self.assertEqual(
            set(recognized["granted_items"]),
            {"live_isolation", "restoration_drill"},
        )

        limited = adjudicate_independent_work(
            self.db,
            person,
            equipment_model=NEW_MODEL,
            items=["live_isolation", "restoration_drill"],
            at=52,
        )
        self.assertTrue(limited["allowed"], limited)
        full = adjudicate_independent_work(
            self.db, person, equipment_model=NEW_MODEL, items=ITEMS, at=52
        )
        self.assertFalse(full["allowed"])
        self.assertTrue(any("interlock_verification" in b for b in full["blockers"]))

        impossible = recognize_equipment_scope(
            self.db,
            "qual-model-impossible",
            person=person,
            from_model=OLD_MODEL,
            to_model=NEW_MODEL,
            at=53,
            mapped_items=["new_digital_interlock_reset"],
        )
        self.assertEqual(impossible["decision"], "no_proven_scope_to_recognize")

        # Old scope remains valid; model transition did not rewrite history.
        self.assertTrue(
            can_work_independently(self.db, person, equipment_model=OLD_MODEL, at=54)
        )

    def test_upload_timeout_reconciles_saved_video_without_second_practical(self):
        person = "offline-exam-engineer"
        self.seed_attendance(person)
        self.assertEqual(
            open_assessment(
                self.db,
                "assessment-timeout",
                person=person,
                proctor="proctor-park",
                issuer="cert-body",
                equipment_model=MODEL,
                valid_from=1,
                valid_until=300,
                at=10,
            )["decision"],
            "assessment_opened",
        )
        self.assertEqual(
            save_evidence(
                self.db,
                "video-offline",
                "assessment-timeout",
                kind="video",
                sha256="saved-video-hash",
                at=11,
            )["decision"],
            "evidence_saved",
        )
        for i, item in enumerate(ITEMS, 2):
            self.assertEqual(
                record_item_result(
                    self.db,
                    "assessment-timeout",
                    item,
                    True,
                    at=i,
                    evidence_id="video-offline",
                )["decision"],
                "item_result_recorded",
            )
        self.assertEqual(
            sign_assessment(self.db, "assessment-timeout", at=20)["decision"],
            "assessment_signed",
        )

        timeout = submit_assessment(self.db, "assessment-timeout", outcome="timeout", at=21)
        self.assertEqual(timeout["decision"], "upload_timeout")
        self.assertFalse(active_qualification(self.db, person, equipment_model=MODEL, at=21))

        again = retry_assessment_upload(self.db, "assessment-timeout", succeed=False, at=22)
        self.assertEqual(again["decision"], "upload_timeout")
        self.assertEqual(again["attempt_count"], 2)
        self.assertFalse(again["worker_required_to_repeat_practical"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM cd_assessments").fetchone()[0], 1
        )
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM cd_item_results").fetchone()[0], 3
        )

        mismatch = receive_assessment_result(
            self.db,
            "assessment-timeout",
            evidence_sha256="wrong-hash",
            at=23,
        )
        self.assertEqual(mismatch["decision"], "evidence_hash_mismatch")
        self.assertFalse(active_qualification(self.db, person, equipment_model=MODEL, at=23))

        recovered = retry_assessment_upload(
            self.db,
            "assessment-timeout",
            succeed=True,
            evidence_sha256="saved-video-hash",
            central_receipt="central-receipt-1",
            at=24,
        )
        self.assertEqual(recovered["decision"], "upload_received", recovered)
        q = recovered["qualification"]
        self.assertIsNotNone(q)
        self.assertEqual(q["allowed_items"], ITEMS)

        duplicate = retry_assessment_upload(
            self.db, "assessment-timeout", succeed=True, at=25
        )
        self.assertEqual(duplicate["decision"], "upload_received")
        self.assertTrue(duplicate.get("duplicate"))
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM cd_qualifications WHERE assessment_id='assessment-timeout'").fetchone()[0],
            1,
        )

    def test_proctor_revocation_opens_traceable_review_and_customer_notice_after_acceptance(self):
        story = self.complete_independent_story("revoke")
        revoked = revoke_assessment(
            self.db,
            story["assessment"],
            revoked_by="proctor-voss",
            authority="proctor",
            reason="Proctor withdraws remote observation attestation pending video audit.",
            at=90,
        )
        self.assertEqual(revoked["decision"], "assessment_revoked", revoked)
        self.assertEqual(len(revoked["reviews"]), 1)
        review_id = revoked["reviews"][0]["id"]
        self.assertTrue(any(n["audience"] == "customer" for n in revoked["notifications"]))
        self.assertTrue(any(n["audience"] == "worker" for n in revoked["notifications"]))

        q = self.db.execute(
            "SELECT status FROM cd_qualifications WHERE id=?",
            (f"qual-{story['assessment']}",),
        ).fetchone()
        self.assertEqual(q["status"], "revoked")
        revoked_chain = verify_delivery_chain(self.db, story["wo"])
        self.assertFalse(revoked_chain["verified"])
        self.assertTrue(any("qualification_revoked" in e for e in revoked_chain["errors"]))

        report = self.db.execute(
            "SELECT status,review_status FROM cd_reports_maintenance WHERE id=?",
            (story["report"],),
        ).fetchone()
        self.assertEqual(report["status"], "accepted")
        self.assertEqual(report["review_status"], "open")
        acceptance = self.db.execute(
            "SELECT id FROM cd_acceptances WHERE id=?", (story["acceptance"],)
        ).fetchone()
        self.assertIsNotNone(acceptance)

        # History is preserved, not rewritten as though training never happened.
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM training_events WHERE person=?", (story["person"],)).fetchone()[0],
            2,
        )
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cd_operations").fetchone()[0], 3)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM cd_evidence").fetchone()[0], 1)

        # Re-using the old assignment or operation ID path must also fail safely.
        repeated = authorize_independent_work(self.db, story["wo"], story["person"], at=90)
        self.assertEqual(repeated["decision"], "independent_work_denied")
        self.assertIn("qualification_revoked", repeated["blockers"])
        blocked_operation = record_operation(
            self.db, "op-after-revoke", story["wo"], "live_isolation", story["person"], at=90
        )
        self.assertEqual(blocked_operation["decision"], "qualified_operation_required")

        new_wo = self.open_production_order("after-revocation")
        denied = authorize_independent_work(self.db, new_wo, story["person"], at=91)
        self.assertEqual(denied["decision"], "independent_work_denied")
        self.assertIn("qualification_revoked", denied["blockers"])

        # Corrective rework is delivered by a separately qualified senior.
        senior = "senior-rework"
        self.certify_senior(senior, suffix="rework")
        rework = assign_mentored_delivery(self.db, new_wo, senior, story["person"], at=92)
        self.assertEqual(rework["decision"], "mentored_delivery_authorized", rework)
        for index, item in enumerate(ITEMS, 1):
            self.assertEqual(
                record_operation(self.db, f"op-rework-{index}", new_wo, item, senior, at=93 + index)["decision"],
                "operation_recorded",
            )

        finished = complete_review(
            self.db,
            review_id,
            outcome="rework_required",
            reviewer="chief-engineer-pei",
            at=100,
            corrective_action="Assign a senior engineer to re-perform and witness critical isolation.",
        )
        self.assertEqual(finished["decision"], "review_completed")
        customer_followups = self.db.execute(
            "SELECT COUNT(*) FROM cd_notifications WHERE audience='customer' AND kind='review_completed'"
        ).fetchone()[0]
        self.assertEqual(customer_followups, 1)

    def test_senior_temporary_departure_pauses_until_return(self):
        story = self.complete_mentored_story("leave", leave_temporarily=True)
        chain = verify_delivery_chain(self.db, story["wo"])
        self.assertTrue(chain["verified"], chain["errors"])
        absent_incidents = self.db.execute(
            "SELECT COUNT(*) FROM cd_incidents WHERE work_order_id=? AND code IN "
            "('operation_attempted_while_senior_absent','apprentice_overreach_blocked')",
            (story["wo"],),
        ).fetchone()[0]
        self.assertGreaterEqual(absent_incidents, 2)

    def test_apprentice_overreach_is_recorded_but_not_operation(self):
        senior = "senior-overreach"
        apprentice = "apprentice-overreach"
        self.certify_senior(senior, suffix="overreach")
        wo = self.open_production_order("overreach")
        assigned = assign_mentored_delivery(self.db, wo, senior, apprentice, at=40)
        self.assertEqual(assigned["decision"], "mentored_delivery_authorized")

        denied = record_operation(
            self.db, "op-overreach-bad", wo, "restoration_drill", apprentice, at=41
        )
        self.assertEqual(denied["decision"], "apprentice_operation_denied")
        self.assertIsNotNone(denied["incident_id"])
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM cd_operations WHERE work_order_id=?", (wo,)).fetchone()[0],
            0,
        )
        incidents = self.db.execute(
            "SELECT code FROM cd_incidents WHERE work_order_id=?", (wo,)
        ).fetchall()
        self.assertIn("apprentice_overreach_blocked", [i["code"] for i in incidents])

        # The report cannot be issued until the senior performs the work.
        premature = issue_maintenance_report(self.db, "report-premature", wo, at=42)
        self.assertEqual(premature["decision"], "operations_incomplete")

    def test_training_fee_refund_and_attendance_cannot_create_either_fee(self):
        story = self.complete_independent_story("refund")
        bad_training_fee = create_training_fee(
            self.db, "fee-attendance-bad", "attendance-count-2", 999
        )
        # There is no assessment row, so the normal basis guard rejects it.
        self.assertEqual(bad_training_fee["decision"], "training_fee_requires_completed_signed_assessment")
        from tenurecourt.competency_delivery import attendance_fee_attempt

        self.assertEqual(
            attendance_fee_attempt(self.db)["decision"],
            "attendance_cannot_support_fee",
        )

        refunded = refund_fee(
            self.db,
            f"fee-assessment-{story['assessment'].split('-', 1)[1]}",
            reason="Certification body credits cancelled add-on examination after audit.",
            at=200,
            requested_by="training-manager",
            refund_id="refund-1",
        )
        self.assertEqual(refunded["decision"], "fee_refunded", refunded)
        self.assertEqual(refunded["status"], "refunded")

        repeated = refund_fee(
            self.db,
            f"fee-assessment-{story['assessment'].split('-', 1)[1]}",
            reason="same refund retry",
            at=201,
            refund_id="refund-1",
        )
        self.assertEqual(repeated["decision"], "refund_reused")
        refund_count = self.db.execute(
            "SELECT COUNT(*) FROM cd_fee_refunds WHERE id='refund-1'"
        ).fetchone()[0]
        self.assertEqual(refund_count, 1)

    def test_missing_or_failed_practical_items_do_not_create_qualification_or_fee(self):
        person = "incomplete-practical"
        self.seed_attendance(person)
        open_assessment(
            self.db,
            "assessment-incomplete",
            person=person,
            proctor="proctor-han",
            issuer="cert-body",
            equipment_model=MODEL,
            valid_from=1,
            valid_until=100,
            at=10,
        )
        save_evidence(self.db, "evidence-incomplete", "assessment-incomplete", sha256="hash", at=11)
        record_item_result(
            self.db, "assessment-incomplete", "live_isolation", True, at=12, evidence_id="evidence-incomplete"
        )

        incomplete = sign_assessment(self.db, "assessment-incomplete", at=20)
        self.assertEqual(incomplete["decision"], "assessment_incomplete")
        fee = create_training_fee(self.db, "fee-incomplete", "assessment-incomplete", 100, at=21)
        self.assertEqual(fee["decision"], "training_fee_requires_completed_signed_assessment")

        record_item_result(self.db, "assessment-incomplete", "interlock_verification", False, at=13)
        record_item_result(self.db, "assessment-incomplete", "restoration_drill", True, at=14)
        signed = sign_assessment(self.db, "assessment-incomplete", at=22)
        self.assertEqual(signed["decision"], "assessment_signed")
        self.assertFalse(signed["passed"])
        not_qualified = adjudicate_independent_work(
            self.db, person, equipment_model=MODEL, at=23
        )
        self.assertFalse(not_qualified["allowed"])
        maintenance_fee = create_maintenance_fee(
            self.db, "fee-maintenance-without-acceptance", amount=1
        )
        self.assertEqual(maintenance_fee["decision"], "maintenance_fee_requires_customer_acceptance")

    def test_production_lease_mismatch_is_rejected_independently_of_competency(self):
        namespace = self.seed_lease("ns-lease-check")
        denied = open_work_order(
            self.db,
            "wo-bad-lease",
            customer="customer-lease",
            namespace=namespace,
            lease_id=f"lease-{namespace}",
            team="wrong-team",
            equipment_model=MODEL,
            require_lease=True,
            at=60,
        )
        self.assertEqual(denied["decision"], "production_lease_invalid")
        self.assertEqual(
            self.db.execute("SELECT COUNT(*) FROM cd_work_orders WHERE id='wo-bad-lease'").fetchone()[0],
            0,
        )

    def test_dossier_links_all_chain_elements_for_independent_delivery(self):
        story = self.complete_independent_story("dossier")
        dossier = delivery_dossier(self.db, story["wo"])
        self.assertTrue(dossier["chain_verification"]["verified"], dossier["chain_verification"])
        self.assertEqual(len(dossier["attendance"]), 2)
        self.assertEqual(dossier["assessment"]["id"], story["assessment"])
        self.assertEqual(dossier["qualification"]["assessment_id"], story["assessment"])
        self.assertEqual(len(dossier["operations"]), 3)
        self.assertEqual(dossier["acceptance"]["id"], story["acceptance"])
        self.assertGreaterEqual(len(dossier["fees"]), 2)
        self.assertIn("live_isolation", dossier["qualification"]["allowed_items"])

    def test_class_facade_supports_same_story_with_own_memory_db(self):
        board = CompetencyDelivery()
        board.seed_attendance if hasattr(board, "seed_attendance") else None
        attend(board.db, "att-facade-1", "facade-person", COURSE, "one")
        attend(board.db, "att-facade-2", "facade-person", COURSE, "two")
        self.assertTrue(provisional_qualified(board.db, "facade-person", COURSE))
        denied = board.adjudicate_independent_work(
            person="facade-person", equipment_model=MODEL, at=10
        )
        self.assertEqual(denied["decision"], "independent_work_denied")
        self.assertFalse(board.can_work_independently(person="facade-person", equipment_model=MODEL, at=10))
        board.close()


if __name__ == "__main__":
    unittest.main()
