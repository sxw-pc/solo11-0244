"""Executable grid demand response: baseline, metering, delivery, settlement.

grid_offer/dispatch_note record what the facility *promised*; this module
adjudicates what it *delivered*, so a settlement statement can never claim a
reduction the utility meter did not show.

- The counterfactual baseline is rule-based (same-type measured days,
  temperature-adjusted, minus pre-scheduled maintenance) so it cannot be
  inflated by cherry-picking high-load days.
- Utility meter, UPS/battery discharge, diesel generation and server load
  must close the energy balance on one shared timeline; load shifted to
  diesel is itemized and charged, never booked as free savings.
- Customer obligations are enforced before grid revenue: batch delays need a
  contract limit and a customer permit, production writes cannot be paused,
  compensation is actually paid, and discharge stops at the disaster-recovery
  reserve floor.
- Instructions, responses, readings, impacted jobs and receipts all keep
  their original versions; late metering corrections re-settle into real
  clawbacks or repayments.
"""

import json

from .store import one, rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS grid_sites(
 site TEXT PRIMARY KEY,temp_coeff REAL,closure_tol REAL,drift_tol INTEGER,
 slot_hours REAL,rate REAL,diesel_cost REAL);
CREATE TABLE IF NOT EXISTS grid_battery(
 site TEXT PRIMARY KEY,capacity_kwh REAL,soc_kwh REAL,reserve_min_kwh REAL,charge_eff REAL);
CREATE TABLE IF NOT EXISTS grid_baseline_days(
 site TEXT,day TEXT,day_type TEXT,temp REAL,avg_load REAL,PRIMARY KEY(site,day));
CREATE TABLE IF NOT EXISTS grid_maintenance(
 id TEXT PRIMARY KEY,site TEXT,start INTEGER,end INTEGER,kw_drop REAL,scheduled_at INTEGER);
CREATE TABLE IF NOT EXISTS grid_contracts(
 customer TEXT PRIMARY KEY,max_batch_delay INTEGER,comp_per_kw_slot REAL);
CREATE TABLE IF NOT EXISTS grid_jobs(
 id TEXT PRIMARY KEY,site TEXT,customer TEXT,kind TEXT,kw REAL,
 start INTEGER,end INTEGER,delay INTEGER,status TEXT,completion_at INTEGER);
CREATE TABLE IF NOT EXISTS grid_permits(job TEXT,actor TEXT,max_delay INTEGER,PRIMARY KEY(job,actor));
CREATE TABLE IF NOT EXISTS grid_instructions(
 id TEXT PRIMARY KEY,site TEXT,kw REAL,start INTEGER,end INTEGER,
 issued_at INTEGER,day_type TEXT,temp REAL,status TEXT);
CREATE TABLE IF NOT EXISTS grid_responses(
 id TEXT PRIMARY KEY,instruction TEXT,path TEXT,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS grid_response_events(
 id TEXT PRIMARY KEY,response TEXT,at INTEGER,status TEXT,note TEXT);
CREATE TABLE IF NOT EXISTS grid_response_actions(
 response TEXT,kind TEXT,ref TEXT,kw REAL,delay INTEGER);
CREATE TABLE IF NOT EXISTS grid_guarantees(
 id TEXT PRIMARY KEY,response TEXT,provider TEXT,kw REAL,cost REAL,status TEXT);
CREATE TABLE IF NOT EXISTS grid_guarantee_deliveries(
 id TEXT PRIMARY KEY,guarantee TEXT,at INTEGER,delivered_kw REAL);
CREATE TABLE IF NOT EXISTS grid_readings(
 id TEXT PRIMARY KEY,site TEXT,at INTEGER,utility REAL,diesel REAL,
 batt_dis REAL,batt_chg REAL,server_load REAL,meter_at INTEGER,
 status TEXT,version INTEGER,supersedes TEXT,reason TEXT);
CREATE TABLE IF NOT EXISTS grid_outages(
 id TEXT PRIMARY KEY,site TEXT,start INTEGER,end INTEGER,recorded_at INTEGER);
CREATE TABLE IF NOT EXISTS grid_impacts(
 id TEXT PRIMARY KEY,site TEXT,job TEXT,kind TEXT,start INTEGER,end INTEGER,note TEXT);
CREATE TABLE IF NOT EXISTS grid_ledger(
 id TEXT PRIMARY KEY,site TEXT,instruction TEXT,kind TEXT,ref TEXT,
 customer TEXT,amount REAL,status TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS grid_receipts(
 id TEXT PRIMARY KEY,instruction TEXT,version INTEGER,at INTEGER,
 decision TEXT,reward REAL,body TEXT);
"""


def _ensure(db):
    db.executescript(SCHEMA)


def _site(db, site):
    return one(db, "SELECT * FROM grid_sites WHERE site=?", (site,))


def _ledger(db, site, instruction, kind, ref, customer, amount, status, at):
    n = len(rows(db, "SELECT id FROM grid_ledger")) + 1
    db.execute(
        "INSERT INTO grid_ledger VALUES(?,?,?,?,?,?,?,?,?)",
        (f"led-{n}", site, instruction, kind, ref, customer, amount, status, at),
    )


def _impact(db, site, job, kind, start, end, note):
    n = len(rows(db, "SELECT id FROM grid_impacts")) + 1
    db.execute(
        "INSERT INTO grid_impacts VALUES(?,?,?,?,?,?,?)",
        (f"imp-{n}", site, job, kind, start, end, note),
    )


def _response_event(db, response, at, status, note):
    n = len(rows(db, "SELECT id FROM grid_response_events WHERE response=?", (response,))) + 1
    db.execute(
        "INSERT INTO grid_response_events VALUES(?,?,?,?,?)",
        (f"{response}-ev{n}", response, at, status, note),
    )


# --- site, battery, baseline inputs ---------------------------------------


def configure_site(db, site, temp_coeff=0.0, closure_tol=0.5, drift_tol=2, slot_hours=1.0, rate=0.0, diesel_cost=0.0):
    _ensure(db)
    db.execute(
        "INSERT INTO grid_sites VALUES(?,?,?,?,?,?,?) ON CONFLICT(site) DO UPDATE SET "
        "temp_coeff=excluded.temp_coeff,closure_tol=excluded.closure_tol,drift_tol=excluded.drift_tol,"
        "slot_hours=excluded.slot_hours,rate=excluded.rate,diesel_cost=excluded.diesel_cost",
        (site, temp_coeff, closure_tol, drift_tol, slot_hours, rate, diesel_cost),
    )
    return {"decision": "configured"}


def register_battery(db, site, capacity_kwh, soc_kwh, reserve_min_kwh, charge_eff=1.0):
    _ensure(db)
    if one(db, "SELECT site FROM grid_battery WHERE site=?", (site,)):
        return {"decision": "duplicate_site"}
    if not 0 <= reserve_min_kwh <= soc_kwh <= capacity_kwh:
        return {"decision": "invalid_battery"}
    db.execute(
        "INSERT INTO grid_battery VALUES(?,?,?,?,?)",
        (site, capacity_kwh, soc_kwh, reserve_min_kwh, charge_eff),
    )
    return {"decision": "registered"}


def battery_status(db, site, at):
    """State of charge derived from the registered start plus metered flows."""
    _ensure(db)
    b = one(db, "SELECT * FROM grid_battery WHERE site=?", (site,))
    if not b:
        return None
    cfg = _site(db, site)
    slot = cfg["slot_hours"] if cfg else 1.0
    soc = b["soc_kwh"]
    for r in rows(db, "SELECT * FROM grid_readings WHERE site=? AND at<=? AND status='valid' ORDER BY at", (site, at)):
        soc += (r["batt_chg"] * b["charge_eff"] - r["batt_dis"]) * slot
    return {
        "site": site,
        "at": at,
        "soc_kwh": soc,
        "reserve_min_kwh": b["reserve_min_kwh"],
        "above_reserve": soc >= b["reserve_min_kwh"],
    }


def record_day(db, site, day, day_type, temp, avg_load):
    _ensure(db)
    if one(db, "SELECT day FROM grid_baseline_days WHERE site=? AND day=?", (site, day)):
        return {"decision": "duplicate_day"}
    db.execute("INSERT INTO grid_baseline_days VALUES(?,?,?,?,?)", (site, day, day_type, temp, avg_load))
    return {"decision": "recorded"}


def schedule_maintenance(db, id, site, start, end, kw_drop, scheduled_at):
    _ensure(db)
    if one(db, "SELECT id FROM grid_maintenance WHERE id=?", (id,)):
        return {"decision": "duplicate_maintenance"}
    db.execute("INSERT INTO grid_maintenance VALUES(?,?,?,?,?,?)", (id, site, start, end, kw_drop, scheduled_at))
    return {"decision": "scheduled"}


def compute_baseline(db, site, day_type, temp, start, end, as_of):
    """Auditable counterfactual load for the window [start, end].

    Same-type measured days only; with four or more candidates the highest
    and lowest day are trimmed symmetrically so no cherry-picked hot day can
    pump the baseline. Temperature-adjusted, then reduced by maintenance that
    was already scheduled before the instruction was issued.
    """
    _ensure(db)
    cfg = _site(db, site)
    coeff = cfg["temp_coeff"] if cfg else 0.0
    candidates = rows(db, "SELECT * FROM grid_baseline_days WHERE site=?", (site,))
    excluded = [{"day": d["day"], "reason": "day_type_mismatch"} for d in candidates if d["day_type"] != day_type]
    pool = sorted((d for d in candidates if d["day_type"] == day_type), key=lambda d: d["avg_load"])
    if len(pool) >= 4:
        excluded += [
            {"day": pool[0]["day"], "reason": "outlier_low"},
            {"day": pool[-1]["day"], "reason": "outlier_high"},
        ]
        pool = pool[1:-1]
    if not pool:
        return {"decision": "no_baseline_days", "excluded": excluded}
    base = sum(d["avg_load"] for d in pool) / len(pool)
    temps = sum(d["temp"] for d in pool) / len(pool)
    maintenance = rows(
        db,
        "SELECT * FROM grid_maintenance WHERE site=? AND scheduled_at<? AND start<=? AND end>=?",
        (site, as_of, end, start),
    )
    drop = sum(m["kw_drop"] for m in maintenance)
    return {
        "decision": "ok",
        "baseline_kw": base + coeff * (temp - temps) - drop,
        "included": [d["day"] for d in pool],
        "excluded": excluded,
        "temp_adjustment": coeff * (temp - temps),
        "maintenance_kw": drop,
    }


# --- customer contracts, jobs, permits ------------------------------------


def register_contract(db, customer, max_batch_delay, comp_per_kw_slot):
    _ensure(db)
    if one(db, "SELECT customer FROM grid_contracts WHERE customer=?", (customer,)):
        return {"decision": "duplicate_contract"}
    db.execute("INSERT INTO grid_contracts VALUES(?,?,?)", (customer, max_batch_delay, comp_per_kw_slot))
    return {"decision": "registered"}


def register_job(db, id, site, customer, kind, kw, start, end):
    _ensure(db)
    if one(db, "SELECT id FROM grid_jobs WHERE id=?", (id,)):
        return {"decision": "duplicate_job"}
    if kind not in ("batch", "production"):
        return {"decision": "invalid_kind"}
    db.execute(
        "INSERT INTO grid_jobs VALUES(?,?,?,?,?,?,?,0,'scheduled',NULL)",
        (id, site, customer, kind, kw, start, end),
    )
    return {"decision": "registered"}


def permit_delay(db, job, actor, max_delay):
    _ensure(db)
    if not one(db, "SELECT id FROM grid_jobs WHERE id=?", (job,)):
        return {"decision": "unknown_job"}
    db.execute("INSERT OR REPLACE INTO grid_permits VALUES(?,?,?)", (job, actor, max_delay))
    return {"decision": "permitted"}


# --- grid instruction and the two delivery paths ---------------------------


def instruction(db, id, site, kw, start, end, issued_at, day_type, temp):
    _ensure(db)
    if one(db, "SELECT id FROM grid_instructions WHERE id=?", (id,)):
        return {"decision": "duplicate"}
    db.execute(
        "INSERT INTO grid_instructions VALUES(?,?,?,?,?,?,?,?,?)",
        (id, site, kw, start, end, issued_at, day_type, temp, "open"),
    )
    return {"decision": "registered"}


def _open_instruction(db, instruction):
    ins = one(db, "SELECT * FROM grid_instructions WHERE id=?", (instruction,))
    if not ins:
        return None, {"decision": "unknown_instruction"}
    if ins["status"] != "open":
        return None, {"decision": "instruction_closed"}
    if one(db, "SELECT id FROM grid_responses WHERE instruction=?", (instruction,)):
        return None, {"decision": "response_locked"}
    return ins, None


def respond_battery(db, instruction, discharge_kw, at, guarantee=None):
    """Commit verified battery discharge plus a purchased short-term backup
    guarantee covering the reserve window the discharge shortens."""
    _ensure(db)
    ins, err = _open_instruction(db, instruction)
    if err:
        return err
    if discharge_kw < ins["kw"]:
        return {"decision": "shortfall", "required_kw": ins["kw"]}
    b = one(db, "SELECT * FROM grid_battery WHERE site=?", (ins["site"],))
    if not b:
        return {"decision": "no_battery"}
    cfg = _site(db, ins["site"])
    slots = ins["end"] - ins["start"] + 1
    need = discharge_kw * slots * (cfg["slot_hours"] if cfg else 1.0)
    if need > b["capacity_kwh"]:
        return {"decision": "insufficient_capacity"}
    soc = battery_status(db, ins["site"], at)["soc_kwh"]
    if soc - need < b["reserve_min_kwh"]:
        return {"decision": "reserve_floor", "soc_kwh": soc, "reserve_min_kwh": b["reserve_min_kwh"]}
    if not guarantee:
        return {"decision": "guarantee_required"}
    if guarantee.get("kw", 0) < discharge_kw:
        return {"decision": "insufficient_guarantee"}
    rid = f"resp-{instruction}"
    db.execute("INSERT INTO grid_responses VALUES(?,?,?,?,?)", (rid, instruction, "battery", at, "active"))
    _response_event(db, rid, at, "active", "battery discharge committed")
    db.execute("INSERT INTO grid_response_actions VALUES(?,?,?,?,?)", (rid, "discharge", None, discharge_kw, 0))
    gid = guarantee.get("id", f"gty-{instruction}")
    cost = guarantee.get("cost", 0.0)
    db.execute(
        "INSERT INTO grid_guarantees VALUES(?,?,?,?,?,?)",
        (gid, rid, guarantee.get("provider"), guarantee["kw"], cost, "purchased"),
    )
    _ledger(db, ins["site"], instruction, "guarantee_cost", gid, None, -cost, "paid", at)
    return {"decision": "committed", "response": rid, "path": "battery", "guarantee": gid}


def respond_demand(db, instruction, at, delays=(), downclock_kw=0.0):
    """Commit customer-permitted batch delays plus rack downclocking.

    Production writes are never paused: a live lease lets the customer keep
    writing, it does not let the operator sell that write capacity to the grid.
    """
    _ensure(db)
    ins, err = _open_instruction(db, instruction)
    if err:
        return err
    plan = []
    covered = downclock_kw
    for job_id, delay in delays:
        job = one(db, "SELECT * FROM grid_jobs WHERE id=?", (job_id,))
        if not job or job["site"] != ins["site"]:
            return {"decision": "unknown_job", "job": job_id}
        if job["kind"] != "batch":
            return {"decision": "production_write_protected", "job": job_id}
        if job["status"] != "scheduled":
            return {"decision": "job_not_delayable", "job": job_id}
        if job["end"] < ins["start"] or job["start"] > ins["end"]:
            return {"decision": "job_outside_window", "job": job_id}
        contract = one(db, "SELECT * FROM grid_contracts WHERE customer=?", (job["customer"],))
        if not contract:
            return {"decision": "unknown_contract", "customer": job["customer"]}
        if delay > contract["max_batch_delay"]:
            return {"decision": "delay_beyond_contract", "job": job_id, "max_delay": contract["max_batch_delay"]}
        if not one(db, "SELECT * FROM grid_permits WHERE job=? AND max_delay>=?", (job_id, delay)):
            return {"decision": "permit_required", "job": job_id}
        covered += job["kw"]
        plan.append((job, delay, contract))
    if covered < ins["kw"]:
        return {"decision": "insufficient_coverage", "covered_kw": covered, "required_kw": ins["kw"]}
    rid = f"resp-{instruction}"
    db.execute("INSERT INTO grid_responses VALUES(?,?,?,?,?)", (rid, instruction, "demand", at, "active"))
    _response_event(db, rid, at, "active", "demand response committed")
    for job, delay, contract in plan:
        db.execute("INSERT INTO grid_response_actions VALUES(?,?,?,?,?)", (rid, "delay", job["id"], job["kw"], delay))
        db.execute("UPDATE grid_jobs SET delay=?,status='delayed' WHERE id=?", (delay, job["id"]))
        comp = job["kw"] * delay * contract["comp_per_kw_slot"]
        _ledger(db, ins["site"], instruction, "compensation", job["id"], job["customer"], -comp, "owed", at)
        _impact(db, ins["site"], job["id"], "delayed", ins["start"], ins["end"], f"delayed {delay} slots under {instruction}")
    if downclock_kw:
        db.execute("INSERT INTO grid_response_actions VALUES(?,?,?,?,?)", (rid, "downclock", None, downclock_kw, 0))
    return {"decision": "committed", "response": rid, "path": "demand", "delayed": [j["id"] for j, _, _ in plan]}


# --- metered reality --------------------------------------------------------


def _check_reading(cfg, at, meter_at, utility, diesel, batt_dis, batt_chg, server_load):
    drift_tol = cfg["drift_tol"] if cfg else 2
    tol = cfg["closure_tol"] if cfg else 0.5
    if meter_at is not None and abs(meter_at - at) > drift_tol:
        return "disputed", "clock_drift"
    if abs(utility + diesel + batt_dis - server_load - batt_chg) > tol:
        return "unclosed", "unclosed"
    return "valid", "recorded"


def record_reading(db, id, site, at, utility, diesel=0.0, batt_dis=0.0, batt_chg=0.0, server_load=0.0, meter_at=None):
    _ensure(db)
    if one(db, "SELECT id FROM grid_readings WHERE id=?", (id,)):
        return {"decision": "duplicate_reading"}
    if one(db, "SELECT id FROM grid_readings WHERE site=? AND at=? AND status!='superseded'", (site, at)):
        return {"decision": "slot_occupied"}
    status, decision = _check_reading(_site(db, site), at, meter_at, utility, diesel, batt_dis, batt_chg, server_load)
    db.execute(
        "INSERT INTO grid_readings VALUES(?,?,?,?,?,?,?,?,?,?,1,NULL,NULL)",
        (id, site, at, utility, diesel, batt_dis, batt_chg, server_load, meter_at, status),
    )
    return {"decision": decision, "status": status}


def correct_reading(db, id, original, reason, at, **fields):
    """Version a late metering correction; the original row is retained and
    every already-settled instruction touching the slot is re-settled."""
    _ensure(db)
    old = one(db, "SELECT * FROM grid_readings WHERE id=?", (original,))
    if not old:
        return {"decision": "unknown_reading"}
    if old["status"] == "superseded":
        return {"decision": "already_superseded"}
    if one(db, "SELECT id FROM grid_readings WHERE id=?", (id,)):
        return {"decision": "duplicate_reading"}
    vals = {k: fields.get(k, old[k]) for k in ("utility", "diesel", "batt_dis", "batt_chg", "server_load", "meter_at")}
    status, decision = _check_reading(
        _site(db, old["site"]), old["at"], vals["meter_at"],
        vals["utility"], vals["diesel"], vals["batt_dis"], vals["batt_chg"], vals["server_load"],
    )
    db.execute("UPDATE grid_readings SET status='superseded' WHERE id=?", (original,))
    db.execute(
        "INSERT INTO grid_readings VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (id, old["site"], old["at"], vals["utility"], vals["diesel"], vals["batt_dis"], vals["batt_chg"],
         vals["server_load"], vals["meter_at"], status, old["version"] + 1, original, reason),
    )
    resettled = []
    for ins in rows(
        db,
        "SELECT * FROM grid_instructions WHERE site=? AND start<=? AND end>=? AND status='settled'",
        (old["site"], old["at"], old["at"]),
    ):
        resettled.append(_resettle(db, ins, at, f"reading correction {id}: {reason}"))
    return {"decision": "corrected" if status == "valid" else decision, "status": status,
            "version": old["version"] + 1, "resettled": resettled}


def _latest_reading(db, site, at):
    return one(
        db,
        "SELECT * FROM grid_readings WHERE site=? AND at=? AND status!='superseded' ORDER BY version DESC LIMIT 1",
        (site, at),
    )


# --- outages, guarantees, recovery ------------------------------------------


def deliver_guarantee(db, id, guarantee, at, delivered_kw):
    _ensure(db)
    if not one(db, "SELECT id FROM grid_guarantees WHERE id=?", (guarantee,)):
        return {"decision": "unknown_guarantee"}
    db.execute("INSERT INTO grid_guarantee_deliveries VALUES(?,?,?,?)", (id, guarantee, at, delivered_kw))
    return {"decision": "recorded"}


def record_outage(db, id, site, start, end, at):
    """A utility outage tests each path's promise: battery responses must show
    the purchased guarantee actually arrived; demand responses owe their
    delayed jobs capacity after recovery. Outage slots earn no grid credit."""
    _ensure(db)
    if one(db, "SELECT id FROM grid_outages WHERE id=?", (id,)):
        return {"decision": "duplicate_outage"}
    db.execute("INSERT INTO grid_outages VALUES(?,?,?,?,?)", (id, site, start, end, at))
    results = []
    for resp in rows(
        db,
        "SELECT r.* FROM grid_responses r JOIN grid_instructions i ON i.id=r.instruction "
        "WHERE i.site=? AND i.start<=? AND i.end>=? AND r.status='active'",
        (site, end, start),
    ):
        if resp["path"] == "battery":
            action = one(db, "SELECT kw FROM grid_response_actions WHERE response=? AND kind='discharge'", (resp["id"],))
            required = action["kw"] if action else 0.0
            delivered = sum(
                d["delivered_kw"]
                for g in rows(db, "SELECT * FROM grid_guarantees WHERE response=?", (resp["id"],))
                for d in rows(db, "SELECT * FROM grid_guarantee_deliveries WHERE guarantee=? AND at>=? AND at<=?", (g["id"], start, end))
            )
            if required > 0 and delivered >= required:
                db.execute("UPDATE grid_guarantees SET status='delivered' WHERE response=?", (resp["id"],))
                _response_event(db, resp["id"], at, "active", f"backup verified during outage {id}")
                results.append({"response": resp["id"], "decision": "backup_verified"})
            else:
                db.execute("UPDATE grid_responses SET status='backup_default' WHERE id=?", (resp["id"],))
                db.execute("UPDATE grid_guarantees SET status='defaulted' WHERE response=?", (resp["id"],))
                _response_event(db, resp["id"], at, "backup_default", "purchased guarantee failed to arrive")
                for job in rows(
                    db,
                    "SELECT * FROM grid_jobs WHERE site=? AND status IN ('scheduled','delayed','resumed') AND start<=? AND end>=?",
                    (site, end, start),
                ):
                    _impact(db, site, job["id"], "interrupted", start, end, f"utility outage {id} without verified backup")
                results.append({"response": resp["id"], "decision": "backup_default"})
        else:
            for job in rows(db, "SELECT * FROM grid_jobs WHERE site=? AND status='delayed'", (site,)):
                db.execute("UPDATE grid_jobs SET status='awaiting_recovery' WHERE id=?", (job["id"],))
                _impact(db, site, job["id"], "awaiting_recovery", start, end, f"utility outage {id}; capacity owed after recovery")
            _response_event(db, resp["id"], at, "active", f"outage {id}; delayed jobs await recovery capacity")
            results.append({"response": resp["id"], "decision": "recovery_owed"})
    resettled = []
    for ins in rows(
        db,
        "SELECT * FROM grid_instructions WHERE site=? AND start<=? AND end>=? AND status='settled'",
        (site, end, start),
    ):
        resettled.append(_resettle(db, ins, at, f"outage {id}"))
    return {"decision": "recorded", "responses": results, "resettled": resettled}


def recover_jobs(db, site, at, capacity_kw):
    """Give recovery capacity to jobs whose delay was interrupted by an outage."""
    _ensure(db)
    allocations = []
    remaining = capacity_kw
    for job in rows(db, "SELECT * FROM grid_jobs WHERE site=? AND status='awaiting_recovery' ORDER BY start,id", (site,)):
        if remaining < job["kw"]:
            continue
        remaining -= job["kw"]
        db.execute("UPDATE grid_jobs SET status='resumed' WHERE id=?", (job["id"],))
        allocations.append({"job": job["id"], "kw": job["kw"], "at": at})
    return {"decision": "recovered", "allocations": allocations, "unused_capacity_kw": remaining}


def cancel_job(db, job, at):
    _ensure(db)
    j = one(db, "SELECT * FROM grid_jobs WHERE id=?", (job,))
    if not j:
        return {"decision": "unknown_job"}
    if j["status"] in ("completed", "cancelled"):
        return {"decision": "not_cancellable"}
    db.execute("UPDATE grid_jobs SET status='cancelled' WHERE id=?", (job,))
    db.execute("UPDATE grid_ledger SET status='void' WHERE kind='compensation' AND ref=? AND status='owed'", (job,))
    _impact(db, j["site"], job, "cancelled", at, at, "customer cancelled job")
    return {"decision": "cancelled"}


def finish_jobs(db, site, at):
    _ensure(db)
    done = []
    for job in rows(db, "SELECT * FROM grid_jobs WHERE site=? AND status IN ('scheduled','delayed','resumed')", (site,)):
        effective_end = job["end"] + job["delay"]
        if effective_end <= at:
            db.execute("UPDATE grid_jobs SET status='completed',completion_at=? WHERE id=?", (effective_end, job["id"]))
            done.append({"job": job["id"], "completion_at": effective_end})
    return {"decision": "finished", "completed": done}


# --- adjudication and settlement --------------------------------------------


def adjudicate(db, instruction):
    """Formal ruling from metered evidence only; offers and plans count for
    nothing until the utility meter, on one closed timeline, says so."""
    _ensure(db)
    ins = one(db, "SELECT * FROM grid_instructions WHERE id=?", (instruction,))
    if not ins:
        return {"decision": "unknown_instruction"}
    cfg = _site(db, ins["site"])
    slot_hours = cfg["slot_hours"] if cfg else 1.0
    rate = cfg["rate"] if cfg else 0.0
    base = compute_baseline(db, ins["site"], ins["day_type"], ins["temp"], ins["start"], ins["end"], ins["issued_at"])
    if base["decision"] != "ok":
        return {"decision": "non_performance", "reason": "no_baseline", "instruction": instruction}
    response = one(db, "SELECT * FROM grid_responses WHERE instruction=?", (instruction,))
    sources = {"battery_kwh": 0.0, "diesel_kwh": 0.0, "demand_kwh": 0.0}
    delivered, missing, excluded = {}, [], []
    for s in range(ins["start"], ins["end"] + 1):
        if one(db, "SELECT id FROM grid_outages WHERE site=? AND start<=? AND end>=?", (ins["site"], s, s)):
            excluded.append(s)
            continue
        r = _latest_reading(db, ins["site"], s)
        if not r or r["status"] != "valid":
            missing.append(s)
            continue
        d = base["baseline_kw"] - r["utility"]
        delivered[s] = d
        sources["battery_kwh"] += r["batt_dis"] * slot_hours
        sources["diesel_kwh"] += r["diesel"] * slot_hours
        sources["demand_kwh"] += max(0.0, d - r["batt_dis"] - r["diesel"]) * slot_hours
    resp_status = response["status"] if response else None
    if resp_status == "backup_default":
        decision, reason = "non_performance", "backup_default"
    elif not delivered and excluded:
        decision, reason = "non_performance", "no_settlable_slots"
    elif missing:
        decision, reason = "non_performance", "evidence_missing"
    elif not delivered or min(delivered.values()) < ins["kw"]:
        decision, reason = "non_performance", "shortfall"
    else:
        decision, reason = "performed", None
    paid_kwh = sum(min(d, ins["kw"]) for d in delivered.values()) * slot_hours if decision == "performed" else 0.0
    return {
        "decision": decision,
        "reason": reason,
        "instruction": instruction,
        "baseline_kw": base["baseline_kw"],
        "delivered_kw": delivered,
        "missing_slots": missing,
        "excluded_slots": excluded,
        "paid_kwh": paid_kwh,
        "reward": paid_kwh * rate,
        "sources": sources,
        "response_path": response["path"] if response else None,
        "response_status": resp_status,
    }


def _receipt_body(db, ins, adj, response, **extra):
    cfg = _site(db, ins["site"])
    diesel_cost = adj["sources"]["diesel_kwh"] * (cfg["diesel_cost"] if cfg else 0.0)
    guarantee_cost = 0.0
    compensation = 0.0
    if response:
        guarantee_cost = sum(g["cost"] for g in rows(db, "SELECT * FROM grid_guarantees WHERE response=?", (response["id"],)))
        compensation = sum(
            -l["amount"]
            for l in rows(db, "SELECT * FROM grid_ledger WHERE instruction=? AND kind='compensation' AND status='paid'", (ins["id"],))
        )
    body = {
        "baseline_kw": adj["baseline_kw"],
        "delivered_kw": adj["delivered_kw"],
        "sources": adj["sources"],
        "paid_kwh": adj["paid_kwh"],
        "reward": adj["reward"],
        "diesel_cost": diesel_cost,
        "guarantee_cost": guarantee_cost,
        "compensation": compensation,
        "missing_slots": adj["missing_slots"],
        "excluded_slots": adj["excluded_slots"],
        "response_path": adj["response_path"],
        "response_status": adj["response_status"],
        "reason": adj["reason"],
    }
    body["net"] = adj["reward"] - diesel_cost - guarantee_cost - compensation
    body.update(extra)
    return body, diesel_cost, guarantee_cost, compensation


def settle(db, instruction, at):
    _ensure(db)
    ins = one(db, "SELECT * FROM grid_instructions WHERE id=?", (instruction,))
    if not ins:
        return {"decision": "unknown_instruction"}
    if one(db, "SELECT id FROM grid_receipts WHERE instruction=?", (instruction,)):
        return {"decision": "already_settled", "receipt": receipt(db, instruction)}
    adj = adjudicate(db, instruction)
    response = one(db, "SELECT * FROM grid_responses WHERE instruction=?", (instruction,))
    for led in rows(db, "SELECT * FROM grid_ledger WHERE instruction=? AND kind='compensation' AND status='owed'", (instruction,)):
        job = one(db, "SELECT * FROM grid_jobs WHERE id=?", (led["ref"],))
        # customers are paid for delays even when the grid pays nothing;
        # a cancelled job voids its own compensation
        status = "void" if job and job["status"] == "cancelled" else "paid"
        db.execute("UPDATE grid_ledger SET status=? WHERE id=?", (status, led["id"]))
    body, diesel_cost, _, _ = _receipt_body(db, ins, adj, response)
    if diesel_cost:
        _ledger(db, ins["site"], instruction, "diesel_cost", None, None, -diesel_cost, "paid", at)
    if adj["reward"]:
        _ledger(db, ins["site"], instruction, "reward", instruction, None, adj["reward"], "paid", at)
    db.execute("UPDATE grid_instructions SET status='settled' WHERE id=?", (instruction,))
    db.execute(
        "INSERT INTO grid_receipts VALUES(?,?,?,?,?,?,?)",
        (f"rcpt-{instruction}-v1", instruction, 1, at, adj["decision"], adj["reward"], json.dumps(body, sort_keys=True)),
    )
    return {"decision": "settled", "adjudication": adj["decision"], "receipt": receipt(db, instruction)}


def _resettle(db, ins, at, reason):
    """Re-adjudicate a settled instruction and book the delta as a real
    clawback or repayment; earlier receipts stay on record."""
    last = one(db, "SELECT * FROM grid_receipts WHERE instruction=? ORDER BY version DESC LIMIT 1", (ins["id"],))
    adj = adjudicate(db, ins["id"])
    response = one(db, "SELECT * FROM grid_responses WHERE instruction=?", (ins["id"],))
    body, _, _, _ = _receipt_body(db, ins, adj, response, supersedes=last["id"], trigger=reason)
    version = last["version"] + 1
    db.execute(
        "INSERT INTO grid_receipts VALUES(?,?,?,?,?,?,?)",
        (f"rcpt-{ins['id']}-v{version}", ins["id"], version, at, adj["decision"], adj["reward"], json.dumps(body, sort_keys=True)),
    )
    delta = adj["reward"] - last["reward"]
    if delta < 0:
        _ledger(db, ins["site"], ins["id"], "clawback", last["id"], None, delta, "paid", at)
    elif delta > 0:
        _ledger(db, ins["site"], ins["id"], "additional_reward", last["id"], None, delta, "paid", at)
    return {"receipt": f"rcpt-{ins['id']}-v{version}", "decision": adj["decision"], "delta": delta}


def receipt(db, instruction):
    _ensure(db)
    r = one(db, "SELECT * FROM grid_receipts WHERE instruction=? ORDER BY version DESC LIMIT 1", (instruction,))
    if not r:
        return None
    return {**r, "body": json.loads(r["body"])}


def ledger(db, site=None, instruction=None, kind=None):
    _ensure(db)
    q, args = "SELECT * FROM grid_ledger WHERE 1=1", []
    if site:
        q += " AND site=?"
        args.append(site)
    if instruction:
        q += " AND instruction=?"
        args.append(instruction)
    if kind:
        q += " AND kind=?"
        args.append(kind)
    return rows(db, q, tuple(args))
