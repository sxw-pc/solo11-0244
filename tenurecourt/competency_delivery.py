"""Competent delivery ledger for high-voltage maintenance.

The old qualification note intentionally turns two attendance rows into a
"provisional" positive.  Attendance only proves that a person was present at a
lecture; it is not evidence that they can safely perform energized isolation,
interlock verification or restoration.

This module keeps the safety chain explicit and recoverable:

attendance -> original assessment evidence/report -> scoped qualification ->
authorized work order -> actual operations -> maintenance report -> customer
acceptance -> independent fees.

The module uses SQLite only through the DB-API connection supplied by the
caller.  It is standard-library only and does not delete or rewrite historical
training, evidence, operation or acceptance records.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import time
import uuid
from typing import Any, Optional

from .qualification_note import provisional_qualified
from .training_attendance import attend as legacy_record_attendance

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

CORE_ITEMS = (
    "live_isolation",
    "interlock_verification",
    "restoration_drill",
)

ITEM_LABELS_ZH = {
    "live_isolation": "带电隔离实操",
    "interlock_verification": "联锁验证实操",
    "restoration_drill": "恢复演练实操",
}

DEFAULT_POSITION = "hv_switching_technician"
DEFAULT_LEVEL = "independent_hv_switching"

SCHEMA = """
CREATE TABLE IF NOT EXISTS training_events(
    event TEXT PRIMARY KEY, person TEXT, course TEXT, session TEXT
);

CREATE TABLE IF NOT EXISTS cd_assessments(
    id TEXT PRIMARY KEY,
    person TEXT NOT NULL,
    position TEXT NOT NULL,
    equipment_model TEXT NOT NULL,
    operation_level TEXT NOT NULL,
    required_items TEXT NOT NULL,
    proctor TEXT NOT NULL,
    issuer TEXT NOT NULL,
    valid_from INTEGER,
    valid_until INTEGER,
    status TEXT NOT NULL,
    idempotency_key TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS cd_item_results(
    assessment_id TEXT NOT NULL,
    item TEXT NOT NULL,
    passed INTEGER NOT NULL,
    evidence_id TEXT,
    recorded_by TEXT,
    note TEXT,
    at INTEGER,
    PRIMARY KEY(assessment_id, item)
);

CREATE TABLE IF NOT EXISTS cd_evidence(
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    uri TEXT,
    sha256 TEXT,
    device_receipt TEXT,
    central_receipt TEXT,
    status TEXT NOT NULL,
    payload TEXT,
    saved_at INTEGER,
    received_at INTEGER
);

CREATE TABLE IF NOT EXISTS cd_reports(
    id TEXT PRIMARY KEY,
    assessment_id TEXT NOT NULL UNIQUE,
    result TEXT NOT NULL,
    status TEXT NOT NULL,
    proctor TEXT NOT NULL,
    signature TEXT NOT NULL,
    signed_at INTEGER,
    sync_status TEXT NOT NULL DEFAULT 'signed_locally',
    central_receipt TEXT,
    report_hash TEXT NOT NULL,
    payload TEXT NOT NULL,
    revoked_by TEXT,
    revoked_at INTEGER,
    revocation_reason TEXT
);

CREATE TABLE IF NOT EXISTS cd_qualifications(
    id TEXT PRIMARY KEY,
    person TEXT NOT NULL,
    position TEXT NOT NULL,
    equipment_model TEXT NOT NULL,
    operation_level TEXT NOT NULL,
    allowed_items TEXT NOT NULL,
    scope_type TEXT NOT NULL DEFAULT 'full_assessment',
    assessment_id TEXT,
    report_id TEXT,
    source_qualification_id TEXT,
    recognition_reference TEXT,
    proctor TEXT,
    issuer TEXT NOT NULL,
    valid_from INTEGER NOT NULL,
    valid_until INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at INTEGER,
    revoked_by TEXT,
    revoked_at INTEGER,
    revocation_reason TEXT
);

CREATE TABLE IF NOT EXISTS cd_work_orders(
    id TEXT PRIMARY KEY,
    customer TEXT,
    namespace TEXT,
    lease_id TEXT,
    team TEXT,
    generation INTEGER,
    position TEXT NOT NULL,
    equipment_model TEXT NOT NULL,
    operation_level TEXT NOT NULL,
    required_items TEXT NOT NULL,
    scheduled_at INTEGER,
    require_lease INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    delivery_mode TEXT,
    operator_id TEXT,
    qualification_id TEXT,
    senior_id TEXT,
    senior_qualification_id TEXT,
    apprentice_id TEXT,
    mentorship_id TEXT,
    created_at INTEGER,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS cd_operations(
    id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    item TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    qualification_id TEXT,
    supervised_by TEXT,
    evidence_id TEXT,
    note TEXT,
    at INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded',
    UNIQUE(work_order_id, seq)
);

CREATE TABLE IF NOT EXISTS cd_mentorships(
    id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL UNIQUE,
    mentor_id TEXT NOT NULL,
    mentor_qualification_id TEXT NOT NULL,
    apprentice_id TEXT NOT NULL,
    allowed_practice_items TEXT NOT NULL,
    status TEXT NOT NULL,
    present INTEGER NOT NULL DEFAULT 1,
    started_at INTEGER,
    ended_at INTEGER,
    mentor_signature TEXT,
    completion_note TEXT
);

CREATE TABLE IF NOT EXISTS cd_mentorship_evidence(
    id TEXT PRIMARY KEY,
    mentorship_id TEXT NOT NULL,
    work_order_id TEXT NOT NULL,
    apprentice_id TEXT NOT NULL,
    item TEXT NOT NULL,
    method TEXT NOT NULL,
    note TEXT,
    evidence_id TEXT,
    signed_by TEXT NOT NULL,
    signature TEXT NOT NULL,
    at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cd_supervision_events(
    id TEXT PRIMARY KEY,
    mentorship_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    actor_id TEXT,
    reason TEXT,
    at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cd_reports_maintenance(
    id TEXT PRIMARY KEY,
    work_order_id TEXT NOT NULL UNIQUE,
    actual_operator_id TEXT NOT NULL,
    delivery_mode TEXT NOT NULL,
    qualification_id TEXT,
    mentorship_id TEXT,
    issued_by TEXT,
    issued_at INTEGER NOT NULL,
    summary TEXT,
    payload TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'issued',
    review_status TEXT NOT NULL DEFAULT 'none'
);

CREATE TABLE IF NOT EXISTS cd_acceptances(
    id TEXT PRIMARY KEY,
    report_id TEXT NOT NULL UNIQUE,
    work_order_id TEXT NOT NULL,
    customer TEXT,
    accepted_by TEXT,
    receipt TEXT,
    at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cd_fees(
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    direction TEXT NOT NULL,
    payer TEXT,
    payee TEXT,
    amount REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'CNY',
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    work_order_id TEXT,
    assessment_id TEXT,
    acceptance_id TEXT,
    basis TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at INTEGER,
    due_at INTEGER,
    paid_at INTEGER,
    payment_reference TEXT,
    refunded_amount REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS cd_fee_refunds(
    id TEXT PRIMARY KEY,
    fee_id TEXT NOT NULL,
    amount REAL NOT NULL,
    reason TEXT,
    requested_by TEXT,
    at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cd_reviews(
    id TEXT PRIMARY KEY,
    reason_code TEXT NOT NULL,
    assessment_id TEXT,
    qualification_id TEXT,
    work_order_id TEXT,
    report_id TEXT,
    acceptance_id TEXT,
    person TEXT,
    customer TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    reason TEXT,
    created_at INTEGER,
    reviewer TEXT,
    completed_at INTEGER,
    outcome TEXT,
    corrective_action TEXT
);

CREATE TABLE IF NOT EXISTS cd_notifications(
    id TEXT PRIMARY KEY,
    at INTEGER NOT NULL,
    audience TEXT NOT NULL,
    recipient TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject TEXT,
    body TEXT NOT NULL,
    refs TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'recorded'
);

CREATE TABLE IF NOT EXISTS cd_incidents(
    id TEXT PRIMARY KEY,
    at INTEGER NOT NULL,
    work_order_id TEXT,
    person TEXT,
    code TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cd_audit_events(
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    at INTEGER NOT NULL,
    actor TEXT,
    event_type TEXT NOT NULL,
    aggregate_type TEXT,
    aggregate_id TEXT,
    payload TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cd_qual_person_scope
    ON cd_qualifications(person, position, equipment_model, operation_level, status);
CREATE INDEX IF NOT EXISTS idx_cd_ops_work_order ON cd_operations(work_order_id, seq);
CREATE INDEX IF NOT EXISTS idx_cd_reviews_person ON cd_reviews(person, status);
"""

# ---------------------------------------------------------------------------
# Low level helpers
# ---------------------------------------------------------------------------


def ensure_schema(db: sqlite3.Connection) -> sqlite3.Connection:
    """Create the competency tables. Safe to call repeatedly."""
    if getattr(db, "row_factory", None) is None:
        db.row_factory = sqlite3.Row
    ready = _one(
        db,
        "SELECT 1 AS x FROM sqlite_master WHERE type='table' AND name='cd_assessments'",
    )
    if not ready:
        db.executescript(SCHEMA)
    # store.connect() uses isolation_level=None and store.tx() owns COMMIT.
    # For a stock sqlite3 connection, commit the DDL ourselves.
    if getattr(db, "isolation_level", None) is not None:
        try:
            db.commit()
        except sqlite3.Error:
            pass
    return db


def _now(at: Any = None) -> int:
    return int(time.time()) if at is None else at


def _uuid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _next_id(db: sqlite3.Connection, table: str, prefix: str) -> str:
    # Append-only tables make count+1 deterministic under event replay.
    n = _one(db, f"SELECT COALESCE(COUNT(*),0)+1 AS n FROM {table}")["n"]
    return f"{prefix}-{n}"


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: Any, default: Any = None) -> Any:
    if value is None or value == "":
        return default
    return json.loads(value)


def _one(db: sqlite3.Connection, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    row = db.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _all(db: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in db.execute(sql, params).fetchall()]


def _commit(db: sqlite3.Connection) -> None:
    # isolation_level=None is the store module's autocommit/externally-managed
    # transaction mode; stock connections ('', DEFERRED) need an explicit commit.
    if getattr(db, "isolation_level", None) is None:
        return
    try:
        db.commit()
    except sqlite3.Error:
        pass


def ok(decision: str, **details: Any) -> dict[str, Any]:
    return {"ok": True, "allowed": True, "decision": decision, **details}


def deny(decision: str, **details: Any) -> dict[str, Any]:
    return {"ok": False, "allowed": False, "decision": decision, **details}


def _audit(
    db: sqlite3.Connection,
    event_type: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: dict[str, Any],
    at: Any = None,
    actor: Optional[str] = None,
    event_id: Optional[str] = None,
) -> dict[str, Any]:
    at = _now(at)
    if event_id is None:
        next_seq = _one(db, "SELECT COALESCE(MAX(seq),0)+1 AS n FROM cd_audit_events")["n"]
        event_id = f"audit-{next_seq}"
    last = _one(db, "SELECT hash FROM cd_audit_events ORDER BY seq DESC LIMIT 1")
    prev_hash = last["hash"] if last else "0" * 64
    body = {
        "event_id": event_id,
        "at": at,
        "actor": actor,
        "event_type": event_type,
        "aggregate_type": aggregate_type,
        "aggregate_id": aggregate_id,
        "payload": payload,
    }
    encoded = _dumps(body)
    record_hash = hashlib.sha256((prev_hash + encoded).encode("utf-8")).hexdigest()
    db.execute(
        """INSERT INTO cd_audit_events
           (event_id,at,actor,event_type,aggregate_type,aggregate_id,payload,prev_hash,hash)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            event_id,
            at,
            actor,
            event_type,
            aggregate_type,
            aggregate_id,
            _dumps(payload),
            prev_hash,
            record_hash,
        ),
    )
    return {"event_id": event_id, "hash": record_hash, "prev_hash": prev_hash}


def verify_audit_chain(db: sqlite3.Connection) -> dict[str, Any]:
    ensure_schema(db)
    rows = _all(db, "SELECT * FROM cd_audit_events ORDER BY seq")
    prev_hash = "0" * 64
    for row in rows:
        body = {
            "event_id": row["event_id"],
            "at": row["at"],
            "actor": row["actor"],
            "event_type": row["event_type"],
            "aggregate_type": row["aggregate_type"],
            "aggregate_id": row["aggregate_id"],
            "payload": _loads(row["payload"], {}),
        }
        expected = hashlib.sha256((prev_hash + _dumps(body)).encode("utf-8")).hexdigest()
        if row["prev_hash"] != prev_hash or expected != row["hash"]:
            return {"verified": False, "broken_seq": row["seq"], "events": len(rows)}
        prev_hash = row["hash"]
    return {"verified": True, "events": len(rows)}


def _notify(
    db: sqlite3.Connection,
    recipient: str,
    kind: str,
    body: str,
    *,
    audience: str = "party",
    subject: Optional[str] = None,
    refs: Optional[dict[str, Any]] = None,
    at: Any = None,
    notification_id: Optional[str] = None,
) -> dict[str, Any]:
    notification_id = notification_id or _next_id(db, "cd_notifications", "note")
    at = _now(at)
    refs = refs or {}
    db.execute(
        """INSERT INTO cd_notifications
           (id,at,audience,recipient,kind,subject,body,refs,status)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            notification_id,
            at,
            audience,
            recipient,
            kind,
            subject,
            body,
            _dumps(refs),
            "recorded",
        ),
    )
    return _one(db, "SELECT * FROM cd_notifications WHERE id=?", (notification_id,))


def _incident(
    db: sqlite3.Connection,
    code: str,
    *,
    work_order_id: Optional[str] = None,
    person: Optional[str] = None,
    detail: Optional[dict[str, Any]] = None,
    at: Any = None,
    incident_id: Optional[str] = None,
) -> dict[str, Any]:
    incident_id = incident_id or _next_id(db, "cd_incidents", "inc")
    at = _now(at)
    db.execute(
        "INSERT INTO cd_incidents(id,at,work_order_id,person,code,detail) VALUES(?,?,?,?,?,?)",
        (incident_id, at, work_order_id, person, code, _dumps(detail or {})),
    )
    _audit(
        db,
        "safety_incident_recorded",
        "work_order",
        work_order_id or incident_id,
        {"code": code, "person": person, "detail": detail or {}},
        at=at,
        actor=person,
    )
    return {"id": incident_id, "code": code, "at": at}


def _hash_snapshot(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(_dumps(snapshot).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Attendance bridge and authoritative adjudication
# ---------------------------------------------------------------------------


def attend(db: sqlite3.Connection, event: str, person: str, course: str, session: str) -> dict[str, Any]:
    """Record a theory attendance event using the isolated legacy table."""
    ensure_schema(db)
    legacy_record_attendance(db, event, person, course, session)
    _audit(
        db,
        "training_attendance_recorded",
        "person",
        person,
        {"event": event, "course": course, "session": session},
        actor=person,
    )
    _commit(db)
    return ok("attendance_recorded", event=event, person=person, course=course, session=session)


def provisional_status(
    db: sqlite3.Connection, person: str, course: Optional[str] = None
) -> dict[str, Any]:
    """Return the legacy two-attendance provisional flag without using it."""
    ensure_schema(db)
    if course is not None:
        provisional = bool(provisional_qualified(db, person, course))
        count = _one(
            db,
            "SELECT COUNT(*) AS n FROM training_events WHERE person=? AND course=?",
            (person, course),
        )["n"]
        courses = [course]
    else:
        row = _one(
            db,
            """SELECT COUNT(*) AS n FROM training_events
               WHERE person=? AND course IN (
                   SELECT course FROM training_events WHERE person=?
                   GROUP BY course HAVING COUNT(*) >= 2
               )""",
            (person, person),
        )
        provisional = bool(
            _one(
                db,
                "SELECT 1 AS x FROM training_events WHERE person=? GROUP BY course HAVING COUNT(*)>=2 LIMIT 1",
                (person,),
            )
        )
        count = row["n"] if row else 0
        courses = [
            r["course"]
            for r in _all(
                db,
                "SELECT DISTINCT course FROM training_events WHERE person=?",
                (person,),
            )
        ]
    return {
        "provisional_qualified": provisional,
        "attendance_count": count,
        "courses": courses,
        "authoritative": False,
        "meaning": "Attendance is a training record only and never grants independent operational scope.",
    }


def active_qualification(
    db: sqlite3.Connection,
    person: str,
    *,
    position: Optional[str] = None,
    equipment_model: Optional[str] = None,
    operation_level: Optional[str] = None,
    at: Any = None,
    qualification_id: Optional[str] = None,
    item: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    ensure_schema(db)
    at = _now(at)
    sql = "SELECT * FROM cd_qualifications WHERE person=? AND status='active' AND valid_from<=? AND valid_until>=?"
    params: list[Any] = [person, at, at]
    if qualification_id:
        sql += " AND id=?"
        params.append(qualification_id)
    if position:
        sql += " AND position=?"
        params.append(position)
    if equipment_model:
        sql += " AND equipment_model=?"
        params.append(equipment_model)
    if operation_level:
        sql += " AND operation_level=?"
        params.append(operation_level)
    sql += " ORDER BY created_at DESC, id DESC"
    for row in _all(db, sql, tuple(params)):
        row["allowed_items"] = _loads(row["allowed_items"], [])
        if item is None or item in row["allowed_items"]:
            return row
    return None


def qualification_blockers(
    db: sqlite3.Connection,
    person: str,
    *,
    position: str = DEFAULT_POSITION,
    equipment_model: str,
    operation_level: str = DEFAULT_LEVEL,
    items: tuple[str, ...] | list[str] = CORE_ITEMS,
    at: Any = None,
    qualification_id: Optional[str] = None,
) -> list[str]:
    """Explain every reason the person cannot independently perform scope."""
    ensure_schema(db)
    at = _now(at)
    blockers: list[str] = []
    if qualification_id:
        q = _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (qualification_id,))
        if not q:
            return ["qualification_unknown"]
        if q["person"] != person:
            blockers.append("qualification_person_mismatch")
        if q["status"] != "active":
            blockers.append(f"qualification_{q['status']}")
        if not (q["valid_from"] <= at <= q["valid_until"]):
            blockers.append("qualification_outside_valid_period")
        if blockers:
            return blockers
        q_items = set(_loads(q["allowed_items"], []))
        missing = sorted(set(items) - q_items)
        if missing:
            blockers.append("missing_item_scope:" + ",".join(missing))
        return blockers

    scoped = _all(
        db,
        """SELECT * FROM cd_qualifications
           WHERE person=? AND position=? AND equipment_model=? AND operation_level=?
           ORDER BY created_at DESC""",
        (person, position, equipment_model, operation_level),
    )
    if not scoped:
        other_models = _all(
            db,
            "SELECT DISTINCT equipment_model,status FROM cd_qualifications WHERE person=? AND position=?",
            (person, position),
        )
        if other_models:
            blockers.append("no_active_qualification_for_exact_equipment_model_or_level")
        else:
            blockers.append("no_scoped_qualification")
        return blockers

    active = [q for q in scoped if q["status"] == "active" and q["valid_from"] <= at <= q["valid_until"]]
    if not active:
        statuses = {q["status"] for q in scoped}
        if "revoked" in statuses:
            blockers.append("qualification_revoked")
        elif any(not (q["valid_from"] <= at <= q["valid_until"]) for q in scoped):
            blockers.append("qualification_expired_or_not_yet_valid")
        else:
            blockers.append("qualification_not_active")
        return blockers

    # One qualification is the bounded endorsement presented for the job.
    # Do not stitch unrelated certificates together unless an explicit future
    # endorsement aggregate is introduced and stored on the work order.
    for q in active:
        q_items = set(_loads(q["allowed_items"], []))
        if set(items).issubset(q_items):
            return []
    granted: set[str] = set()
    for q in active:
        granted.update(_loads(q["allowed_items"], []))
    missing = sorted(set(items) - granted)
    if missing:
        blockers.append("missing_item_scope:" + ",".join(missing))
    else:
        blockers.append("no_single_qualification_covers_complete_operation_scope")
    return blockers


def adjudicate_independent_work(
    db: sqlite3.Connection,
    person: str,
    *,
    equipment_model: str,
    position: str = DEFAULT_POSITION,
    operation_level: str = DEFAULT_LEVEL,
    items: tuple[str, ...] | list[str] = CORE_ITEMS,
    at: Any = None,
    course: Optional[str] = None,
    qualification_id: Optional[str] = None,
) -> dict[str, Any]:
    """Authoritative safety decision. Attendance can never allow work."""
    ensure_schema(db)
    at = _now(at)
    attendance = provisional_status(db, person, course)
    blockers = qualification_blockers(
        db,
        person,
        position=position,
        equipment_model=equipment_model,
        operation_level=operation_level,
        items=items,
        at=at,
        qualification_id=qualification_id,
    )
    qualifications = _all(
        db,
        """SELECT id,position,equipment_model,operation_level,allowed_items,status,
                  valid_from,valid_until,assessment_id,issuer,proctor
           FROM cd_qualifications WHERE person=? ORDER BY created_at DESC""",
        (person,),
    )
    for q in qualifications:
        q["allowed_items"] = _loads(q["allowed_items"], [])
    if blockers:
        return deny(
            "independent_work_denied",
            person=person,
            position=position,
            equipment_model=equipment_model,
            operation_level=operation_level,
            required_items=list(items),
            blockers=blockers,
            provisional_qualified=attendance["provisional_qualified"],
            attendance=attendance,
            qualifications=qualifications,
            explanation_zh=(
                "两次出勤只产生临时提示；缺少绑定岗位、型号、级别、实操项目、监考人、"
                "签发机构和有效期的资格，不得独立操作。"
            ),
        )
    q = active_qualification(
        db,
        person,
        position=position,
        equipment_model=equipment_model,
        operation_level=operation_level,
        at=at,
        qualification_id=qualification_id,
    )
    return ok(
        "independent_authorized",
        person=person,
        qualification=q,
        provisional_qualified=attendance["provisional_qualified"],
        attendance=attendance,
        explanation_zh="独立作业决定只来自已核实且范围、时间均有效的实操资格。",
    )


def can_work_independently(db: sqlite3.Connection, person: str, **scope: Any) -> bool:
    return bool(adjudicate_independent_work(db, person, **scope).get("allowed"))


# ---------------------------------------------------------------------------
# Assessment originals, signatures and disconnected upload recovery
# ---------------------------------------------------------------------------


def open_assessment(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    person: str,
    proctor: str,
    issuer: str,
    valid_from: Any,
    valid_until: Any,
    equipment_model: str,
    position: str = DEFAULT_POSITION,
    operation_level: str = DEFAULT_LEVEL,
    items: Optional[list[str] | tuple[str, ...]] = None,
    at: Any = None,
    idempotency_key: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    items = tuple(items or CORE_ITEMS)
    old = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    if old:
        same = (
            old["person"] == person
            and old["position"] == position
            and old["equipment_model"] == equipment_model
            and old["operation_level"] == operation_level
            and _loads(old["required_items"], []) == list(items)
            and old["proctor"] == proctor
            and old["issuer"] == issuer
        )
        if not same:
            return deny("assessment_id_conflict", assessment_id=assessment_id)
        return ok("assessment_reused", assessment=_loads(_dumps(old)))

    if valid_until < valid_from:
        return deny("invalid_validity_period", assessment_id=assessment_id)
    db.execute(
        """INSERT INTO cd_assessments
           (id,person,position,equipment_model,operation_level,required_items,proctor,issuer,
            valid_from,valid_until,status,idempotency_key,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            assessment_id,
            person,
            position,
            equipment_model,
            operation_level,
            _dumps(list(items)),
            proctor,
            issuer,
            valid_from,
            valid_until,
            "in_progress",
            idempotency_key,
            at,
            at,
        ),
    )
    audit = _audit(
        db,
        "assessment_opened",
        "assessment",
        assessment_id,
        {
            "person": person,
            "position": position,
            "equipment_model": equipment_model,
            "operation_level": operation_level,
            "items": list(items),
            "proctor": proctor,
            "issuer": issuer,
            "valid_from": valid_from,
            "valid_until": valid_until,
        },
        at=at,
        actor=issuer,
    )
    _commit(db)
    return ok("assessment_opened", assessment_id=assessment_id, audit=audit)


def save_evidence(
    db: sqlite3.Connection,
    evidence_id: str,
    assessment_id: str,
    *,
    kind: str = "video",
    uri: Optional[str] = None,
    sha256: Optional[str] = None,
    content: Any = None,
    device_receipt: Optional[str] = None,
    at: Any = None,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    assessment = _one(db, "SELECT id FROM cd_assessments WHERE id=?", (assessment_id,))
    if not assessment:
        return deny("unknown_assessment", assessment_id=assessment_id)
    if content is not None and sha256 is None:
        if isinstance(content, bytes):
            sha256 = hashlib.sha256(content).hexdigest()
        else:
            sha256 = hashlib.sha256(str(content).encode("utf-8")).hexdigest()
    if sha256 is None:
        # Callers may supply the device-reported hash.  A missing hash is
        # represented explicitly rather than silently fabricating integrity.
        sha256 = _hash_snapshot({"evidence_id": evidence_id, "uri": uri, "payload": payload or {}})
    old = _one(db, "SELECT * FROM cd_evidence WHERE id=?", (evidence_id,))
    if old:
        if old["sha256"] != sha256 or old["assessment_id"] != assessment_id:
            return deny("evidence_id_conflict", evidence_id=evidence_id)
        return ok("evidence_reused", evidence=_loads(_dumps(old)))
    db.execute(
        """INSERT INTO cd_evidence
           (id,assessment_id,kind,uri,sha256,device_receipt,central_receipt,status,payload,saved_at,received_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence_id,
            assessment_id,
            kind,
            uri,
            sha256,
            device_receipt or f"device-receipt-{evidence_id}",
            None,
            "device_saved",
            _dumps(payload or {}),
            at,
            None,
        ),
    )
    _audit(
        db,
        "assessment_evidence_saved_on_device",
        "assessment",
        assessment_id,
        {"evidence_id": evidence_id, "kind": kind, "sha256": sha256},
        at=at,
        actor="proctor-device",
    )
    _commit(db)
    return ok("evidence_saved", evidence_id=evidence_id, sha256=sha256, status="device_saved")


def record_item_result(
    db: sqlite3.Connection,
    assessment_id: str,
    item: str,
    passed: bool,
    *,
    at: Any = None,
    evidence_id: Optional[str] = None,
    recorded_by: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    assessment = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    if not assessment:
        return deny("unknown_assessment", assessment_id=assessment_id)
    items = _loads(assessment["required_items"], [])
    if item not in items:
        return deny("assessment_item_out_of_scope", assessment_id=assessment_id, item=item, scope=items)
    report = _one(db, "SELECT status FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if report and report["status"] in {"signed", "revoked"}:
        return deny("assessment_record_locked", assessment_id=assessment_id, report_status=report["status"])
    old = _one(
        db,
        "SELECT * FROM cd_item_results WHERE assessment_id=? AND item=?",
        (assessment_id, item),
    )
    if old:
        same = bool(old["passed"]) == bool(passed) and (evidence_id is None or old["evidence_id"] == evidence_id)
        if not same:
            return deny("item_result_conflict", assessment_id=assessment_id, item=item)
        return ok("item_result_reused", assessment_id=assessment_id, item=item, passed=bool(old["passed"]))
    if evidence_id and not _one(db, "SELECT id FROM cd_evidence WHERE id=? AND assessment_id=?", (evidence_id, assessment_id)):
        return deny("unknown_evidence", evidence_id=evidence_id, assessment_id=assessment_id)
    db.execute(
        "INSERT INTO cd_item_results(assessment_id,item,passed,evidence_id,recorded_by,note,at) VALUES(?,?,?,?,?,?,?)",
        (assessment_id, item, 1 if passed else 0, evidence_id, recorded_by or assessment["proctor"], note, at),
    )
    _audit(
        db,
        "assessment_item_recorded",
        "assessment",
        assessment_id,
        {"item": item, "passed": bool(passed), "evidence_id": evidence_id},
        at=at,
        actor=recorded_by or assessment["proctor"],
    )
    _commit(db)
    return ok("item_result_recorded", assessment_id=assessment_id, item=item, passed=bool(passed))


def sign_assessment(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    report_id: Optional[str] = None,
    proctor: Optional[str] = None,
    signature: Optional[str] = None,
    at: Any = None,
    signed_at: Any = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(signed_at if signed_at is not None else at)
    assessment = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    if not assessment:
        return deny("unknown_assessment", assessment_id=assessment_id)
    proctor = proctor or assessment["proctor"]
    if proctor != assessment["proctor"]:
        return deny("proctor_does_not_match_assessment", expected=assessment["proctor"], provided=proctor)
    existing = _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if existing:
        return ok("assessment_already_signed", report=_loads(_dumps(existing)))

    required = _loads(assessment["required_items"], [])
    results = _all(db, "SELECT * FROM cd_item_results WHERE assessment_id=?", (assessment_id,))
    by_item = {r["item"]: r for r in results}
    missing = [item for item in required if item not in by_item]
    if missing:
        return deny("assessment_incomplete", assessment_id=assessment_id, missing_items=missing)
    evidence = _all(db, "SELECT id,sha256,kind FROM cd_evidence WHERE assessment_id=?", (assessment_id,))
    if not evidence:
        return deny("original_evidence_required", assessment_id=assessment_id)

    failed = [item for item in required if not bool(by_item[item]["passed"])]
    result = "passed" if not failed else "failed"
    signature = signature or f"proctor-signature:{assessment_id}:{proctor}"
    report_id = report_id or f"report-{assessment_id}"
    snapshot = {
        "report_id": report_id,
        "assessment": assessment,
        "item_results": results,
        "evidence": evidence,
        "result": result,
        "proctor": proctor,
        "signed_at": at,
        "signature": signature,
    }
    report_hash = _hash_snapshot(snapshot)
    db.execute(
        """INSERT INTO cd_reports
           (id,assessment_id,result,status,proctor,signature,signed_at,sync_status,
            central_receipt,report_hash,payload)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            report_id,
            assessment_id,
            result,
            "signed",
            proctor,
            signature,
            at,
            "signed_locally",
            None,
            report_hash,
            _dumps(snapshot),
        ),
    )
    db.execute("UPDATE cd_assessments SET status=?, updated_at=? WHERE id=?", ("signed_" + result, at, assessment_id))
    _audit(
        db,
        "assessment_report_signed",
        "assessment",
        assessment_id,
        {"report_id": report_id, "result": result, "failed_items": failed, "report_hash": report_hash},
        at=at,
        actor=proctor,
    )
    _commit(db)
    return ok(
        "assessment_signed",
        assessment_id=assessment_id,
        report_id=report_id,
        result=result,
        passed=not failed,
        failed_items=failed,
        report_hash=report_hash,
        sync_status="signed_locally",
    )


def _issue_qualification_from_report(
    db: sqlite3.Connection,
    assessment: dict[str, Any],
    report: dict[str, Any],
    *,
    qualification_id: Optional[str] = None,
    at: Any = None,
) -> dict[str, Any]:
    existing = _one(db, "SELECT * FROM cd_qualifications WHERE assessment_id=?", (assessment["id"],))
    if existing:
        existing["allowed_items"] = _loads(existing["allowed_items"], [])
        return ok("qualification_already_issued", qualification=existing)
    if report["result"] != "passed":
        return deny("assessment_not_passed", assessment_id=assessment["id"], result=report["result"])
    at = _now(at)
    qualification_id = qualification_id or f"qual-{assessment['id']}"
    items = _loads(assessment["required_items"], [])
    db.execute(
        """INSERT INTO cd_qualifications
           (id,person,position,equipment_model,operation_level,allowed_items,scope_type,
            assessment_id,report_id,proctor,issuer,valid_from,valid_until,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            qualification_id,
            assessment["person"],
            assessment["position"],
            assessment["equipment_model"],
            assessment["operation_level"],
            _dumps(items),
            "full_assessment",
            assessment["id"],
            report["id"],
            report["proctor"],
            assessment["issuer"],
            assessment["valid_from"],
            assessment["valid_until"],
            "active",
            at,
        ),
    )
    q = _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (qualification_id,))
    q["allowed_items"] = _loads(q["allowed_items"], [])
    _audit(
        db,
        "qualification_issued_from_verified_assessment",
        "qualification",
        qualification_id,
        {
            "assessment_id": assessment["id"],
            "report_id": report["id"],
            "scope": {
                "position": assessment["position"],
                "equipment_model": assessment["equipment_model"],
                "operation_level": assessment["operation_level"],
                "items": items,
            },
            "issuer": assessment["issuer"],
            "proctor": report["proctor"],
            "valid_from": assessment["valid_from"],
            "valid_until": assessment["valid_until"],
        },
        at=at,
        actor=assessment["issuer"],
    )
    return ok("qualification_issued", qualification=q)


def issue_qualification(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    qualification_id: Optional[str] = None,
    at: Any = None,
) -> dict[str, Any]:
    ensure_schema(db)
    assessment = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    report = _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if not assessment or not report:
        return deny("signed_assessment_required", assessment_id=assessment_id)
    if report["status"] != "signed":
        return deny("assessment_report_not_valid", report_status=report["status"])
    result = _issue_qualification_from_report(
        db, assessment, report, qualification_id=qualification_id, at=at
    )
    _commit(db)
    return result


def submit_assessment(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    outcome: str = "timeout",
    timeout: Optional[bool] = None,
    at: Any = None,
) -> dict[str, Any]:
    """Attempt central upload. ``timeout`` leaves the result unverified, not failed."""
    ensure_schema(db)
    at = _now(at)
    report = _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if not report:
        return deny("signed_assessment_required", assessment_id=assessment_id)
    if report["sync_status"] == "received":
        q = _one(db, "SELECT id FROM cd_qualifications WHERE assessment_id=?", (assessment_id,))
        return ok("upload_already_received", assessment_id=assessment_id, qualification_id=q and q["id"])
    is_timeout = timeout if timeout is not None else outcome == "timeout"
    db.execute(
        "UPDATE cd_reports SET sync_status=? WHERE assessment_id=?",
        ("upload_timeout" if is_timeout else "upload_attempted", assessment_id),
    )
    _audit(
        db,
        "assessment_upload_attempted",
        "assessment",
        assessment_id,
        {"outcome": "timeout" if is_timeout else outcome, "at": at},
        at=at,
        actor="proctor-device",
    )
    _commit(db)
    if is_timeout:
        return deny(
            "upload_timeout",
            assessment_id=assessment_id,
            sync_status="upload_timeout",
            assessment_result=report["result"],
            qualification_issued=False,
            recovery=(
                "Device evidence is retained. Retry the same assessment and reconcile its "
                "original hash; do not create a second practical attempt or use attendance."
            ),
        )
    return ok("upload_attempted", assessment_id=assessment_id, sync_status="upload_attempted")


def receive_assessment_result(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    central_receipt: Optional[str] = None,
    evidence_sha256: Optional[str] = None,
    at: Any = None,
) -> dict[str, Any]:
    """Reconcile a central result with evidence saved by the proctor device."""
    ensure_schema(db)
    at = _now(at)
    assessment = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    report = _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if not assessment or not report:
        return deny("signed_assessment_required", assessment_id=assessment_id)
    if report["sync_status"] == "received":
        q = _one(db, "SELECT * FROM cd_qualifications WHERE assessment_id=?", (assessment_id,))
        if q:
            q["allowed_items"] = _loads(q["allowed_items"], [])
        return ok(
            "upload_received",
            duplicate=True,
            assessment_id=assessment_id,
            central_receipt=report["central_receipt"],
            qualification=q,
        )

    evidence_rows = _all(db, "SELECT * FROM cd_evidence WHERE assessment_id=?", (assessment_id,))
    if not evidence_rows:
        return deny("device_evidence_missing", assessment_id=assessment_id)
    if evidence_sha256 and not any(e["sha256"] == evidence_sha256 for e in evidence_rows):
        _incident(
            db,
            "assessment_evidence_hash_mismatch",
            person=assessment["person"],
            detail={"assessment_id": assessment_id, "provided_sha256": evidence_sha256},
            at=at,
        )
        _commit(db)
        return deny("evidence_hash_mismatch", assessment_id=assessment_id)

    central_receipt = central_receipt or f"central-receipt-{assessment_id}"
    for evidence in evidence_rows:
        db.execute(
            "UPDATE cd_evidence SET status='received', central_receipt=?, received_at=? WHERE id=?",
            (central_receipt, at, evidence["id"]),
        )
    db.execute(
        "UPDATE cd_reports SET sync_status='received', central_receipt=? WHERE id=?",
        (central_receipt, report["id"]),
    )
    report = _one(db, "SELECT * FROM cd_reports WHERE id=?", (report["id"],))
    issued = _issue_qualification_from_report(db, assessment, report, at=at)
    _audit(
        db,
        "assessment_upload_recovered",
        "assessment",
        assessment_id,
        {"central_receipt": central_receipt, "qualification_decision": issued["decision"]},
        at=at,
        actor="central-certification-ledger",
    )
    _commit(db)
    return ok(
        "upload_received",
        assessment_id=assessment_id,
        central_receipt=central_receipt,
        qualification=issued.get("qualification"),
        qualification_decision=issued["decision"],
    )


def retry_assessment_upload(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    succeed: bool = True,
    evidence_sha256: Optional[str] = None,
    central_receipt: Optional[str] = None,
    at: Any = None,
) -> dict[str, Any]:
    """Retry network delivery without duplicating the dangerous practical exam."""
    ensure_schema(db)
    report = _one(db, "SELECT sync_status FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if not report:
        return deny("signed_assessment_required", assessment_id=assessment_id)
    attempts = _one(
        db,
        "SELECT COUNT(*) AS n FROM cd_audit_events WHERE event_type='assessment_upload_attempted' AND aggregate_id=?",
        (assessment_id,),
    )["n"]
    if not succeed:
        result = submit_assessment(db, assessment_id, outcome="timeout", at=at)
        result["attempt_count"] = attempts + 1
        result["duplicate_assessment_created"] = False
        result["worker_required_to_repeat_practical"] = False
        return result
    result = receive_assessment_result(
        db,
        assessment_id,
        central_receipt=central_receipt,
        evidence_sha256=evidence_sha256,
        at=at,
    )
    result["attempt_count_before_recovery"] = attempts
    result["duplicate_assessment_created"] = False
    result["worker_required_to_repeat_practical"] = False
    return result


# ---------------------------------------------------------------------------
# Equipment model transition
# ---------------------------------------------------------------------------


def recognize_equipment_scope(
    db: sqlite3.Connection,
    new_qualification_id: str,
    *,
    person: str,
    from_model: str,
    to_model: str,
    at: Any = None,
    mapped_items: Optional[list[str] | tuple[str, ...]] = None,
    requested_items: Optional[list[str] | tuple[str, ...]] = None,
    operation_level: Optional[str] = None,
    issuer: Optional[str] = None,
    valid_until: Any = None,
    recognition_reference: Optional[str] = None,
) -> dict[str, Any]:
    """Recognize only scope already proven on the old equipment model.

    ``mapped_items`` are the items the responsible issuer accepts as equivalent
    on the new model.  They are still intersected with the old qualification's
    actual item endorsement.  Nothing unproven is granted.
    """
    ensure_schema(db)
    at = _now(at)
    old = active_qualification(
        db,
        person,
        equipment_model=from_model,
        operation_level=operation_level,
        at=at,
    )
    if not old:
        return deny("active_old_model_qualification_required", person=person, from_model=from_model)
    if operation_level and operation_level != old["operation_level"]:
        return deny(
            "model_transition_may_not_raise_operation_level",
            requested=operation_level,
            proven=old["operation_level"],
        )
    operation_level = old["operation_level"]
    old_items = set(old["allowed_items"])
    candidates = set(mapped_items if mapped_items is not None else old_items)
    if requested_items is not None:
        candidates &= set(requested_items)
    granted = sorted(candidates & old_items)
    unproven = sorted(candidates - old_items) + sorted((set(requested_items or []) - old_items))
    unproven = sorted(set(unproven))
    if not granted:
        return deny(
            "no_proven_scope_to_recognize",
            old_model=from_model,
            new_model=to_model,
            old_allowed_items=sorted(old_items),
            unproven_items=unproven,
        )
    existing = _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (new_qualification_id,))
    if existing:
        return ok("model_scope_already_recognized", qualification=_loads(_dumps(existing)))
    valid_until = min(old["valid_until"], valid_until if valid_until is not None else old["valid_until"])
    db.execute(
        """INSERT INTO cd_qualifications
           (id,person,position,equipment_model,operation_level,allowed_items,scope_type,
            assessment_id,report_id,source_qualification_id,recognition_reference,
            proctor,issuer,valid_from,valid_until,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            new_qualification_id,
            person,
            old["position"],
            to_model,
            operation_level,
            _dumps(granted),
            "model_transition_proven_items_only",
            old["assessment_id"],
            old["report_id"],
            old["id"],
            recognition_reference or f"model-map-{from_model}-{to_model}",
            old["proctor"],
            issuer or old["issuer"],
            at,
            valid_until,
            "active",
            at,
        ),
    )
    q = _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (new_qualification_id,))
    q["allowed_items"] = _loads(q["allowed_items"], [])
    _audit(
        db,
        "equipment_model_scope_recognized_limited",
        "qualification",
        new_qualification_id,
        {
            "from_model": from_model,
            "to_model": to_model,
            "granted_items": granted,
            "unproven_items": unproven,
            "source_qualification_id": old["id"],
        },
        at=at,
        actor=issuer or old["issuer"],
    )
    _commit(db)
    return ok(
        "model_scope_recognized",
        qualification=q,
        granted_items=granted,
        unproven_items=unproven,
        explanation_zh="型号更换不扩大操作级别或项目范围；仅承认旧证已证明且经映射的项目。",
    )


change_equipment_model = recognize_equipment_scope


# ---------------------------------------------------------------------------
# Production lease bridge
# ---------------------------------------------------------------------------


def verify_production_lease(
    db: sqlite3.Connection,
    namespace: str,
    at: Any,
    *,
    lease_id: Optional[str] = None,
    team: Optional[str] = None,
    generation: Optional[int] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    table = _one(db, "SELECT name FROM sqlite_master WHERE type='table' AND name='leases'")
    if not table:
        return deny("production_lease_store_unavailable", namespace=namespace)
    if lease_id:
        lease = _one(db, "SELECT * FROM leases WHERE id=?", (lease_id,))
        if not lease or lease["namespace"] != namespace:
            return deny("production_lease_invalid", reason="lease_not_found_or_namespace_mismatch")
    else:
        lease = _one(
            db,
            """SELECT * FROM leases
               WHERE namespace=? AND status='active' AND start<=? AND expires>=?
               ORDER BY generation DESC LIMIT 1""",
            (namespace, at, at),
        )
        if not lease:
            return deny("production_lease_invalid", reason="no_active_lease_in_effective_period")
    if lease["status"] != "active" or not (lease["start"] <= at <= lease["expires"]):
        return deny("production_lease_invalid", reason="lease_not_active_or_outside_period", lease=lease)
    if team and lease["holder"] != team:
        return deny("production_lease_invalid", reason="lease_holder_mismatch", expected=lease["holder"], provided=team)
    if generation is not None and lease["generation"] != generation:
        return deny(
            "production_lease_invalid",
            reason="lease_generation_mismatch",
            expected=lease["generation"],
            provided=generation,
        )
    return ok("production_lease_active", lease=lease)


# ---------------------------------------------------------------------------
# Work orders and two lawful delivery arrangements
# ---------------------------------------------------------------------------


def open_work_order(
    db: sqlite3.Connection,
    work_order_id: str,
    *,
    equipment_model: str,
    customer: Optional[str] = None,
    namespace: Optional[str] = None,
    lease_id: Optional[str] = None,
    team: Optional[str] = None,
    generation: Optional[int] = None,
    position: str = DEFAULT_POSITION,
    operation_level: str = DEFAULT_LEVEL,
    items: Optional[list[str] | tuple[str, ...]] = None,
    scheduled_at: Any = None,
    at: Any = None,
    require_lease: bool = False,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at if at is not None else scheduled_at)
    items = tuple(items or CORE_ITEMS)
    old = _one(db, "SELECT * FROM cd_work_orders WHERE id=?", (work_order_id,))
    if old:
        same = (
            old["equipment_model"] == equipment_model
            and old["position"] == position
            and old["operation_level"] == operation_level
            and _loads(old["required_items"], []) == list(items)
            and (customer is None or old["customer"] == customer)
            and (namespace is None or old["namespace"] == namespace)
        )
        if not same:
            return deny("work_order_id_conflict", work_order_id=work_order_id)
        return ok("work_order_reused", work_order=_loads(_dumps(old)))
    if require_lease:
        lease = verify_production_lease(
            db, namespace or "", at, lease_id=lease_id, team=team, generation=generation
        )
        if not lease.get("allowed"):
            return lease
    db.execute(
        """INSERT INTO cd_work_orders
           (id,customer,namespace,lease_id,team,generation,position,equipment_model,operation_level,
            required_items,scheduled_at,require_lease,status,delivery_mode,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            work_order_id,
            customer,
            namespace,
            lease_id,
            team,
            generation,
            position,
            equipment_model,
            operation_level,
            _dumps(list(items)),
            at,
            1 if require_lease else 0,
            "planned",
            None,
            at,
            at,
        ),
    )
    _audit(db, "work_order_opened", "work_order", work_order_id, {"customer": customer, "equipment_model": equipment_model, "items": list(items)}, at=at)
    _commit(db)
    return ok("work_order_opened", work_order_id=work_order_id)


def _load_work_order(db: sqlite3.Connection, work_order_id: str) -> Optional[dict[str, Any]]:
    wo = _one(db, "SELECT * FROM cd_work_orders WHERE id=?", (work_order_id,))
    if wo:
        wo["required_items"] = _loads(wo["required_items"], [])
    return wo


def _check_work_lease(db: sqlite3.Connection, wo: dict[str, Any], at: Any) -> Optional[dict[str, Any]]:
    if not wo["require_lease"]:
        return None
    result = verify_production_lease(
        db,
        wo["namespace"],
        at,
        lease_id=wo["lease_id"],
        team=wo["team"],
        generation=wo["generation"],
    )
    return None if result.get("allowed") else result


def authorize_independent_work(
    db: sqlite3.Connection,
    work_order_id: str,
    technician_id: str,
    *,
    at: Any = None,
    qualification_id: Optional[str] = None,
    course: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    wo = _load_work_order(db, work_order_id)
    if not wo:
        return deny("unknown_work_order", work_order_id=work_order_id)
    if wo["delivery_mode"]:
        if wo["delivery_mode"] == "independent_certified" and wo["operator_id"] == technician_id:
            lease_denied = _check_work_lease(db, wo, at)
            if lease_denied:
                return lease_denied
            blockers = qualification_blockers(
                db,
                technician_id,
                position=wo["position"],
                equipment_model=wo["equipment_model"],
                operation_level=wo["operation_level"],
                items=wo["required_items"],
                at=at,
                qualification_id=wo["qualification_id"],
            )
            if blockers:
                return deny(
                    "independent_work_denied",
                    duplicate=True,
                    work_order_id=work_order_id,
                    person=technician_id,
                    blockers=blockers,
                    provisional_qualified=provisional_status(db, technician_id, course)["provisional_qualified"],
                )
            q = active_qualification(db, technician_id, qualification_id=wo["qualification_id"], at=at)
            return ok("independent_authorized", duplicate=True, work_order_id=work_order_id, qualification=q)
        return deny(
            "work_order_delivery_already_assigned",
            work_order_id=work_order_id,
            delivery_mode=wo["delivery_mode"],
            operator_id=wo["operator_id"],
        )
    lease_denied = _check_work_lease(db, wo, at)
    if lease_denied:
        return lease_denied

    decision = adjudicate_independent_work(
        db,
        technician_id,
        position=wo["position"],
        equipment_model=wo["equipment_model"],
        operation_level=wo["operation_level"],
        items=wo["required_items"],
        at=at,
        course=course,
        qualification_id=qualification_id,
    )
    if not decision["allowed"]:
        _incident(
            db,
            "independent_dispatch_denied",
            work_order_id=work_order_id,
            person=technician_id,
            detail={"blockers": decision["blockers"], "provisional_qualified": decision["provisional_qualified"]},
            at=at,
        )
        _commit(db)
        decision["work_order_id"] = work_order_id
        return decision

    q = decision["qualification"]
    db.execute(
        """UPDATE cd_work_orders
           SET status='independent_authorized', delivery_mode='independent_certified',
               operator_id=?, qualification_id=?, updated_at=?
           WHERE id=?""",
        (technician_id, q["id"], at, work_order_id),
    )
    _audit(
        db,
        "independent_operator_authorized",
        "work_order",
        work_order_id,
        {"operator_id": technician_id, "qualification_id": q["id"]},
        at=at,
        actor=technician_id,
    )
    _commit(db)
    return ok("independent_authorized", work_order_id=work_order_id, operator_id=technician_id, qualification=q)


def assign_mentored_delivery(
    db: sqlite3.Connection,
    work_order_id: str,
    senior_id: str,
    apprentice_id: str,
    *,
    at: Any = None,
    mentorship_id: Optional[str] = None,
    allowed_practice_items: Optional[list[str] | tuple[str, ...]] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    wo = _load_work_order(db, work_order_id)
    if not wo:
        return deny("unknown_work_order", work_order_id=work_order_id)
    if wo["delivery_mode"]:
        if (
            wo["delivery_mode"] == "senior_operator_with_apprentice"
            and wo["senior_id"] == senior_id
            and wo["apprentice_id"] == apprentice_id
        ):
            return ok("mentored_delivery_authorized", duplicate=True, mentorship_id=wo["mentorship_id"])
        return deny(
            "work_order_delivery_already_assigned",
            delivery_mode=wo["delivery_mode"],
            existing_senior_id=wo["senior_id"],
            existing_apprentice_id=wo["apprentice_id"],
        )
    if senior_id == apprentice_id:
        return deny("senior_and_apprentice_must_differ")
    lease_denied = _check_work_lease(db, wo, at)
    if lease_denied:
        return lease_denied
    senior_q = active_qualification(
        db,
        senior_id,
        position=wo["position"],
        equipment_model=wo["equipment_model"],
        operation_level=wo["operation_level"],
        at=at,
    )
    blockers = qualification_blockers(
        db,
        senior_id,
        position=wo["position"],
        equipment_model=wo["equipment_model"],
        operation_level=wo["operation_level"],
        items=wo["required_items"],
        at=at,
    )
    if blockers or not senior_q:
        return deny("senior_not_fully_qualified", senior_id=senior_id, blockers=blockers)

    # Safety-critical work-order operations remain the senior's legal acts.
    # Any separate practice scope must be explicitly narrow and is never billed
    # or accepted as the work order's actual operation.
    practice = list(allowed_practice_items or [])
    outside = sorted(set(practice) - set(wo["required_items"]))
    if outside:
        return deny("practice_item_outside_work_scope", items=outside)
    mentorship_id = mentorship_id or f"mentor-{work_order_id}"
    db.execute(
        """INSERT INTO cd_mentorships
           (id,work_order_id,mentor_id,mentor_qualification_id,apprentice_id,
            allowed_practice_items,status,present,started_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            mentorship_id,
            work_order_id,
            senior_id,
            senior_q["id"],
            apprentice_id,
            _dumps(practice),
            "active",
            1,
            at,
        ),
    )
    db.execute(
        """UPDATE cd_work_orders
           SET status='mentored_authorized', delivery_mode='senior_operator_with_apprentice',
               operator_id=?, qualification_id=?, senior_id=?, senior_qualification_id=?,
               apprentice_id=?, mentorship_id=?, updated_at=?
           WHERE id=?""",
        (senior_id, senior_q["id"], senior_id, senior_q["id"], apprentice_id, mentorship_id, at, work_order_id),
    )
    db.execute(
        "INSERT INTO cd_supervision_events(id,mentorship_id,kind,actor_id,reason,at) VALUES(?,?,?,?,?,?)",
        (_next_id(db, "cd_supervision_events", "sup"), mentorship_id, "senior_present", senior_id, "mentored_delivery_started", at),
    )
    _audit(
        db,
        "mentored_delivery_authorized",
        "work_order",
        work_order_id,
        {"senior_id": senior_id, "apprentice_id": apprentice_id, "mentorship_id": mentorship_id},
        at=at,
        actor=senior_id,
    )
    _commit(db)
    return ok(
        "mentored_delivery_authorized",
        work_order_id=work_order_id,
        actual_operator_id=senior_id,
        senior_qualification=senior_q,
        apprentice_id=apprentice_id,
        mentorship_id=mentorship_id,
        allowed_practice_items=practice,
        explanation_zh="资深工程师是实际操作人；学徒仅在限定范围内见习，带教记录不发放资格。",
    )


def _load_mentorship(db: sqlite3.Connection, mentorship_id: str) -> Optional[dict[str, Any]]:
    m = _one(db, "SELECT * FROM cd_mentorships WHERE id=?", (mentorship_id,))
    if m:
        m["allowed_practice_items"] = _loads(m["allowed_practice_items"], [])
    return m


def senior_departure(
    db: sqlite3.Connection,
    *,
    mentorship_id: Optional[str] = None,
    work_order_id: Optional[str] = None,
    at: Any = None,
    reason: str = "temporary_departure",
    departure_id: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    if not mentorship_id and work_order_id:
        wo = _load_work_order(db, work_order_id)
        mentorship_id = wo and wo["mentorship_id"]
    m = _load_mentorship(db, mentorship_id or "")
    if not m:
        return deny("unknown_mentorship", mentorship_id=mentorship_id, work_order_id=work_order_id)
    if not m["present"]:
        return ok("senior_already_absent", mentorship_id=m["id"], duplicate=True)
    db.execute("UPDATE cd_mentorships SET present=0, status='senior_absent' WHERE id=?", (m["id"],))
    db.execute(
        "INSERT INTO cd_supervision_events(id,mentorship_id,kind,actor_id,reason,at) VALUES(?,?,?,?,?,?)",
        (departure_id or _next_id(db, "cd_supervision_events", "sup"), m["id"], "senior_departed", m["mentor_id"], reason, at),
    )
    _audit(db, "senior_departed", "work_order", m["work_order_id"], {"mentorship_id": m["id"], "reason": reason}, at=at, actor=m["mentor_id"])
    _commit(db)
    return ok("senior_departure_recorded", mentorship_id=m["id"], work_order_id=m["work_order_id"], present=False)


# Friendly aliases.
record_senior_departure = senior_departure
depart_senior = senior_departure


def senior_return(
    db: sqlite3.Connection,
    *,
    mentorship_id: Optional[str] = None,
    work_order_id: Optional[str] = None,
    at: Any = None,
    senior_id: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    if not mentorship_id and work_order_id:
        wo = _load_work_order(db, work_order_id)
        mentorship_id = wo and wo["mentorship_id"]
    m = _load_mentorship(db, mentorship_id or "")
    if not m:
        return deny("unknown_mentorship")
    if senior_id and senior_id != m["mentor_id"]:
        return deny("different_senior_must_be_authorized_as_replacement")
    q = active_qualification(db, m["mentor_id"], qualification_id=m["mentor_qualification_id"], at=at)
    if not q:
        return deny("returning_senior_qualification_not_valid")
    db.execute("UPDATE cd_mentorships SET present=1, status='active' WHERE id=?", (m["id"],))
    db.execute(
        "INSERT INTO cd_supervision_events(id,mentorship_id,kind,actor_id,reason,at) VALUES(?,?,?,?,?,?)",
        (_next_id(db, "cd_supervision_events", "sup"), m["id"], "senior_returned", m["mentor_id"], "temporary_departure_ended", at),
    )
    _audit(db, "senior_returned", "work_order", m["work_order_id"], {"mentorship_id": m["id"]}, at=at, actor=m["mentor_id"])
    _commit(db)
    return ok("senior_return_recorded", mentorship_id=m["id"], present=True)


record_senior_return = senior_return
return_senior = senior_return


def record_operation(
    db: sqlite3.Connection,
    operation_id: str,
    work_order_id: str,
    item: str,
    actor_id: str,
    *,
    at: Any = None,
    evidence_id: Optional[str] = None,
    supervised_by: Optional[str] = None,
    note: Optional[str] = None,
) -> dict[str, Any]:
    """Record an actual accepted work-order operation after authorization checks."""
    ensure_schema(db)
    at = _now(at)
    existing = _one(db, "SELECT * FROM cd_operations WHERE id=?", (operation_id,))
    if existing:
        if existing["work_order_id"] == work_order_id and existing["item"] == item and existing["actor_id"] == actor_id:
            return ok("operation_recorded", duplicate=True, operation=_loads(_dumps(existing)))
        return deny("operation_id_conflict", operation_id=operation_id)

    wo = _load_work_order(db, work_order_id)
    if not wo:
        return deny("unknown_work_order", work_order_id=work_order_id)
    if not wo["delivery_mode"]:
        return deny("work_order_not_authorized", work_order_id=work_order_id)
    if item not in wo["required_items"]:
        return deny("operation_not_in_work_scope", item=item, required_items=wo["required_items"])
    lease_denied = _check_work_lease(db, wo, at)
    if lease_denied:
        return lease_denied

    actor_role = "operator"
    qualification_id = wo["qualification_id"]

    if wo["delivery_mode"] == "independent_certified":
        if actor_id != wo["operator_id"]:
            _incident(
                db,
                "unauthorized_operation_actor",
                work_order_id=work_order_id,
                person=actor_id,
                detail={"expected_operator": wo["operator_id"], "item": item},
                at=at,
            )
            _commit(db)
            return deny("operator_mismatch", expected_operator=wo["operator_id"], actor=actor_id)
        blockers = qualification_blockers(
            db,
            actor_id,
            position=wo["position"],
            equipment_model=wo["equipment_model"],
            operation_level=wo["operation_level"],
            items=[item],
            at=at,
            qualification_id=wo["qualification_id"],
        )
        if blockers:
            _incident(
                db,
                "unqualified_operation_blocked",
                work_order_id=work_order_id,
                person=actor_id,
                detail={"blockers": blockers, "item": item},
                at=at,
            )
            _commit(db)
            return deny("qualified_operation_required", blockers=blockers)
        actor_role = "independent_operator"

    elif wo["delivery_mode"] == "senior_operator_with_apprentice":
        m = _load_mentorship(db, wo["mentorship_id"])
        if actor_id == wo["apprentice_id"]:
            incident = _incident(
                db,
                "apprentice_overreach_blocked",
                work_order_id=work_order_id,
                person=actor_id,
                detail={
                    "item": item,
                    "actual_operator": wo["senior_id"],
                    "senior_present": bool(m and m["present"]),
                    "rule": "Apprentice may observe/limited-practice in mentoring log, never act as work-order operator.",
                },
                at=at,
            )
            _commit(db)
            return deny(
                "apprentice_operation_denied",
                work_order_id=work_order_id,
                apprentice_id=actor_id,
                item=item,
                actual_operator_id=wo["senior_id"],
                incident_id=incident["id"],
                explanation_zh="学徒不得独立执行或顶替实际操作人；该尝试已记录为越权事件。",
            )
        if actor_id != wo["senior_id"]:
            _incident(
                db,
                "unauthorized_operation_actor",
                work_order_id=work_order_id,
                person=actor_id,
                detail={"expected_senior": wo["senior_id"], "item": item},
                at=at,
            )
            _commit(db)
            return deny("actual_senior_operator_required", expected_senior=wo["senior_id"], actor=actor_id)
        if not m or not m["present"] or m["status"] != "active":
            _incident(
                db,
                "operation_attempted_while_senior_absent",
                work_order_id=work_order_id,
                person=actor_id,
                detail={"item": item, "mentorship_status": m and m["status"]},
                at=at,
            )
            _commit(db)
            return deny(
                "senior_absent_operation_paused",
                work_order_id=work_order_id,
                item=item,
                explanation_zh="资深工程师离场期间必须暂停，不得由学徒或其他人顶替危险操作。",
            )
        blockers = qualification_blockers(
            db,
            actor_id,
            position=wo["position"],
            equipment_model=wo["equipment_model"],
            operation_level=wo["operation_level"],
            items=[item],
            at=at,
            qualification_id=wo["senior_qualification_id"],
        )
        if blockers:
            return deny("senior_qualification_no_longer_valid", blockers=blockers)
        supervised_by = actor_id
        qualification_id = wo["senior_qualification_id"]
        actor_role = "senior_actual_operator"
    else:
        return deny("unknown_delivery_mode", delivery_mode=wo["delivery_mode"])

    seq = _one(db, "SELECT COALESCE(MAX(seq),0)+1 AS n FROM cd_operations WHERE work_order_id=?", (work_order_id,))["n"]
    db.execute(
        """INSERT INTO cd_operations
           (id,work_order_id,seq,item,actor_id,actor_role,qualification_id,supervised_by,
            evidence_id,note,at,status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            operation_id,
            work_order_id,
            seq,
            item,
            actor_id,
            actor_role,
            qualification_id,
            supervised_by,
            evidence_id,
            note,
            at,
            "recorded",
        ),
    )
    db.execute("UPDATE cd_work_orders SET status='in_progress', updated_at=? WHERE id=?", (at, work_order_id))
    _audit(
        db,
        "actual_operation_recorded",
        "work_order",
        work_order_id,
        {"operation_id": operation_id, "seq": seq, "item": item, "actor_id": actor_id, "actor_role": actor_role},
        at=at,
        actor=actor_id,
    )
    _commit(db)
    return ok("operation_recorded", operation_id=operation_id, work_order_id=work_order_id, seq=seq, item=item, actor_id=actor_id)


perform_operation = record_operation


def record_apprentice_evidence(
    db: sqlite3.Connection,
    evidence_record_id: str,
    mentorship_id: str,
    item: str,
    *,
    at: Any = None,
    method: str = "observed",
    note: Optional[str] = None,
    evidence_id: Optional[str] = None,
    signed_by: Optional[str] = None,
    signature: Optional[str] = None,
) -> dict[str, Any]:
    """Record real observation/supervised-practice evidence, not qualification."""
    ensure_schema(db)
    at = _now(at)
    m = _load_mentorship(db, mentorship_id)
    if not m:
        return deny("unknown_mentorship", mentorship_id=mentorship_id)
    wo = _load_work_order(db, m["work_order_id"])
    if item not in wo["required_items"]:
        return deny("apprentice_evidence_item_out_of_scope", item=item)
    if method not in {"observed", "supervised_practice"}:
        return deny("unknown_apprentice_evidence_method", method=method)
    if method == "observed":
        actual = _one(
            db,
            """SELECT id FROM cd_operations
               WHERE work_order_id=? AND item=? AND actor_id=? AND status='recorded'""",
            (wo["id"], item, m["mentor_id"]),
        )
        if not actual:
            return deny(
                "observation_requires_actual_senior_operation",
                item=item,
                work_order_id=wo["id"],
            )
    if method == "supervised_practice" and item not in m["allowed_practice_items"]:
        return deny("practice_item_not_limited_or_authorized", item=item, allowed=m["allowed_practice_items"])
    if not m["present"]:
        return deny("mentor_absent_cannot_sign_current_evidence")
    old = _one(db, "SELECT id FROM cd_mentorship_evidence WHERE id=?", (evidence_record_id,))
    if old:
        return ok("apprentice_evidence_reused", evidence_record_id=evidence_record_id)
    signed_by = signed_by or m["mentor_id"]
    if signed_by != m["mentor_id"]:
        return deny("only_assigned_mentor_may_sign_evidence")
    signature = signature or f"mentor-signature:{evidence_record_id}:{signed_by}"
    db.execute(
        """INSERT INTO cd_mentorship_evidence
           (id,mentorship_id,work_order_id,apprentice_id,item,method,note,evidence_id,
            signed_by,signature,at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (
            evidence_record_id,
            mentorship_id,
            m["work_order_id"],
            m["apprentice_id"],
            item,
            method,
            note,
            evidence_id,
            signed_by,
            signature,
            at,
        ),
    )
    _audit(
        db,
        "apprentice_skill_evidence_recorded",
        "mentorship",
        mentorship_id,
        {"record_id": evidence_record_id, "item": item, "method": method, "qualified": False},
        at=at,
        actor=signed_by,
    )
    _commit(db)
    return ok(
        "apprentice_evidence_recorded",
        evidence_record_id=evidence_record_id,
        apprentice_id=m["apprentice_id"],
        item=item,
        method=method,
        grants_qualification=False,
    )


record_observation = record_apprentice_evidence


def complete_mentorship(
    db: sqlite3.Connection,
    mentorship_id: str,
    *,
    at: Any = None,
    signature: Optional[str] = None,
    summary: Optional[str] = None,
    require_all_items: bool = True,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    m = _load_mentorship(db, mentorship_id)
    if not m:
        return deny("unknown_mentorship", mentorship_id=mentorship_id)
    if m["status"] == "completed":
        return ok("mentorship_completed", duplicate=True, mentorship_id=mentorship_id)
    wo = _load_work_order(db, m["work_order_id"])
    evidence = _all(db, "SELECT DISTINCT item FROM cd_mentorship_evidence WHERE mentorship_id=?", (mentorship_id,))
    seen = {e["item"] for e in evidence}
    missing = sorted(set(wo["required_items"]) - seen)
    if require_all_items and missing:
        return deny("mentorship_evidence_incomplete", missing_items=missing)
    if not m["present"] or m["status"] != "active":
        return deny("mentor_must_return_before_completing_record", mentorship_id=mentorship_id, status=m["status"])
    signature = signature or f"mentor-completion:{mentorship_id}:{m['mentor_id']}"
    db.execute(
        "UPDATE cd_mentorships SET status='completed', present=0, ended_at=?, mentor_signature=?, completion_note=? WHERE id=?",
        (
            at,
            signature,
            summary or "Apprentice observed the certified senior perform all safety-critical operations.",
            mentorship_id,
        ),
    )
    _audit(
        db,
        "mentorship_completed",
        "mentorship",
        mentorship_id,
        {"items_observed": sorted(seen), "missing_items": missing, "grants_qualification": False},
        at=at,
        actor=m["mentor_id"],
    )
    _commit(db)
    return ok("mentorship_completed", mentorship_id=mentorship_id, items_observed=sorted(seen), missing_items=missing, grants_qualification=False)


# ---------------------------------------------------------------------------
# Maintenance report and customer acceptance
# ---------------------------------------------------------------------------


def _completed_operation_items(db: sqlite3.Connection, work_order_id: str) -> tuple[set[str], list[dict[str, Any]]]:
    ops = _all(db, "SELECT * FROM cd_operations WHERE work_order_id=? AND status='recorded' ORDER BY seq", (work_order_id,))
    return {op["item"] for op in ops}, ops


def issue_maintenance_report(
    db: sqlite3.Connection,
    report_id: str,
    work_order_id: str,
    *,
    at: Any = None,
    issued_by: str = "maintenance-desk",
    summary: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    wo = _load_work_order(db, work_order_id)
    if not wo:
        return deny("unknown_work_order", work_order_id=work_order_id)
    existing = _one(db, "SELECT * FROM cd_reports_maintenance WHERE work_order_id=?", (work_order_id,))
    if existing:
        if existing["id"] == report_id:
            return ok("report_already_issued", report=_loads(_dumps(existing)))
        return deny("report_id_conflict_for_work_order", existing_report_id=existing["id"])

    done_items, ops = _completed_operation_items(db, work_order_id)
    missing = sorted(set(wo["required_items"]) - done_items)
    if missing:
        return deny("operations_incomplete", work_order_id=work_order_id, missing_items=missing)

    mentorship = None
    mentor_evidence: list[dict[str, Any]] = []
    if wo["delivery_mode"] == "senior_operator_with_apprentice":
        mentorship = _load_mentorship(db, wo["mentorship_id"])
        if not mentorship or mentorship["status"] != "completed":
            return deny("completed_mentorship_record_required", mentorship_id=wo["mentorship_id"])
        mentor_evidence = _all(
            db,
            "SELECT id,item,method,signed_by,at FROM cd_mentorship_evidence WHERE mentorship_id=? ORDER BY at,id",
            (wo["mentorship_id"],),
        )

    q = _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (wo["qualification_id"],))
    if not q or q["status"] != "active":
        return deny("actual_operator_qualification_not_active", qualification_status=q and q["status"])

    snapshot = {
        "report_id": report_id,
        "work_order": wo,
        "actual_operations": ops,
        "actual_operator_id": wo["operator_id"],
        "qualification_id": wo["qualification_id"],
        "qualification_scope": {
            "position": q["position"],
            "equipment_model": q["equipment_model"],
            "operation_level": q["operation_level"],
            "allowed_items": _loads(q["allowed_items"], []),
            "issuer": q["issuer"],
            "proctor": q["proctor"],
            "valid_from": q["valid_from"],
            "valid_until": q["valid_until"],
        },
        "mentorship": mentorship,
        "mentor_evidence": mentor_evidence,
        "issued_at": at,
        "summary": summary,
    }
    report_hash = _hash_snapshot(snapshot)
    default_summary = (
        "由持完整资格的工程师独立完成高压检修与恢复。"
        if wo["delivery_mode"] == "independent_certified"
        else "由资深资格工程师实际完成全部高压操作；学徒仅见习并留下签署带教证据，未独立操作。"
    )
    db.execute(
        """INSERT INTO cd_reports_maintenance
           (id,work_order_id,actual_operator_id,delivery_mode,qualification_id,mentorship_id,
            issued_by,issued_at,summary,payload,report_hash,status,review_status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            report_id,
            work_order_id,
            wo["operator_id"],
            wo["delivery_mode"],
            wo["qualification_id"],
            wo["mentorship_id"],
            issued_by,
            at,
            summary or default_summary,
            _dumps(snapshot),
            report_hash,
            "issued",
            "none",
        ),
    )
    db.execute("UPDATE cd_work_orders SET status='reported', updated_at=? WHERE id=?", (at, work_order_id))
    _notify(
        db,
        f"customer:{wo['customer'] or 'unknown'}",
        "maintenance_report_issued",
        summary or default_summary,
        audience="customer",
        subject="检修维护报告已出具，待客户签收",
        refs={"report_id": report_id, "work_order_id": work_order_id, "report_hash": report_hash},
        at=at,
        notification_id=f"notice-report-{report_id}",
    )
    _audit(db, "maintenance_report_issued", "report", report_id, {"work_order_id": work_order_id, "hash": report_hash}, at=at)
    _commit(db)
    return ok("report_issued", report_id=report_id, work_order_id=work_order_id, report_hash=report_hash, summary=summary or default_summary)


def accept_maintenance_report(
    db: sqlite3.Connection,
    acceptance_id: str,
    report_id: str,
    *,
    at: Any = None,
    customer: Optional[str] = None,
    accepted_by: Optional[str] = None,
    receipt: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    report = _one(db, "SELECT * FROM cd_reports_maintenance WHERE id=?", (report_id,))
    if not report:
        return deny("unknown_report", report_id=report_id)
    existing = _one(db, "SELECT * FROM cd_acceptances WHERE report_id=?", (report_id,))
    if existing:
        return ok("report_already_accepted", acceptance=_loads(_dumps(existing)), duplicate=True)
    if report["review_status"] == "open":
        return deny("report_under_review_acceptance_held", report_id=report_id)
    if report["review_status"] not in {"none", "closed_confirmed"}:
        return deny(
            "report_acceptance_held_pending_corrective_action",
            report_id=report_id,
            review_status=report["review_status"],
        )
    wo = _load_work_order(db, report["work_order_id"])
    if customer and wo.get("customer") and customer != wo["customer"]:
        return deny("customer_mismatch", expected=wo["customer"], provided=customer)
    acceptance = {
        "id": acceptance_id,
        "report_id": report_id,
        "work_order_id": report["work_order_id"],
        "customer": customer or wo.get("customer"),
        "accepted_by": accepted_by or "customer-representative",
        "receipt": receipt or f"customer-receipt-{acceptance_id}",
        "at": at,
    }
    db.execute(
        "INSERT INTO cd_acceptances(id,report_id,work_order_id,customer,accepted_by,receipt,at) VALUES(?,?,?,?,?,?,?)",
        tuple(acceptance.values()),
    )
    db.execute("UPDATE cd_reports_maintenance SET status='accepted' WHERE id=?", (report_id,))
    db.execute("UPDATE cd_work_orders SET status='accepted', updated_at=? WHERE id=?", (at, report["work_order_id"]))
    _notify(
        db,
        f"customer:{acceptance['customer'] or 'unknown'}",
        "maintenance_report_accepted",
        "客户已签收仅由合格实际操作人完成的检修报告。",
        audience="customer",
        subject="检修报告签收回执",
        refs={"report_id": report_id, "acceptance_id": acceptance_id},
        at=at,
        notification_id=f"notice-accept-{acceptance_id}",
    )
    _audit(db, "maintenance_report_accepted", "report", report_id, {"acceptance_id": acceptance_id}, at=at, actor=accepted_by)
    _commit(db)
    return ok("report_accepted", **acceptance)


accept_report = accept_maintenance_report


# ---------------------------------------------------------------------------
# Fees: assessment/training and customer maintenance have separate bases
# ---------------------------------------------------------------------------


def create_fee(
    db: sqlite3.Connection,
    fee_id: str,
    *,
    kind: str,
    amount: float,
    source_type: str,
    source_id: str,
    at: Any = None,
    direction: str = "payable",
    payer: Optional[str] = None,
    payee: Optional[str] = None,
    currency: str = "CNY",
    work_order_id: Optional[str] = None,
    assessment_id: Optional[str] = None,
    acceptance_id: Optional[str] = None,
    paid: bool = False,
    basis: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    if amount is None or float(amount) <= 0:
        return deny("fee_amount_must_be_positive", fee_id=fee_id, amount=amount)
    if source_type == "attendance" or kind in {"attendance_fee", "lecture_count_fee"}:
        return deny(
            "attendance_cannot_support_fee",
            fee_id=fee_id,
            explanation_zh="出勤次数只能证明听课，不能推出考核费或客户验收费。",
        )
    old = _one(db, "SELECT * FROM cd_fees WHERE id=?", (fee_id,))
    if old:
        return ok("fee_reused", fee=_loads(_dumps(old)))
    duplicate_source = _one(
        db,
        "SELECT id FROM cd_fees WHERE kind=? AND source_type=? AND source_id=? AND status!='void'",
        (kind, source_type, source_id),
    )
    if duplicate_source:
        return deny("duplicate_fee_for_source", existing_fee_id=duplicate_source["id"])

    status = "paid" if paid else "due"
    fee = {
        "id": fee_id,
        "kind": kind,
        "direction": direction,
        "payer": payer,
        "payee": payee,
        "amount": amount,
        "currency": currency,
        "source_type": source_type,
        "source_id": source_id,
        "work_order_id": work_order_id,
        "assessment_id": assessment_id or (source_id if source_type == "assessment" else None),
        "acceptance_id": acceptance_id or (source_id if source_type == "acceptance" else None),
        "basis": _dumps(basis or {}),
        "status": status,
        "created_at": at,
        "due_at": at,
        "paid_at": at if paid else None,
        "payment_reference": f"payment-{fee_id}" if paid else None,
        "refunded_amount": 0,
    }
    db.execute(
        """INSERT INTO cd_fees
           (id,kind,direction,payer,payee,amount,currency,source_type,source_id,work_order_id,
            assessment_id,acceptance_id,basis,status,created_at,due_at,paid_at,payment_reference,refunded_amount)
           VALUES(:id,:kind,:direction,:payer,:payee,:amount,:currency,:source_type,:source_id,:work_order_id,
            :assessment_id,:acceptance_id,:basis,:status,:created_at,:due_at,:paid_at,:payment_reference,:refunded_amount)""",
        fee,
    )
    _audit(db, "fee_created_" + status, "fee", fee_id, {"kind": kind, "amount": amount, "source_type": source_type, "source_id": source_id}, at=at, actor=payer)
    _commit(db)
    return ok("fee_paid" if paid else "fee_due", fee_id=fee_id, status=status)


def create_training_fee(
    db: sqlite3.Connection,
    fee_id: str,
    assessment_id: str,
    amount: float,
    *,
    at: Any = None,
    payer: str = "platform",
    payee: Optional[str] = None,
    kind: str = "external_assessment_fee",
    paid: bool = False,
) -> dict[str, Any]:
    ensure_schema(db)
    assessment = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    report = _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if not assessment or not report or report["status"] not in {"signed", "received"}:
        return deny("training_fee_requires_completed_signed_assessment", assessment_id=assessment_id)
    if report["status"] == "revoked":
        return deny("training_fee_blocked_for_revoked_report", assessment_id=assessment_id)
    return create_fee(
        db,
        fee_id,
        kind=kind,
        amount=amount,
        source_type="assessment",
        source_id=assessment_id,
        at=at,
        direction="payable",
        payer=payer,
        payee=payee or assessment["issuer"],
        assessment_id=assessment_id,
        paid=paid,
        basis={
            "assessment_id": assessment_id,
            "report_id": report["id"],
            "completed_and_signed": True,
            "result": report["result"],
            "attendance_count_not_a_basis": True,
        },
    )


create_assessment_fee = create_training_fee


def create_maintenance_fee(
    db: sqlite3.Connection,
    fee_id: str,
    *,
    amount: float,
    acceptance_id: Optional[str] = None,
    report_id: Optional[str] = None,
    at: Any = None,
    payer: Optional[str] = None,
    payee: str = "tenurecourt-maintenance",
    paid: bool = False,
) -> dict[str, Any]:
    ensure_schema(db)
    acceptance = None
    if acceptance_id:
        acceptance = _one(db, "SELECT * FROM cd_acceptances WHERE id=?", (acceptance_id,))
    elif report_id:
        acceptance = _one(db, "SELECT * FROM cd_acceptances WHERE report_id=?", (report_id,))
    if not acceptance:
        return deny("maintenance_fee_requires_customer_acceptance", report_id=report_id, acceptance_id=acceptance_id)
    report = _one(db, "SELECT * FROM cd_reports_maintenance WHERE id=?", (acceptance["report_id"],))
    wo = _load_work_order(db, acceptance["work_order_id"])
    if not report or not wo:
        return deny("accepted_report_chain_incomplete")
    ops, operation_rows = _completed_operation_items(db, wo["id"])
    if set(wo["required_items"]) - ops:
        return deny("maintenance_fee_requires_actual_completed_operations")
    return create_fee(
        db,
        fee_id,
        kind="customer_maintenance_service_fee",
        amount=amount,
        source_type="acceptance",
        source_id=acceptance["id"],
        at=at,
        direction="receivable",
        payer=payer or acceptance["customer"],
        payee=payee,
        work_order_id=wo["id"],
        acceptance_id=acceptance["id"],
        paid=paid,
        basis={
            "acceptance_id": acceptance["id"],
            "report_id": report["id"],
            "work_order_id": wo["id"],
            "actual_operator_id": report["actual_operator_id"],
            "delivery_mode": report["delivery_mode"],
            "operation_ids": [op["id"] for op in operation_rows],
            "attendance_count_not_a_basis": True,
        },
    )


def pay_fee(
    db: sqlite3.Connection,
    fee_id: str,
    *,
    at: Any = None,
    payment_reference: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    fee = _one(db, "SELECT * FROM cd_fees WHERE id=?", (fee_id,))
    if not fee:
        return deny("unknown_fee", fee_id=fee_id)
    if fee["status"] == "paid":
        return ok("fee_already_paid", fee_id=fee_id, duplicate=True)
    if fee["status"] in {"refunded", "void"}:
        return deny("fee_not_payable", status=fee["status"])
    db.execute(
        "UPDATE cd_fees SET status='paid', paid_at=?, payment_reference=? WHERE id=?",
        (at, payment_reference or f"payment-{fee_id}-{at}", fee_id),
    )
    _audit(db, "fee_paid", "fee", fee_id, {"amount": fee["amount"]}, at=at)
    _commit(db)
    return ok("fee_paid", fee_id=fee_id, paid_at=at)


def refund_fee(
    db: sqlite3.Connection,
    fee_id: str,
    *,
    reason: str,
    at: Any = None,
    amount: Optional[float] = None,
    refund_id: Optional[str] = None,
    requested_by: Optional[str] = None,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    fee = _one(db, "SELECT * FROM cd_fees WHERE id=?", (fee_id,))
    if not fee:
        return deny("unknown_fee", fee_id=fee_id)

    refund_id = refund_id or _next_id(db, "cd_fee_refunds", "refund")
    existing_refund = _one(db, "SELECT * FROM cd_fee_refunds WHERE id=?", (refund_id,))
    if existing_refund:
        return ok("refund_reused", refund=_loads(_dumps(existing_refund)))

    refundable = float(fee["amount"]) - float(fee["refunded_amount"])
    refund_amount = refundable if amount is None else amount
    if refund_amount <= 0 or refund_amount > refundable + 1e-9:
        return deny("invalid_refund_amount", fee_id=fee_id, refundable=refundable, requested=refund_amount)
    if fee["status"] in {"void", "refunded"} and refundable <= 0:
        return ok("fee_already_refunded", fee_id=fee_id, duplicate=True)

    db.execute(
        "INSERT INTO cd_fee_refunds(id,fee_id,amount,reason,requested_by,at) VALUES(?,?,?,?,?,?)",
        (refund_id, fee_id, refund_amount, reason, requested_by, at),
    )
    new_refunded = float(fee["refunded_amount"]) + refund_amount
    status = "refunded" if new_refunded + 1e-9 >= float(fee["amount"]) else "partially_refunded"
    if fee["status"] == "due" and new_refunded + 1e-9 >= float(fee["amount"]):
        status = "void"
    db.execute("UPDATE cd_fees SET refunded_amount=?, status=? WHERE id=?", (new_refunded, status, fee_id))
    _audit(db, "fee_refunded", "fee", fee_id, {"refund_id": refund_id, "amount": refund_amount, "reason": reason}, at=at, actor=requested_by)
    _notify(
        db,
        f"recipient:{fee['payer'] or 'payer'}",
        "training_fee_refunded" if fee["assessment_id"] else "fee_refunded",
        f"费用 {fee_id} 已退回 {refund_amount:g}，原因：{reason}。",
        audience="payer",
        subject="费用退回记录",
        refs={"fee_id": fee_id, "refund_id": refund_id},
        at=at,
    )
    _commit(db)
    return ok("fee_refunded" if status == "refunded" else "fee_voided" if status == "void" else "fee_partially_refunded", fee_id=fee_id, refund_id=refund_id, amount=refund_amount, status=status)


refund_training_fee = refund_fee


def attendance_fee_attempt(db: sqlite3.Connection, *args: Any, **kwargs: Any) -> dict[str, Any]:
    """Explicit negative use case: no fee may be inferred from attendance."""
    ensure_schema(db)
    return deny(
        "attendance_cannot_support_fee",
        explanation_zh="同一出勤次数既不能推出培训机构的考核到期债权，也不能推出客户检修验收债权。",
    )


# ---------------------------------------------------------------------------
# Revocation, traceable reviews and customer notices
# ---------------------------------------------------------------------------


def _descendant_qualification_ids(db: sqlite3.Connection, qualification_id: str) -> list[str]:
    result = [qualification_id]
    frontier = [qualification_id]
    while frontier:
        qid = frontier.pop()
        children = _all(db, "SELECT id FROM cd_qualifications WHERE source_qualification_id=?", (qid,))
        for child in children:
            if child["id"] not in result:
                result.append(child["id"])
                frontier.append(child["id"])
    return result


def revoke_assessment(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    revoked_by: str,
    reason: str,
    at: Any = None,
    authority: str = "proctor",
    revocation_id: Optional[str] = None,
) -> dict[str, Any]:
    """Revoke a signed report and open reviews for every past independent job."""
    ensure_schema(db)
    at = _now(at)
    authority = {
        "invigilator": "proctor",
        "examiner": "proctor",
        "certification_body": "issuer",
        "certifier": "issuer",
        "organization": "issuer",
    }.get(authority, authority)
    if authority not in {"proctor", "issuer"}:
        return deny("unknown_revocation_authority", authority=authority)
    assessment = _one(db, "SELECT * FROM cd_assessments WHERE id=?", (assessment_id,))
    report = _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment_id,))
    if not assessment or not report:
        return deny("signed_assessment_required", assessment_id=assessment_id)
    if report["status"] == "revoked":
        existing_reviews = _all(db, "SELECT * FROM cd_reviews WHERE assessment_id=?", (assessment_id,))
        return ok("assessment_already_revoked", duplicate=True, assessment_id=assessment_id, reviews=existing_reviews)

    if authority == "proctor" and revoked_by != report["proctor"]:
        return deny("only_original_proctor_may_withdraw_attestation", expected=report["proctor"], provided=revoked_by)
    if authority == "issuer" and revoked_by != assessment["issuer"]:
        return deny("only_issuer_may_revoke_certification", expected=assessment["issuer"], provided=revoked_by)

    db.execute(
        "UPDATE cd_reports SET status='revoked', revoked_by=?, revoked_at=?, revocation_reason=? WHERE id=?",
        (revoked_by, at, reason, report["id"]),
    )
    db.execute("UPDATE cd_assessments SET status='revoked', updated_at=? WHERE id=?", (at, assessment_id))
    quals = _all(db, "SELECT * FROM cd_qualifications WHERE assessment_id=?", (assessment_id,))
    all_qids: list[str] = []
    for q in quals:
        for qid in _descendant_qualification_ids(db, q["id"]):
            if qid not in all_qids:
                all_qids.append(qid)
        db.execute(
            "UPDATE cd_qualifications SET status='revoked', revoked_by=?, revoked_at=?, revocation_reason=? WHERE id=?",
            (revoked_by, at, reason, q["id"]),
        )
    # Revoke derived model scopes too.
    for qid in all_qids:
        db.execute(
            "UPDATE cd_qualifications SET status='revoked', revoked_by=?, revoked_at=?, revocation_reason=? WHERE id=? AND status!='revoked'",
            (revoked_by, at, reason, qid),
        )

    reviews: list[dict[str, Any]] = []
    notifications: list[dict[str, Any]] = []
    if all_qids:
        placeholders = ",".join("?" for _ in all_qids)
        work_orders = _all(
            db,
            f"""SELECT * FROM cd_work_orders
                WHERE delivery_mode='independent_certified'
                  AND qualification_id IN ({placeholders})
                ORDER BY id""",
            tuple(all_qids),
        )
        for wo in work_orders:
            existing_review = _one(
                db,
                "SELECT * FROM cd_reviews WHERE assessment_id=? AND work_order_id=? AND reason_code='assessment_revoked'",
                (assessment_id, wo["id"]),
            )
            if existing_review:
                reviews.append(existing_review)
                continue
            maintenance_report = _one(db, "SELECT * FROM cd_reports_maintenance WHERE work_order_id=?", (wo["id"],))
            acceptance = _one(db, "SELECT * FROM cd_acceptances WHERE work_order_id=?", (wo["id"],))
            review_id = f"review-{wo['id']}-{assessment_id}"
            db.execute(
                """INSERT INTO cd_reviews
                   (id,reason_code,assessment_id,qualification_id,work_order_id,report_id,acceptance_id,
                    person,customer,status,reason,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    review_id,
                    "assessment_revoked",
                    assessment_id,
                    wo["qualification_id"],
                    wo["id"],
                    maintenance_report and maintenance_report["id"],
                    acceptance and acceptance["id"],
                    wo["operator_id"],
                    wo["customer"],
                    "open",
                    reason,
                    at,
                ),
            )
            if maintenance_report:
                db.execute(
                    "UPDATE cd_reports_maintenance SET review_status='open' WHERE id=?",
                    (maintenance_report["id"],),
                )
            review = _one(db, "SELECT * FROM cd_reviews WHERE id=?", (review_id,))
            reviews.append(review)
            customer_note = _notify(
                db,
                f"customer:{wo['customer'] or 'unknown'}",
                "independent_work_review_notice",
                (
                    f"考核报告 {assessment_id} 已被{authority} {revoked_by} 撤销，原因：{reason}。"
                    f"对应检修工单 {wo['id']} 已建立复核 {review_id}；既有报告不删除，"
                    "我们将复核当时作业证据并告知结论。"
                ),
                audience="customer",
                subject="既往独立高压作业复核与客户通知",
                refs={
                    "review_id": review_id,
                    "work_order_id": wo["id"],
                    "assessment_id": assessment_id,
                    "report_id": maintenance_report and maintenance_report["id"],
                    "acceptance_id": acceptance and acceptance["id"],
                    "signed_report": bool(acceptance),
                },
                at=at,
                notification_id=f"notice-review-{review_id}",
            )
            notifications.append(customer_note)
            worker_note = _notify(
                db,
                f"worker:{wo['operator_id']}",
                "qualification_revoked_review",
                (
                    f"你的考核 {assessment_id} 已撤销；历史出勤和训练记录仍保留。"
                    f"工单 {wo['id']} 进入复核 {review_id}，复核完成前不得独立作业。"
                ),
                audience="worker",
                subject="资格撤销与既往作业复核",
                refs={"review_id": review_id, "work_order_id": wo["id"], "assessment_id": assessment_id},
                at=at,
                notification_id=f"notice-worker-{review_id}",
            )
            notifications.append(worker_note)

    _audit(
        db,
        "assessment_revoked_reviews_opened",
        "assessment",
        assessment_id,
        {"revoked_by": revoked_by, "authority": authority, "reason": reason, "review_count": len(reviews)},
        at=at,
        actor=revoked_by,
    )
    _commit(db)
    return ok(
        "assessment_revoked",
        assessment_id=assessment_id,
        qualification_ids=all_qids,
        reviews=reviews,
        notifications=notifications,
        historical_training_preserved=True,
    )


withdraw_by_proctor = revoke_assessment


def open_review(
    db: sqlite3.Connection,
    review_id: str,
    *,
    reason: str,
    opened_by: str,
    at: Any = None,
    reason_code: str = "manual_quality_review",
    work_order_id: Optional[str] = None,
    assessment_id: Optional[str] = None,
    qualification_id: Optional[str] = None,
    notify_customer: bool = False,
) -> dict[str, Any]:
    """Open a traceable post-acceptance review without rewriting history."""
    ensure_schema(db)
    at = _now(at)
    existing = _one(db, "SELECT * FROM cd_reviews WHERE id=?", (review_id,))
    if existing:
        return ok("review_already_open", review=existing, duplicate=True)

    wo = _load_work_order(db, work_order_id) if work_order_id else None
    report = wo and _one(db, "SELECT * FROM cd_reports_maintenance WHERE work_order_id=?", (work_order_id,))
    acceptance = report and _one(db, "SELECT * FROM cd_acceptances WHERE report_id=?", (report["id"],))
    person = wo and wo["operator_id"]
    customer = wo and wo["customer"]
    if qualification_id is None and wo:
        qualification_id = wo["qualification_id"]
    if assessment_id is None and qualification_id:
        q = _one(db, "SELECT assessment_id FROM cd_qualifications WHERE id=?", (qualification_id,))
        assessment_id = q and q["assessment_id"]

    db.execute(
        """INSERT INTO cd_reviews
           (id,reason_code,assessment_id,qualification_id,work_order_id,report_id,acceptance_id,
            person,customer,status,reason,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,'open',?,?)""",
        (
            review_id,
            reason_code,
            assessment_id,
            qualification_id,
            work_order_id,
            report and report["id"],
            acceptance and acceptance["id"],
            person,
            customer,
            reason,
            at,
        ),
    )
    if report:
        db.execute("UPDATE cd_reports_maintenance SET review_status='open' WHERE id=?", (report["id"],))
    notification = None
    if notify_customer and customer:
        notification = _notify(
            db,
            f"customer:{customer}",
            "review_opened",
            f"工单 {work_order_id} 已建立复核 {review_id}：{reason}。记录不会被删除。",
            audience="customer",
            subject="检修报告复核通知",
            refs={"review_id": review_id, "work_order_id": work_order_id, "report_id": report and report["id"]},
            at=at,
            notification_id=f"notice-review-open-{review_id}",
        )
    _audit(db, "review_opened", "review", review_id, {"reason_code": reason_code, "opened_by": opened_by}, at=at, actor=opened_by)
    _commit(db)
    return ok("review_opened", review_id=review_id, notification=notification)


def complete_review(
    db: sqlite3.Connection,
    review_id: str,
    *,
    outcome: str,
    reviewer: str,
    at: Any = None,
    corrective_action: Optional[str] = None,
    notify_customer: bool = True,
) -> dict[str, Any]:
    ensure_schema(db)
    at = _now(at)
    review = _one(db, "SELECT * FROM cd_reviews WHERE id=?", (review_id,))
    if not review:
        return deny("unknown_review", review_id=review_id)
    if review["status"] == "completed":
        return ok("review_already_completed", review=review, duplicate=True)
    allowed_outcomes = {"confirmed_valid", "rework_required", "report_corrected", "qualification_upheld"}
    if outcome not in allowed_outcomes:
        return deny("invalid_review_outcome", allowed=sorted(allowed_outcomes))
    db.execute(
        "UPDATE cd_reviews SET status='completed', outcome=?, reviewer=?, completed_at=?, corrective_action=? WHERE id=?",
        (outcome, reviewer, at, corrective_action, review_id),
    )
    if review["report_id"]:
        review_status = "closed_confirmed" if outcome in {"confirmed_valid", "qualification_upheld"} else outcome
        db.execute("UPDATE cd_reports_maintenance SET review_status=? WHERE id=?", (review_status, review["report_id"]))
    notification = None
    if notify_customer and review["customer"]:
        notification = _notify(
            db,
            f"customer:{review['customer']}",
            "review_completed",
            f"复核 {review_id} 已完成，结论：{outcome}。{corrective_action or ''}",
            audience="customer",
            subject="检修报告复核结论",
            refs={"review_id": review_id, "outcome": outcome, "work_order_id": review["work_order_id"]},
            at=at,
            notification_id=f"notice-review-done-{review_id}",
        )
    _audit(db, "review_completed", "review", review_id, {"outcome": outcome, "reviewer": reviewer}, at=at, actor=reviewer)
    _commit(db)
    return ok("review_completed", review_id=review_id, outcome=outcome, notification=notification)


# ---------------------------------------------------------------------------
# Dossier / causal chain verification
# ---------------------------------------------------------------------------


def delivery_dossier(db: sqlite3.Connection, work_order_id: str) -> dict[str, Any]:
    ensure_schema(db)
    wo = _load_work_order(db, work_order_id)
    if not wo:
        return deny("unknown_work_order", work_order_id=work_order_id)
    ops = _all(db, "SELECT * FROM cd_operations WHERE work_order_id=? ORDER BY seq", (work_order_id,))
    report = _one(db, "SELECT * FROM cd_reports_maintenance WHERE work_order_id=?", (work_order_id,))
    acceptance = report and _one(db, "SELECT * FROM cd_acceptances WHERE report_id=?", (report["id"],))
    q = wo["qualification_id"] and _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (wo["qualification_id"],))
    assessment = q and q["assessment_id"] and _one(db, "SELECT * FROM cd_assessments WHERE id=?", (q["assessment_id"],))
    assessment_report = assessment and _one(db, "SELECT * FROM cd_reports WHERE assessment_id=?", (assessment["id"],))
    evidence = assessment and _all(db, "SELECT * FROM cd_evidence WHERE assessment_id=?", (assessment["id"],))
    people = [wo["operator_id"]]
    if wo.get("apprentice_id") and wo["apprentice_id"] not in people:
        people.append(wo["apprentice_id"])
    attendance = _all(
        db,
        f"SELECT * FROM training_events WHERE person IN ({','.join('?' for _ in people)})",
        tuple(people),
    )
    fees = _all(db, "SELECT * FROM cd_fees WHERE work_order_id=? OR assessment_id=?", (work_order_id, wo["qualification_id"] and q["assessment_id"]))
    reviews = _all(db, "SELECT * FROM cd_reviews WHERE work_order_id=?", (work_order_id,))
    notifications = _all(db, "SELECT * FROM cd_notifications WHERE json_extract(refs, '$.work_order_id')=?", (work_order_id,))
    mentorship = wo["mentorship_id"] and _load_mentorship(db, wo["mentorship_id"])
    mentor_evidence = wo["mentorship_id"] and _all(db, "SELECT * FROM cd_mentorship_evidence WHERE mentorship_id=?", (wo["mentorship_id"],))
    chain = verify_delivery_chain(db, work_order_id)
    return {
        "decision": "dossier",
        "work_order": wo,
        "attendance": attendance,
        "assessment": assessment,
        "assessment_report": assessment_report,
        "assessment_evidence": evidence,
        "qualification": q,
        "mentorship": mentorship,
        "mentor_evidence": mentor_evidence,
        "operations": ops,
        "report": report,
        "acceptance": acceptance,
        "fees": fees,
        "reviews": reviews,
        "notifications": notifications,
        "chain_verification": chain,
    }


def verify_delivery_chain(
    db: sqlite3.Connection,
    work_order_id: str,
    *,
    require_customer_acceptance: bool = True,
) -> dict[str, Any]:
    """Verify causal links, report snapshot and current safety/review status."""
    ensure_schema(db)
    errors: list[str] = []
    wo = _load_work_order(db, work_order_id)
    if not wo:
        return {"verified": False, "errors": ["unknown_work_order"]}

    ops = _all(db, "SELECT * FROM cd_operations WHERE work_order_id=? ORDER BY seq", (work_order_id,))
    done = {op["item"] for op in ops}
    missing = set(wo["required_items"]) - done
    if missing:
        errors.append("missing_operations:" + ",".join(sorted(missing)))

    qid = wo["qualification_id"]
    q = qid and _one(db, "SELECT * FROM cd_qualifications WHERE id=?", (qid,))
    if not q:
        errors.append("missing_actual_operator_qualification")
    else:
        if q["person"] != wo["operator_id"]:
            errors.append("qualification_operator_mismatch")
        if q["equipment_model"] != wo["equipment_model"] or q["position"] != wo["position"]:
            errors.append("qualification_scope_mismatch")
        if q["status"] != "active":
            errors.append(f"qualification_{q['status']}")
        original_report = _one(db, "SELECT * FROM cd_reports WHERE id=?", (q["report_id"],))
        if not original_report or original_report["assessment_id"] != q["assessment_id"]:
            errors.append("qualification_without_original_report")
        elif original_report["status"] == "revoked":
            errors.append("original_assessment_report_revoked")
        evidence = _all(db, "SELECT id FROM cd_evidence WHERE assessment_id=?", (q["assessment_id"],))
        if not evidence:
            errors.append("assessment_without_original_evidence")

    for op in ops:
        if op["status"] != "recorded":
            errors.append(f"operation_{op['id']}_not_recorded")
        if op["actor_id"] != wo["operator_id"]:
            errors.append(f"operation_{op['id']}_actor_not_actual_operator")
        if q:
            allowed = _loads(q["allowed_items"], [])
            if op["item"] not in allowed:
                errors.append(f"operation_{op['id']}_outside_qualification_scope")
            if not (q["valid_from"] <= op["at"] <= q["valid_until"]):
                errors.append(f"operation_{op['id']}_outside_qualification_validity")

    if wo["delivery_mode"] == "senior_operator_with_apprentice":
        mentorship = _load_mentorship(db, wo["mentorship_id"]) if wo["mentorship_id"] else None
        if not mentorship:
            errors.append("missing_mentorship_record")
        else:
            if mentorship["status"] != "completed":
                errors.append("mentorship_not_completed")
            mentor_items = {
                row["item"]
                for row in _all(
                    db,
                    "SELECT DISTINCT item FROM cd_mentorship_evidence WHERE mentorship_id=?",
                    (mentorship["id"],),
                )
            }
            missing_evidence = set(wo["required_items"]) - mentor_items
            if missing_evidence:
                errors.append("missing_mentorship_evidence:" + ",".join(sorted(missing_evidence)))

    maintenance_report = _one(db, "SELECT * FROM cd_reports_maintenance WHERE work_order_id=?", (work_order_id,))
    acceptance = maintenance_report and _one(db, "SELECT * FROM cd_acceptances WHERE report_id=?", (maintenance_report["id"],))
    if not maintenance_report:
        errors.append("missing_maintenance_report")
    else:
        snapshot = _loads(maintenance_report["payload"], {})
        if _hash_snapshot(snapshot) != maintenance_report["report_hash"]:
            errors.append("maintenance_report_hash_mismatch")
        if snapshot.get("actual_operations") != ops:
            errors.append("operations_changed_after_report_issuance")
        if maintenance_report["review_status"] == "open":
            errors.append("maintenance_report_review_open")
        elif maintenance_report["review_status"] not in {"none", "closed_confirmed"}:
            errors.append("maintenance_report_review_" + maintenance_report["review_status"])
        if not acceptance and maintenance_report["status"] == "accepted":
            errors.append("accepted_status_without_acceptance")
    if require_customer_acceptance and not acceptance:
        errors.append("missing_customer_acceptance")
    if acceptance and not maintenance_report:
        errors.append("acceptance_without_report")

    audit_chain = verify_audit_chain(db)
    if not audit_chain["verified"]:
        errors.append("audit_hash_chain_broken")
    return {"verified": not errors, "errors": errors, "work_order_id": work_order_id, "operation_count": len(ops)}


# ---------------------------------------------------------------------------
# Convenience high-level exam path
# ---------------------------------------------------------------------------


def certify_by_practical_exam(
    db: sqlite3.Connection,
    assessment_id: str,
    *,
    person: str,
    proctor: str,
    issuer: str,
    valid_from: Any,
    valid_until: Any,
    equipment_model: str,
    at: Any = None,
    position: str = DEFAULT_POSITION,
    operation_level: str = DEFAULT_LEVEL,
    items: Optional[list[str] | tuple[str, ...]] = None,
    video_id: Optional[str] = None,
    video_sha256: Optional[str] = None,
    receive: bool = True,
    fee_id: Optional[str] = None,
    fee_amount: Optional[float] = None,
) -> dict[str, Any]:
    """Complete a normal onsite practical exam and optionally book its fee."""
    items = tuple(items or CORE_ITEMS)
    opened = open_assessment(
        db,
        assessment_id,
        person=person,
        proctor=proctor,
        issuer=issuer,
        valid_from=valid_from,
        valid_until=valid_until,
        equipment_model=equipment_model,
        position=position,
        operation_level=operation_level,
        items=items,
        at=at,
    )
    if not opened.get("allowed") and opened["decision"] not in {"assessment_reused"}:
        return opened
    video_id = video_id or f"video-{assessment_id}"
    saved = save_evidence(
        db,
        video_id,
        assessment_id,
        kind="video",
        sha256=video_sha256 or f"sha-{assessment_id}-video",
        at=at,
    )
    if not saved.get("allowed"):
        return saved
    for index, item in enumerate(items, 1):
        recorded = record_item_result(
            db,
            assessment_id,
            item,
            True,
            at=(at if at is None else at + index),
            evidence_id=video_id,
        )
        if not recorded.get("allowed"):
            return recorded
    signed = sign_assessment(db, assessment_id, at=(at if at is None else at + len(items) + 1))
    if not signed.get("allowed"):
        return signed
    if receive:
        recovered = receive_assessment_result(db, assessment_id, at=(at if at is None else at + len(items) + 2))
        if not recovered.get("allowed"):
            return recovered
    else:
        recovered = issue_qualification(db, assessment_id, at=at)
    fee = None
    if fee_id and fee_amount is not None:
        fee = create_training_fee(db, fee_id, assessment_id, fee_amount, at=at, paid=True)
    return ok(
        "technician_certified",
        assessment_id=assessment_id,
        sign=signed,
        recovery=recovered,
        qualification=recovered.get("qualification") or recovered.get("qualification"),
        fee=fee,
    )


# ---------------------------------------------------------------------------
# DB-first facade and event dispatcher
# ---------------------------------------------------------------------------


class CompetencyDelivery:
    """Object facade. It forwards calls to the DB-first functions."""

    def __init__(self, db: Optional[sqlite3.Connection | str] = None, path: str = ":memory:"):
        if db is None:
            db = sqlite3.connect(path)
        elif isinstance(db, str):
            db = sqlite3.connect(db)
        if getattr(db, "row_factory", None) is None:
            db.row_factory = sqlite3.Row
        self.db = ensure_schema(db)

    def __getattr__(self, name: str) -> Any:
        fn = globals().get(name)
        if callable(fn):
            def bound(*args: Any, **kwargs: Any) -> Any:
                return fn(self.db, *args, **kwargs)

            return bound
        raise AttributeError(name)

    def close(self) -> None:
        self.db.close()


CompetencyCourt = CompetencyDelivery
CompetencyService = CompetencyDelivery
CompetencyLedger = CompetencyDelivery

# Discoverable verb aliases for callers and tests using different vocabulary.
record_attendance = attend
add_attendance = attend
mark_attendance = attend
can_independently_execute = can_work_independently
is_independently_authorized = can_work_independently
dispatch_work = authorize_independent_work
schedule_independent_operator = authorize_independent_work
assign_mentor = assign_mentored_delivery
record_work_operation = record_operation
submit_operation = record_operation
issue_report = issue_maintenance_report
create_review = open_review
charge_training_fee = create_training_fee
charge_maintenance_fee = create_maintenance_fee


_OP_ALIASES = {
    "attend": attend,
    "adjudicate": adjudicate_independent_work,
    "adjudicate_independent_work": adjudicate_independent_work,
    "can_work_independently": can_work_independently,
    "certify_by_practical_exam": certify_by_practical_exam,
    "practical_certification_path": certify_by_practical_exam,
    "open_assessment": open_assessment,
    "save_evidence": save_evidence,
    "record_item_result": record_item_result,
    "sign_assessment": sign_assessment,
    "submit_assessment": submit_assessment,
    "receive_assessment_result": receive_assessment_result,
    "retry_assessment_upload": retry_assessment_upload,
    "issue_qualification": issue_qualification,
    "recognize_equipment_scope": recognize_equipment_scope,
    "change_equipment_model": recognize_equipment_scope,
    "open_work_order": open_work_order,
    "authorize_independent_work": authorize_independent_work,
    "assign_mentored_delivery": assign_mentored_delivery,
    "senior_departure": senior_departure,
    "depart_senior": depart_senior,
    "record_senior_departure": record_senior_departure,
    "senior_return": senior_return,
    "return_senior": return_senior,
    "record_senior_return": record_senior_return,
    "record_operation": record_operation,
    "perform_operation": perform_operation,
    "record_apprentice_evidence": record_apprentice_evidence,
    "record_observation": record_observation,
    "complete_mentorship": complete_mentorship,
    "issue_maintenance_report": issue_maintenance_report,
    "accept_maintenance_report": accept_maintenance_report,
    "accept_report": accept_report,
    "create_fee": create_fee,
    "create_training_fee": create_training_fee,
    "create_assessment_fee": create_assessment_fee,
    "create_maintenance_fee": create_maintenance_fee,
    "pay_fee": pay_fee,
    "refund_fee": refund_fee,
    "refund_training_fee": refund_training_fee,
    "revoke_assessment": revoke_assessment,
    "withdraw_by_proctor": withdraw_by_proctor,
    "open_review": open_review,
    "complete_review": complete_review,
    "verify_delivery_chain": verify_delivery_chain,
    "delivery_dossier": delivery_dossier,
}


def run(db: sqlite3.Connection, event: dict[str, Any]) -> dict[str, Any]:
    """Repository-style event entry point: ``run(db, {"op": "...", ...})``."""
    ensure_schema(db)
    event = dict(event)
    op = event.pop("op", None) or event.pop("command", None)
    if not op:
        raise ValueError("competency event requires op")
    fn = _OP_ALIASES.get(op) or _OP_ALIASES.get(op.replace("-", "_"))
    if fn is None:
        raise ValueError(f"unknown competency op: {op}")
    event.pop("subsystem", None)
    # Most aggregate commands use ``id`` as their first positional ID.
    id_keys = {
        "attend": "event",
        "certify_by_practical_exam": "assessment_id",
        "open_assessment": "assessment_id",
        "save_evidence": "evidence_id",
        "record_item_result": "assessment_id",
        "sign_assessment": "assessment_id",
        "submit_assessment": "assessment_id",
        "receive_assessment_result": "assessment_id",
        "retry_assessment_upload": "assessment_id",
        "issue_qualification": "assessment_id",
        "recognize_equipment_scope": "new_qualification_id",
        "open_work_order": "work_order_id",
        "authorize_independent_work": "work_order_id",
        "assign_mentored_delivery": "work_order_id",
        "record_operation": "operation_id",
        "record_apprentice_evidence": "evidence_record_id",
        "complete_mentorship": "mentorship_id",
        "issue_maintenance_report": "report_id",
        "accept_maintenance_report": "acceptance_id",
        "accept_report": "acceptance_id",
        "create_fee": "fee_id",
        "create_training_fee": "fee_id",
        "create_assessment_fee": "fee_id",
        "create_maintenance_fee": "fee_id",
        "pay_fee": "fee_id",
        "refund_fee": "fee_id",
        "refund_training_fee": "fee_id",
        "revoke_assessment": "assessment_id",
        "withdraw_by_proctor": "assessment_id",
        "open_review": "review_id",
        "complete_review": "review_id",
        "delivery_dossier": "work_order_id",
        "verify_delivery_chain": "work_order_id",
    }
    id_key = id_keys.get(op)
    if id_key and id_key not in event and "id" in event:
        event[id_key] = event.pop("id")
    if "id" in event:
        signature = inspect.signature(fn)
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        if "id" not in signature.parameters and not accepts_kwargs:
            event.pop("id")
    return fn(db, **event)
