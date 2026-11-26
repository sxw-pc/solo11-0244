import unittest

from tenurecourt.personnel_muster import (
    ASSEMBLED,
    MEDICAL_HANDOVER,
    PENDING,
    PersonnelMuster,
    comparison_snapshot,
    run as personnel_run,
)
from tenurecourt.site_alarm import badge
from tenurecourt.store import connect
from tenurecourt.muster_note import provisional_clear


class PersonnelMusterTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    # ------------------------------------------------------------------
    # Shared story state
    # ------------------------------------------------------------------
    def _story_board(self):
        """Same initial facts used by both rescue arrangements."""
        board = PersonnelMuster(incident_id="coolant-leak-b2", leak_at=5)
        board.register_person(
            "c7",
            "程墙",
            "contractor",
            employer="CoolServ Industrial Services",
            employment_verified=True,
            shift="night-shift",
            entry_area="inner-equipment-room",
        )
        board.register_person(
            "b2",
            "林涛",
            "employee",
            employer="FacilityOps",
            employment_verified=True,
            shift="night-shift",
            entry_area="outer-corridor",
        )
        board.issue_card("card-c7", "c7", at=1)
        board.issue_card("card-b2", "b2", at=1)
        board.record_entry("c7", 10, "inner-equipment-room", source="shift_and_badge_in", evidence_id="entry-c7")
        board.record_entry("b2", 8, "outer-corridor", source="shift_roster", evidence_id="entry-b2")
        board.record_badge("badge-in-c7", "card-c7", "in", 10, gate="inner-door")
        board.record_badge("badge-out-c7", "card-c7", "out", 20, gate="inner-door")
        board.record_sighting(
            "supervisor-inner-c7",
            "c7",
            22,
            "inner-equipment-room",
            witness="duty-supervisor-han",
            source="supervisor_report",
            statement="Duty supervisor says the contractor is still in the inner room.",
        )
        board.record_radio(
            "radio-c7-25",
            "c7",
            25,
            "inner-equipment-room",
            operator="radio-officer-zhou",
            message="responded once from equipment room",
        )
        board.record_lost_contact(
            "radio-c7-lost",
            "c7",
            27,
            operator="radio-officer-zhou",
            message="repeated calls unanswered; contact lost",
        )
        board.record_pressure_reading(
            "pressure-12",
            "backup-route",
            at=12,
            pressure_differential_ok=True,
            reading=14.5,
            recorded_by="sensor-bacnet-7",
        )
        board.record_manual_patrol(
            "patrol-before-leak",
            "backup-route",
            inspected_at=2,
            inspector="guard-bao",
            safe=True,
        )
        return board

    def _run_assisted_evacuation_path(self):
        board = self._story_board()
        route = board.record_manual_patrol(
            "patrol-after-leak",
            "backup-route",
            inspected_at=32,
            inspector="safety-chen",
            safe=True,
            note="post-leak visual check, damp floor but passable",
        )
        self.assertEqual(route["decision"], "manual_patrol_recorded")
        authorized = board.authorize_route_use(
            "backup-route", commander_id="commander-zhao", at=33, commander_name="Zhao"
        )
        self.assertEqual(authorized["decision"], "route_authorized")

        dispatched = board.dispatch_search(
            "order-assisted-a",
            "c7",
            "authorized-field-team-1",
            34,
            command_group="incident-command",
            accepted_by="team-lead-an",
            task="assisted_backup_route_evacuation",
            timeout_minutes=20,
        )
        self.assertEqual(dispatched["decision"], "search_dispatched")
        self.assertEqual(
            board.confirm_assembly("sign-b2", "b2", 35, method="biometric", witness="muster-marshal-ma")["decision"],
            "assembly_confirmed",
        )
        self.assertEqual(
            board.confirm_rescue_recovery(
                "recovery-a", "c7", 38, order_id="order-assisted-a", rescued_by="team-lead-an"
            )["decision"],
            "rescue_recovery_confirmed",
        )
        self.assertEqual(
            board.confirm_assembly(
                "sign-c7", "c7", 40, method="signature", witness="muster-marshal-ma"
            )["decision"],
            "assembly_confirmed",
        )
        self.assertEqual(
            board.sign_clearance(
                "final-a",
                "commander-zhao",
                42,
                commander_name="Commander Zhao",
                scope="final",
                statement="All rostered persons personally signed at assembly point.",
            )["decision"],
            "clearance_signed",
        )
        self.assertEqual(
            board.record_safety_reset(
                "reset-a", 50, authorized_by="commander-zhao", note="coolant isolated and ventilation verified"
            )["decision"],
            "safety_reset_recorded",
        )
        return board

    def _run_controlled_rescue_path(self):
        board = self._story_board()
        route = board.route_report("backup-route")
        self.assertFalse(route["safe_confirmed"])
        self.assertIn("last_manual_patrol_before_leak", route["blockers"])

        # The person who carried c7's card is physically outside and is
        # accounted separately by personal assembly sign-in.
        self.assertEqual(
            board.confirm_assembly("sign-b2-b", "b2", 28, method="biometric", witness="marshal-ma")["decision"],
            "assembly_confirmed",
        )

        isolation = board.order_controlled_isolation(
            "isolation-b",
            commander_id="commander-zhao",
            at=29,
            area="inner-equipment-room",
            reason="backup route cannot be physically rechecked in the rescue window",
        )
        self.assertEqual(isolation["decision"], "controlled_isolation_ordered")

        dispatched = board.dispatch_search(
            "order-rescue-b",
            "c7",
            "inner-rescue-team",
            30,
            command_group="command-group-1",
            accepted_by="rescue-lead-ra",
            task="locate inner room contractor and transfer to medical reception",
            timeout_minutes=10,
            medical_team="ambulance-7",
            medical_receiver="paramedic-nina",
        )
        self.assertEqual(dispatched["decision"], "search_dispatched")

        controlled = board.sign_clearance(
            "controlled-b",
            "commander-zhao",
            30,
            commander_name="Commander Zhao",
            scope="controlled_isolation",
            statement="Zone handed to named rescue/medical task; this is not an all-personnel-safe certificate.",
        )
        self.assertEqual(controlled["decision"], "clearance_signed")
        self.assertTrue(controlled["valid"])

        duplicate = board.dispatch_search(
            "order-rescue-b-duplicate",
            "c7",
            "second-rescue-team",
            32,
            command_group="command-group-2",
            accepted_by="second-lead-sun",
            timeout_minutes=8,
        )
        self.assertEqual(duplicate["decision"], "duplicate_dispatch")

        escalated = board.process_escalations(41)
        self.assertEqual([item["person_id"] for item in escalated["due"]], ["c7"])

        deconflicted = board.deconflict_dispatch(
            "c7",
            "order-rescue-b",
            deconflicted_by="commander-zhao",
            at=42,
            rationale="One accountable rescue owner; second team supports under lead order.",
        )
        self.assertEqual(deconflicted["decision"], "dispatch_deconflicted")

        self.assertEqual(
            board.confirm_rescue_recovery(
                "recovery-b", "c7", 48, order_id="order-rescue-b", rescued_by="firefighter-lee"
            )["decision"],
            "rescue_recovery_confirmed",
        )
        handoff = board.complete_medical_handoff(
            "handoff-b",
            "c7",
            65,
            recovery_id="recovery-b",
            rescue_order_id="order-rescue-b",
            rescued_by="firefighter-lee",
            medical_team="ambulance-7",
            received_by="nurse-nina",
            transport_requested_at=30,
            transport_arrived_at=60,
            deadline=50,
            recorded_at=70,
            destination="surface-medical-receiving",
        )
        self.assertEqual(handoff["decision"], "medical_handoff_verified")
        self.assertTrue(handoff["transport_late"])
        self.assertEqual(handoff["recorded_late_seconds"], 5)

        final = board.sign_clearance(
            "final-b",
            "commander-zhao",
            72,
            commander_name="Commander Zhao",
            scope="final",
            corrects="controlled-b",
        )
        self.assertEqual(final["decision"], "clearance_signed")
        self.assertEqual(
            board.record_safety_reset("reset-b", 80, authorized_by="commander-zhao")["decision"],
            "safety_reset_recorded",
        )
        return board

    # ------------------------------------------------------------------
    # Core badge-count contrast
    # ------------------------------------------------------------------
    def test_in_then_proxy_out_legacy_becomes_true_but_actual_remains_pending(self):
        badge(self.db, "legacy-in", "contractor-7", "inner-door", "in", 10)
        self.assertFalse(provisional_clear(self.db, "contractor-7"))

        board = PersonnelMuster(leak_at=5)
        board.register_person(
            "c7",
            "Contractor Seven",
            "contractor",
            employer="CoolServ",
            employment_verified=True,
            shift="night",
            entry_area="inner-equipment-room",
        )
        board.issue_card("card-c7", "c7")
        board.record_entry("c7", 10, source="badge_in")
        board.record_badge("legacy-in", "card-c7", "in", 10, "inner-door")

        before = comparison_snapshot(self.db, "contractor-7", board, "c7")
        self.assertFalse(before["legacy_balance_clear"])
        self.assertEqual(before["actual_status"], PENDING)
        self.assertFalse(before["actual_accounted"])

        # The physical out card is later found to have been carried by a mate.
        badge(self.db, "legacy-proxy-out", "contractor-7", "inner-door", "out", 20)
        self.assertTrue(provisional_clear(self.db, "contractor-7"))
        board.record_badge("legacy-proxy-out", "card-c7", "out", 20, "inner-door")

        after = comparison_snapshot(self.db, "contractor-7", board, "c7")
        self.assertTrue(after["legacy_balance_clear"])
        self.assertEqual(after["actual_status"], PENDING)
        self.assertFalse(after["actual_accounted"])

        blocked = board.sign_clearance("cannot-final", "cdr", 21, scope="final")
        self.assertEqual(blocked["decision"], "clearance_blocked")
        self.assertIn("unconfirmed_personnel", blocked["blockers"])

        # Only personal assembly changes the muster state.
        signed = board.confirm_assembly("personal-sign", "c7", 40, method="biometric")
        self.assertEqual(signed["decision"], "assembly_confirmed")
        self.assertEqual(board.person_report("c7")["status"], ASSEMBLED)

    def test_sqlite_run_adapter_reconstructs_board_between_commands(self):
        payload = {
            "op": "register_person",
            "person_id": "p1",
            "full_name": "SQLite Worker",
            "role": "contractor",
            "employer": "CoolServ",
            "employment_verified": True,
            "shift": "night",
            "entry_area": "inner-room",
        }
        self.assertEqual(personnel_run(self.db, payload)["decision"], "registered")
        self.assertEqual(
            personnel_run(self.db, {"op": "record_entry", "person_id": "p1", "at": 10})["decision"],
            "entry_recorded",
        )
        report = personnel_run(self.db, {"op": "person_report", "person_id": "p1"})
        self.assertEqual(report["status"], PENDING)
        self.assertEqual(report["entry_area"], "inner-room")

    def test_proxy_assembly_signin_is_rejected(self):
        board = PersonnelMuster()
        board.register_person(
            "p1", "Pack Worker", "employee", employer="Ops", employment_verified=True,
            shift="day", entry_area="hall",
        )
        result = board.confirm_assembly(
            "proxy-sign", "p1", 20, self_present=False, witness="friend", method="signature"
        )
        self.assertEqual(result["decision"], "proxy_assembly_signin_rejected")
        self.assertFalse(board.person_report("p1")["accounted"])

    # ------------------------------------------------------------------
    # Identity and credentials
    # ------------------------------------------------------------------
    def test_visitor_can_become_escort_and_authorization_is_enforced(self):
        board = PersonnelMuster()
        board.register_person(
            "emp-1", "Regular Escort", "employee", employer="FacilityOps",
            employment_verified=True, shift="day", entry_area="lobby", escort_qualified=True,
        )
        board.register_person(
            "visitor-1", "Victor Visitor", "visitor", escort_id="emp-1",
            shift="visit-am", entry_area="lobby",
        )

        denied = board.authorize_escort("visitor-1", at=20, authorized_by="security-chief")
        self.assertEqual(denied["decision"], "escort_authorization_denied")

        authorized = board.authorize_escort(
            "visitor-1", at=21, authorized_by="security-chief", briefing_id="briefing-9"
        )
        self.assertEqual(authorized["decision"], "escort_authorized")

        second_visitor = board.register_person(
            "visitor-2", "Vera Guest", "visitor", escort_id="visitor-1",
            shift="visit-am", entry_area="lobby",
        )
        self.assertEqual(second_visitor["decision"], "registered")
        self.assertEqual(board.person_report("visitor-2")["escort_id"], "visitor-1")

        board.register_person(
            "visitor-3", "Unauthorized Escort", "visitor", escort_id="emp-1",
            shift="visit-am", entry_area="lobby",
        )
        blocked = board.register_person(
            "visitor-4", "Fourth Guest", "visitor", escort_id="visitor-3",
            shift="visit-am", entry_area="lobby",
        )
        self.assertEqual(blocked["decision"], "identity_incomplete")
        self.assertIn("escort_not_authorized", blocked["errors"])

    def test_card_reissue_and_duplicate_names_do_not_merge_lives(self):
        board = PersonnelMuster()
        for pid, employer in [("person-a", "Employer A"), ("person-b", "Employer B")]:
            board.register_person(
                pid,
                "张伟",
                "contractor",
                employer=employer,
                employment_verified=True,
                shift="night",
                entry_area="electrical-room",
            )
        self.assertEqual(len(board.people_named("张伟")), 2)

        board.issue_card("card-a1", "person-a", at=1)
        reissue = board.issue_card("card-a2", "person-a", at=2, replaces="card-a1")
        self.assertEqual(reissue["decision"], "card_issued")
        self.assertEqual(board.cards["card-a1"]["status"], "reissued")

        conflict = board.issue_card("card-a2", "person-b", at=3)
        self.assertEqual(conflict["decision"], "card_identity_conflict")

        board.record_badge("badge-new-card", "card-a2", "out", 20, gate="egress")
        report_a = board.person_report("person-a")
        report_b = board.person_report("person-b")
        self.assertEqual([c["card_id"] for c in report_a["cards"]], ["card-a1", "card-a2"])
        self.assertEqual([c["card_id"] for c in report_b["cards"]], [])
        self.assertEqual(report_a["status"], PENDING)
        self.assertEqual(report_b["status"], PENDING)

    def test_card_borrowing_investigation_keeps_borrower_and_owner_separate(self):
        board = self._story_board()
        board.record_badge(
            "confirmed-borrowed-out",
            "card-c7",
            "out",
            21,
            gate="inner-door",
            actual_person_id="b2",
            witnessed_by="camera-operator",
        )
        evidence = board.evidence["confirmed-borrowed-out"]
        self.assertTrue(evidence["proxy"])
        self.assertEqual(evidence["adjudicated_actual_person_id"], "b2")

        owner = board.person_report("c7")
        borrower = board.person_report("b2")
        self.assertFalse(owner["accounted"])
        self.assertTrue(any(e["evidence_id"] == "confirmed-borrowed-out" for e in borrower["evidence_timeline"]))
        self.assertFalse(any(e["evidence_id"] == "confirmed-borrowed-out" and not e["proxy"] for e in borrower["evidence_timeline"]))

    # ------------------------------------------------------------------
    # Re-entry and signed-clearance correction
    # ------------------------------------------------------------------
    def test_verified_return_to_room_reenters_risk_census_and_flags_certificate(self):
        board = PersonnelMuster()
        board.register_person(
            "p1", "Return Worker", "employee", employer="Ops", employment_verified=True,
            shift="day", entry_area="equipment-room",
        )
        board.record_entry("p1", 10)
        board.confirm_assembly("sign-1", "p1", 20, method="biometric")
        board.sign_clearance("final-1", "cdr-zhao", 25, scope="final")
        self.assertTrue(board.certificates["final-1"]["currently_valid"])

        board.record_camera(
            "camera-return",
            "p1",
            30,
            "equipment-room",
            device_id="cam-inner",
            reviewed_by="operator-he",
        )
        reentry = board.report_reentry("p1", 30, "equipment-room", evidence_id="camera-return")
        self.assertEqual(reentry["decision"], "reentry_recorded")
        self.assertEqual(reentry["episode"], 2)

        report = board.person_report("p1")
        self.assertEqual(report["status"], PENDING)
        self.assertTrue(report["current_episode"]["reentry"])
        self.assertEqual(board.certificates["final-1"]["status"], "correction_required")

        correction = board.correct_clearance(
            "correction-1",
            "final-1",
            31,
            corrected_by="commander-zhao",
            reason="Verified camera shows the worker returned after personal assembly.",
            corrected_statement="One person is again inside the risk census; rescue check reopened.",
        )
        self.assertEqual(correction["decision"], "clearance_correction_recorded")
        self.assertEqual(board.certificates["final-1"]["status"], "corrected")
        self.assertFalse(board.certificates["final-1"]["currently_valid"])

    def test_commander_can_record_erroneous_signed_all_clear_then_correct_it(self):
        board = self._story_board()
        signed = board.record_signed_clearance(
            "paper-all-clear",
            "commander-zhao",
            28,
            commander_name="Commander Zhao",
            scope="all_clear",
            statement="All personnel have left.",
        )
        self.assertEqual(signed["decision"], "unsupported_clearance_recorded")
        self.assertFalse(signed["valid"])
        self.assertTrue(any("unconfirmed_personnel" in item for item in signed["blockers"]))

        correction = board.correct_clearance(
            "paper-correction",
            "paper-all-clear",
            29,
            corrected_by="commander-zhao",
            reason="Supervisor and radio identify c7 in inner room; out swipe was proxy.",
            corrected_statement="Do not state all personnel are safe; c7 remains the subject of rescue task.",
        )
        self.assertEqual(correction["decision"], "clearance_correction_recorded")
        certificate = board.certificates["paper-all-clear"]
        self.assertEqual(certificate["status"], "corrected")
        self.assertEqual(certificate["corrections"][0]["correction_id"], "paper-correction")

    # ------------------------------------------------------------------
    # Dispatch, camera time and disputes
    # ------------------------------------------------------------------
    def test_two_command_groups_cannot_create_two_responsible_owners(self):
        board = self._story_board()
        first = board.dispatch_search(
            "order-1", "c7", "team-a", 30, command_group="group-1",
            accepted_by="lead-a", timeout_minutes=10,
        )
        second = board.dispatch_search(
            "order-2", "c7", "team-b", 31, command_group="group-2",
            accepted_by="lead-b", timeout_minutes=10,
        )
        self.assertEqual(first["decision"], "search_dispatched")
        self.assertEqual(second["decision"], "duplicate_dispatch")

        report = board.person_report("c7")
        self.assertEqual(report["responsible_team"]["order_id"], "order-1")
        muster = board.muster_report(32)
        self.assertIn("order-2", muster["unresolved_duplicate_orders"])

        deconflicted = board.deconflict_dispatch(
            "c7", "order-1", deconflicted_by="ic", at=33
        )
        self.assertIn("order-2", deconflicted["stood_down"])
        self.assertEqual(board.muster_report(34)["unresolved_duplicate_orders"], [])
        self.assertEqual(board.person_report("c7")["responsible_team"]["order_id"], "order-1")

    def test_camera_clock_drift_keeps_raw_time_and_uses_corrected_order(self):
        board = PersonnelMuster()
        board.register_person(
            "p1", "Clock Worker", "employee", employer="Ops", employment_verified=True,
            shift="night", entry_area="inner-room",
        )
        board.record_entry("p1", 10, "inner-room")
        board.record_radio("radio", "p1", 25, "inner-room", operator="op")
        board.record_camera(
            "camera-fast",
            "p1",
            40,
            "corridor-junction",
            device_id="camera-fast",
            reviewed_by="camera-reviewer",
        )

        before = board.person_report("p1")
        self.assertEqual(before["last_credible_location"]["source"], "camera")
        self.assertEqual(before["last_credible_location"]["at"], 40)

        calibration = board.calibrate_camera(
            "camera-fast", offset_seconds=-20, calibrated_by="clock-tech", at=45
        )
        self.assertEqual(calibration["decision"], "camera_calibrated")

        report = board.person_report("p1")
        camera_timeline = next(item for item in report["evidence_timeline"] if item["evidence_id"] == "camera-fast")
        self.assertEqual(camera_timeline["observed_at"], 40)
        self.assertEqual(camera_timeline["effective_at"], 20)
        self.assertEqual(camera_timeline["details"]["displayed_at"], 40)
        self.assertEqual(report["last_credible_location"]["source"], "radio")
        self.assertEqual(report["last_credible_location"]["at"], 25)
        self.assertEqual(report["last_credible_location"]["area"], "inner-room")

    def test_contradictory_sources_remain_disputed_until_actual_investigation(self):
        board = PersonnelMuster()
        board.register_person(
            "p1", "Contradiction Worker", "contractor", employer="CoolServ",
            employment_verified=True, shift="night", entry_area="inner-room",
        )
        board.record_entry("p1", 10, "inner-room", source="shift_roster")
        board.record_camera("cam-inner", "p1", 30, "inner-room", device_id="cam-1", reviewed_by="reviewer")
        board.record_sighting(
            "guard-gate", "p1", 31, "north-gate", witness="guard-wu",
            statement="I saw them at north gate",
        )
        opened = board.open_dispute(
            "location-dispute",
            person_id="p1",
            kind="incompatible_locations",
            evidence_ids=["cam-inner", "guard-gate"],
            opened_at=32,
            opened_by="commander-zhao",
            question="Can the worker move from inner room to north gate in one minute?",
        )
        self.assertEqual(opened["decision"], "dispute_opened")

        unresolved = board.person_report("p1")
        self.assertEqual(len(unresolved["open_disputes"]), 1)
        # Both contradictory direct observations are withheld while unresolved;
        # the fixed entry-area anchor remains available.
        self.assertEqual(unresolved["last_credible_location"]["source"], "shift_roster")

        assigned = board.assign_investigation(
            "location-dispute",
            investigation_id="investigation-1",
            investigator="inspector-he",
            at=33,
            mandate="Check camera identity and guard line of sight.",
        )
        self.assertEqual(assigned["decision"], "investigation_assigned")
        resolved = board.resolve_dispute(
            "location-dispute",
            investigation_id="investigation-1",
            investigator="inspector-he",
            at=38,
            finding="Guard recognized a lookalike; camera identification is verified.",
            accepted_evidence_ids=["cam-inner"],
            rejected_evidence_ids=["guard-gate"],
        )
        self.assertEqual(resolved["decision"], "dispute_resolved")

        report = board.person_report("p1")
        self.assertEqual(report["open_disputes"], [])
        self.assertEqual(report["last_credible_location"]["evidence_id"], "cam-inner")
        self.assertEqual(board.evidence["guard-gate"]["adjudication"], "rejected")
        self.assertEqual(board.disputes["location-dispute"]["resolution"]["investigator"], "inspector-he")

    # ------------------------------------------------------------------
    # Medical lateness and clearance preconditions
    # ------------------------------------------------------------------
    def test_late_medical_transport_after_escalation_is_accepted(self):
        board = PersonnelMuster()
        board.register_person(
            "p1", "Medical Worker", "contractor", employer="CoolServ",
            employment_verified=True, shift="night", entry_area="inner-room",
        )
        board.record_entry("p1", 8, "inner-room")
        board.record_radio("radio-1", "p1", 12, "inner-room")
        board.dispatch_search(
            "order-med", "p1", "rescue-team", 10, accepted_by="lead",
            escalate_at=20, medical_team="unit-9", medical_receiver="dispatch-nurse",
        )
        due = board.process_escalations(21)
        self.assertEqual(len(due["due"]), 1)
        board.confirm_rescue_recovery("recovery-med", "p1", 25, order_id="order-med", rescued_by="rescuer")

        handoff = board.complete_medical_handoff(
            "handoff-late",
            "p1",
            45,
            recovery_id="recovery-med",
            received_by="nurse",
            medical_team="unit-9",
            transport_requested_at=10,
            transport_arrived_at=40,
            deadline=30,
            recorded_at=50,
        )
        self.assertEqual(handoff["decision"], "medical_handoff_verified")
        self.assertTrue(handoff["transport_late"])
        self.assertEqual(handoff["transport_response_seconds"], 30)
        self.assertEqual(handoff["recorded_late_seconds"], 5)

        report = board.person_report("p1")
        self.assertEqual(report["status"], MEDICAL_HANDOVER)
        self.assertTrue(report["accounted"])
        self.assertIsNone(report["responsible_team"])

    def test_controlled_clearance_requires_real_accepted_handover_owner(self):
        board = PersonnelMuster()
        board.register_person(
            "p1", "No Owner", "contractor", employer="CoolServ", employment_verified=True,
            shift="night", entry_area="inner-room",
        )
        board.record_entry("p1", 10)
        board.record_radio("radio", "p1", 15, "inner-room")
        board.order_controlled_isolation("iso", commander_id="cdr", at=16)
        board.dispatch_search("order-unaccepted", "p1", "team", 17, timeout_minutes=10)

        blocked = board.sign_clearance("controlled", "cdr", 18, scope="controlled")
        self.assertEqual(blocked["decision"], "clearance_blocked")
        self.assertTrue(any("real_handover_not_accepted" in item for item in blocked["blockers"]))

        accepted = board.accept_dispatch("order-unaccepted", "lead", 18)
        self.assertEqual(accepted["decision"], "dispatch_accepted")
        signed = board.sign_clearance("controlled-2", "cdr", 19, scope="controlled")
        self.assertEqual(signed["decision"], "clearance_signed")

    # ------------------------------------------------------------------
    # Full path-level requirements
    # ------------------------------------------------------------------
    def test_assisted_evacuation_path_produces_individual_assembly_and_restart_basis(self):
        board = self._run_assisted_evacuation_path()
        report = board.muster_report(51)
        self.assertTrue(report["all_accounted"])
        self.assertEqual(set(report["assembled"]), {"c7", "b2"})
        self.assertEqual(report["pending"], [])
        self.assertEqual(report["valid_final_clearance_id"], "final-a")
        restart = board.restart_assessment(51)
        self.assertTrue(restart["restart_authorized"])
        self.assertEqual(restart["earliest_restart_at"], 50)
        self.assertTrue(board.verify_journal_chain())

        family = board.briefing("family", "c7", now=51)
        self.assertIn("集合点", family["headline_zh"])
        self.assertIn("sign-c7", family["verification_refs"])

    def test_controlled_isolation_path_keeps_rescue_medical_task_and_blocks_early_restart(self):
        board = self._run_controlled_rescue_path()

        early_family = board.briefing("family", "c7", now=35)
        self.assertIn("尚未完成本人安全确认", early_family["headline_zh"])
        self.assertTrue(any("升级期限：40" in line for line in early_family["may_state_zh"]))
        self.assertTrue(any("不得仅凭代刷门禁" in line for line in early_family["must_not_state_zh"]))

        early_restart = board.restart_assessment(55)
        self.assertFalse(early_restart["restart_authorized"])
        self.assertIn("personnel_muster_incomplete", early_restart["blockers"])

        report = board.muster_report(81)
        self.assertTrue(report["all_accounted"])
        self.assertEqual(report["medical_handover"], ["c7"])
        self.assertEqual(report["assembled"], ["b2"])
        self.assertEqual(report["valid_final_clearance_id"], "final-b")

        family = board.briefing("family", "c7", now=81)
        self.assertIn("医疗组交接", family["headline_zh"])
        self.assertIn("handoff-b", family["verification_refs"])
        employer = board.briefing("employer", "c7", now=81)
        self.assertTrue(any("CoolServ" in line for line in employer["may_state_zh"]))

        restart = board.restart_assessment(81)
        self.assertTrue(restart["restart_authorized"])
        self.assertEqual(restart["earliest_restart_at"], 80)
        self.assertTrue(board.verify_journal_chain())

    def test_same_initial_state_supports_two_distinct_but_verifiable_outcomes(self):
        assisted = self._run_assisted_evacuation_path()
        controlled = self._run_controlled_rescue_path()

        assisted_person = assisted.person_report("c7")
        controlled_person = controlled.person_report("c7")

        self.assertEqual(assisted_person["status"], ASSEMBLED)
        self.assertEqual(controlled_person["status"], MEDICAL_HANDOVER)
        self.assertEqual(assisted_person["current_location"]["area"], "assembly-point")
        self.assertEqual(controlled_person["current_location"]["area"], "surface-medical-receiving")
        self.assertEqual(assisted_person["last_credible_location"]["area"], "inner-equipment-room")
        self.assertEqual(controlled_person["last_credible_location"]["area"], "inner-equipment-room")
        self.assertEqual(
            assisted.assignments["order-assisted-a"]["task"],
            "assisted_backup_route_evacuation",
        )
        self.assertIn("locate inner room", controlled.assignments["order-rescue-b"]["task"])
        self.assertTrue(assisted.route_report("backup-route")["safe_confirmed"])
        self.assertTrue(controlled.isolation["status"] == "active")

        assisted_restart = assisted.restart_assessment(51)
        controlled_at_assisted_time = controlled.restart_assessment(51)
        controlled_restart = controlled.restart_assessment(81)
        self.assertTrue(assisted_restart["restart_authorized"])
        self.assertFalse(controlled_at_assisted_time["restart_authorized"])
        self.assertTrue(controlled_restart["restart_authorized"])
        self.assertGreater(controlled_restart["earliest_restart_at"], assisted_restart["earliest_restart_at"])

        assisted_statement = assisted.briefing("family", "c7", now=51)["headline_zh"]
        controlled_statement = controlled.briefing("family", "c7", now=35)["headline_zh"]
        self.assertNotEqual(assisted_statement, controlled_statement)
        self.assertIn("集合点", assisted_statement)
        self.assertIn("尚未完成", controlled_statement)

    def test_dossier_provides_verifiable_location_and_handover_chain(self):
        board = self._run_controlled_rescue_path()
        dossier = board.person_dossier("c7", now=81)
        self.assertEqual(dossier["identity"]["employer"], "CoolServ Industrial Services")
        self.assertEqual(dossier["last_credible_location"]["area"], "inner-equipment-room")
        source_ids = {event["evidence_id"] for event in dossier["evidence_timeline"]}
        self.assertIn("radio-c7-25", source_ids)
        self.assertIn("badge-out-c7", source_ids)
        self.assertIn("recovery-b", source_ids)
        self.assertIn("handoff-b", source_ids)
        self.assertTrue(dossier["journal_intact"])

        commander_brief = board.briefing("commander", "c7", now=81)
        self.assertEqual(commander_brief["audience"], "commander")
        self.assertIsNotNone(commander_brief["dossier"])
        self.assertTrue(commander_brief["dossier"]["journal_intact"])


if __name__ == "__main__":
    unittest.main()
