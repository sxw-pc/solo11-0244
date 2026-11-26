"""Replayable physical-usage to customer/vendor settlement ledger.

The module deliberately separates four ideas:

* physical facts proven by the storage vendor;
* customer signed usage candidates (retries and competing claims are not summed);
* immutable commercial documents (notices, invoices, supplements and credit notes);
* immutable money receipts and append-only reversals/refunds.

Everything written through :func:`run` is journaled.  Replaying the journal into a
fresh database reproduces every projection table.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .store import connect as store_connect
from .store import one, rows, tx

_COUNTER = count()

SCHEMA = """
CREATE TABLE IF NOT EXISTS us_command_journal(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 command_id TEXT UNIQUE NOT NULL,
 op TEXT NOT NULL,
 payload TEXT NOT NULL,
 result TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_namespace_uids(
 namespace_uid TEXT PRIMARY KEY,
 namespace_id TEXT NOT NULL,
 generation INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS us_ns_name_idx ON us_namespace_uids(namespace_id);

CREATE TABLE IF NOT EXISTS us_resources(
 resource_id TEXT PRIMARY KEY,
 namespace_id TEXT NOT NULL,
 namespace_uid TEXT NOT NULL,
 generation INTEGER NOT NULL,
 fingerprint TEXT NOT NULL DEFAULT '',
 owner TEXT,
 status TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_resource_epochs(
 resource_id TEXT NOT NULL,
 epoch_no INTEGER NOT NULL,
 generation INTEGER NOT NULL,
 owner TEXT NOT NULL,
 fingerprint TEXT NOT NULL DEFAULT '',
 valid_from INTEGER NOT NULL,
 valid_to INTEGER,
 command_id TEXT NOT NULL,
 PRIMARY KEY(resource_id, epoch_no)
);

CREATE TABLE IF NOT EXISTS us_usage(
 usage_id TEXT PRIMARY KEY,
 period TEXT NOT NULL,
 namespace_id TEXT NOT NULL,
 namespace_uid TEXT NOT NULL,
 resource_id TEXT NOT NULL,
 request_seq TEXT NOT NULL,
 persisted_from INTEGER NOT NULL,
 persisted_to INTEGER NOT NULL,
 byte_start INTEGER NOT NULL DEFAULT -1,
 byte_end INTEGER NOT NULL DEFAULT -1,
 counter_id TEXT NOT NULL DEFAULT 'vendor-counter',
 counter_generation INTEGER NOT NULL DEFAULT 1,
 vendor_id TEXT NOT NULL DEFAULT 'storage-vendor',
 physical_units INTEGER NOT NULL DEFAULT 0,
 vendor_unit_cost INTEGER NOT NULL DEFAULT 1,
 vendor_amount INTEGER NOT NULL DEFAULT 0,
 customer_unit_price INTEGER NOT NULL DEFAULT 1,
 owner_at_use TEXT,
 attributed_customer TEXT,
 attribution_status TEXT NOT NULL,
 created_at INTEGER NOT NULL,
 command_id TEXT NOT NULL,
 UNIQUE(namespace_uid, resource_id, request_seq, persisted_from, persisted_to,
        byte_start, byte_end, counter_id, counter_generation)
);

CREATE TABLE IF NOT EXISTS us_vendor_proofs(
 proof_id TEXT PRIMARY KEY,
 usage_id TEXT NOT NULL UNIQUE,
 vendor_id TEXT NOT NULL,
 counter_id TEXT NOT NULL,
 counter_generation INTEGER NOT NULL,
 units INTEGER NOT NULL,
 unit_cost INTEGER NOT NULL,
 amount INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_signoffs(
 signoff_id TEXT PRIMARY KEY,
 usage_id TEXT NOT NULL,
 customer TEXT NOT NULL,
 claimed_units INTEGER NOT NULL,
 accepted_units INTEGER NOT NULL,
 free_units INTEGER NOT NULL DEFAULT 0,
 billable_units INTEGER NOT NULL,
 request_key TEXT NOT NULL UNIQUE,
 unit_price INTEGER NOT NULL,
 amount INTEGER NOT NULL,
 signed_at INTEGER NOT NULL,
 command_id TEXT NOT NULL,
 UNIQUE(usage_id, customer)
);

CREATE TABLE IF NOT EXISTS us_cases(
 case_id TEXT PRIMARY KEY,
 period TEXT,
 namespace_id TEXT,
 namespace_uid TEXT,
 disputed_usage_id TEXT,
 disputed_usage_ids TEXT NOT NULL DEFAULT '[]',
 usage_count INTEGER NOT NULL,
 physical_units INTEGER NOT NULL,
 physical_amount INTEGER NOT NULL,
 undisputed_units INTEGER NOT NULL,
 undisputed_amount INTEGER NOT NULL,
 undisputed_vendor_amount INTEGER NOT NULL,
 disputed_units INTEGER NOT NULL,
 disputed_amount INTEGER NOT NULL,
 disputed_vendor_amount INTEGER NOT NULL,
 unclaimed_units INTEGER NOT NULL,
 unclaimed_amount INTEGER NOT NULL,
 nonbillable_units INTEGER NOT NULL,
 nonbillable_amount INTEGER NOT NULL,
 claim_line_units INTEGER NOT NULL,
 claim_line_amount INTEGER NOT NULL,
 extra_claim_units INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_case_usage(
 case_id TEXT NOT NULL,
 usage_id TEXT NOT NULL,
 PRIMARY KEY(case_id, usage_id)
);

CREATE TABLE IF NOT EXISTS us_vendor_bills(
 bill_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL,
 vendor_id TEXT NOT NULL,
 amount_due INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_settlements(
 settlement_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL,
 mode TEXT NOT NULL,
 vendor_amount INTEGER NOT NULL,
 platform_amount INTEGER NOT NULL,
 guarantor_amount INTEGER NOT NULL DEFAULT 0,
 guarantor TEXT,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL,
 UNIQUE(case_id)
);

CREATE TABLE IF NOT EXISTS us_case_finalizations(
 finalization_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL UNIQUE,
 mode TEXT NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL,
 result_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_invoices(
 invoice_id TEXT PRIMARY KEY,
 case_id TEXT,
 period TEXT,
 namespace_id TEXT,
 namespace_uid TEXT,
 customer TEXT NOT NULL,
 doc_type TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('debit','credit')),
 original_invoice_id TEXT,
 recovery_id TEXT,
 source_notice_id TEXT,
 total_amount INTEGER NOT NULL CHECK(total_amount >= 0),
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_invoice_lines(
 line_id TEXT PRIMARY KEY,
 invoice_id TEXT NOT NULL,
 usage_id TEXT,
 description TEXT NOT NULL,
 responsible_party TEXT,
 period TEXT,
 units INTEGER NOT NULL DEFAULT 0,
 unit_price INTEGER NOT NULL DEFAULT 0,
 amount INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS us_usage_notices(
 notice_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL,
 customer TEXT NOT NULL,
 period TEXT,
 confirmed_units INTEGER NOT NULL,
 confirmed_amount INTEGER NOT NULL,
 disputed_memo_units INTEGER NOT NULL,
 disputed_memo_amount INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_payments(
 payment_id TEXT PRIMARY KEY,
 case_id TEXT,
 direction TEXT NOT NULL,
 payer TEXT NOT NULL,
 payee TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount > 0),
 funding_source TEXT,
 purpose TEXT,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_payment_reversals(
 reversal_id TEXT PRIMARY KEY,
 payment_id TEXT NOT NULL,
 amount INTEGER NOT NULL CHECK(amount > 0),
 reason TEXT NOT NULL DEFAULT '',
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_allocations(
 allocation_id TEXT PRIMARY KEY,
 payment_id TEXT NOT NULL,
 target_type TEXT NOT NULL,
 target_id TEXT NOT NULL,
 amount INTEGER NOT NULL,
 reversal_id TEXT,
 original_allocation_id TEXT,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS us_alloc_target_idx ON us_allocations(target_type,target_id);

CREATE TABLE IF NOT EXISTS us_recovery_items(
 recovery_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL,
 usage_id TEXT NOT NULL,
 customer TEXT,
 receivable_amount INTEGER NOT NULL,
 cost_amount INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_guarantees(
 guarantee_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL UNIQUE,
 guarantor TEXT NOT NULL,
 amount INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS us_guarantee_refunds(
 refund_id TEXT PRIMARY KEY,
 guarantee_id TEXT NOT NULL,
 payment_id TEXT NOT NULL UNIQUE,
 amount INTEGER NOT NULL,
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_attribution_proofs(
 proof_id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL,
 usage_id TEXT NOT NULL,
 customer TEXT NOT NULL,
 issuer TEXT NOT NULL DEFAULT 'platform',
 evidence TEXT NOT NULL DEFAULT '',
 at INTEGER NOT NULL,
 command_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS us_ledger(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,
 command_id TEXT NOT NULL,
 at INTEGER NOT NULL,
 account TEXT NOT NULL,
 debit INTEGER NOT NULL DEFAULT 0,
 credit INTEGER NOT NULL DEFAULT 0,
 party TEXT,
 case_id TEXT,
 usage_id TEXT,
 ref_type TEXT,
 ref_id TEXT
);

CREATE TRIGGER IF NOT EXISTS us_journal_no_update BEFORE UPDATE ON us_command_journal
BEGIN SELECT RAISE(ABORT, 'command journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_journal_no_delete BEFORE DELETE ON us_command_journal
BEGIN SELECT RAISE(ABORT, 'command journal is append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_invoices_no_update BEFORE UPDATE ON us_invoices
BEGIN SELECT RAISE(ABORT, 'invoices are append-only; issue a supplement or credit note'); END;
CREATE TRIGGER IF NOT EXISTS us_invoices_no_delete BEFORE DELETE ON us_invoices
BEGIN SELECT RAISE(ABORT, 'invoices are append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_payments_no_update BEFORE UPDATE ON us_payments
BEGIN SELECT RAISE(ABORT, 'payment receipts are append-only; issue a reversal'); END;
CREATE TRIGGER IF NOT EXISTS us_payments_no_delete BEFORE DELETE ON us_payments
BEGIN SELECT RAISE(ABORT, 'payment receipts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_reversals_no_update BEFORE UPDATE ON us_payment_reversals
BEGIN SELECT RAISE(ABORT, 'reversals are append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_ledger_no_update BEFORE UPDATE ON us_ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_ledger_no_delete BEFORE DELETE ON us_ledger
BEGIN SELECT RAISE(ABORT, 'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_vendor_proofs_no_update BEFORE UPDATE ON us_vendor_proofs
BEGIN SELECT RAISE(ABORT, 'vendor proofs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_signoffs_no_update BEFORE UPDATE ON us_signoffs
BEGIN SELECT RAISE(ABORT, 'customer signoffs are append-only'); END;
CREATE TRIGGER IF NOT EXISTS us_attribution_no_update BEFORE UPDATE ON us_attribution_proofs
BEGIN SELECT RAISE(ABORT, 'attribution evidence is append-only'); END;
"""


class SettlementError(Exception):
    def __init__(self, decision: str, **extra: Any):
        self.result = {"decision": decision}
        self.result.update(extra)
        super().__init__(decision)


def install(db) -> None:
    if getattr(db, "row_factory", None) is None:
        db.row_factory = sqlite3.Row
    existing = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='us_command_journal'"
    ).fetchone()
    if existing:
        return
    db.executescript(SCHEMA)


def connect(path: str = ":memory:"):
    db = store_connect(path)
    install(db)
    return db


@contextmanager
def _atomic(db):
    if db.in_transaction:
        name = f"us_savepoint_{next(_COUNTER)}"
        db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            db.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            db.execute(f"RELEASE SAVEPOINT {name}")
    else:
        with tx(db):
            yield


def _g(x: Dict[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in x and x[key] is not None:
            return x[key]
    return default


def _int(x: Dict[str, Any], *keys: str, default: int = 0, required: bool = False) -> int:
    value = _g(x, *keys, default=None)
    if value is None:
        if required:
            raise SettlementError("missing_field", fields=list(keys))
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettlementError("invalid_integer", fields=list(keys))
    if value < 0:
        raise SettlementError("negative_amount_not_allowed", fields=list(keys))
    return value


def _text(x: Dict[str, Any], *keys: str, required: bool = False, default: str = "") -> str:
    value = _g(x, *keys, default=default)
    if value is None or value == "":
        if required:
            raise SettlementError("missing_field", fields=list(keys))
        return ""
    return str(value)


def _command_id(x: Dict[str, Any]) -> str:
    explicit = _g(x, "command_id", "event_id")
    if explicit:
        return str(explicit)
    payload = json.dumps(x, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "cmd_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def run(db, event: Dict[str, Any]) -> Dict[str, Any]:
    """Apply one settlement command and journal it idempotently."""
    if not isinstance(event, dict):
        return {"decision": "invalid_event"}
    install(db)
    cid = _command_id(event)
    payload_text = _canonical(event)

    with _atomic(db):
        old = one(db, "SELECT result FROM us_command_journal WHERE command_id=?", (cid,))
        if old:
            return json.loads(old["result"])

        try:
            with _atomic(db):
                result = _dispatch(db, event, cid)
        except SettlementError as exc:
            result = exc.result

        db.execute(
            "INSERT INTO us_command_journal(command_id,op,payload,result) VALUES(?,?,?,?)",
            (cid, event.get("op", ""), payload_text, _canonical(result)),
        )
        return result


def submit(db, op: str, **event: Any) -> Dict[str, Any]:
    event["op"] = op
    return run(db, event)


def _dispatch(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    op = x.get("op")
    handlers = {
        "bind_namespace": _bind_namespace,
        "namespace": _bind_namespace,
        "register_resource": _register_resource,
        "resource": _register_resource,
        "handoff": _handoff,
        "transfer_holder": _handoff,
        "delete_resource": _delete_resource,
        "delete": _delete_resource,
        "restore_resource": _restore_resource,
        "reappear": _restore_resource,
        "vendor_proof": _vendor_proof,
        "prove_vendor_usage": _vendor_proof,
        "counter_proof": _vendor_proof,
        "customer_signoff": _customer_signoff,
        "sign_usage": _customer_signoff,
        "signoff": _customer_signoff,
        "reconcile": _reconcile,
        "reconcile_period": _reconcile,
        "settle_reserve": _settle_reserve,
        "reserve_settlement": _settle_reserve,
        "settle_guarantee": _settle_guarantee,
        "guarantee_settlement": _settle_guarantee,
        "attribute": _attribute,
        "provide_attribution_proof": _attribute,
        "finalize_attribution": _finalize_attribution,
        "issue_actual_bills": _finalize_attribution,
        "issue_credit_note": _issue_credit_note,
        "credit_note": _issue_credit_note,
        "issue_charge": _issue_charge,
        "append_supplement": _issue_charge,
        "pay_customer": _pay_customer,
        "customer_payment": _pay_customer,
        "pay_vendor": _pay_vendor,
        "vendor_payment": _pay_vendor,
        "reverse_payment": _reverse_payment,
        "reverse": _reverse_payment,
        "refund_guarantee": _refund_guarantee,
    }
    handler = handlers.get(op)
    if handler is None:
        raise SettlementError("unknown_op", op=op)
    return handler(db, x, cid)


def _post(db, cid: str, at: int, entries: Iterable[tuple], **ref: Any) -> None:
    entries = list(entries)
    debit = sum(e[1] for e in entries)
    credit = sum(e[2] for e in entries)
    if debit != credit:
        raise RuntimeError(f"unbalanced ledger entry for {cid}: {debit} != {credit}")
    for account, d, c in entries:
        db.execute(
            """INSERT INTO us_ledger(command_id,at,account,debit,credit,party,case_id,usage_id,ref_type,ref_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (cid, at, account, d, c, ref.get("party"), ref.get("case_id"),
             ref.get("usage_id"), ref.get("ref_type"), ref.get("ref_id")),
        )


def _ensure_namespace(db, x: Dict[str, Any], cid: str) -> tuple[str, str]:
    ns = _text(x, "namespace", "namespace_id", "ns", required=True)
    uid = _text(x, "uid", "namespace_uid", "ns_uid")
    if uid:
        old = one(db, "SELECT * FROM us_namespace_uids WHERE namespace_uid=?", (uid,))
        if old and old["namespace_id"] != ns:
            raise SettlementError("uid_not_reusable", namespace=ns, uid=uid,
                                  original_namespace=old["namespace_id"])
        if not old:
            gen = _int(x, "namespace_generation", "ns_generation", default=1)
            at = _int(x, "at", default=0)
            db.execute(
                """INSERT INTO us_namespace_uids(namespace_uid,namespace_id,generation,at,command_id)
                   VALUES(?,?,?,?,?)""",
                (uid, ns, gen, at, cid),
            )
        return ns, uid

    matches = rows(db, "SELECT namespace_uid FROM us_namespace_uids WHERE namespace_id=?", (ns,))
    if not matches:
        raise SettlementError("namespace_uid_required", namespace=ns)
    if len(matches) > 1:
        raise SettlementError("namespace_uid_ambiguous_after_rebuild", namespace=ns,
                              uids=[r["namespace_uid"] for r in matches])
    return ns, matches[0]["namespace_uid"]


def _bind_namespace(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    ns = _text(x, "namespace", "namespace_id", "id", required=True)
    uid = _text(x, "uid", "namespace_uid", required=True)
    generation = _int(x, "generation", default=1)
    at = _int(x, "at", default=0)
    old_uid = one(db, "SELECT * FROM us_namespace_uids WHERE namespace_uid=?", (uid,))
    if old_uid and old_uid["namespace_id"] != ns:
        raise SettlementError("uid_not_reusable", uid=uid, namespace=ns)
    if not old_uid:
        db.execute(
            """INSERT INTO us_namespace_uids(namespace_uid,namespace_id,generation,at,command_id)
               VALUES(?,?,?,?,?)""",
            (uid, ns, generation, at, cid),
        )
        return {"decision": "namespace_bound", "namespace": ns, "uid": uid, "generation": generation}
    return {"decision": "namespace_reused", "namespace": ns, "uid": uid,
            "generation": old_uid["generation"]}


def _register_resource(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    rid = _text(x, "resource_id", "resource", "id", required=True)
    ns, uid = _ensure_namespace(db, x, cid)
    generation = _int(x, "generation", default=1)
    at = _int(x, "at", default=0)
    owner = _text(x, "owner", "holder", "customer", default="unknown") or "unknown"
    fingerprint = _text(x, "fingerprint", default="")
    old = one(db, "SELECT * FROM us_resources WHERE resource_id=?", (rid,))
    if old:
        if old["namespace_id"] != ns or old["namespace_uid"] != uid:
            raise SettlementError("resource_identity_conflict", resource=rid)
        if old["generation"] == generation and old["fingerprint"] == fingerprint:
            return {"decision": "reused", "resource_id": rid}
        raise SettlementError("resource_conflict", resource=rid,
                              current_generation=old["generation"])
    db.execute(
        """INSERT INTO us_resources(resource_id,namespace_id,namespace_uid,generation,fingerprint,
             owner,status,created_at,command_id) VALUES(?,?,?,?,?,?,'active',?,?)""",
        (rid, ns, uid, generation, fingerprint, owner, at, cid),
    )
    db.execute(
        """INSERT INTO us_resource_epochs(resource_id,epoch_no,generation,owner,fingerprint,
             valid_from,valid_to,command_id) VALUES(?,1,?,?,?,?,NULL,?)""",
        (rid, generation, owner, fingerprint, at, cid),
    )
    return {"decision": "registered", "resource_id": rid, "owner": owner}


def _current_epoch(db, rid: str):
    return one(db, """SELECT * FROM us_resource_epochs WHERE resource_id=?
                      ORDER BY epoch_no DESC LIMIT 1""", (rid,))


def _handoff(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    rid = _text(x, "resource_id", "resource", "id", required=True)
    new_owner = _text(x, "to_owner", "new_holder", "to_team", "new_owner", required=True)
    at = _int(x, "at", required=True) if "at" in x else 0
    r = one(db, "SELECT * FROM us_resources WHERE resource_id=?", (rid,))
    if not r:
        raise SettlementError("unknown_resource", resource=rid)
    epoch = _current_epoch(db, rid)
    if epoch and at < epoch["valid_from"]:
        raise SettlementError("invalid_handoff_time", resource=rid)
    if r["owner"] == new_owner:
        return {"decision": "holder_unchanged", "resource_id": rid, "holder": new_owner}
    if epoch and epoch["valid_to"] is None:
        db.execute("UPDATE us_resource_epochs SET valid_to=? WHERE resource_id=? AND epoch_no=?",
                   (at, rid, epoch["epoch_no"]))
    next_no = (epoch["epoch_no"] + 1) if epoch else 1
    db.execute(
        """INSERT INTO us_resource_epochs(resource_id,epoch_no,generation,owner,fingerprint,
             valid_from,valid_to,command_id) VALUES(?,?,?,?,?,?,NULL,?)""",
        (rid, next_no, r["generation"], new_owner, r["fingerprint"], at, cid),
    )
    db.execute("UPDATE us_resources SET owner=? WHERE resource_id=?", (new_owner, rid))
    return {"decision": "holder_changed", "resource_id": rid, "from": r["owner"], "to": new_owner}


def _delete_resource(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    rid = _text(x, "resource_id", "resource", "id", required=True)
    at = _int(x, "at", default=0)
    generation = _int(x, "generation", default=-1)
    r = one(db, "SELECT * FROM us_resources WHERE resource_id=?", (rid,))
    if not r:
        raise SettlementError("unknown_resource", resource=rid)
    if r["status"] == "deleted":
        return {"decision": "already_deleted", "resource_id": rid}
    if generation >= 0 and generation != r["generation"]:
        raise SettlementError("stale_resource_generation", resource=rid,
                              current_generation=r["generation"])
    epoch = _current_epoch(db, rid)
    if epoch and epoch["valid_to"] is None:
        db.execute("UPDATE us_resource_epochs SET valid_to=? WHERE resource_id=? AND epoch_no=?",
                   (at, rid, epoch["epoch_no"]))
    db.execute("UPDATE us_resources SET status='deleted' WHERE resource_id=?", (rid,))
    return {"decision": "deleted", "resource_id": rid, "at": at}


def _restore_resource(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    rid = _text(x, "resource_id", "resource", "id", required=True)
    new_generation = _int(x, "new_generation", "generation", required=True)
    at = _int(x, "at", default=0)
    owner = _text(x, "owner", "holder") or "unknown"
    fingerprint = _text(x, "fingerprint", default="")
    r = one(db, "SELECT * FROM us_resources WHERE resource_id=?", (rid,))
    if not r:
        raise SettlementError("unknown_resource", resource=rid)
    if r["status"] != "deleted":
        raise SettlementError("resource_not_deleted", resource=rid)
    if new_generation <= r["generation"]:
        raise SettlementError("stale_reappearance", resource=rid,
                              current_generation=r["generation"])
    db.execute(
        "UPDATE us_resources SET generation=?,fingerprint=?,owner=?,status='active' WHERE resource_id=?",
        (new_generation, fingerprint, owner, rid),
    )
    epoch = _current_epoch(db, rid)
    next_no = (epoch["epoch_no"] + 1) if epoch else 1
    db.execute(
        """INSERT INTO us_resource_epochs(resource_id,epoch_no,generation,owner,fingerprint,
             valid_from,valid_to,command_id) VALUES(?,?,?,?,?,?,NULL,?)""",
        (rid, next_no, new_generation, owner, fingerprint, at, cid),
    )
    decision = "restored" if owner != "unknown" else "quarantined_reappearance"
    return {"decision": decision, "resource_id": rid, "generation": new_generation, "owner": owner}


def _interval(x: Dict[str, Any]) -> tuple[int, int]:
    start = _g(x, "persisted_from", "from_at", "start_at", "start")
    end = _g(x, "persisted_to", "to_at", "end_at", "end")
    at = _g(x, "at", "persisted_at")
    if start is None:
        start = at if at is not None else 0
    if end is None:
        end = at if at is not None else start
    start = int(start)
    end = int(end)
    if end < start:
        raise SettlementError("invalid_persistence_interval")
    return start, end


def _usage_identity(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    ns, uid = _ensure_namespace(db, x, cid)
    rid = _text(x, "resource_id", "resource", required=True)
    request_seq = _text(x, "request_seq", "sequence", "client_request_seq", required=True)
    p_from, p_to = _interval(x)
    return {
        "namespace_id": ns,
        "namespace_uid": uid,
        "resource_id": rid,
        "request_seq": str(request_seq),
        "persisted_from": p_from,
        "persisted_to": p_to,
        "byte_start": _int(x, "byte_start", "range_start", default=-1),
        "byte_end": _int(x, "byte_end", "range_end", default=-1),
        "counter_id": _text(x, "counter_id", default="vendor-counter"),
        "counter_generation": _int(x, "counter_generation", "counter_epoch", default=1),
    }


def _usage_natural_id(ident: Dict[str, Any]) -> str:
    raw = "|".join(str(ident[k]) for k in (
        "namespace_uid", "resource_id", "request_seq", "persisted_from", "persisted_to",
        "byte_start", "byte_end", "counter_id", "counter_generation"))
    return "usg_" + hashlib.sha256(raw.encode()).hexdigest()[:18]


def _get_or_create_usage(db, x: Dict[str, Any], cid: str, at: int) -> sqlite3.Row | Dict[str, Any]:
    supplied_usage_id = _text(x, "usage_id")
    if supplied_usage_id:
        existing_by_id = one(db, "SELECT * FROM us_usage WHERE usage_id=?", (supplied_usage_id,))
        if existing_by_id:
            return existing_by_id

    ident = _usage_identity(db, x, cid)
    resource = one(db, "SELECT * FROM us_resources WHERE resource_id=?", (ident["resource_id"],))
    if resource and resource["namespace_uid"] != ident["namespace_uid"]:
        raise SettlementError("resource_identity_conflict", resource=ident["resource_id"],
                              original_namespace_uid=resource["namespace_uid"],
                              presented_namespace_uid=ident["namespace_uid"])
    if not resource:
        at = _int(x, "at", default=0)
        owner = _text(x, "owner_at_use", "owner", "holder", "customer") or "unknown"
        generation = _int(x, "resource_generation", "generation", default=1)
        db.execute(
            """INSERT INTO us_resources(resource_id,namespace_id,namespace_uid,generation,
                 fingerprint,owner,status,created_at,command_id)
               VALUES(?,?,?,?,?,?,'active',?,?)""",
            (ident["resource_id"], ident["namespace_id"], ident["namespace_uid"], generation,
             _text(x, "fingerprint", default=""), owner, at, cid),
        )
        db.execute(
            """INSERT INTO us_resource_epochs(resource_id,epoch_no,generation,owner,fingerprint,
                 valid_from,valid_to,command_id) VALUES(?,1,?,?,?,?,NULL,?)""",
            (ident["resource_id"], generation, owner, _text(x, "fingerprint", default=""),
             ident["persisted_from"], cid),
        )

    usage_id = supplied_usage_id or _usage_natural_id(ident)
    u = one(db, "SELECT * FROM us_usage WHERE usage_id=?", (usage_id,))
    natural = one(db, """SELECT * FROM us_usage WHERE namespace_uid=? AND resource_id=?
                         AND request_seq=? AND persisted_from=? AND persisted_to=?
                         AND byte_start=? AND byte_end=? AND counter_id=? AND counter_generation=?""",
                  tuple(ident[k] for k in (
                      "namespace_uid", "resource_id", "request_seq", "persisted_from", "persisted_to",
                      "byte_start", "byte_end", "counter_id", "counter_generation")))
    if natural and u and natural["usage_id"] != u["usage_id"]:
        raise SettlementError("usage_identity_conflict", usage_id=usage_id,
                              existing_usage_id=natural["usage_id"])
    if natural:
        return natural
    # A restarted vendor counter can report a new generation for the exact same
    # request/resource/persistence interval.  That is a duplicate physical fact,
    # not a second usage; the counter generation remains on the first fact.
    physical = one(db, """SELECT * FROM us_usage
                          WHERE namespace_uid=? AND resource_id=? AND request_seq=?
                            AND persisted_from=? AND persisted_to=? AND byte_start=? AND byte_end=?
                          ORDER BY rowid LIMIT 1""",
                   (ident["namespace_uid"], ident["resource_id"], ident["request_seq"],
                    ident["persisted_from"], ident["persisted_to"],
                    ident["byte_start"], ident["byte_end"]))
    if physical:
        return physical
    if u:
        # An explicit ID was supplied but its natural key differs from the payload.
        raise SettlementError("usage_identity_conflict", usage_id=usage_id)

    owner = _text(x, "owner_at_use", "owner", "holder", "customer")
    if not owner:
        epoch_owner = one(db, """SELECT owner FROM us_resource_epochs
                                 WHERE resource_id=? AND valid_from<=?
                                   AND (valid_to IS NULL OR valid_to>?)
                                 ORDER BY epoch_no DESC LIMIT 1""",
                          (ident["resource_id"], ident["persisted_from"], ident["persisted_from"]))
        owner = epoch_owner["owner"] if epoch_owner else None
    period = _text(x, "period") or f"P{ident['persisted_from']}"
    unit_price = _int(x, "customer_unit_price", "unit_price", default=1)
    vendor_cost = _int(x, "vendor_unit_cost", "unit_cost", "unit_price", default=1)
    vendor_id = _text(x, "vendor", "vendor_id", default="storage-vendor")
    db.execute(
        """INSERT INTO us_usage(usage_id,period,namespace_id,namespace_uid,resource_id,request_seq,
             persisted_from,persisted_to,byte_start,byte_end,counter_id,counter_generation,vendor_id,
             customer_unit_price,vendor_unit_cost,owner_at_use,attribution_status,created_at,command_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'unclaimed',?,?)""",
        (usage_id, period, ident["namespace_id"], ident["namespace_uid"], ident["resource_id"],
         ident["request_seq"], ident["persisted_from"], ident["persisted_to"], ident["byte_start"],
         ident["byte_end"], ident["counter_id"], ident["counter_generation"], vendor_id,
         unit_price, vendor_cost, owner, at, cid),
    )
    return one(db, "SELECT * FROM us_usage WHERE usage_id=?", (usage_id,))


def _vendor_proof(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    proof_id = _text(x, "proof_id", "id", required=True)
    at = _int(x, "at", default=0)
    units = _int(x, "units", "physical_units", "quantity", required=True) if _g(x, "units", "physical_units", "quantity") is not None else None
    if units is None:
        raise SettlementError("missing_field", fields=["units"])
    unit_cost = _int(x, "unit_cost", "vendor_unit_price", "cost_per_unit", "unit_price", default=1)
    amount = _int(x, "amount", "vendor_amount", default=units * unit_cost)
    if "amount" not in x and "vendor_amount" not in x and amount != units * unit_cost:
        amount = units * unit_cost
    old_proof = one(db, "SELECT * FROM us_vendor_proofs WHERE proof_id=?", (proof_id,))
    u = _get_or_create_usage(db, x, cid, at)
    if old_proof:
        if old_proof["usage_id"] != u["usage_id"] or old_proof["units"] != units or old_proof["amount"] != amount:
            raise SettlementError("proof_conflict", proof_id=proof_id)
        return {"decision": "reused", "proof_id": proof_id, "usage_id": u["usage_id"], "amount": amount}
    existing = one(db, "SELECT * FROM us_vendor_proofs WHERE usage_id=?", (u["usage_id"],))
    if existing:
        if existing["units"] == units and existing["amount"] == amount:
            return {"decision": "duplicate_proof", "proof_id": existing["proof_id"],
                    "usage_id": u["usage_id"], "amount": amount}
        raise SettlementError("vendor_proof_conflict", usage_id=u["usage_id"])

    vendor = _text(x, "vendor", "vendor_id", default=u["vendor_id"])
    db.execute(
        """INSERT INTO us_vendor_proofs(proof_id,usage_id,vendor_id,counter_id,counter_generation,
             units,unit_cost,amount,at,command_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (proof_id, u["usage_id"], vendor, u["counter_id"], u["counter_generation"],
         units, unit_cost, amount, at, cid),
    )
    db.execute(
        """UPDATE us_usage SET physical_units=?, vendor_unit_cost=?, vendor_amount=?, vendor_id=?
           WHERE usage_id=?""",
        (units, unit_cost, amount, vendor, u["usage_id"]),
    )
    _post(db, cid, at, [
        ("cost.storage", amount, 0),
        ("vendor.payable", 0, amount),
    ], case_id=x.get("case_id"), usage_id=u["usage_id"], ref_type="vendor_proof", ref_id=proof_id)
    return {"decision": "vendor_usage_proven", "proof_id": proof_id,
            "usage_id": u["usage_id"], "units": units, "vendor_amount": amount}


def _customer_signoff(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    signoff_id = _text(x, "signoff_id", "id", required=True)
    customer = _text(x, "customer", "account", "payer", required=True)
    at = _int(x, "at", "signed_at", default=0)
    accepted = _int(x, "accepted_units", "units", "quantity", required=True) if _g(x, "accepted_units", "units", "quantity") is not None else None
    if accepted is None:
        raise SettlementError("missing_field", fields=["units"])
    claimed = _int(x, "claimed_units", "logged_units", default=accepted)
    free = _int(x, "free_units", default=0)
    if "billable_units" in x:
        billable = _int(x, "billable_units")
    elif x.get("contract_billable", True) is False:
        billable = 0
    else:
        billable = max(0, accepted - free)
    if billable > accepted:
        raise SettlementError("billable_exceeds_accepted", customer=customer)
    unit_price = _int(x, "unit_price", "customer_unit_price", default=1)
    amount = _int(x, "amount", default=billable * unit_price)

    _ns, uid = _ensure_namespace(db, x, cid)
    request_seq = _text(x, "request_seq", "sequence", "client_request_seq", required=True)
    request_key = f"{uid}\x1f{customer}\x1f{request_seq}"
    same_request = one(db, "SELECT * FROM us_signoffs WHERE request_key=?", (request_key,))
    if same_request:
        if same_request["accepted_units"] == accepted and same_request["billable_units"] == billable:
            return {"decision": "reused_customer_request", "signoff_id": same_request["signoff_id"],
                    "usage_id": same_request["usage_id"]}
        raise SettlementError("request_sequence_reused_with_different_quantity",
                              signoff_id=same_request["signoff_id"], customer=customer,
                              request_seq=request_seq)

    old = one(db, "SELECT * FROM us_signoffs WHERE signoff_id=?", (signoff_id,))
    u = _get_or_create_usage(db, x, cid, at)
    if old:
        if old["usage_id"] != u["usage_id"] or old["customer"] != customer or old["billable_units"] != billable:
            raise SettlementError("signoff_conflict", signoff_id=signoff_id)
        return {"decision": "reused", "signoff_id": signoff_id, "usage_id": u["usage_id"]}

    same_claim = one(db, "SELECT * FROM us_signoffs WHERE usage_id=? AND customer=?",
                     (u["usage_id"], customer))
    if same_claim:
        if same_claim["accepted_units"] == accepted and same_claim["billable_units"] == billable:
            return {"decision": "reused_signoff", "signoff_id": same_claim["signoff_id"],
                    "usage_id": u["usage_id"]}
        raise SettlementError("signoff_conflict", usage_id=u["usage_id"], customer=customer)

    db.execute(
        """INSERT INTO us_signoffs(signoff_id,usage_id,customer,claimed_units,accepted_units,
             free_units,billable_units,request_key,unit_price,amount,signed_at,command_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (signoff_id, u["usage_id"], customer, claimed, accepted, free, billable, request_key,
         unit_price, amount, at, cid),
    )
    count = one(db, "SELECT COUNT(*) AS n FROM us_signoffs WHERE usage_id=?", (u["usage_id"],))["n"]
    if count > 1:
        db.execute("UPDATE us_usage SET attribution_status='disputed', attributed_customer=NULL WHERE usage_id=?",
                   (u["usage_id"],))
        decision = "disputed_candidate_recorded"
    else:
        db.execute("UPDATE us_usage SET attribution_status='claimed' WHERE usage_id=?", (u["usage_id"],))
        decision = "signed"
    return {"decision": decision, "signoff_id": signoff_id, "usage_id": u["usage_id"],
            "candidate_count": count, "billable_units": billable, "amount": amount}


def _case_usage_ids(db, x: Dict[str, Any]) -> List[str]:
    if x.get("usage_ids"):
        return [str(v) for v in x["usage_ids"]]
    case_id = x.get("case_id")
    if case_id:
        links = rows(db, "SELECT usage_id FROM us_case_usage WHERE case_id=? ORDER BY rowid", (case_id,))
        if links:
            return [r["usage_id"] for r in links]
    period = x.get("period")
    ns = x.get("namespace") or x.get("namespace_id")
    if period:
        q = "SELECT usage_id FROM us_usage WHERE period=?"
        args: list[Any] = [period]
        if ns:
            q += " AND namespace_id=?"
            args.append(ns)
        q += " ORDER BY usage_id"
        return [r["usage_id"] for r in rows(db, q, tuple(args))]
    return [r["usage_id"] for r in rows(db, "SELECT usage_id FROM us_usage ORDER BY usage_id")]


def _reconcile(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    case_id = _text(x, "case_id", "id", required=True)
    old_case = one(db, "SELECT * FROM us_cases WHERE case_id=?", (case_id,))
    if old_case:
        return {"decision": "case_already_reconciled", "case_id": case_id}
    at = _int(x, "at", default=0)
    usage_ids = _case_usage_ids(db, x)
    if not usage_ids:
        raise SettlementError("no_usage_to_reconcile", case_id=case_id)

    totals = dict(physical_units=0, physical_amount=0, undisputed_units=0, undisputed_amount=0,
                  undisputed_vendor_amount=0, disputed_units=0, disputed_amount=0,
                  disputed_vendor_amount=0, unclaimed_units=0, unclaimed_amount=0,
                  nonbillable_units=0, nonbillable_amount=0, claim_line_units=0,
                  claim_line_amount=0, extra_claim_units=0)
    disputed_ids: List[str] = []
    namespace_id = namespace_uid = period = None

    for usage_id in usage_ids:
        u = one(db, "SELECT * FROM us_usage WHERE usage_id=?", (usage_id,))
        if not u:
            raise SettlementError("unknown_usage", usage_id=usage_id)
        if x.get("require_proof", True) and not one(db, "SELECT proof_id FROM us_vendor_proofs WHERE usage_id=?", (usage_id,)):
            raise SettlementError("missing_vendor_proof", usage_id=usage_id)
        namespace_id = namespace_id or u["namespace_id"]
        namespace_uid = namespace_uid or u["namespace_uid"]
        period = period or u["period"]
        signs = rows(db, "SELECT * FROM us_signoffs WHERE usage_id=? ORDER BY customer", (usage_id,))

        totals["physical_units"] += u["physical_units"]
        totals["physical_amount"] += u["vendor_amount"]
        totals["claim_line_units"] += sum(s["billable_units"] for s in signs)
        totals["claim_line_amount"] += sum(s["amount"] for s in signs)
        max_billable = max([s["billable_units"] for s in signs], default=0)
        if signs:
            # Covers both bytes never accepted and accepted bytes that the
            # contract makes free/non-billable.  Unclaimed physical usage is
            # kept in its own bucket instead.
            totals["nonbillable_units"] += max(0, u["physical_units"] - max_billable)
            totals["nonbillable_amount"] += (
                max(0, u["physical_units"] - max_billable) * u["customer_unit_price"]
            )

        if len(signs) == 1:
            s = signs[0]
            totals["undisputed_units"] += s["billable_units"]
            totals["undisputed_amount"] += s["amount"]
            totals["undisputed_vendor_amount"] += u["vendor_amount"]
            db.execute("UPDATE us_usage SET attribution_status='uncontested', attributed_customer=? WHERE usage_id=?",
                       (s["customer"], usage_id))
        elif len(signs) > 1:
            disputed_ids.append(usage_id)
            potential = u["physical_units"] * u["customer_unit_price"]
            totals["disputed_units"] += u["physical_units"]
            totals["disputed_amount"] += potential
            totals["disputed_vendor_amount"] += u["vendor_amount"]
            totals["extra_claim_units"] += max(0, sum(s["billable_units"] for s in signs) - u["physical_units"])
            db.execute("UPDATE us_usage SET attribution_status='disputed', attributed_customer=NULL WHERE usage_id=?",
                       (usage_id,))
        else:
            totals["unclaimed_units"] += u["physical_units"]
            totals["unclaimed_amount"] += u["vendor_amount"]
            db.execute("UPDATE us_usage SET attribution_status='unclaimed' WHERE usage_id=?", (usage_id,))

    db.execute(
        """INSERT INTO us_cases(case_id,period,namespace_id,namespace_uid,disputed_usage_id,
             disputed_usage_ids,usage_count,physical_units,physical_amount,undisputed_units,
             undisputed_amount,undisputed_vendor_amount,disputed_units,disputed_amount,
             disputed_vendor_amount,unclaimed_units,unclaimed_amount,nonbillable_units,
             nonbillable_amount,claim_line_units,claim_line_amount,extra_claim_units,at,command_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (case_id, x.get("period", period), x.get("namespace", namespace_id), namespace_uid,
         disputed_ids[0] if disputed_ids else None, _canonical(disputed_ids), len(usage_ids),
         totals["physical_units"], totals["physical_amount"], totals["undisputed_units"],
         totals["undisputed_amount"], totals["undisputed_vendor_amount"], totals["disputed_units"],
         totals["disputed_amount"], totals["disputed_vendor_amount"], totals["unclaimed_units"],
         totals["unclaimed_amount"], totals["nonbillable_units"], totals["nonbillable_amount"],
         totals["claim_line_units"], totals["claim_line_amount"], totals["extra_claim_units"], at, cid),
    )
    db.executemany("INSERT INTO us_case_usage(case_id,usage_id) VALUES(?,?)",
                   [(case_id, uid) for uid in usage_ids])
    bill_id = _text(x, "vendor_bill_id") or f"VB-{case_id}"
    vendor = _text(x, "vendor", "vendor_id", default="storage-vendor")
    db.execute(
        "INSERT INTO us_vendor_bills(bill_id,case_id,vendor_id,amount_due,at,command_id) VALUES(?,?,?,?,?,?)",
        (bill_id, case_id, vendor, totals["physical_amount"], at, cid),
    )
    return {"decision": "reconciled", "case_id": case_id, "vendor_bill_id": bill_id, **totals,
            "disputed_usage_ids": disputed_ids}


def _require_case(db, case_id: str):
    case = one(db, "SELECT * FROM us_cases WHERE case_id=?", (case_id,))
    if not case:
        raise SettlementError("unknown_case", case_id=case_id)
    return case


def _group_undisputed_lines(db, case_id: str) -> Dict[str, List[Dict[str, Any]]]:
    records = rows(db, """
        SELECT u.*, s.customer, s.billable_units, s.unit_price AS signoff_price, s.amount AS line_amount,
               s.signoff_id
        FROM us_case_usage cu
        JOIN us_usage u ON u.usage_id=cu.usage_id
        JOIN us_signoffs s ON s.usage_id=u.usage_id
        WHERE cu.case_id=?
          AND (SELECT COUNT(*) FROM us_signoffs z WHERE z.usage_id=u.usage_id)=1
        ORDER BY s.customer,u.usage_id
    """, (case_id,))
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for r in records:
        grouped.setdefault(r["customer"], []).append(r)
    return grouped


def _create_invoice(db, cid: str, at: int, invoice_id: str, customer: str,
                    lines: List[Dict[str, Any]], *, case_id: Optional[str] = None,
                    doc_type: str = "invoice", direction: str = "debit",
                    original_invoice_id: Optional[str] = None,
                    recovery_id: Optional[str] = None,
                    source_notice_id: Optional[str] = None,
                    period: Optional[str] = None,
                    post_ledger: bool = True) -> Dict[str, Any]:
    total = sum(int(line["amount"]) for line in lines)
    db.execute(
        """INSERT INTO us_invoices(invoice_id,case_id,period,customer,doc_type,direction,
             original_invoice_id,recovery_id,source_notice_id,total_amount,at,command_id)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (invoice_id, case_id, period, customer, doc_type, direction, original_invoice_id,
         recovery_id, source_notice_id, total, at, cid),
    )
    for i, line in enumerate(lines, 1):
        line_id = line.get("line_id") or f"{invoice_id}-L{i}"
        db.execute(
            """INSERT INTO us_invoice_lines(line_id,invoice_id,usage_id,description,
                 responsible_party,period,units,unit_price,amount) VALUES(?,?,?,?,?,?,?,?,?)""",
            (line_id, invoice_id, line.get("usage_id"), line.get("description", "usage"),
             line.get("responsible_party") or line.get("customer") or customer,
             line.get("period"), line.get("units", 0), line.get("unit_price", 0), line["amount"]),
        )
    if post_ledger and total:
        if direction == "debit":
            _post(db, cid, at, [
                ("ar.customer", total, 0),
                ("revenue.usage", 0, total),
            ], party=customer, case_id=case_id, ref_type="invoice", ref_id=invoice_id)
        else:
            _post(db, cid, at, [
                ("revenue.usage", total, 0),
                ("ar.customer", 0, total),
            ], party=customer, case_id=case_id, ref_type="credit_note", ref_id=invoice_id)
    return {"invoice_id": invoice_id, "customer": customer, "amount": total,
            "doc_type": doc_type, "direction": direction}


def _create_payment_row(db, cid: str, at: int, payment_id: str, direction: str,
                        payer: str, payee: str, amount: int, *,
                        case_id: Optional[str] = None, source: str = "",
                        purpose: str = "") -> None:
    db.execute(
        """INSERT INTO us_payments(payment_id,case_id,direction,payer,payee,amount,
             funding_source,purpose,at,command_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (payment_id, case_id, direction, payer, payee, amount, source, purpose, at, cid),
    )


def _allocate(db, cid: str, at: int, allocation_id: str, payment_id: str,
              target_type: str, target_id: str, amount: int, *,
              reversal_id: Optional[str] = None,
              original_allocation_id: Optional[str] = None) -> None:
    db.execute(
        """INSERT INTO us_allocations(allocation_id,payment_id,target_type,target_id,amount,
             reversal_id,original_allocation_id,at,command_id) VALUES(?,?,?,?,?,?,?,?,?)""",
        (allocation_id, payment_id, target_type, target_id, amount, reversal_id,
         original_allocation_id, at, cid),
    )


def _settle_reserve(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    case_id = _text(x, "case_id", required=True)
    case = _require_case(db, case_id)
    sid = _text(x, "settlement_id", "id") or f"STL-{case_id}-reserve"
    old = one(db, "SELECT * FROM us_settlements WHERE case_id=?", (case_id,))
    if old:
        return {"decision": "settlement_already_recorded", "settlement_id": old["settlement_id"], "mode": old["mode"]}
    at = _int(x, "at", default=0)
    vendor_amount = case["physical_amount"]
    payment_id = _text(x, "vendor_payment_id") or f"VP-{sid}"
    payer = _text(x, "reserve_account", "payer", default="platform_reserve")
    bill_id = f"VB-{case_id}"

    db.execute(
        """INSERT INTO us_settlements(settlement_id,case_id,mode,vendor_amount,platform_amount,
             guarantor_amount,at,command_id) VALUES(?,?,'reserve',?,?,0,?,?)""",
        (sid, case_id, vendor_amount, vendor_amount, at, cid),
    )
    _create_payment_row(db, cid, at, payment_id, "vendor_out", payer,
                        _text(x, "vendor", default="storage-vendor"), vendor_amount,
                        case_id=case_id, source="platform_reserve", purpose="vendor_settlement")
    _allocate(db, cid, at, f"AL-{payment_id}", payment_id, "vendor_bill", bill_id, vendor_amount)
    _post(db, cid, at, [
        ("vendor.payable", vendor_amount, 0),
        ("cash.platform", 0, vendor_amount),
    ], case_id=case_id, ref_type="vendor_payment", ref_id=payment_id)

    invoices = []
    for customer, items in _group_undisputed_lines(db, case_id).items():
        lines = [{
            "usage_id": i["usage_id"],
            "description": f"accepted usage {i['usage_id']}",
            "units": i["billable_units"],
            "unit_price": i["signoff_price"],
            "amount": i["line_amount"],
            "responsible_party": customer,
            "period": i["period"],
        } for i in items]
        inv = _create_invoice(db, cid, at, f"INV-{sid}-{_safe(customer)}", customer, lines,
                              case_id=case_id, period=case["period"])
        invoices.append(inv)

    recovery_ids = []
    for idx, usage_id in enumerate(json.loads(case["disputed_usage_ids"] or "[]"), 1):
        u = one(db, "SELECT * FROM us_usage WHERE usage_id=?", (usage_id,))
        rid = f"REC-{case_id}-{idx}"
        db.execute(
            """INSERT INTO us_recovery_items(recovery_id,case_id,usage_id,customer,
                 receivable_amount,cost_amount,at,command_id) VALUES(?,?,?,NULL,?,?,?,?)""",
            (rid, case_id, usage_id, u["physical_units"] * u["customer_unit_price"],
             u["vendor_amount"], at, cid),
        )
        _post(db, cid, at, [
            ("recovery.pending", u["vendor_amount"], 0),
            ("cost.storage", 0, u["vendor_amount"]),
        ], case_id=case_id, usage_id=usage_id, ref_type="recovery_item", ref_id=rid)
        recovery_ids.append(rid)

    return {"decision": "reserve_settled", "settlement_id": sid, "case_id": case_id,
            "vendor_payment_id": payment_id, "vendor_paid": vendor_amount,
            "invoice_ids": [i["invoice_id"] for i in invoices],
            "invoiced_total": sum(i["amount"] for i in invoices),
            "recovery_ids": recovery_ids,
            "pending_recovery_amount": case["disputed_amount"]}


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in value)[:48]


def _customer_case_lines(db, case_id: str) -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    records = rows(db, """
        SELECT u.*, s.customer AS candidate, s.billable_units, s.unit_price AS signoff_price,
               s.amount AS line_amount
        FROM us_case_usage cu
        JOIN us_usage u ON u.usage_id=cu.usage_id
        JOIN us_signoffs s ON s.usage_id=u.usage_id
        WHERE cu.case_id=?
        ORDER BY s.customer,u.usage_id
    """, (case_id,))
    result: Dict[str, Dict[str, list]] = {}
    for r in records:
        bucket = result.setdefault(r["candidate"], {"confirmed": [], "disputed": []})
        count = one(db, "SELECT COUNT(*) AS n FROM us_signoffs WHERE usage_id=?", (r["usage_id"],))["n"]
        line = {
            "usage_id": r["usage_id"], "units": r["billable_units"],
            "unit_price": r["signoff_price"], "amount": r["line_amount"],
            "period": r["period"], "physical_units": r["physical_units"],
            "customer": r["candidate"],
        }
        bucket["disputed" if count > 1 else "confirmed"].append(line)
    return result


def _settle_guarantee(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    case_id = _text(x, "case_id", required=True)
    case = _require_case(db, case_id)
    old = one(db, "SELECT * FROM us_settlements WHERE case_id=?", (case_id,))
    if old:
        return {"decision": "settlement_already_recorded", "settlement_id": old["settlement_id"], "mode": old["mode"]}
    sid = _text(x, "settlement_id", "id") or f"STL-{case_id}-guarantee"
    at = _int(x, "at", default=0)
    total = case["physical_amount"]
    guarantee_amount = case["disputed_vendor_amount"]
    platform_amount = total - guarantee_amount
    guarantor = _text(x, "guarantor", default="contracted_guarantor")
    bill_id = f"VB-{case_id}"
    platform_pid = _text(x, "platform_payment_id") or f"VPP-{sid}"
    guarantee_pid = _text(x, "guarantee_payment_id") or f"VGP-{sid}"
    guarantee_id = _text(x, "guarantee_id") or f"GUA-{case_id}"

    db.execute(
        """INSERT INTO us_settlements(settlement_id,case_id,mode,vendor_amount,platform_amount,
             guarantor_amount,guarantor,at,command_id) VALUES(?,?, 'guarantee',?,?,?,?,?,?)""",
        (sid, case_id, total, platform_amount, guarantee_amount, guarantor, at, cid),
    )
    _create_payment_row(db, cid, at, platform_pid, "vendor_out", "platform",
                        _text(x, "vendor", default="storage-vendor"), platform_amount,
                        case_id=case_id, source="platform", purpose="undisputed_vendor_share")
    _allocate(db, cid, at, f"AL-{platform_pid}", platform_pid, "vendor_bill", bill_id, platform_amount)
    _post(db, cid, at, [
        ("vendor.payable", platform_amount, 0),
        ("cash.platform", 0, platform_amount),
    ], case_id=case_id, ref_type="vendor_payment", ref_id=platform_pid)

    if guarantee_amount:
        _create_payment_row(db, cid, at, guarantee_pid, "vendor_out", guarantor,
                            _text(x, "vendor", default="storage-vendor"), guarantee_amount,
                            case_id=case_id, source="guarantee", purpose="disputed_vendor_share")
        _allocate(db, cid, at, f"AL-{guarantee_pid}", guarantee_pid, "vendor_bill", bill_id, guarantee_amount)
        _post(db, cid, at, [
            ("vendor.payable", guarantee_amount, 0),
            ("guarantee.refundable", 0, guarantee_amount),
        ], case_id=case_id, ref_type="guarantee_payment", ref_id=guarantee_pid)
        db.execute(
            "INSERT INTO us_guarantees(guarantee_id,case_id,guarantor,amount,at,command_id) VALUES(?,?,?,?,?,?)",
            (guarantee_id, case_id, guarantor, guarantee_amount, at, cid),
        )

    notice_ids = []
    for customer, bucket in _customer_case_lines(db, case_id).items():
        confirmed = bucket["confirmed"]
        disputed = bucket["disputed"]
        nid = f"NTC-{sid}-{_safe(customer)}"
        db.execute(
            """INSERT INTO us_usage_notices(notice_id,case_id,customer,period,confirmed_units,
                 confirmed_amount,disputed_memo_units,disputed_memo_amount,at,command_id)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (nid, case_id, customer, case["period"],
             sum(i["units"] for i in confirmed), sum(i["amount"] for i in confirmed),
             sum(i["units"] for i in disputed), sum(i["amount"] for i in disputed), at, cid),
        )
        notice_ids.append(nid)

    return {"decision": "guarantee_settled", "settlement_id": sid, "case_id": case_id,
            "vendor_paid": total, "platform_paid": platform_amount,
            "guarantor_paid": guarantee_amount, "guarantee_id": guarantee_id,
            "platform_payment_id": platform_pid, "guarantee_payment_id": guarantee_pid,
            "notice_ids": notice_ids, "formal_invoices_issued": False}


def _attribute(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    proof_id = _text(x, "proof_id", "id", required=True)
    case_id = _text(x, "case_id", required=True)
    customer = _text(x, "customer", "true_customer", required=True)
    at = _int(x, "at", default=0)
    case = _require_case(db, case_id)
    usage_id = _text(x, "usage_id") or case["disputed_usage_id"]
    if not usage_id:
        raise SettlementError("usage_id_required")
    link = one(db, "SELECT 1 FROM us_case_usage WHERE case_id=? AND usage_id=?", (case_id, usage_id))
    if not link:
        raise SettlementError("usage_not_in_case", case_id=case_id, usage_id=usage_id)
    old = one(db, "SELECT * FROM us_attribution_proofs WHERE proof_id=?", (proof_id,))
    if old:
        return {"decision": "reused", "proof_id": proof_id}
    candidate = one(db, "SELECT 1 FROM us_signoffs WHERE usage_id=? AND customer=?", (usage_id, customer))
    if not candidate and not x.get("allow_non_candidate", False):
        raise SettlementError("customer_is_not_claim_candidate", usage_id=usage_id, customer=customer)
    db.execute(
        """INSERT INTO us_attribution_proofs(proof_id,case_id,usage_id,customer,issuer,evidence,at,command_id)
           VALUES(?,?,?,?,?,?,?,?)""",
        (proof_id, case_id, usage_id, customer, _text(x, "issuer", default="platform"),
         _text(x, "evidence", default=""), at, cid),
    )
    db.execute(
        "UPDATE us_usage SET attribution_status='attributed', attributed_customer=? WHERE usage_id=?",
        (customer, usage_id),
    )
    return {"decision": "attributed", "proof_id": proof_id, "usage_id": usage_id, "customer": customer}


def _attributed_customer(db, usage_id: str) -> Optional[str]:
    proof = one(db, """SELECT customer FROM us_attribution_proofs WHERE usage_id=?
                       ORDER BY rowid DESC LIMIT 1""", (usage_id,))
    if proof:
        return proof["customer"]
    u = one(db, "SELECT attributed_customer FROM us_usage WHERE usage_id=?", (usage_id,))
    return u["attributed_customer"] if u else None


def _finalize_attribution(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    case_id = _text(x, "case_id", required=True)
    fid = _text(x, "finalization_id", "id") or f"FINAL-{case_id}"
    old = one(db, "SELECT * FROM us_case_finalizations WHERE finalization_id=?", (fid,))
    if old:
        return json.loads(old["result_json"])
    old_case_final = one(db, "SELECT * FROM us_case_finalizations WHERE case_id=?", (case_id,))
    if old_case_final:
        return json.loads(old_case_final["result_json"])
    case = _require_case(db, case_id)
    settlement = one(db, "SELECT * FROM us_settlements WHERE case_id=?", (case_id,))
    if not settlement:
        raise SettlementError("settlement_required_before_finalization", case_id=case_id)
    at = _int(x, "at", default=0)
    disputed_ids = json.loads(case["disputed_usage_ids"] or "[]")
    attribution = {uid: _attributed_customer(db, uid) for uid in disputed_ids}
    if any(not customer for customer in attribution.values()):
        raise SettlementError("attribution_required", case_id=case_id)

    results: Dict[str, Any]
    if settlement["mode"] == "reserve":
        invoices = []
        for idx, usage_id in enumerate(disputed_ids, 1):
            customer = attribution[usage_id]
            u = one(db, "SELECT * FROM us_usage WHERE usage_id=?", (usage_id,))
            amount = u["physical_units"] * u["customer_unit_price"]
            recovery = one(db, "SELECT recovery_id FROM us_recovery_items WHERE case_id=? AND usage_id=?",
                           (case_id, usage_id))
            invoice_id = f"SUP-{fid}-{idx}"
            inv = _create_invoice(db, cid, at, invoice_id, customer, [{
                "usage_id": usage_id, "description": "late attribution supplemental charge",
                "units": u["physical_units"], "unit_price": u["customer_unit_price"],
                "amount": amount, "responsible_party": customer, "period": u["period"],
            }], case_id=case_id, doc_type="supplement", recovery_id=recovery["recovery_id"] if recovery else None)
            invoices.append(inv)
        results = {"decision": "supplemental_charges_issued", "finalization_id": fid,
                   "case_id": case_id, "invoice_ids": [i["invoice_id"] for i in invoices],
                   "amount": sum(i["amount"] for i in invoices)}
    else:
        grouped: Dict[str, List[dict]] = {}
        for items in _group_undisputed_lines(db, case_id).values():
            for item in items:
                grouped.setdefault(item["customer"], []).append(item)
        for usage_id in disputed_ids:
            customer = attribution[usage_id]
            u = one(db, "SELECT * FROM us_usage WHERE usage_id=?", (usage_id,))
            grouped.setdefault(customer, []).append({
                "usage_id": usage_id, "period": u["period"],
                "billable_units": u["physical_units"], "signoff_price": u["customer_unit_price"],
                "line_amount": u["physical_units"] * u["customer_unit_price"],
            })
        invoices = []
        for customer, items in sorted(grouped.items()):
            lines = [{
                "usage_id": i["usage_id"], "description": "actual settled usage",
                "units": i["billable_units"], "unit_price": i["signoff_price"],
                "amount": i["line_amount"], "responsible_party": customer, "period": i.get("period"),
            } for i in items]
            notices = rows(db, "SELECT notice_id FROM us_usage_notices WHERE case_id=? AND customer=?",
                           (case_id, customer))
            inv = _create_invoice(db, cid, at, f"INV-{fid}-{_safe(customer)}", customer, lines,
                                  case_id=case_id, doc_type="invoice", period=case["period"],
                                  source_notice_id=notices[0]["notice_id"] if notices else None)
            invoices.append(inv)
        results = {"decision": "actual_bills_issued", "finalization_id": fid, "case_id": case_id,
                   "invoice_ids": [i["invoice_id"] for i in invoices],
                   "invoiced_total": sum(i["amount"] for i in invoices)}

    db.execute(
        "INSERT INTO us_case_finalizations(finalization_id,case_id,mode,at,command_id,result_json) VALUES(?,?,?,?,?,?)",
        (fid, case_id, settlement["mode"], at, cid, _canonical(results)),
    )
    return results


def _invoice_allocated(db, invoice_id: str) -> int:
    row = one(db, "SELECT COALESCE(SUM(amount),0) AS n FROM us_allocations WHERE target_type='invoice' AND target_id=?",
              (invoice_id,))
    return int(row["n"])


def _bill_allocated(db, bill_id: str) -> int:
    row = one(db, "SELECT COALESCE(SUM(amount),0) AS n FROM us_allocations WHERE target_type='vendor_bill' AND target_id=?",
              (bill_id,))
    return int(row["n"])


def _pay_customer(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    payment_id = _text(x, "payment_id", "id", required=True)
    old_payment = one(db, "SELECT * FROM us_payments WHERE payment_id=?", (payment_id,))
    if old_payment:
        return {"decision": "payment_already_recorded", "payment_id": payment_id, "amount": old_payment["amount"]}
    customer = _text(x, "customer", "payer", required=True)
    amount = _int(x, "amount", required=True) if _g(x, "amount") is not None else None
    if amount is None or amount <= 0:
        raise SettlementError("positive_amount_required")
    at = _int(x, "at", default=0)
    invoice_id = _text(x, "invoice_id")
    case_id = _text(x, "case_id") or None

    if not invoice_id:
        _create_payment_row(db, cid, at, payment_id, "customer_in", customer, "platform", amount,
                            case_id=case_id, source="customer", purpose="on_account")
        _post(db, cid, at, [
            ("cash.platform", amount, 0),
            ("customer.credit", 0, amount),
        ], party=customer, case_id=case_id, ref_type="payment", ref_id=payment_id)
        return {"decision": "payment_received_on_account", "payment_id": payment_id,
                "unapplied_amount": amount}

    inv = one(db, "SELECT * FROM us_invoices WHERE invoice_id=?", (invoice_id,))
    if not inv:
        raise SettlementError("unknown_invoice", invoice_id=invoice_id)
    if inv["customer"] != customer:
        raise SettlementError("invoice_customer_mismatch", invoice_id=invoice_id, customer=customer)
    receivable = inv["total_amount"] if inv["direction"] == "debit" else -inv["total_amount"]
    allocated = _invoice_allocated(db, invoice_id)
    open_amount = receivable - allocated
    _create_payment_row(db, cid, at, payment_id, "customer_in", customer, "platform", amount,
                        case_id=inv["case_id"], source="customer", purpose="invoice_payment")
    if open_amount <= 0:
        _post(db, cid, at, [
            ("cash.platform", amount, 0),
            ("customer.credit", 0, amount),
        ], party=customer, case_id=inv["case_id"], ref_type="duplicate_payment", ref_id=payment_id)
        return {"decision": "duplicate_payment_held_as_credit", "payment_id": payment_id,
                "invoice_id": invoice_id, "applied_amount": 0, "unapplied_amount": amount}

    applied = min(amount, open_amount)
    _allocate(db, cid, at, f"AL-{payment_id}", payment_id, "invoice", invoice_id, applied)
    remainder = amount - applied
    if remainder and not x.get("allow_credit", True):
        raise SettlementError("overpayment", invoice_id=invoice_id, open_amount=open_amount)
    entries = [("cash.platform", amount, 0), ("ar.customer", 0, applied)]
    if remainder:
        entries.append(("customer.credit", 0, remainder))
    _post(db, cid, at, entries, party=customer, case_id=inv["case_id"],
          ref_type="payment", ref_id=payment_id)
    status = "paid" if applied >= open_amount else "partial"
    return {"decision": "payment_applied", "payment_id": payment_id, "invoice_id": invoice_id,
            "status": status, "applied_amount": applied, "unapplied_amount": remainder,
            "invoice_balance": max(0, open_amount - applied)}


def _pay_vendor(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    payment_id = _text(x, "payment_id", "id", required=True)
    old_payment = one(db, "SELECT * FROM us_payments WHERE payment_id=?", (payment_id,))
    if old_payment:
        return {"decision": "payment_already_recorded", "payment_id": payment_id}
    bill_id = _text(x, "bill_id", "vendor_bill_id", required=True)
    bill = one(db, "SELECT * FROM us_vendor_bills WHERE bill_id=?", (bill_id,))
    if not bill:
        raise SettlementError("unknown_vendor_bill", bill_id=bill_id)
    amount = _int(x, "amount", required=True) if _g(x, "amount") is not None else None
    if amount is None or amount <= 0:
        raise SettlementError("positive_amount_required")
    at = _int(x, "at", default=0)
    payer = _text(x, "payer", "funding_source", default="platform")
    allocated = _bill_allocated(db, bill_id)
    open_amount = bill["amount_due"] - allocated
    _create_payment_row(db, cid, at, payment_id, "vendor_out", payer, bill["vendor_id"], amount,
                        case_id=bill["case_id"], source=payer, purpose="vendor_bill_payment")
    if open_amount <= 0:
        _post(db, cid, at, [
            ("vendor.credit", amount, 0),
            ("cash.platform", 0, amount),
        ], case_id=bill["case_id"], ref_type="duplicate_vendor_payment", ref_id=payment_id)
        return {"decision": "duplicate_vendor_payment_held", "payment_id": payment_id,
                "unapplied_amount": amount}
    applied = min(amount, open_amount)
    _allocate(db, cid, at, f"AL-{payment_id}", payment_id, "vendor_bill", bill_id, applied)
    remainder = amount - applied
    entries = [("vendor.payable", applied, 0), ("cash.platform", 0, applied)]
    if remainder:
        entries = [("vendor.payable", open_amount, 0), ("vendor.credit", remainder, 0),
                   ("cash.platform", 0, amount)]
    _post(db, cid, at, entries, case_id=bill["case_id"], ref_type="vendor_payment", ref_id=payment_id)
    return {"decision": "vendor_payment_applied", "payment_id": payment_id, "bill_id": bill_id,
            "applied_amount": applied, "unapplied_amount": remainder,
            "bill_balance": max(0, open_amount - applied)}


def _reverse_payment(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    reversal_id = _text(x, "reversal_id", "id", required=True)
    payment_id = _text(x, "payment_id", required=True)
    payment = one(db, "SELECT * FROM us_payments WHERE payment_id=?", (payment_id,))
    if not payment:
        raise SettlementError("unknown_payment", payment_id=payment_id)
    old_rev = one(db, "SELECT * FROM us_payment_reversals WHERE reversal_id=?", (reversal_id,))
    if old_rev:
        return {"decision": "reused", "reversal_id": reversal_id}
    reversed_total = one(db, "SELECT COALESCE(SUM(amount),0) AS n FROM us_payment_reversals WHERE payment_id=?",
                         (payment_id,))["n"]
    remaining = payment["amount"] - reversed_total
    amount = _int(x, "amount", default=remaining)
    if amount <= 0 or amount > remaining:
        raise SettlementError("invalid_reversal_amount", remaining_reversible=remaining)
    at = _int(x, "at", default=0)
    reason = _text(x, "reason", default="")
    db.execute(
        "INSERT INTO us_payment_reversals(reversal_id,payment_id,amount,reason,at,command_id) VALUES(?,?,?,?,?,?)",
        (reversal_id, payment_id, amount, reason, at, cid),
    )

    remaining_to_reverse = amount
    reversed_allocations = 0
    target_invoice = _text(x, "invoice_id")
    if target_invoice:
        alloc = one(db, """SELECT * FROM us_allocations
                           WHERE payment_id=? AND target_type='invoice' AND target_id=? AND amount>0
                           ORDER BY rowid LIMIT 1""", (payment_id, target_invoice))
        if alloc:
            net = one(db, "SELECT COALESCE(SUM(amount),0) AS n FROM us_allocations WHERE original_allocation_id=? OR allocation_id=?",
                      (alloc["allocation_id"], alloc["allocation_id"]))["n"]
            reversible = max(0, net)
            take = min(remaining_to_reverse, reversible)
            if take:
                _allocate(db, cid, at, f"AL-{reversal_id}", payment_id, "invoice", target_invoice,
                          -take, reversal_id=reversal_id, original_allocation_id=alloc["allocation_id"])
                remaining_to_reverse -= take
                reversed_allocations += take
    else:
        allocs = rows(db, """SELECT a.*, COALESCE((
                SELECT SUM(z.amount) FROM us_allocations z
                WHERE z.original_allocation_id=a.allocation_id),0) AS reversed_amount
            FROM us_allocations a
            WHERE a.payment_id=? AND a.amount>0 ORDER BY a.rowid""", (payment_id,))
        for alloc in allocs:
            net = alloc["amount"] - alloc["reversed_amount"]
            if net <= 0:
                continue
            take = min(remaining_to_reverse, net)
            if take <= 0:
                break
            _allocate(db, cid, at, f"AL-{reversal_id}-{alloc['allocation_id']}", payment_id,
                      alloc["target_type"], alloc["target_id"], -take,
                      reversal_id=reversal_id, original_allocation_id=alloc["allocation_id"])
            remaining_to_reverse -= take
            reversed_allocations += take

    if payment["direction"] == "customer_in":
        entries = [("ar.customer", reversed_allocations, 0)]
        unapplied_reversal = amount - reversed_allocations
        if unapplied_reversal:
            entries.append(("customer.credit", unapplied_reversal, 0))
        entries.append(("cash.platform", 0, amount))
        _post(db, cid, at, entries, party=payment["payer"], case_id=payment["case_id"],
              ref_type="payment_reversal", ref_id=reversal_id)
    else:
        entries = [("cash.platform", amount, 0)]
        if reversed_allocations:
            entries.append(("vendor.payable", 0, reversed_allocations))
        unapplied_reversal = amount - reversed_allocations
        if unapplied_reversal:
            entries.append(("vendor.credit", 0, unapplied_reversal))
        _post(db, cid, at, entries, case_id=payment["case_id"],
              ref_type="payment_reversal", ref_id=reversal_id)

    return {"decision": "payment_reversed", "reversal_id": reversal_id,
            "payment_id": payment_id, "amount": amount,
            "allocation_reopened": reversed_allocations}


def _refund_guarantee(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    refund_id = _text(x, "refund_id", "id", required=True)
    case_id = _text(x, "case_id")
    guarantee_id = _text(x, "guarantee_id")
    if guarantee_id:
        g = one(db, "SELECT * FROM us_guarantees WHERE guarantee_id=?", (guarantee_id,))
    elif case_id:
        g = one(db, "SELECT * FROM us_guarantees WHERE case_id=?", (case_id,))
    else:
        raise SettlementError("guarantee_id_or_case_id_required")
    if not g:
        raise SettlementError("unknown_guarantee")
    old = one(db, "SELECT * FROM us_guarantee_refunds WHERE refund_id=?", (refund_id,))
    if old:
        return {"decision": "reused", "refund_id": refund_id}
    refunded = one(db, "SELECT COALESCE(SUM(amount),0) AS n FROM us_guarantee_refunds WHERE guarantee_id=?",
                   (g["guarantee_id"],))["n"]
    outstanding = g["amount"] - refunded
    amount = _int(x, "amount", default=outstanding)
    if amount <= 0 or amount > outstanding:
        raise SettlementError("guarantee_refund_exceeded", outstanding=outstanding)
    at = _int(x, "at", default=0)
    payment_id = _text(x, "payment_id") or f"PR-{refund_id}"
    _create_payment_row(db, cid, at, payment_id, "guarantee_refund", "platform", g["guarantor"],
                        amount, case_id=g["case_id"], source="platform", purpose="guarantee_refund")
    db.execute(
        "INSERT INTO us_guarantee_refunds(refund_id,guarantee_id,payment_id,amount,at,command_id) VALUES(?,?,?,?,?,?)",
        (refund_id, g["guarantee_id"], payment_id, amount, at, cid),
    )
    _post(db, cid, at, [
        ("guarantee.refundable", amount, 0),
        ("cash.platform", 0, amount),
    ], party=g["guarantor"], case_id=g["case_id"], ref_type="guarantee_refund", ref_id=refund_id)
    return {"decision": "guarantee_refunded", "refund_id": refund_id,
            "guarantee_id": g["guarantee_id"], "amount": amount,
            "remaining_guarantee_obligation": outstanding - amount}


def _issue_credit_note(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    note_id = _text(x, "credit_note_id", "id", "invoice_id", required=True)
    original_id = _text(x, "original_invoice_id", "original_id", required=True)
    original = one(db, "SELECT * FROM us_invoices WHERE invoice_id=?", (original_id,))
    if not original:
        raise SettlementError("unknown_invoice", invoice_id=original_id)
    amount = _int(x, "amount", required=True) if _g(x, "amount") is not None else original["total_amount"]
    already = one(db, """SELECT COALESCE(SUM(total_amount),0) AS n FROM us_invoices
                         WHERE original_invoice_id=? AND direction='credit'""", (original_id,))["n"]
    if amount <= 0 or already + amount > original["total_amount"]:
        raise SettlementError("credit_note_exceeds_invoice", already_credited=already,
                              invoice_amount=original["total_amount"])
    at = _int(x, "at", default=0)
    inv = _create_invoice(db, cid, at, note_id, original["customer"], [{
        "usage_id": _text(x, "usage_id") or None,
        "description": _text(x, "reason", default="append-only correction / reversal"),
        "units": _int(x, "units", default=0), "unit_price": _int(x, "unit_price", default=0),
        "amount": amount, "responsible_party": original["customer"],
    }], case_id=original["case_id"], doc_type="credit_note", direction="credit",
        original_invoice_id=original_id)
    return {"decision": "credit_note_issued", **inv}


def _issue_charge(db, x: Dict[str, Any], cid: str) -> Dict[str, Any]:
    invoice_id = _text(x, "invoice_id", "id", required=True)
    customer = _text(x, "customer", required=True)
    amount = _int(x, "amount") if _g(x, "amount") is not None else None
    units = _int(x, "units", "billable_units", default=0)
    unit_price = _int(x, "unit_price", default=1)
    if amount is None:
        amount = units * unit_price
    at = _int(x, "at", default=0)
    inv = _create_invoice(db, cid, at, invoice_id, customer, [{
        "usage_id": _text(x, "usage_id") or None,
        "description": _text(x, "description", default="supplemental charge"),
        "units": units, "unit_price": unit_price, "amount": amount,
        "responsible_party": customer,
    }], case_id=_text(x, "case_id") or None, doc_type=_text(x, "doc_type", default="supplement"),
        direction="debit", original_invoice_id=_text(x, "original_invoice_id") or None,
        recovery_id=_text(x, "recovery_id") or None)
    return {"decision": "supplemental_charge_issued", **inv}


def account_balances(db) -> Dict[str, int]:
    records = rows(db, "SELECT account, COALESCE(SUM(debit-credit),0) AS balance FROM us_ledger GROUP BY account")
    return {r["account"]: int(r["balance"]) for r in records}


def report(db, case_id: Optional[str] = None) -> Dict[str, Any]:
    install(db)
    if case_id:
        case = one(db, "SELECT * FROM us_cases WHERE case_id=?", (case_id,))
        if not case:
            return {"decision": "unknown_case", "case_id": case_id}
        result: Dict[str, Any] = {"case": dict(case)}
        settlement = one(db, "SELECT * FROM us_settlements WHERE case_id=?", (case_id,))
        result["settlement"] = dict(settlement) if settlement else None

        bill = one(db, "SELECT * FROM us_vendor_bills WHERE case_id=?", (case_id,))
        if bill:
            allocated = _bill_allocated(db, bill["bill_id"])
            result["vendor"] = {
                "bill_id": bill["bill_id"], "amount_due": bill["amount_due"],
                "paid": allocated, "outstanding": bill["amount_due"] - allocated,
            }
        else:
            result["vendor"] = None

        customers: Dict[str, Any] = {}
        for inv in rows(db, "SELECT * FROM us_invoices WHERE case_id=? ORDER BY customer,invoice_id", (case_id,)):
            c = customers.setdefault(inv["customer"], {
                "invoiced": 0, "paid": 0, "balance": 0, "invoices": [], "notices": []})
            signed_amount = inv["total_amount"] * (1 if inv["direction"] == "debit" else -1)
            allocated = _invoice_allocated(db, inv["invoice_id"])
            c["invoiced"] += signed_amount
            c["paid"] += allocated
            c["invoices"].append({"invoice_id": inv["invoice_id"], "type": inv["doc_type"],
                                  "direction": inv["direction"], "amount": inv["total_amount"],
                                  "net_amount": signed_amount, "paid": allocated,
                                  "balance": signed_amount - allocated})
        for n in rows(db, "SELECT * FROM us_usage_notices WHERE case_id=? ORDER BY customer", (case_id,)):
            c = customers.setdefault(n["customer"], {
                "invoiced": 0, "paid": 0, "balance": 0, "invoices": [], "notices": []})
            c["notices"].append(dict(n))
        for c in customers.values():
            c["balance"] = c["invoiced"] - c["paid"]
        result["customers"] = customers

        recoveries = []
        for r in rows(db, "SELECT * FROM us_recovery_items WHERE case_id=? ORDER BY recovery_id", (case_id,)):
            item = dict(r)
            inv = one(db, "SELECT invoice_id FROM us_invoices WHERE recovery_id=? ORDER BY rowid LIMIT 1",
                       (r["recovery_id"],))
            item["invoice_id"] = inv["invoice_id"] if inv else None
            item["invoiced"] = bool(inv)
            item["collected"] = bool(inv and _invoice_allocated(db, inv["invoice_id"]) >=
                                     one(db, "SELECT total_amount FROM us_invoices WHERE invoice_id=?",
                                         (inv["invoice_id"],))["total_amount"])
            recoveries.append(item)
        result["recovery_items"] = recoveries

        g = one(db, "SELECT * FROM us_guarantees WHERE case_id=?", (case_id,))
        if g:
            refunded = one(db, "SELECT COALESCE(SUM(amount),0) AS n FROM us_guarantee_refunds WHERE guarantee_id=?",
                           (g["guarantee_id"],))["n"]
            result["guarantee"] = {"guarantee_id": g["guarantee_id"], "guarantor": g["guarantor"],
                                   "paid": g["amount"], "refunded": int(refunded),
                                   "outstanding": g["amount"] - int(refunded)}
        else:
            result["guarantee"] = None

        result["balances"] = account_balances(db)
        result["verification"] = verify(db, case_id)
        return result

    cases = rows(db, "SELECT case_id FROM us_cases ORDER BY case_id")
    return {"cases": [c["case_id"] for c in cases], "balances": account_balances(db),
            "verification": verify(db)}


def verify(db, case_id: Optional[str] = None) -> Dict[str, Any]:
    errors: List[str] = []
    where = ""
    args: tuple = ()
    if case_id:
        where = " WHERE case_id=?"
        args = (case_id,)
    ledger = one(db, f"SELECT COALESCE(SUM(debit),0) d, COALESCE(SUM(credit),0) c FROM us_ledger{where}", args)
    if int(ledger["d"]) != int(ledger["c"]):
        errors.append("ledger_not_balanced")

    for bill in rows(db, f"SELECT * FROM us_vendor_bills{where}", args):
        paid = _bill_allocated(db, bill["bill_id"])
        if paid > bill["amount_due"]:
            errors.append(f"vendor_bill_over_allocated:{bill['bill_id']}")

    invoice_q = "SELECT * FROM us_invoices" + (" WHERE case_id=?" if case_id else "")
    invoice_args = (case_id,) if case_id else ()
    for inv in rows(db, invoice_q, invoice_args):
        if inv["direction"] != "debit":
            continue
        paid = _invoice_allocated(db, inv["invoice_id"])
        # Overpayment is allowed only as explicitly unallocated cash; allocations never exceed invoice.
        if paid > inv["total_amount"]:
            errors.append(f"invoice_over_allocated:{inv['invoice_id']}")

    if case_id:
        case = _require_case(db, case_id)
        if case["physical_units"] < case["undisputed_units"] + case["disputed_units"] + case["unclaimed_units"]:
            errors.append("compatible_usage_exceeds_physical")
    return {"ok": not errors, "errors": errors}


def replay(source: Any) -> Dict[str, Any]:
    close_source = False
    if isinstance(source, (str, Path)):
        db = connect(str(source))
        close_source = True
    else:
        db = source
    target = connect(":memory:")
    errors: List[Dict[str, Any]] = []
    try:
        commands = rows(db, "SELECT payload,result FROM us_command_journal ORDER BY seq")
        for index, command in enumerate(commands, 1):
            event = json.loads(command["payload"])
            actual = run(target, event)
            expected = json.loads(command["result"])
            if actual != expected:
                errors.append({"step": index, "expected": expected, "actual": actual})

        tables = [r[0] for r in target.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'us_%' ORDER BY name")]
        for table in tables:
            source_rows = [tuple(r) for r in db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            target_rows = [tuple(r) for r in target.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            if source_rows != target_rows:
                errors.append({"table": table, "source_rows": len(source_rows),
                               "target_rows": len(target_rows)})
        return {"verified": not errors, "commands": len(commands), "errors": errors}
    finally:
        target.close()
        if close_source:
            db.close()
