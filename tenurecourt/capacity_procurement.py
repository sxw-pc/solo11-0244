"""Physical-slot grounded capacity procurement.

A vendor quotation is only paper until it is bound to concrete physical slots.
A physical slot's identity is never reusable: it is formed from the cabinet,
device serial, the vendor's upstream title, the failure domain and the measured
delivery fingerprint. Two offers on the same resale chain therefore collapse to
one set of physical slots -- quotations are never added across resellers.

Customer replicas may only be activated after each replica has a reservation on
an independent failure domain, a valid upstream lease window and a passed
write/read-back delivery test on the actual device. Vendor down payments follow
delivered, tested capacity only. Every later change (lease expiry, device
replacement, offer withdrawal, proof correction) keeps the original proofs and
payments and opens a new liability record instead of rewriting history.
"""

import sqlite3

from .store import one, rows

SCHEMA = """
CREATE TABLE IF NOT EXISTS cp_devices(
  device_id TEXT PRIMARY KEY,
  rack TEXT,
  serial TEXT,
  upstream_title TEXT,
  failure_domain TEXT,
  measurement_fingerprint TEXT UNIQUE,
  status TEXT,
  registered_at INTEGER,
  replaced_by TEXT
);
CREATE TABLE IF NOT EXISTS cp_slots(
  slot_id TEXT PRIMARY KEY,
  device_id TEXT,
  rack TEXT,
  position TEXT,
  failure_domain TEXT,
  status TEXT,
  UNIQUE(device_id, position),
  UNIQUE(rack, position)
);
CREATE TABLE IF NOT EXISTS cp_offer_checks(
  offer TEXT PRIMARY KEY,
  vendor TEXT,
  rack TEXT,
  slots INTEGER,
  expiry INTEGER,
  upstream_title TEXT,
  decision TEXT,
  distinct_slots INTEGER,
  checked_at INTEGER
);
CREATE TABLE IF NOT EXISTS cp_offer_slots(
  offer TEXT,
  slot_id TEXT,
  PRIMARY KEY(offer, slot_id)
);
CREATE TABLE IF NOT EXISTS cp_orders(
  order_id TEXT PRIMARY KEY,
  customer TEXT,
  required_copies INTEGER,
  status TEXT,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS cp_reservations(
  reservation_id TEXT PRIMARY KEY,
  order_id TEXT,
  slot_id TEXT,
  customer TEXT,
  failure_domain TEXT,
  source TEXT,
  migration_id TEXT,
  lease_start INTEGER,
  lease_expires INTEGER,
  status TEXT,
  created_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS cp_one_active_reservation_per_slot
  ON cp_reservations(slot_id) WHERE status='active';
CREATE TABLE IF NOT EXISTS cp_delivery_tests(
  test_id TEXT PRIMARY KEY,
  reservation_id TEXT,
  slot_id TEXT,
  device_id TEXT,
  measured_fingerprint TEXT,
  write_token TEXT UNIQUE,
  readback TEXT,
  lease_start INTEGER,
  lease_expires INTEGER,
  at INTEGER,
  status TEXT
);
CREATE TABLE IF NOT EXISTS cp_customer_writes(
  write_id TEXT PRIMARY KEY,
  customer TEXT,
  slot_id TEXT,
  at INTEGER,
  detected TEXT
);
CREATE TABLE IF NOT EXISTS cp_migrations(
  migration_id TEXT PRIMARY KEY,
  order_id TEXT,
  source_customer TEXT,
  source_failure_domain TEXT,
  target_failure_domain TEXT,
  snapshot_id TEXT,
  restore_deadline INTEGER,
  restored_at INTEGER,
  discount_liability TEXT,
  accepted_delay INTEGER,
  status TEXT,
  created_at INTEGER
);
CREATE TABLE IF NOT EXISTS cp_migration_old_slots(
  migration_id TEXT,
  slot_id TEXT,
  old_reservation_id TEXT,
  PRIMARY KEY(migration_id, slot_id)
);
CREATE TABLE IF NOT EXISTS cp_vendor_payments(
  payment_id TEXT PRIMARY KEY,
  offer TEXT,
  vendor TEXT,
  slot_id TEXT,
  order_id TEXT,
  failure_domain TEXT,
  reservation_id TEXT,
  amount INTEGER,
  test_id TEXT,
  at INTEGER,
  status TEXT
);
CREATE TABLE IF NOT EXISTS cp_activation_proofs(
  proof_id TEXT PRIMARY KEY,
  order_id TEXT,
  customer TEXT,
  service_start INTEGER,
  at INTEGER,
  status TEXT,
  superseded_by TEXT,
  reason TEXT
);
CREATE TABLE IF NOT EXISTS cp_procurement_liabilities(
  liability_id TEXT PRIMARY KEY,
  kind TEXT,
  ref_type TEXT,
  ref_id TEXT,
  amount INTEGER,
  status TEXT,
  detail TEXT,
  created_at INTEGER,
  settled_at INTEGER
);
CREATE TABLE IF NOT EXISTS slot_offers(
  offer TEXT PRIMARY KEY, rack TEXT, slots INTEGER, expiry INTEGER
);
"""

EFFECTIVE_OFFER = "verified"


def ensure_schema(db):
    # Individual execute() calls so an outer BEGIN IMMEDIATE transaction is
    # not implicitly committed (executescript commits before running).
    for statement in SCHEMA.split(";"):
        statement = statement.strip()
        if statement:
            db.execute(statement)


def _add_liability(db, liability_id, kind, ref_type, ref_id, amount, detail, at):
    db.execute(
        "INSERT INTO cp_procurement_liabilities VALUES(?,?,?,?,?,?,?,?,?)",
        (liability_id, kind, ref_type, ref_id, amount, "owed", detail, at, None),
    )


def _effective_offer_for_slot(db, slot_id):
    return one(db,
        """
        SELECT c.* FROM cp_offer_slots s
        JOIN cp_offer_checks c ON c.offer = s.offer
        WHERE s.slot_id=? AND c.decision='verified'
        """,
        (slot_id,),
    )


def effective_capacity(db, rack=None):
    """Distinct physical slots backed by an effective (non-duplicate,
    non-withdrawn) offer. Resold offers are counted once."""
    sql = """
        SELECT COUNT(DISTINCT s.slot_id) AS n
        FROM cp_offer_slots s
        JOIN cp_offer_checks c ON c.offer = s.offer
        JOIN cp_slots p ON p.slot_id = s.slot_id
        WHERE c.decision='verified'
    """
    args = []
    if rack is not None:
        sql += " AND p.rack=?"
        args.append(rack)
    return one(db, sql, tuple(args))["n"]


def readiness(db, order_id, at):
    """Replica readiness against real, writable, independent persistence."""
    order = one(db, "SELECT * FROM cp_orders WHERE order_id=?", (order_id,))
    if not order:
        return {"ready": False, "problems": ["unknown_order"], "replicas": []}
    reservations = rows(
        db,
        "SELECT * FROM cp_reservations WHERE order_id=? AND status='active' ORDER BY reservation_id",
        (order_id,),
    )
    problems = []
    if len(reservations) != order["required_copies"]:
        problems.append("replica_count_%s_of_%s" % (len(reservations), order["required_copies"]))
    domains = {r["failure_domain"] for r in reservations}
    if len(domains) != len(reservations):
        problems.append("failure_domains_not_independent")
    replicas = []
    for r in reservations:
        slot = one(db, "SELECT * FROM cp_slots WHERE slot_id=?", (r["slot_id"],))
        device = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (slot["device_id"],))
        test = one(db,
            "SELECT * FROM cp_delivery_tests WHERE reservation_id=? AND status='passed' ORDER BY at DESC LIMIT 1",
            (r["reservation_id"],),
        )
        offer = _effective_offer_for_slot(db, r["slot_id"])
        issues = []
        if not slot or slot["status"] != "available":
            issues.append("slot_not_available")
        if not device or device["status"] != "active":
            issues.append("device_not_active")
        if not test:
            issues.append("delivery_test_missing")
        elif test["measured_fingerprint"] != device["measurement_fingerprint"]:
            issues.append("device_identity_mismatch")
        if not (r["lease_start"] <= at <= r["lease_expires"]):
            issues.append("lease_not_in_window")
        if not offer:
            issues.append("offer_not_effective")
        if issues:
            problems.extend("%s:%s" % (r["failure_domain"], i) for i in issues)
        replicas.append(
            {
                "reservation_id": r["reservation_id"],
                "slot_id": r["slot_id"],
                "failure_domain": r["failure_domain"],
                "ready": not issues,
                "issues": issues,
            }
        )
    return {"ready": not problems, "problems": problems, "replicas": replicas}


def stray_writes(db, migration_id=None):
    if migration_id is None:
        return rows(db, "SELECT * FROM cp_customer_writes WHERE detected IS NOT NULL")
    return rows(db,
        """
        SELECT w.* FROM cp_customer_writes w
        JOIN cp_migration_old_slots o ON o.slot_id = w.slot_id
        WHERE o.migration_id=? AND w.detected IS NOT NULL
        """,
        (migration_id,),
    )


def run(db, x):
    ensure_schema(db)
    op = x["op"]

    if op == "record_quote":
        if one(db, "SELECT 1 FROM slot_offers WHERE offer=?", (x["offer"],)):
            return {"decision": "reused"}
        db.execute(
            "INSERT INTO slot_offers VALUES(?,?,?,?)",
            (x["offer"], x["rack"], x["slots"], x["expiry"]),
        )
        return {"decision": "quote_recorded"}

    if op == "register_device":
        if one(db, "SELECT 1 FROM cp_devices WHERE measurement_fingerprint=?", (x["fingerprint"],)):
            return {"decision": "device_identity_reused"}
        if one(db, "SELECT 1 FROM cp_devices WHERE device_id=?", (x["id"],)):
            return {"decision": "reused"}
        db.execute(
            "INSERT INTO cp_devices VALUES(?,?,?,?,?,?,?,?,?)",
            (
                x["id"], x["rack"], x["serial"], x["upstream_title"],
                x["failure_domain"], x["fingerprint"], "active", x["at"], None,
            ),
        )
        return {"decision": "device_registered"}

    if op == "register_slot":
        device = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (x["device_id"],))
        if not device or device["status"] != "active":
            return {"decision": "device_unavailable"}
        if one(
            db,
            "SELECT 1 FROM cp_slots WHERE (device_id=? AND position=?) OR (rack=? AND position=?)",
            (x["device_id"], x["position"], device["rack"], x["position"]),
        ):
            return {"decision": "slot_identity_reused"}
        slot_id = x["id"]
        db.execute(
            "INSERT INTO cp_slots VALUES(?,?,?,?,?,?)",
            (slot_id, device["device_id"], device["rack"], x["position"],
             device["failure_domain"], "available"),
        )
        return {"decision": "slot_registered", "slot_id": slot_id}

    if op == "verify_offer":
        if one(db, "SELECT 1 FROM cp_offer_checks WHERE offer=?", (x["offer"],)):
            return {"decision": "reused"}
        quote = one(db, "SELECT * FROM slot_offers WHERE offer=?", (x["offer"],))
        if quote and (
            quote["rack"] != x["rack"]
            or quote["slots"] != x["slots"]
            or quote["expiry"] != x["expiry"]
        ):
            return {"decision": "quote_mismatch"}
        slot_ids = x["slot_ids"]
        if len(slot_ids) != x["slots"] or len(set(slot_ids)) != len(slot_ids):
            return {"decision": "offer_slot_list_invalid"}
        bound = set()
        for sid in slot_ids:
            slot = one(db, "SELECT * FROM cp_slots WHERE slot_id=?", (sid,))
            if not slot:
                return {"decision": "slot_unknown", "slot_id": sid}
            if slot["rack"] != x["rack"]:
                return {"decision": "slot_rack_mismatch", "slot_id": sid}
            device = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (slot["device_id"],))
            if device["upstream_title"] != x["upstream_title"]:
                return {"decision": "upstream_title_mismatch", "slot_id": sid}
            bound.add(sid)
        overlap = rows(
            db,
            """
            SELECT DISTINCT os.slot_id FROM cp_offer_slots os
            JOIN cp_offer_checks oc ON oc.offer = os.offer
            WHERE oc.decision='verified' AND os.slot_id IN (%s)
            """ % ",".join("?" * len(slot_ids)),
            tuple(slot_ids),
        )
        if overlap:
            db.execute(
                "INSERT INTO cp_offer_checks VALUES(?,?,?,?,?,?,?,?,?)",
                (x["offer"], x["vendor"], x["rack"], x["slots"], x["expiry"],
                 x["upstream_title"], "duplicate", len(bound), x["at"]),
            )
            return {
                "decision": "resold_capacity_duplicate",
                "distinct_slots": len(bound),
                "duplicate_slots": [r["slot_id"] for r in overlap],
            }
        db.executemany(
            "INSERT INTO cp_offer_slots VALUES(?,?)",
            [(x["offer"], sid) for sid in slot_ids],
        )
        db.execute(
            "INSERT INTO cp_offer_checks VALUES(?,?,?,?,?,?,?,?,?)",
            (x["offer"], x["vendor"], x["rack"], x["slots"], x["expiry"],
             x["upstream_title"], EFFECTIVE_OFFER, len(bound), x["at"]),
        )
        return {"decision": "offer_verified", "distinct_slots": len(bound)}

    if op == "withdraw_offer":
        offer = one(db, "SELECT * FROM cp_offer_checks WHERE offer=?", (x["offer"],))
        if not offer:
            return {"decision": "unknown_offer"}
        db.execute(
            "UPDATE cp_offer_checks SET decision='withdrawn' WHERE offer=?",
            (x["offer"],),
        )
        liabilities = 0
        revoked = 0
        for r in rows(
            db,
            """
            SELECT r.* FROM cp_reservations r
            JOIN cp_offer_slots os ON os.slot_id = r.slot_id
            WHERE os.offer=? AND r.status='active'
            """,
            (x["offer"],),
        ):
            order = one(db, "SELECT * FROM cp_orders WHERE order_id=?", (r["order_id"],))
            if order["status"] == "active":
                # The activated replica lost its upstream backing: readiness
                # must drop while the original proof and payment stay on file.
                db.execute(
                    "UPDATE cp_reservations SET status='impaired' WHERE reservation_id=?",
                    (r["reservation_id"],),
                )
                _add_liability(
                    db, "%s-liab-%s" % (x["offer"], r["reservation_id"]),
                    "offer_withdrawal", "reservation", r["reservation_id"],
                    x.get("amount", 0), "offer withdrawn after activation", x["at"],
                )
                liabilities += 1
            else:
                db.execute(
                    "UPDATE cp_reservations SET status='revoked_pending' WHERE reservation_id=?",
                    (r["reservation_id"],),
                )
                revoked += 1
        return {"decision": "offer_withdrawn", "revoked_reservations": revoked,
                "liabilities": liabilities}

    if op == "reinstate_offer":
        offer = one(db, "SELECT * FROM cp_offer_checks WHERE offer=?", (x["offer"],))
        if not offer or offer["decision"] != "withdrawn":
            return {"decision": "offer_not_withdrawn"}
        db.execute(
            "UPDATE cp_offer_checks SET decision='verified' WHERE offer=?",
            (x["offer"],),
        )
        db.execute(
            """
            UPDATE cp_reservations SET status='active'
            WHERE status='revoked_pending' AND slot_id IN (
              SELECT slot_id FROM cp_offer_slots WHERE offer=?
            )
            """,
            (x["offer"],),
        )
        return {"decision": "offer_reinstated"}

    if op == "place_order":
        if one(db, "SELECT 1 FROM cp_orders WHERE order_id=?", (x["order_id"],)):
            return {"decision": "reused"}
        if x["required_copies"] < 1:
            return {"decision": "invalid_order"}
        db.execute(
            "INSERT INTO cp_orders VALUES(?,?,?,?,?)",
            (x["order_id"], x["customer"], x["required_copies"], "open", x["at"]),
        )
        return {"decision": "order_placed"}

    if op == "reserve":
        order = one(db, "SELECT * FROM cp_orders WHERE order_id=?", (x["order_id"],))
        if not order or order["status"] not in ("open", "deferred", "active"):
            return {"decision": "order_not_open"}
        if x["lease_expires"] <= x["lease_start"] or not (
            x["lease_start"] <= x["at"] <= x["lease_expires"]
        ):
            return {"decision": "lease_window_invalid"}
        slot = one(db, "SELECT * FROM cp_slots WHERE slot_id=?", (x["slot_id"],))
        if not slot or slot["status"] != "available":
            return {"decision": "slot_unavailable"}
        if not _effective_offer_for_slot(db, x["slot_id"]):
            return {"decision": "offer_not_verified"}
        if one(
            db,
            "SELECT 1 FROM cp_reservations WHERE order_id=? AND failure_domain=? AND status='active'",
            (x["order_id"], slot["failure_domain"]),
        ):
            return {"decision": "failure_domain_not_independent"}
        try:
            db.execute(
                "INSERT INTO cp_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (x["reservation_id"], x["order_id"], x["slot_id"], order["customer"],
                 slot["failure_domain"], x.get("source", "new"), x.get("migration_id"),
                 x["lease_start"], x["lease_expires"], "active", x["at"]),
            )
        except sqlite3.IntegrityError:
            return {"decision": "slot_already_reserved"}
        return {"decision": "reserved", "failure_domain": slot["failure_domain"]}

    if op == "delivery_test":
        r = one(db, "SELECT * FROM cp_reservations WHERE reservation_id=?", (x["reservation_id"],))
        if not r or r["status"] != "active":
            return {"decision": "reservation_not_active"}
        if one(db, "SELECT 1 FROM cp_delivery_tests WHERE test_id=?", (x["test_id"],)):
            return {"decision": "reused"}
        if one(db, "SELECT 1 FROM cp_delivery_tests WHERE write_token=?", (x["write_token"],)):
            return {"decision": "write_token_reused"}
        slot = one(db, "SELECT * FROM cp_slots WHERE slot_id=?", (r["slot_id"],))
        device = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (slot["device_id"],))
        if device["status"] != "active" or slot["status"] != "available":
            return {"decision": "device_unavailable"}
        if x["measured_fingerprint"] != device["measurement_fingerprint"]:
            return {"decision": "device_identity_mismatch"}
        if not (r["lease_start"] <= x["at"] <= r["lease_expires"]):
            return {"decision": "delivery_outside_lease"}
        db.execute(
            "INSERT INTO cp_delivery_tests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (x["test_id"], r["reservation_id"], r["slot_id"], device["device_id"],
             x["measured_fingerprint"], x["write_token"], x["readback"],
             r["lease_start"], r["lease_expires"], x["at"], "passed"),
        )
        return {"decision": "delivery_passed"}

    if op == "record_customer_write":
        if one(db, "SELECT 1 FROM cp_customer_writes WHERE write_id=?", (x["write_id"],)):
            return {"decision": "reused"}
        active = one(db,
            "SELECT 1 FROM cp_reservations WHERE customer=? AND slot_id=? AND status='active'",
            (x["customer"], x["slot_id"]),
        )
        detected = None if active else "stale_replica_write"
        db.execute(
            "INSERT INTO cp_customer_writes VALUES(?,?,?,?,?)",
            (x["write_id"], x["customer"], x["slot_id"], x["at"], detected),
        )
        if detected:
            return {"decision": "stale_replica_write_detected", "slot_id": x["slot_id"]}
        return {"decision": "customer_write_recorded"}

    if op == "pay_vendor":
        offer = one(db, "SELECT * FROM cp_offer_checks WHERE offer=?", (x["offer"],))
        if not offer or offer["decision"] != "verified":
            return {"decision": "offer_not_effective"}
        if one(db, "SELECT 1 FROM cp_vendor_payments WHERE payment_id=?", (x["payment_id"],)):
            return {"decision": "reused"}
        bound = one(db,
            "SELECT 1 FROM cp_offer_slots WHERE offer=? AND slot_id=?",
            (x["offer"], x["slot_id"]),
        )
        if not bound:
            return {"decision": "slot_not_in_offer"}
        r = one(db,
            "SELECT * FROM cp_reservations WHERE reservation_id=? AND status='active'",
            (x["reservation_id"],),
        )
        if not r or r["slot_id"] != x["slot_id"]:
            return {"decision": "reservation_not_active"}
        test = one(db,
            "SELECT * FROM cp_delivery_tests WHERE reservation_id=? AND status='passed' ORDER BY at DESC LIMIT 1",
            (x["reservation_id"],),
        )
        if not test:
            return {"decision": "payment_requires_delivery"}
        if one(
            db,
            "SELECT 1 FROM cp_vendor_payments WHERE order_id=? AND failure_domain=? AND status='paid'",
            (r["order_id"], r["failure_domain"]),
        ):
            return {"decision": "replacement_payment_duplicate"}
        db.execute(
            "INSERT INTO cp_vendor_payments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (x["payment_id"], x["offer"], offer["vendor"], x["slot_id"], r["order_id"],
             r["failure_domain"], r["reservation_id"], x["amount"], test["test_id"],
             x["at"], "paid"),
        )
        return {"decision": "vendor_paid", "amount": x["amount"]}

    if op == "activate_order":
        order = one(db, "SELECT * FROM cp_orders WHERE order_id=?", (x["order_id"],))
        if not order:
            return {"decision": "unknown_order"}
        if order["status"] == "active":
            return {"decision": "already_active"}
        state = readiness(db, x["order_id"], x["at"])
        if not state["ready"]:
            return {"decision": "replicas_not_ready", "problems": state["problems"]}
        latest_test = one(db,
            """
            SELECT MAX(t.at) AS at FROM cp_delivery_tests t
            JOIN cp_reservations r ON r.reservation_id = t.reservation_id
            WHERE r.order_id=? AND r.status='active' AND t.status='passed'
            """,
            (x["order_id"],),
        )
        service_start = max(x["at"], latest_test["at"])
        db.execute(
            "INSERT INTO cp_activation_proofs VALUES(?,?,?,?,?,?,?,?)",
            (x["proof_id"], x["order_id"], order["customer"], service_start,
             x["at"], "issued", None, None),
        )
        db.execute("UPDATE cp_orders SET status='active' WHERE order_id=?", (x["order_id"],))
        return {"decision": "activated", "proof_id": x["proof_id"],
                "service_start": service_start}

    if op == "defer_order":
        order = one(db, "SELECT * FROM cp_orders WHERE order_id=?", (x["order_id"],))
        if not order or order["status"] != "open":
            return {"decision": "order_not_deferrable"}
        if x["compensation"] <= 0:
            return {"decision": "compensation_required"}
        _add_liability(
            db, x["liability_id"], "delay_compensation", "order", x["order_id"],
            x["compensation"], "contractual activation delay until %s" % x["target_date"],
            x["at"],
        )
        db.execute("UPDATE cp_orders SET status='deferred' WHERE order_id=?", (x["order_id"],))
        return {"decision": "order_deferred", "liability_id": x["liability_id"]}

    if op == "plan_migration":
        source_res = one(db,
            "SELECT * FROM cp_reservations WHERE customer=? AND failure_domain=? AND status='active'",
            (x["source_customer"], x["source_failure_domain"]),
        )
        if not source_res:
            return {"decision": "source_tenure_missing"}
        if x["target_failure_domain"] == x["source_failure_domain"]:
            return {"decision": "failure_domain_not_independent"}
        target = one(db,
            "SELECT * FROM cp_slots WHERE failure_domain=? AND status='available' LIMIT 1",
            (x["target_failure_domain"],),
        )
        if not target:
            return {"decision": "no_target_capacity"}
        _add_liability(
            db, x["liability_id"], "migration_discount", "customer", x["source_customer"],
            x["discount_amount"], "verified migration out of %s" % x["source_failure_domain"],
            x["at"],
        )
        db.execute(
            "INSERT INTO cp_migrations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (x["migration_id"], x["order_id"], x["source_customer"],
             x["source_failure_domain"], x["target_failure_domain"], x["snapshot_id"],
             x["restore_deadline"], None, x["liability_id"],
             1 if x.get("accepted_delay") else 0, "planned", x["at"]),
        )
        return {"decision": "migration_planned", "migration_id": x["migration_id"]}

    if op == "record_customer_delay_acceptance":
        m = one(db, "SELECT * FROM cp_migrations WHERE migration_id=?", (x["migration_id"],))
        if not m:
            return {"decision": "unknown_migration"}
        if not x["accepted"]:
            return {"decision": "delay_declined"}
        db.execute(
            "UPDATE cp_migrations SET accepted_delay=1 WHERE migration_id=?",
            (x["migration_id"],),
        )
        return {"decision": "delay_acceptance_recorded"}

    if op == "confirm_snapshot_restore":
        m = one(db, "SELECT * FROM cp_migrations WHERE migration_id=?", (x["migration_id"],))
        if not m:
            return {"decision": "unknown_migration"}
        if x["restored_at"] > m["restore_deadline"]:
            return {"decision": "snapshot_restore_sla_breach"}
        db.execute(
            "UPDATE cp_migrations SET restored_at=? WHERE migration_id=?",
            (x["restored_at"], x["migration_id"]),
        )
        return {"decision": "snapshot_restore_verified"}

    if op == "migration_ready":
        m = one(db, "SELECT * FROM cp_migrations WHERE migration_id=?", (x["migration_id"],))
        if not m:
            return {"decision": "unknown_migration"}
        reasons = []
        if not m["restored_at"] or m["restored_at"] > m["restore_deadline"]:
            reasons.append("snapshot_restore_unverified")
        if not m["accepted_delay"]:
            reasons.append("delay_not_accepted")
        target_slot = one(db,
            """
            SELECT s.* FROM cp_slots s
            JOIN cp_reservations r ON r.slot_id = s.slot_id
            WHERE r.customer=? AND r.failure_domain=? AND r.status='active'
              AND r.migration_id=?
            """,
            (m["source_customer"], m["target_failure_domain"], m["migration_id"]),
        )
        if not target_slot:
            reasons.append("target_reservation_missing")
        elif not one(db,
            """
            SELECT 1 FROM cp_delivery_tests
            WHERE slot_id=? AND status='passed'
            """,
            (target_slot["slot_id"],),
        ):
            reasons.append("target_delivery_missing")
        if reasons:
            return {"decision": "migration_blocked", "reasons": reasons}
        db.execute("UPDATE cp_migrations SET status='ready' WHERE migration_id=?", (x["migration_id"],))
        return {"decision": "migration_ready"}

    if op == "cutover":
        m = one(db, "SELECT * FROM cp_migrations WHERE migration_id=?", (x["migration_id"],))
        if not m or m["status"] != "ready":
            return {"decision": "cutover_requirements_unmet"}
        old = one(db,
            "SELECT * FROM cp_reservations WHERE customer=? AND failure_domain=? AND status='active'",
            (m["source_customer"], m["source_failure_domain"]),
        )
        if not old:
            return {"decision": "source_tenure_missing"}
        target = one(db,
            """
            SELECT r.* FROM cp_reservations r
            WHERE r.customer=? AND r.failure_domain=? AND r.migration_id=? AND r.status='active'
            """,
            (m["source_customer"], m["target_failure_domain"], m["migration_id"]),
        )
        if not target or not one(db,
            "SELECT 1 FROM cp_delivery_tests WHERE reservation_id=? AND status='passed'",
            (target["reservation_id"],),
        ):
            return {"decision": "cutover_requirements_unmet"}
        db.execute(
            "UPDATE cp_reservations SET status='released_by_migration' WHERE reservation_id=?",
            (old["reservation_id"],),
        )
        db.execute(
            "INSERT INTO cp_migration_old_slots VALUES(?,?,?)",
            (m["migration_id"], old["slot_id"], old["reservation_id"]),
        )
        db.execute(
            "UPDATE cp_procurement_liabilities SET status='settled', settled_at=? WHERE liability_id=?",
            (x["at"], m["discount_liability"]),
        )
        db.execute("UPDATE cp_migrations SET status='cutover' WHERE migration_id=?", (x["migration_id"],))
        return {"decision": "migration_cutover_complete", "freed_slot": old["slot_id"]}

    if op == "record_lease_expiry":
        r = one(db, "SELECT * FROM cp_reservations WHERE reservation_id=?", (x["reservation_id"],))
        if not r or r["status"] != "active":
            return {"decision": "reservation_not_active"}
        if x["at"] < r["lease_expires"]:
            return {"decision": "lease_still_valid"}
        db.execute(
            "UPDATE cp_reservations SET status='lease_expired' WHERE reservation_id=?",
            (x["reservation_id"],),
        )
        _add_liability(
            db, x["liability_id"], "lease_renewal_gap", "reservation", x["reservation_id"],
            x.get("amount", 0), "upstream lease expired at %s" % r["lease_expires"], x["at"],
        )
        return {"decision": "lease_expired_recorded"}

    if op == "renew_lease":
        r = one(db, "SELECT * FROM cp_reservations WHERE reservation_id=?", (x["reservation_id"],))
        if not r or r["status"] != "lease_expired":
            return {"decision": "reservation_not_expired"}
        if x["lease_expires"] <= x["at"]:
            return {"decision": "lease_window_invalid"}
        slot = one(db, "SELECT * FROM cp_slots WHERE slot_id=?", (r["slot_id"],))
        if not slot or slot["status"] != "available":
            return {"decision": "slot_unavailable"}
        if not _effective_offer_for_slot(db, r["slot_id"]):
            return {"decision": "offer_not_effective"}
        # The expired reservation and its proofs remain on file; the renewal
        # reopens the same physical reservation under a new lease window.
        db.execute(
            "UPDATE cp_reservations SET status='active', lease_start=?, lease_expires=? WHERE reservation_id=?",
            (x["at"], x["lease_expires"], x["reservation_id"]),
        )
        return {"decision": "lease_renewed"}

    if op == "record_device_failure":
        device = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (x["device_id"],))
        if not device or device["status"] != "active":
            return {"decision": "device_unavailable"}
        db.execute("UPDATE cp_devices SET status='failed' WHERE device_id=?", (x["device_id"],))
        db.execute(
            "UPDATE cp_slots SET status='failed' WHERE device_id=?", (x["device_id"],)
        )
        slots = [s["slot_id"] for s in rows(
            db, "SELECT slot_id FROM cp_slots WHERE device_id=?", (x["device_id"],)
        )]
        impaired = 0
        liabilities = 0
        placeholders = ",".join("?" * len(slots)) if slots else "''"
        for r in rows(
            db,
            "SELECT * FROM cp_reservations WHERE status='active' AND slot_id IN (%s)" % placeholders,
            tuple(slots),
        ):
            db.execute(
                "UPDATE cp_reservations SET status='impaired' WHERE reservation_id=?",
                (r["reservation_id"],),
            )
            impaired += 1
            paid = one(db,
                "SELECT 1 FROM cp_vendor_payments WHERE reservation_id=? AND status='paid'",
                (r["reservation_id"],),
            )
            if paid:
                _add_liability(
                    db, "%s-rework-%s" % (x["device_id"], r["reservation_id"]),
                    "device_failure_rework", "reservation", r["reservation_id"],
                    x.get("amount", 0),
                    "device %s failed after down payment" % x["device_id"], x["at"],
                )
                liabilities += 1
        return {"decision": "device_failure_recorded", "impaired": impaired,
                "liabilities": liabilities}

    if op == "replace_device":
        old = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (x["old_device_id"],))
        new = one(db, "SELECT * FROM cp_devices WHERE device_id=?", (x["new_device_id"],))
        if not old or old["status"] != "failed":
            return {"decision": "device_unavailable"}
        if not new or new["status"] != "active":
            return {"decision": "replacement_device_unavailable"}
        if new["measurement_fingerprint"] == old["measurement_fingerprint"]:
            return {"decision": "device_identity_reused"}
        db.execute(
            "UPDATE cp_devices SET replaced_by=? WHERE device_id=?",
            (x["new_device_id"], x["old_device_id"]),
        )
        return {"decision": "device_replacement_recorded"}

    if op == "reprovision_reservation":
        old = one(db, "SELECT * FROM cp_reservations WHERE reservation_id=?", (x["reservation_id"],))
        if not old or old["status"] != "impaired":
            return {"decision": "reservation_not_impaired"}
        slot = one(db, "SELECT * FROM cp_slots WHERE slot_id=?", (x["new_slot_id"],))
        if not slot or slot["status"] != "available":
            return {"decision": "slot_unavailable"}
        if slot["failure_domain"] != old["failure_domain"]:
            return {"decision": "failure_domain_changed"}
        if not _effective_offer_for_slot(db, x["new_slot_id"]):
            return {"decision": "offer_not_verified"}
        try:
            db.execute(
                "INSERT INTO cp_reservations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (x["new_reservation_id"], old["order_id"], x["new_slot_id"], old["customer"],
                 slot["failure_domain"], "replacement", old["migration_id"],
                 x["lease_start"], x["lease_expires"], "active", x["at"]),
            )
        except sqlite3.IntegrityError:
            return {"decision": "slot_already_reserved"}
        db.execute(
            "UPDATE cp_reservations SET status='reprovisioned' WHERE reservation_id=?",
            (x["reservation_id"],),
        )
        return {"decision": "reservation_reprovisioned",
                "new_reservation_id": x["new_reservation_id"]}

    if op == "settle_rework":
        liab = one(db, "SELECT * FROM cp_procurement_liabilities WHERE liability_id=?", (x["liability_id"],))
        if not liab or liab["status"] != "owed":
            return {"decision": "liability_not_open"}
        test = one(db,
            "SELECT 1 FROM cp_delivery_tests WHERE reservation_id=? AND status='passed'",
            (x["reservation_id"],),
        )
        if not test:
            return {"decision": "payment_requires_delivery"}
        db.execute(
            "UPDATE cp_procurement_liabilities SET status='settled', settled_at=? WHERE liability_id=?",
            (x["at"], x["liability_id"]),
        )
        return {"decision": "rework_settled"}

    if op == "correct_activation_proof":
        order = one(db, "SELECT * FROM cp_orders WHERE order_id=?", (x["order_id"],))
        if not order:
            return {"decision": "unknown_order"}
        old_proof = one(db,
            "SELECT * FROM cp_activation_proofs WHERE order_id=? AND status='issued' ORDER BY at DESC LIMIT 1",
            (x["order_id"],),
        )
        if not old_proof:
            return {"decision": "no_activation_proof"}
        state = readiness(db, x["order_id"], x["at"])
        if not state["ready"]:
            return {"decision": "replicas_not_ready", "problems": state["problems"]}
        latest_test = one(db,
            """
            SELECT MAX(t.at) AS at FROM cp_delivery_tests t
            JOIN cp_reservations r ON r.reservation_id = t.reservation_id
            WHERE r.order_id=? AND r.status='active' AND t.status='passed'
            """,
            (x["order_id"],),
        )
        service_start = max(x["at"], latest_test["at"])
        db.execute(
            "UPDATE cp_activation_proofs SET status='corrected', superseded_by=? WHERE proof_id=?",
            (x["proof_id"], old_proof["proof_id"]),
        )
        _add_liability(
            db, x["liability_id"], "activation_correction", "proof", old_proof["proof_id"],
            x.get("amount", 0), x["reason"], x["at"],
        )
        db.execute(
            "INSERT INTO cp_activation_proofs VALUES(?,?,?,?,?,?,?,?)",
            (x["proof_id"], x["order_id"], order["customer"], service_start,
             x["at"], "issued", None, x["reason"]),
        )
        return {"decision": "activation_proof_corrected",
                "old_proof_id": old_proof["proof_id"], "service_start": service_start}

    if op == "lump_refund":
        # Quotation arithmetic can never produce a refundable over-delivery:
        # resold offers are the same physical slots. Any remedy is a per-slot
        # liability record, never a refund against one summed paper number.
        return {"decision": "lump_sum_refund_rejected"}

    raise ValueError(op)
