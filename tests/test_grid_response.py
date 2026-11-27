import unittest

from tenurecourt.store import connect, execute, one, rows
from tenurecourt.grid_offer import offer
from tenurecourt.dispatch_note import provisional_reduction
from tenurecourt import grid_response as gr


class GridResponseTest(unittest.TestCase):
    def setUp(self):
        self.db = connect(":memory:")

    def tearDown(self):
        self.db.close()

    def site(self, site, **kw):
        cfg = dict(temp_coeff=0.0, closure_tol=0.5, drift_tol=2, slot_hours=1.0, rate=10.0, diesel_cost=3.0)
        cfg.update(kw)
        return gr.configure_site(self.db, site, **cfg)

    def days(self, site, loads=(100.0, 100.0, 100.0, 100.0), day_type="weekday", temp=30.0):
        for i, load in enumerate(loads):
            self.assertEqual(gr.record_day(self.db, site, f"{site}-d{i}", day_type, temp, load)["decision"], "recorded")

    def test_offer_is_provisional_until_metered(self):
        db = self.db
        self.site("site-a")
        self.days("site-a")
        gr.instruction(db, "g1", "site-a", 12, 20, 21, 15, "weekday", 30.0)
        self.assertEqual(provisional_reduction(db, "site-a", 20), 0)
        offer(db, "o1", "site-a", 12, 20, 30)
        # the offer is provisionally counted...
        self.assertEqual(provisional_reduction(db, "site-a", 20), 12)
        # ...but the utility meter shows no reduction during the window
        gr.record_reading(db, "m20", "site-a", 20, utility=100, server_load=100, meter_at=20)
        gr.record_reading(db, "m21", "site-a", 21, utility=100, server_load=100, meter_at=21)
        adj = gr.adjudicate(db, "g1")
        self.assertEqual(adj["decision"], "non_performance")
        self.assertEqual(adj["reason"], "shortfall")
        self.assertEqual(adj["baseline_kw"], 100.0)
        self.assertEqual(adj["delivered_kw"], {20: 0.0, 21: 0.0})

    def test_battery_delivery_with_purchased_backup(self):
        db = self.db
        self.site("site-b")
        gr.register_battery(db, "site-b", 100, 100, 40)
        self.days("site-b")
        gr.register_contract(db, "cust-1", 2, 5.0)
        gr.register_job(db, "j1", "site-b", "cust-1", "batch", 30, 20, 21)
        gr.instruction(db, "g1", "site-b", 12, 20, 21, 15, "weekday", 30.0)
        r = gr.respond_battery(db, "g1", 12, 16, guarantee={"id": "gty1", "provider": "backup-co", "kw": 12, "cost": 50.0})
        self.assertEqual(r["decision"], "committed")
        for slot in (20, 21):
            res = gr.record_reading(db, f"m{slot}", "site-b", slot, utility=88, batt_dis=12, server_load=100, meter_at=slot)
            self.assertEqual(res["decision"], "recorded")
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["adjudication"], "performed")
        body = st["receipt"]["body"]
        self.assertEqual(st["receipt"]["reward"], 240.0)
        self.assertEqual(body["sources"]["battery_kwh"], 24.0)
        self.assertEqual(body["guarantee_cost"], 50.0)
        self.assertEqual(body["compensation"], 0.0)
        self.assertEqual(body["net"], 190.0)
        # customer job clocks were kept: j1 finishes at its original end
        fin = gr.finish_jobs(db, "site-b", 22)
        self.assertEqual(fin["completed"], [{"job": "j1", "completion_at": 21}])
        led = {l["kind"]: l["amount"] for l in gr.ledger(db, "site-b")}
        self.assertEqual(led["reward"], 240.0)
        self.assertEqual(led["guarantee_cost"], -50.0)
        # reserve never dipped below the customer disaster-recovery floor
        soc = gr.battery_status(db, "site-b", 21)
        self.assertEqual(soc["soc_kwh"], 76.0)
        self.assertTrue(soc["above_reserve"])

    def test_demand_delivery_with_customer_compensation(self):
        db = self.db
        self.site("site-d")
        gr.register_battery(db, "site-d", 100, 100, 40)
        self.days("site-d")
        gr.register_contract(db, "cust-1", 2, 5.0)
        gr.register_job(db, "j1", "site-d", "cust-1", "batch", 8, 20, 21)
        gr.register_job(db, "j2", "site-d", "cust-1", "batch", 6, 20, 21)
        gr.register_job(db, "j3", "site-d", "cust-1", "production", 10, 20, 21)
        gr.register_job(db, "j4", "site-d", "cust-1", "batch", 5, 20, 21)
        gr.permit_delay(db, "j1", "cust-1-rep", 2)
        gr.permit_delay(db, "j2", "cust-1-rep", 2)
        # the customer lease is live and production writes flow...
        execute(db, "tenure", {"op": "namespace", "id": "ns", "uid": "u1", "generation": 1})
        execute(db, "tenure", {"op": "lease", "id": "l1", "namespace": "ns", "namespace_uid": "u1",
                               "holder": "cust-1", "generation": 1, "start": 1, "expires": 100})
        w = execute(db, "tenure", {"op": "write", "id": "w1", "namespace": "ns", "team": "cust-1",
                                   "generation": 1, "resource": "doc", "at": 20})
        self.assertEqual(w["decision"], "written")
        gr.instruction(db, "g1", "site-d", 12, 20, 21, 15, "weekday", 30.0)
        # ...but a live lease does not let the operator pause production writes
        self.assertEqual(gr.respond_demand(db, "g1", 16, delays=[("j3", 1)])["decision"], "production_write_protected")
        self.assertEqual(gr.respond_demand(db, "g1", 16, delays=[("j1", 3)])["decision"], "delay_beyond_contract")
        self.assertEqual(gr.respond_demand(db, "g1", 16, delays=[("j4", 1)])["decision"], "permit_required")
        r = gr.respond_demand(db, "g1", 16, delays=[("j1", 1), ("j2", 1)])
        self.assertEqual(r["decision"], "committed")
        for slot in (20, 21):
            gr.record_reading(db, f"m{slot}", "site-d", slot, utility=86, server_load=86, meter_at=slot)
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["adjudication"], "performed")
        # same kWh to the grid as the battery path, different customer outcome
        self.assertEqual(st["receipt"]["reward"], 240.0)
        body = st["receipt"]["body"]
        self.assertEqual(body["sources"]["demand_kwh"], 28.0)
        self.assertEqual(body["compensation"], 70.0)
        self.assertEqual(body["net"], 170.0)
        comp = {l["ref"]: l for l in gr.ledger(db, "site-d", kind="compensation")}
        self.assertEqual(comp["j1"]["amount"], -40.0)
        self.assertEqual(comp["j1"]["status"], "paid")
        self.assertEqual(comp["j2"]["amount"], -30.0)
        self.assertEqual(comp["j2"]["status"], "paid")
        # battery reserve untouched on this path
        self.assertEqual(gr.battery_status(db, "site-d", 21)["soc_kwh"], 100.0)
        # production and undelayed batch finish on time; delayed jobs one slot late
        fin = gr.finish_jobs(db, "site-d", 21)
        self.assertEqual({c["job"] for c in fin["completed"]}, {"j3", "j4"})
        fin = gr.finish_jobs(db, "site-d", 22)
        self.assertEqual({c["job"] for c in fin["completed"]}, {"j1", "j2"})
        self.assertTrue(all(c["completion_at"] == 22 for c in fin["completed"]))
        impacts = rows(db, "SELECT * FROM grid_impacts WHERE site='site-d' AND kind='delayed'")
        self.assertEqual({i["job"] for i in impacts}, {"j1", "j2"})

    def test_baseline_day_selection_is_rule_based_not_cherry_picked(self):
        db = self.db
        self.site("site-x", temp_coeff=0.5)
        gr.record_day(db, "site-x", "d1", "weekday", 30.0, 100.0)
        gr.record_day(db, "site-x", "d2", "weekday", 31.0, 102.0)
        gr.record_day(db, "site-x", "d3", "weekday", 29.0, 98.0)
        gr.record_day(db, "site-x", "d4", "weekday", 30.0, 101.0)
        gr.record_day(db, "site-x", "d5", "weekday", 35.0, 180.0)  # inflated day
        gr.record_day(db, "site-x", "d6", "weekend", 35.0, 200.0)  # wrong day type
        # re-registering a day cannot rewrite its measured load
        self.assertEqual(gr.record_day(db, "site-x", "d1", "weekday", 30.0, 150.0)["decision"], "duplicate_day")
        self.assertEqual(one(db, "SELECT avg_load FROM grid_baseline_days WHERE day='d1'")["avg_load"], 100.0)
        # maintenance scheduled before the instruction lowers the counterfactual;
        # maintenance scheduled after it does not
        gr.schedule_maintenance(db, "mnt1", "site-x", 20, 21, 10.0, 5)
        gr.schedule_maintenance(db, "mnt2", "site-x", 20, 21, 50.0, 12)
        base = gr.compute_baseline(db, "site-x", "weekday", 35.0, 20, 21, as_of=10)
        self.assertEqual(base["decision"], "ok")
        self.assertEqual(set(base["included"]), {"d1", "d2", "d4"})
        reasons = {e["day"]: e["reason"] for e in base["excluded"]}
        self.assertEqual(reasons["d5"], "outlier_high")
        self.assertEqual(reasons["d3"], "outlier_low")
        self.assertEqual(reasons["d6"], "day_type_mismatch")
        self.assertEqual(base["maintenance_kw"], 10.0)
        # mean(100,101,102)=101, mean temp 91/3, +0.5*(35-91/3), -10 maintenance
        self.assertAlmostEqual(base["baseline_kw"], 101.0 + 0.5 * (35.0 - 91.0 / 3) - 10.0)
        # the cherry-picked 180/200 days must not pump the baseline
        self.assertLess(base["baseline_kw"], 100.0)

    def test_meter_clock_drift_disputes_reading_until_corrected(self):
        db = self.db
        self.site("site-c")
        gr.register_battery(db, "site-c", 100, 100, 40)
        self.days("site-c")
        gr.instruction(db, "g1", "site-c", 12, 20, 21, 15, "weekday", 30.0)
        gr.respond_battery(db, "g1", 12, 16, guarantee={"id": "gty1", "provider": "p", "kw": 12, "cost": 50.0})
        self.assertEqual(
            gr.record_reading(db, "m20", "site-c", 20, utility=88, batt_dis=12, server_load=100, meter_at=20)["decision"],
            "recorded",
        )
        r = gr.record_reading(db, "m21", "site-c", 21, utility=88, batt_dis=12, server_load=100, meter_at=30)
        self.assertEqual(r["decision"], "clock_drift")
        adj = gr.adjudicate(db, "g1")
        self.assertEqual(adj["decision"], "non_performance")
        self.assertEqual(adj["reason"], "evidence_missing")
        self.assertEqual(adj["missing_slots"], [21])
        # a fresh id cannot overwrite the slot; correction must be versioned
        self.assertEqual(
            gr.record_reading(db, "m21b", "site-c", 21, utility=88, batt_dis=12, server_load=100, meter_at=21)["decision"],
            "slot_occupied",
        )
        c = gr.correct_reading(db, "m21-v2", "m21", "meter clock resynced", 22,
                               utility=88, batt_dis=12, server_load=100, meter_at=21)
        self.assertEqual(c["decision"], "corrected")
        old = one(db, "SELECT * FROM grid_readings WHERE id='m21'")
        self.assertEqual(old["status"], "superseded")
        self.assertEqual(old["meter_at"], 30)  # original version retained
        self.assertEqual(gr.adjudicate(db, "g1")["decision"], "performed")

    def test_energy_must_close_and_diesel_is_not_free(self):
        db = self.db
        self.site("site-e")
        gr.register_battery(db, "site-e", 100, 100, 40)
        self.days("site-e")
        gr.instruction(db, "g1", "site-e", 12, 20, 21, 15, "weekday", 30.0)
        gr.respond_battery(db, "g1", 12, 16, guarantee={"id": "gty1", "provider": "p", "kw": 12, "cost": 50.0})
        # supply (utility+diesel+discharge) must equal use (server load+charge)
        bad = gr.record_reading(db, "m20", "site-e", 20, utility=88, batt_dis=12, server_load=50, meter_at=20)
        self.assertEqual(bad["decision"], "unclosed")
        self.assertEqual(gr.adjudicate(db, "g1")["reason"], "evidence_missing")
        gr.correct_reading(db, "m20-v2", "m20", "fix server load telemetry", 22,
                           utility=78, diesel=10, batt_dis=12, server_load=100, meter_at=20)
        gr.record_reading(db, "m21", "site-e", 21, utility=78, diesel=10, batt_dis=12, server_load=100, meter_at=21)
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["adjudication"], "performed")
        body = st["receipt"]["body"]
        # load shifted to diesel still settles with the grid, but is itemized
        # and charged instead of masquerading as free savings
        self.assertEqual(body["sources"]["diesel_kwh"], 20.0)
        self.assertEqual(body["diesel_cost"], 60.0)
        self.assertEqual(body["net"], 240.0 - 60.0 - 50.0)
        led = {l["kind"]: l["amount"] for l in gr.ledger(db, "site-e")}
        self.assertEqual(led["reward"], 240.0)
        self.assertEqual(led["diesel_cost"], -60.0)

    def test_battery_recharge_spans_periods_and_reserve_floor_holds(self):
        db = self.db
        self.site("site-r")
        gr.register_battery(db, "site-r", 100, 100, 40, charge_eff=1.0)
        self.days("site-r")
        gr.instruction(db, "g1", "site-r", 12, 10, 11, 5, "weekday", 30.0)
        gr.respond_battery(db, "g1", 12, 6, guarantee={"id": "gty1", "provider": "p", "kw": 12, "cost": 50.0})
        gr.record_reading(db, "m10", "site-r", 10, utility=88, batt_dis=12, server_load=100, meter_at=10)
        gr.record_reading(db, "m11", "site-r", 11, utility=88, batt_dis=12, server_load=100, meter_at=11)
        # recharge lands in later slots and raises utility draw there
        gr.record_reading(db, "m12", "site-r", 12, utility=106, batt_chg=6, server_load=100, meter_at=12)
        gr.record_reading(db, "m13", "site-r", 13, utility=106, batt_chg=6, server_load=100, meter_at=13)
        st = gr.settle(db, "g1", 14)
        self.assertEqual(st["adjudication"], "performed")
        self.assertEqual(st["receipt"]["reward"], 240.0)
        soc = gr.battery_status(db, "site-r", 13)
        self.assertEqual(soc["soc_kwh"], 88.0)  # 100 - 24 discharged + 12 recharged
        # a second instruction during recharge may not eat the DR reserve
        gr.instruction(db, "g2", "site-r", 50, 14, 15, 13, "weekday", 30.0)
        r = gr.respond_battery(db, "g2", 50, 14, guarantee={"id": "gty2", "provider": "p", "kw": 50, "cost": 80.0})
        self.assertEqual(r["decision"], "reserve_floor")
        self.assertIsNone(one(db, "SELECT * FROM grid_responses WHERE instruction='g2'"))

    def test_cancelled_job_voids_its_compensation(self):
        db = self.db
        self.site("site-j")
        self.days("site-j")
        gr.register_contract(db, "cust-1", 2, 5.0)
        gr.register_job(db, "j1", "site-j", "cust-1", "batch", 8, 20, 21)
        gr.register_job(db, "j2", "site-j", "cust-1", "batch", 6, 20, 21)
        gr.permit_delay(db, "j1", "rep", 2)
        gr.permit_delay(db, "j2", "rep", 2)
        gr.instruction(db, "g1", "site-j", 12, 20, 21, 15, "weekday", 30.0)
        gr.respond_demand(db, "g1", 16, delays=[("j1", 1), ("j2", 1)])
        self.assertEqual(gr.cancel_job(db, "j2", 17)["decision"], "cancelled")
        for slot in (20, 21):
            gr.record_reading(db, f"m{slot}", "site-j", slot, utility=86, server_load=86, meter_at=slot)
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["adjudication"], "performed")
        comp = {l["ref"]: l for l in gr.ledger(db, "site-j", kind="compensation")}
        self.assertEqual(comp["j1"]["status"], "paid")
        self.assertEqual(comp["j2"]["status"], "void")
        self.assertEqual(st["receipt"]["body"]["compensation"], 40.0)
        impacts = rows(db, "SELECT * FROM grid_impacts WHERE kind='cancelled'")
        self.assertEqual([i["job"] for i in impacts], ["j2"])

    def test_outage_requires_proven_backup_or_marks_default(self):
        db = self.db
        self.site("site-g")
        gr.register_battery(db, "site-g", 300, 300, 40)
        self.days("site-g")
        gr.register_job(db, "j1", "site-g", "cust-1", "batch", 30, 20, 22)
        gr.register_job(db, "j2", "site-g", "cust-1", "batch", 20, 30, 32)
        # path A where the purchased guarantee actually arrives
        gr.instruction(db, "g1", "site-g", 12, 20, 22, 15, "weekday", 30.0)
        gr.respond_battery(db, "g1", 12, 16, guarantee={"id": "gty1", "provider": "p", "kw": 12, "cost": 50.0})
        for slot in (20, 21, 22):
            gr.record_reading(db, f"m{slot}", "site-g", slot, utility=88, batt_dis=12, server_load=100, meter_at=slot)
        gr.deliver_guarantee(db, "gd1", "gty1", 21, 12)
        out = gr.record_outage(db, "out1", "site-g", 21, 21, 21)
        self.assertEqual(out["responses"][0]["decision"], "backup_verified")
        self.assertEqual(one(db, "SELECT status FROM grid_guarantees WHERE id='gty1'")["status"], "delivered")
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["adjudication"], "performed")
        self.assertEqual(st["receipt"]["body"]["excluded_slots"], [21])
        self.assertEqual(st["receipt"]["reward"], 240.0)  # the outage slot earns nothing
        # path A where the purchased guarantee never arrives
        gr.instruction(db, "g2", "site-g", 12, 30, 32, 25, "weekday", 30.0)
        gr.respond_battery(db, "g2", 12, 26, guarantee={"id": "gty2", "provider": "p", "kw": 12, "cost": 50.0})
        for slot in (30, 31, 32):
            gr.record_reading(db, f"n{slot}", "site-g", slot, utility=88, batt_dis=12, server_load=100, meter_at=slot)
        out = gr.record_outage(db, "out2", "site-g", 31, 31, 31)
        self.assertEqual(out["responses"][0]["decision"], "backup_default")
        impacts = rows(db, "SELECT * FROM grid_impacts WHERE kind='interrupted'")
        self.assertEqual([i["job"] for i in impacts], ["j2"])
        st = gr.settle(db, "g2", 35)
        self.assertEqual(st["adjudication"], "non_performance")
        self.assertEqual(st["receipt"]["body"]["reason"], "backup_default")
        kinds = [l["kind"] for l in gr.ledger(db, "site-g", instruction="g2")]
        self.assertNotIn("reward", kinds)
        self.assertEqual(one(db, "SELECT status FROM grid_guarantees WHERE id='gty2'")["status"], "defaulted")

    def test_demand_path_outage_owes_delayed_jobs_capacity(self):
        db = self.db
        self.site("site-o")
        gr.register_battery(db, "site-o", 100, 100, 40)
        self.days("site-o")
        gr.register_contract(db, "cust-1", 2, 5.0)
        gr.register_job(db, "j1", "site-o", "cust-1", "batch", 8, 20, 21)
        gr.register_job(db, "j2", "site-o", "cust-1", "batch", 6, 20, 21)
        gr.permit_delay(db, "j1", "rep", 2)
        gr.permit_delay(db, "j2", "rep", 2)
        gr.instruction(db, "g1", "site-o", 12, 20, 21, 15, "weekday", 30.0)
        gr.respond_demand(db, "g1", 16, delays=[("j1", 1), ("j2", 1)])
        gr.record_reading(db, "m20", "site-o", 20, utility=86, server_load=86, meter_at=20)
        out = gr.record_outage(db, "out1", "site-o", 21, 21, 21)
        self.assertEqual(out["responses"][0]["decision"], "recovery_owed")
        # the recorded cause cannot be flipped from demand to battery after the fact
        flip = gr.respond_battery(db, "g1", 12, 21, guarantee={"id": "gty9", "provider": "p", "kw": 12, "cost": 1.0})
        self.assertEqual(flip["decision"], "response_locked")
        jobs = {j["id"]: j["status"] for j in rows(db, "SELECT * FROM grid_jobs WHERE site='site-o'")}
        self.assertEqual(set(jobs.values()), {"awaiting_recovery"})
        # after recovery the previously delayed jobs get capacity
        rec = gr.recover_jobs(db, "site-o", 22, 20)
        self.assertEqual({a["job"] for a in rec["allocations"]}, {"j1", "j2"})
        fin = gr.finish_jobs(db, "site-o", 23)
        self.assertEqual({c["job"] for c in fin["completed"]}, {"j1", "j2"})
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["adjudication"], "performed")
        self.assertEqual(st["receipt"]["reward"], 120.0)  # only slot 20 is settlable
        comp = [l for l in gr.ledger(db, "site-o", kind="compensation") if l["status"] == "paid"]
        self.assertEqual(len(comp), 2)  # customers are still paid for the delay

    def test_duplicate_instruction_is_recorded_once(self):
        db = self.db
        self.site("site-dup")
        gr.register_battery(db, "site-dup", 100, 100, 40)
        self.days("site-dup")
        self.assertEqual(gr.instruction(db, "g1", "site-dup", 12, 20, 21, 15, "weekday", 30.0)["decision"], "registered")
        dup = gr.instruction(db, "g1", "site-dup", 99, 20, 21, 15, "weekday", 30.0)
        self.assertEqual(dup["decision"], "duplicate")
        ins = one(db, "SELECT * FROM grid_instructions WHERE id='g1'")
        self.assertEqual(ins["kw"], 12)  # original retained
        self.assertEqual(len(rows(db, "SELECT * FROM grid_instructions")), 1)
        gr.respond_battery(db, "g1", 12, 16, guarantee={"id": "gty1", "provider": "p", "kw": 12, "cost": 50.0})
        self.assertEqual(gr.respond_demand(db, "g1", 16, delays=[])["decision"], "response_locked")
        for slot in (20, 21):
            gr.record_reading(db, f"m{slot}", "site-dup", slot, utility=88, batt_dis=12, server_load=100, meter_at=slot)
        self.assertEqual(gr.settle(db, "g1", 25)["adjudication"], "performed")
        self.assertEqual(gr.settle(db, "g1", 26)["decision"], "already_settled")
        self.assertEqual(len(gr.ledger(db, "site-dup", kind="reward")), 1)

    def test_late_meter_correction_claws_back_and_repays(self):
        db = self.db
        self.site("site-m")
        gr.register_battery(db, "site-m", 100, 100, 40)
        self.days("site-m")
        gr.instruction(db, "g1", "site-m", 12, 20, 21, 15, "weekday", 30.0)
        gr.respond_battery(db, "g1", 12, 16, guarantee={"id": "gty1", "provider": "p", "kw": 12, "cost": 50.0})
        gr.record_reading(db, "m20", "site-m", 20, utility=88, batt_dis=12, server_load=100, meter_at=20)
        gr.record_reading(db, "m21", "site-m", 21, utility=88, batt_dis=12, server_load=100, meter_at=21)
        st = gr.settle(db, "g1", 25)
        self.assertEqual(st["receipt"]["reward"], 240.0)
        # late correction: slot 20 actually drew more utility than reported
        c = gr.correct_reading(db, "m20-v2", "m20", "revenue meter recalibration", 40,
                               utility=95, batt_dis=5, server_load=100, meter_at=20)
        self.assertEqual(c["decision"], "corrected")
        self.assertEqual(len(c["resettled"]), 1)
        r2 = gr.receipt(db, "g1")
        self.assertEqual(r2["version"], 2)
        self.assertEqual(r2["decision"], "non_performance")
        claw = gr.ledger(db, "site-m", kind="clawback")
        self.assertEqual(len(claw), 1)
        self.assertEqual(claw[0]["amount"], -240.0)
        # original reading and original receipt are retained
        self.assertEqual(one(db, "SELECT utility FROM grid_readings WHERE id='m20'")["utility"], 88)
        self.assertEqual(one(db, "SELECT status FROM grid_readings WHERE id='m20'")["status"], "superseded")
        self.assertIsNotNone(one(db, "SELECT * FROM grid_receipts WHERE id='rcpt-g1-v1'"))
        # a further correction restoring the evidence repays the clawback
        c = gr.correct_reading(db, "m20-v3", "m20-v2", "recalibration reversed on audit", 50,
                               utility=88, batt_dis=12, server_load=100, meter_at=20)
        self.assertEqual(c["decision"], "corrected")
        r3 = gr.receipt(db, "g1")
        self.assertEqual(r3["version"], 3)
        self.assertEqual(r3["decision"], "performed")
        add = gr.ledger(db, "site-m", kind="additional_reward")
        self.assertEqual(len(add), 1)
        self.assertEqual(add[0]["amount"], 240.0)
        # correcting a superseded version is refused
        stale = gr.correct_reading(db, "m20-v4", "m20", "stale", 60,
                                   utility=88, batt_dis=12, server_load=100, meter_at=20)
        self.assertEqual(stale["decision"], "already_superseded")


if __name__ == "__main__":
    unittest.main()
