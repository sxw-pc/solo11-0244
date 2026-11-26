"""Verifiable personnel muster, rescue handover and clearance ledger.

The old badge-count muster note treats an ``out`` row as proof that the named
badge holder is outside.  A badge only proves that a credential was read at a
gate.  This module therefore keeps identity, credentials, source observations,
search assignments, personal assembly receipts and medical handovers as
separate append-only facts.

The module is intentionally standard-library only.  It can be used with plain
Python objects; :func:`legacy_balance_clear` bridges to the SQLite badge schema
used by :mod:`tenurecourt.site_alarm` and :mod:`tenurecourt.muster_note`.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Optional

PENDING = "pending"
ASSEMBLED = "assembled"
MEDICAL_HANDOVER = "medical_handover"

SAFE_STATUSES = {ASSEMBLED, MEDICAL_HANDOVER}

STATUS_ZH = {
    PENDING: "待确认",
    ASSEMBLED: "集合点本人签认",
    MEDICAL_HANDOVER: "救援-医疗可核实交接",
}

FINAL_SCOPES = {"final", "all_clear", "muster_complete", "muster"}
CONTROLLED_SCOPES = {"controlled", "controlled_isolation", "scene_handover", "isolation"}


class PersonnelMuster:
    """Append-only personnel accounting board for one emergency."""

    def __init__(self, incident_id: str = "muster-1", leak_at: Optional[float] = None):
        self.incident_id = incident_id
        self.leak_at = leak_at

        self.people: dict[str, dict[str, Any]] = {}
        self.cards: dict[str, dict[str, Any]] = {}
        self.evidence: dict[str, dict[str, Any]] = {}
        self.episodes: dict[str, dict[str, Any]] = {}
        self.assignments: dict[str, dict[str, Any]] = {}
        self.active_assignment_id: dict[str, str] = {}
        self.disputes: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, dict[str, Any]] = {}
        self.certificates: dict[str, dict[str, Any]] = {}
        self.safety_reset: Optional[dict[str, Any]] = None
        self.isolation: Optional[dict[str, Any]] = None
        self.camera_calibrations: dict[str, dict[str, Any]] = {}
        self.journal: list[dict[str, Any]] = []
        self._seq = 0

    # ------------------------------------------------------------------
    # Small utilities
    # ------------------------------------------------------------------
    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @staticmethod
    def _normalize_scope(scope: str) -> str:
        if scope in FINAL_SCOPES:
            return "final"
        if scope in CONTROLLED_SCOPES:
            return "controlled"
        return scope

    def _journal_event(self, operation: str, payload: dict[str, Any], result: dict[str, Any]) -> None:
        seq = len(self.journal) + 1
        previous_hash = self.journal[-1]["record_hash"] if self.journal else "0" * 64
        body = json.dumps(
            {"seq": seq, "operation": operation, "payload": payload, "result_decision": result.get("decision")},
            sort_keys=True,
            ensure_ascii=False,
            default=str,
        )
        record_hash = hashlib.sha256((previous_hash + body).encode("utf-8")).hexdigest()
        self.journal.append(
            {
                "seq": seq,
                "operation": operation,
                "payload": copy.deepcopy(payload),
                "decision": result.get("decision"),
                "previous_hash": previous_hash,
                "record_hash": record_hash,
            }
        )

    def verify_journal_chain(self) -> bool:
        previous_hash = "0" * 64
        for record in self.journal:
            if record["previous_hash"] != previous_hash:
                return False
            body = json.dumps(
                {
                    "seq": record["seq"],
                    "operation": record["operation"],
                    "payload": record["payload"],
                    "result_decision": record["decision"],
                },
                sort_keys=True,
                ensure_ascii=False,
                default=str,
            )
            expected = hashlib.sha256((previous_hash + body).encode("utf-8")).hexdigest()
            if expected != record["record_hash"]:
                return False
            previous_hash = record["record_hash"]
        return True

    @staticmethod
    def _unknown(person_id: Optional[str]) -> dict[str, Any]:
        return {"decision": "unknown_person", "person_id": person_id}

    def _public_result(self, result: dict[str, Any]) -> dict[str, Any]:
        return copy.deepcopy(result)

    # ------------------------------------------------------------------
    # Identity, employment, escort and credentials
    # ------------------------------------------------------------------
    def register_person(
        self,
        person_id: str,
        full_name: str,
        role: str,
        *,
        employer: Optional[str] = None,
        employment_verified: bool = False,
        identity_verified: bool = True,
        escort_id: Optional[str] = None,
        escort_qualified: bool = False,
        shift: Optional[str] = None,
        entry_area: Optional[str] = None,
        authorized_areas: Optional[list[str]] = None,
        census_required: bool = True,
        at: float = 0,
        **details: Any,
    ) -> dict[str, Any]:
        """Fix a stable human identity.

        Duplicate legal names are legal: ``person_id`` is the only merge key.
        """
        payload = {
            "person_id": person_id,
            "full_name": full_name,
            "role": role,
            "employer": employer,
            "escort_id": escort_id,
            "shift": shift,
            "entry_area": entry_area,
            "at": at,
        }

        if person_id in self.people:
            old = self.people[person_id]
            same = (
                old["full_name"] == full_name
                and old["role"] == role
                and old.get("employer") == employer
                and old.get("shift") == shift
                and old.get("entry_area") == entry_area
            )
            result = {"decision": "reused" if same else "identity_conflict", "person_id": person_id}
            self._journal_event("register_person", payload, result)
            return self._public_result(result)

        errors: list[str] = []
        if not full_name or not str(full_name).strip():
            errors.append("legal_name_required")
        if not identity_verified:
            errors.append("real_name_identity_not_verified")
        if role in {"employee", "contractor"}:
            if not employer:
                errors.append("employer_required")
            if not employment_verified:
                errors.append("employment_relation_not_verified")
        if role == "visitor" and not escort_id:
            errors.append("visitor_escort_required")
        if escort_id is not None:
            escort = self.people.get(escort_id)
            if not escort:
                errors.append("escort_identity_unknown")
            elif not escort.get("escort_authorized"):
                errors.append("escort_not_authorized")
        if not shift:
            errors.append("shift_required")
        if not entry_area:
            errors.append("fixed_entry_area_required")
        if errors:
            result = {"decision": "identity_incomplete", "person_id": person_id, "errors": errors}
            self._journal_event("register_person", payload, result)
            return self._public_result(result)

        escort = self.people.get(escort_id) if escort_id else None
        person = {
            "person_id": person_id,
            "full_name": full_name,
            "role": role,
            "roles": [role] + (["escort"] if escort_qualified else []),
            "employer": employer,
            "employment_verified": employment_verified,
            "identity_verified": identity_verified,
            "escort_id": escort_id,
            # This flag means the person may act as an escort. Having an
            # authorized escort does not itself authorize a visitor to escort.
            "escort_authorized": bool(escort_qualified),
            "escort_authorizations": [],
            "shift": shift,
            "entry_area": entry_area,
            "authorized_areas": list(authorized_areas or ([entry_area] if entry_area else [])),
            "census_required": census_required,
            "registered_at": at,
            "details": copy.deepcopy(details),
        }
        self.people[person_id] = person
        result = {"decision": "registered", "person_id": person_id}
        self._journal_event("register_person", payload, result)
        return self._public_result(result)

    def people_named(self, full_name: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(p) for p in self.people.values() if p["full_name"] == full_name]

    def authorize_escort(
        self,
        person_id: str,
        *,
        at: float,
        authorized_by: str,
        briefing_id: Optional[str] = None,
        training_verified: bool = False,
        areas: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Convert a vetted visitor into an authorized visitor escort."""
        person = self.people.get(person_id)
        if not person:
            return self._unknown(person_id)
        if person["role"] != "visitor" and not person["escort_authorized"]:
            return {"decision": "escort_authorization_denied", "reason": "not_a_visitor", "person_id": person_id}
        if not briefing_id and not training_verified:
            return {
                "decision": "escort_authorization_denied",
                "reason": "briefing_or_training_required",
                "person_id": person_id,
            }

        authorization = {
            "at": at,
            "authorized_by": authorized_by,
            "briefing_id": briefing_id,
            "training_verified": training_verified,
            "areas": list(areas or person.get("authorized_areas", [])),
        }
        person["escort_authorized"] = True
        if "escort" not in person["roles"]:
            person["roles"].append("escort")
        person["escort_authorizations"].append(authorization)
        result = {"decision": "escort_authorized", "person_id": person_id, "authorization": authorization}
        self._journal_event("authorize_escort", {"person_id": person_id, "at": at}, result)
        return self._public_result(result)

    def issue_card(
        self,
        card_id: str,
        person_id: str,
        at: float = 0,
        *,
        replaces: Optional[str] = None,
        status: str = "active",
    ) -> dict[str, Any]:
        person = self.people.get(person_id)
        if not person:
            return self._unknown(person_id)
        if card_id in self.cards:
            old = self.cards[card_id]
            decision = "reused" if old["person_id"] == person_id else "card_identity_conflict"
            return {"decision": decision, "card_id": card_id, "person_id": person_id}

        old_card = None
        if replaces is not None:
            old_card = self.cards.get(replaces)
            if not old_card:
                return {"decision": "unknown_previous_card", "card_id": card_id, "replaces": replaces}
            if old_card["person_id"] != person_id:
                return {"decision": "card_reissue_identity_conflict", "card_id": card_id, "replaces": replaces}
            old_card["status"] = "reissued"
            old_card["revoked_at"] = at

        self.cards[card_id] = {
            "card_id": card_id,
            "person_id": person_id,
            "status": status,
            "issued_at": at,
            "reissue_of": replaces,
        }
        result = {"decision": "card_issued", "card_id": card_id, "person_id": person_id, "replaces": replaces}
        self._journal_event("issue_card", {"card_id": card_id, "person_id": person_id}, result)
        return self._public_result(result)

    # ------------------------------------------------------------------
    # Evidence ledger
    # ------------------------------------------------------------------
    def _add_evidence(
        self,
        evidence_id: str,
        source: str,
        observed_at: float,
        *,
        person_id: Optional[str] = None,
        card_id: Optional[str] = None,
        area: Optional[str] = None,
        recorded_at: Optional[float] = None,
        direct_identity: bool = False,
        location_credible: Optional[bool] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        if evidence_id in self.evidence:
            return {"decision": "duplicate_evidence_id", "evidence_id": evidence_id}

        episode_number = None
        if person_id is not None and person_id in self.episodes:
            episode_number = self.episodes[person_id]["number"]

        evidence = {
            "evidence_id": evidence_id,
            "seq": self._next_seq(),
            "source": source,
            "observed_at": observed_at,
            "recorded_at": observed_at if recorded_at is None else recorded_at,
            "person_id": person_id,
            "card_id": card_id,
            "area": area,
            "direct_identity": direct_identity,
            # A source that does not identify the person cannot prove their
            # physical location, even when it names their assigned card.
            "location_credible": direct_identity if location_credible is None else location_credible,
            "episode_number": episode_number,
            "adjudication": "active",
            "adjudicated_actual_person_id": None,
            "proxy": False,
            "dispute_ids": [],
            "details": copy.deepcopy(details or {}),
        }
        self.evidence[evidence_id] = evidence
        return {"decision": "evidence_recorded", "evidence": copy.deepcopy(evidence)}

    def record_badge(
        self,
        event_id: str,
        card_id: str,
        direction: str,
        at: float,
        gate: Optional[str] = None,
        *,
        recorded_at: Optional[float] = None,
        identity_verified: bool = False,
        actual_person_id: Optional[str] = None,
        used_by: Optional[str] = None,
        witnessed_by: Optional[str] = None,
        proxy: Optional[bool] = None,
    ) -> dict[str, Any]:
        """Record a credential read.

        Unless a guard/biometric source actually identifies the human, this is
        a card event, not proof that the enrolled person passed the reader.
        """
        if direction not in {"in", "out"}:
            return {"decision": "invalid_badge_direction", "direction": direction}
        actual_person_id = actual_person_id or used_by
        if actual_person_id is not None and actual_person_id not in self.people:
            return self._unknown(actual_person_id)

        card = self.cards.get(card_id)
        owner_id = card["person_id"] if card else None
        if owner_id is None:
            added = self._add_evidence(
                event_id,
                "badge",
                at,
                card_id=card_id,
                area=gate,
                recorded_at=recorded_at,
                direct_identity=False,
                location_credible=False,
                details={"direction": direction, "gate": gate, "unknown_card": True},
            )
            if added["decision"] == "duplicate_evidence_id":
                return {"decision": "reused", "event_id": event_id}
            result = {"decision": "unknown_card", "event_id": event_id, "card_id": card_id}
            self._journal_event("record_badge", {"event_id": event_id, "card_id": card_id}, result)
            return result

        if actual_person_id is not None and proxy is None:
            proxy = actual_person_id != owner_id
        if proxy is None:
            proxy = not identity_verified and card.get("status") != "active"

        added = self._add_evidence(
            event_id,
            "badge",
            at,
            person_id=owner_id,
            card_id=card_id,
            area=gate,
            recorded_at=recorded_at,
            direct_identity=identity_verified,
            location_credible=identity_verified,
            details={
                "direction": direction,
                "gate": gate,
                "card_owner_id": owner_id,
                "actual_person_id": actual_person_id,
                "proxy_swipe": bool(proxy),
                "witnessed_by": witnessed_by,
                "card_status_at_read": card.get("status"),
            },
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "event_id": event_id}

        evidence = self.evidence[event_id]
        if actual_person_id:
            evidence["adjudicated_actual_person_id"] = actual_person_id
        if proxy:
            evidence["proxy"] = True

        entry_result = None
        if direction == "in":
            if identity_verified:
                entry_result = self.record_entry(
                    owner_id,
                    at,
                    gate,
                    source="verified_badge",
                    evidence_id=event_id,
                    verified=True,
                )
            elif self.episodes.get(owner_id, {}).get("state") == "accounted":
                self.open_dispute(
                    None,
                    person_id=owner_id,
                    kind="unverified_card_in_after_accounting",
                    evidence_ids=[event_id],
                    opened_at=at,
                    opened_by="muster-board",
                    question="Card returned inward after personal accounting; re-entry or card misuse must be investigated.",
                )

        result = {
            "decision": "badge_recorded",
            "event_id": event_id,
            "card_owner_id": owner_id,
            "actual_person_id": actual_person_id,
            "proxy_swipe": bool(proxy),
            "entry": entry_result,
        }
        self._journal_event("record_badge", {"event_id": event_id, "card_id": card_id, "direction": direction}, result)
        return self._public_result(result)

    def record_camera(
        self,
        event_id: str,
        person_id: str,
        camera_at: float,
        area: str,
        *,
        device_id: str,
        recorded_at: Optional[float] = None,
        offset_seconds: float = 0,
        clock_uncertainty_seconds: float = 0,
        identification_verified: bool = True,
        reviewed_by: Optional[str] = None,
        confidence: str = "verified",
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        if offset_seconds:
            self.calibrate_camera(
                device_id,
                offset_seconds=offset_seconds,
                calibrated_by=reviewed_by or "camera-ingest",
                at=recorded_at or camera_at,
                note="offset supplied with event",
            )
        added = self._add_evidence(
            event_id,
            "camera",
            camera_at,
            person_id=person_id,
            area=area,
            recorded_at=recorded_at,
            direct_identity=identification_verified,
            location_credible=identification_verified,
            details={
                "device_id": device_id,
                "displayed_at": camera_at,
                "offset_seconds": offset_seconds,
                "clock_uncertainty_seconds": clock_uncertainty_seconds,
                "reviewed_by": reviewed_by,
                "confidence": confidence,
            },
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "event_id": event_id}
        result = {"decision": "camera_recorded", "event_id": event_id, "effective_at": self.effective_time(self.evidence[event_id])}
        self._journal_event("record_camera", {"event_id": event_id, "person_id": person_id}, result)
        return self._public_result(result)

    def calibrate_camera(
        self,
        device_id: str,
        *,
        offset_seconds: float,
        calibrated_by: str,
        at: float,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        """Set true_time = displayed_camera_time + offset_seconds."""
        calibration = {
            "device_id": device_id,
            "offset_seconds": offset_seconds,
            "calibrated_by": calibrated_by,
            "at": at,
            "note": note,
        }
        self.camera_calibrations[device_id] = calibration
        result = {"decision": "camera_calibrated", **calibration}
        self._journal_event("calibrate_camera", {"device_id": device_id}, result)
        return self._public_result(result)

    def effective_time(self, evidence: dict[str, Any]) -> float:
        observed_at = evidence["observed_at"]
        if evidence["source"] != "camera":
            return observed_at
        device_id = evidence.get("details", {}).get("device_id")
        calibration = self.camera_calibrations.get(device_id) if device_id else None
        displayed_at = evidence.get("details", {}).get("displayed_at", observed_at)
        offset = calibration["offset_seconds"] if calibration else evidence.get("details", {}).get("offset_seconds", 0)
        return displayed_at + offset

    def record_radio(
        self,
        event_id: str,
        person_id: str,
        at: float,
        area: Optional[str] = None,
        *,
        responded: bool = True,
        voice_identified: bool = True,
        operator: Optional[str] = None,
        recorded_at: Optional[float] = None,
        message: Optional[str] = None,
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        added = self._add_evidence(
            event_id,
            "radio",
            at,
            person_id=person_id,
            area=area,
            recorded_at=recorded_at,
            direct_identity=voice_identified and responded,
            location_credible=bool(area and responded and voice_identified),
            details={
                "responded": responded,
                "voice_identified": voice_identified,
                "operator": operator,
                "message": message,
            },
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "event_id": event_id}
        result = {"decision": "radio_recorded", "event_id": event_id, "person_id": person_id}
        self._journal_event("record_radio", {"event_id": event_id, "person_id": person_id}, result)
        return self._public_result(result)

    def record_lost_contact(
        self,
        event_id: str,
        person_id: str,
        at: float,
        *,
        operator: str,
        recorded_at: Optional[float] = None,
        message: str = "repeated calls unanswered",
    ) -> dict[str, Any]:
        """Record a failed contact attempt without inventing a location fix."""
        return self.record_radio(
            event_id,
            person_id,
            at,
            area=None,
            responded=False,
            voice_identified=False,
            operator=operator,
            recorded_at=recorded_at,
            message=message,
        )

    def record_sighting(
        self,
        event_id: str,
        person_id: str,
        at: float,
        area: str,
        *,
        witness: str,
        source: str = "witness_report",
        identity_verified: bool = True,
        recorded_at: Optional[float] = None,
        statement: Optional[str] = None,
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        added = self._add_evidence(
            event_id,
            source,
            at,
            person_id=person_id,
            area=area,
            recorded_at=recorded_at,
            direct_identity=identity_verified,
            location_credible=identity_verified,
            details={"witness": witness, "statement": statement},
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "event_id": event_id}
        result = {"decision": "sighting_recorded", "event_id": event_id}
        self._journal_event("record_sighting", {"event_id": event_id, "person_id": person_id}, result)
        return self._public_result(result)

    # ------------------------------------------------------------------
    # Risk-area episodes and re-entry
    # ------------------------------------------------------------------
    def record_entry(
        self,
        person_id: str,
        at: float,
        area: Optional[str] = None,
        *,
        source: str = "manual_entry",
        evidence_id: Optional[str] = None,
        verified: bool = True,
    ) -> dict[str, Any]:
        person = self.people.get(person_id)
        if not person:
            return self._unknown(person_id)
        area = area or person["entry_area"]
        old = self.episodes.get(person_id)
        if old and old["state"] == "open":
            return {"decision": "already_in_risk_census", "person_id": person_id, "episode": old["number"]}

        number = 1 if not old else old["number"] + 1
        reentry = bool(old and old.get("disposition") in SAFE_STATUSES)
        episode = {
            "number": number,
            "state": "open" if verified else "suspected",
            "disposition": PENDING,
            "entered_at": at,
            "area": area,
            "source": source,
            "evidence_id": evidence_id,
            "verified": verified,
            "reentry": reentry,
            "events": [{"at": at, "kind": "entry", "area": area, "source": source, "evidence_id": evidence_id}],
        }
        self.episodes[person_id] = episode

        invalidated: list[str] = []
        if reentry and verified:
            invalidated = self._flag_clearances_for_reentry(person_id, at, evidence_id)
        result = {
            "decision": "reentry_recorded" if reentry else "entry_recorded",
            "person_id": person_id,
            "episode": number,
            "reentry": reentry,
            "invalidated_clearances": invalidated,
        }
        self._journal_event("record_entry", {"person_id": person_id, "at": at, "area": area}, result)
        return self._public_result(result)

    def report_reentry(
        self,
        person_id: str,
        at: float,
        area: Optional[str] = None,
        *,
        evidence_id: Optional[str] = None,
        verified: bool = True,
        reported_by: Optional[str] = None,
    ) -> dict[str, Any]:
        if not verified:
            # An unverified report still creates an auditable contradiction and
            # blocks clearance rather than being silently discarded.
            dispute = self.open_dispute(
                None,
                person_id=person_id,
                kind="unverified_reentry_report",
                evidence_ids=[evidence_id] if evidence_id else [],
                opened_at=at,
                opened_by=reported_by or "unknown",
                question="Unverified report that an accounted person returned to the risk area.",
            )
            return dispute
        return self.record_entry(
            person_id,
            at,
            area,
            source="reentry_report",
            evidence_id=evidence_id or f"reentry-{person_id}-{at}",
            verified=True,
        )

    def _flag_clearances_for_reentry(self, person_id: str, at: float, evidence_id: Optional[str]) -> list[str]:
        changed: list[str] = []
        for cert in self.certificates.values():
            if cert.get("currently_valid") and cert.get("scope") == "final":
                cert["currently_valid"] = False
                cert["valid"] = False
                cert["status"] = "correction_required"
                cert.setdefault("correction_triggers", []).append(
                    {"kind": "verified_reentry", "person_id": person_id, "at": at, "evidence_id": evidence_id}
                )
                changed.append(cert["certificate_id"])
        return changed

    # ------------------------------------------------------------------
    # Contradictions and investigations
    # ------------------------------------------------------------------
    def open_dispute(
        self,
        dispute_id: Optional[str],
        *,
        person_id: Optional[str] = None,
        kind: str,
        evidence_ids: Optional[list[str]] = None,
        opened_at: float,
        opened_by: str,
        question: Optional[str] = None,
        investigator: Optional[str] = None,
    ) -> dict[str, Any]:
        evidence_ids = list(evidence_ids or [])
        for eid in evidence_ids:
            if eid not in self.evidence:
                return {"decision": "unknown_evidence", "evidence_id": eid}

        if dispute_id is None:
            dispute_id = f"dispute-{self._next_seq()}"
        if dispute_id in self.disputes:
            return {"decision": "reused", "dispute_id": dispute_id}

        dispute = {
            "dispute_id": dispute_id,
            "person_id": person_id,
            "kind": kind,
            "evidence_ids": evidence_ids,
            "status": "under_investigation" if investigator else "open",
            "opened_at": opened_at,
            "opened_by": opened_by,
            "question": question,
            "lead_investigator": investigator,
            "investigation_log": [],
            "resolution": None,
        }
        if investigator:
            dispute["investigation_log"].append(
                {"at": opened_at, "investigator": investigator, "action": "assigned", "note": None}
            )
        self.disputes[dispute_id] = dispute
        for eid in evidence_ids:
            self.evidence[eid]["dispute_ids"].append(dispute_id)

        result = {"decision": "dispute_opened", "dispute_id": dispute_id, "status": dispute["status"]}
        self._journal_event("open_dispute", {"dispute_id": dispute_id, "kind": kind}, result)
        return self._public_result(result)

    def assign_investigation(
        self,
        dispute_id: str,
        *,
        investigation_id: str,
        investigator: str,
        at: float,
        mandate: Optional[str] = None,
    ) -> dict[str, Any]:
        dispute = self.disputes.get(dispute_id)
        if not dispute:
            return {"decision": "unknown_dispute", "dispute_id": dispute_id}
        if dispute["status"] == "resolved":
            return {"decision": "dispute_already_resolved", "dispute_id": dispute_id}
        step = {
            "investigation_id": investigation_id,
            "at": at,
            "investigator": investigator,
            "action": "assigned",
            "mandate": mandate,
        }
        dispute["status"] = "under_investigation"
        dispute["lead_investigator"] = investigator
        dispute["investigation_log"].append(step)
        result = {"decision": "investigation_assigned", "dispute_id": dispute_id, "investigation_id": investigation_id}
        self._journal_event("assign_investigation", {"dispute_id": dispute_id}, result)
        return self._public_result(result)

    def resolve_dispute(
        self,
        dispute_id: str,
        *,
        investigation_id: str,
        investigator: str,
        at: float,
        finding: str,
        accepted_evidence_ids: Optional[list[str]] = None,
        rejected_evidence_ids: Optional[list[str]] = None,
        actual_user_id: Optional[str] = None,
        camera_device_id: Optional[str] = None,
        clock_offset_seconds: Optional[float] = None,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        dispute = self.disputes.get(dispute_id)
        if not dispute:
            return {"decision": "unknown_dispute", "dispute_id": dispute_id}
        if actual_user_id is not None and actual_user_id not in self.people:
            return self._unknown(actual_user_id)

        accepted = set(accepted_evidence_ids or [])
        rejected = set(rejected_evidence_ids or [])
        unknown = [eid for eid in accepted | rejected if eid not in self.evidence]
        if unknown:
            return {"decision": "unknown_evidence", "evidence_ids": unknown}

        for eid in accepted:
            self.evidence[eid]["adjudication"] = "accepted"
        for eid in rejected:
            self.evidence[eid]["adjudication"] = "rejected"

        if actual_user_id:
            for eid in dispute["evidence_ids"]:
                evidence = self.evidence.get(eid)
                if evidence and evidence["source"] == "badge":
                    evidence["adjudicated_actual_person_id"] = actual_user_id
                    evidence["proxy"] = actual_user_id != evidence["person_id"]
                    if evidence["adjudication"] == "active":
                        evidence["adjudication"] = "proxy_confirmed"

        if clock_offset_seconds is not None:
            device_id = camera_device_id
            if device_id is None:
                for eid in dispute["evidence_ids"]:
                    ev = self.evidence.get(eid)
                    if ev and ev["source"] == "camera":
                        device_id = ev["details"].get("device_id")
                        break
            if device_id:
                self.calibrate_camera(
                    device_id,
                    offset_seconds=clock_offset_seconds,
                    calibrated_by=investigator,
                    at=at,
                    note=finding,
                )

        resolution = {
            "investigation_id": investigation_id,
            "at": at,
            "investigator": investigator,
            "finding": finding,
            "accepted_evidence_ids": sorted(accepted),
            "rejected_evidence_ids": sorted(rejected),
            "actual_user_id": actual_user_id,
            "clock_offset_seconds": clock_offset_seconds,
            "note": note,
        }
        dispute["status"] = "resolved"
        dispute["resolved_at"] = at
        dispute["resolution"] = resolution
        dispute["investigation_log"].append({**resolution, "action": "resolved"})

        result = {"decision": "dispute_resolved", "dispute_id": dispute_id, "finding": finding}
        self._journal_event("resolve_dispute", {"dispute_id": dispute_id, "finding": finding}, result)
        return self._public_result(result)

    # ------------------------------------------------------------------
    # Routes and isolation
    # ------------------------------------------------------------------
    def record_pressure_reading(
        self,
        reading_id: str,
        route_id: str = "backup-route",
        *,
        at: float,
        pressure_differential_ok: bool = True,
        reading: Optional[float] = None,
        recorded_by: Optional[str] = None,
    ) -> dict[str, Any]:
        route = self.routes.setdefault(route_id, {"route_id": route_id, "events": [], "authorization": None})
        event = {
            "reading_id": reading_id,
            "kind": "pressure",
            "at": at,
            "pressure_differential_ok": pressure_differential_ok,
            "reading": reading,
            "recorded_by": recorded_by,
        }
        route["events"].append(event)
        result = {"decision": "pressure_reading_recorded", "route_id": route_id, "qualified": pressure_differential_ok}
        self._journal_event("record_pressure_reading", {"route_id": route_id, "reading_id": reading_id}, result)
        return self._public_result(result)

    def record_manual_patrol(
        self,
        patrol_id: str,
        route_id: str = "backup-route",
        *,
        inspected_at: float,
        inspector: str,
        safe: bool = True,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        route = self.routes.setdefault(route_id, {"route_id": route_id, "events": [], "authorization": None})
        post_leak = self.leak_at is not None and inspected_at >= self.leak_at
        event = {
            "patrol_id": patrol_id,
            "kind": "manual_patrol",
            "at": inspected_at,
            "inspector": inspector,
            "safe": safe,
            "post_leak": post_leak,
            "note": note,
        }
        route["events"].append(event)
        result = {"decision": "manual_patrol_recorded", "route_id": route_id, "post_leak": post_leak, "safe": safe}
        self._journal_event("record_manual_patrol", {"route_id": route_id, "patrol_id": patrol_id}, result)
        return self._public_result(result)

    def route_report(self, route_id: str = "backup-route") -> dict[str, Any]:
        route = self.routes.get(route_id)
        if not route:
            return {"route_id": route_id, "known": False, "safe_confirmed": False, "blockers": ["route_unknown"]}

        pressures = [e for e in route["events"] if e["kind"] == "pressure"]
        patrols = [e for e in route["events"] if e["kind"] == "manual_patrol"]
        latest_pressure = max(pressures, key=lambda e: e["at"]) if pressures else None
        latest_patrol = max(patrols, key=lambda e: e["at"]) if patrols else None
        pressure_ok = bool(latest_pressure and latest_pressure["pressure_differential_ok"])
        post_leak_patrol = bool(latest_patrol and latest_patrol["post_leak"] and latest_patrol["safe"])

        blockers: list[str] = []
        if not latest_pressure:
            blockers.append("pressure_reading_missing")
        elif not pressure_ok:
            blockers.append("pressure_differential_not_qualified")
        if not latest_patrol:
            blockers.append("manual_patrol_missing")
        elif not latest_patrol["post_leak"]:
            blockers.append("last_manual_patrol_before_leak")
        elif not latest_patrol["safe"]:
            blockers.append("manual_patrol_reported_unsafe")

        return {
            "route_id": route_id,
            "known": True,
            "safe_confirmed": not blockers,
            "pressure_ok": pressure_ok,
            "latest_pressure": copy.deepcopy(latest_pressure),
            "latest_patrol": copy.deepcopy(latest_patrol),
            "post_leak_manual_patrol": post_leak_patrol,
            "authorization": copy.deepcopy(route.get("authorization")),
            "blockers": blockers,
        }

    def authorize_route_use(
        self,
        route_id: str = "backup-route",
        *,
        commander_id: str,
        at: float,
        commander_name: Optional[str] = None,
        commander_role: str = "incident_commander",
    ) -> dict[str, Any]:
        if commander_role != "incident_commander":
            return {"decision": "route_authorization_denied", "reason": "only_incident_commander"}
        report = self.route_report(route_id)
        if not report["safe_confirmed"]:
            return {"decision": "route_not_verified", "route_id": route_id, "blockers": report["blockers"]}
        route = self.routes[route_id]
        route["authorization"] = {
            "commander_id": commander_id,
            "commander_name": commander_name,
            "at": at,
            "authorized": True,
        }
        result = {"decision": "route_authorized", "route_id": route_id, "at": at}
        self._journal_event("authorize_route_use", {"route_id": route_id}, result)
        return self._public_result(result)

    def order_controlled_isolation(
        self,
        order_id: str,
        *,
        commander_id: str,
        at: float,
        area: str = "inner-equipment-room",
        reason: str = "route_status_not_verifiable_in_time",
        commander_role: str = "incident_commander",
    ) -> dict[str, Any]:
        if commander_role != "incident_commander":
            return {"decision": "isolation_order_denied", "reason": "only_incident_commander"}
        self.isolation = {
            "order_id": order_id,
            "commander_id": commander_id,
            "at": at,
            "area": area,
            "reason": reason,
            "status": "active",
        }
        result = {"decision": "controlled_isolation_ordered", "order_id": order_id, "area": area}
        self._journal_event("order_controlled_isolation", {"order_id": order_id}, result)
        return self._public_result(result)

    # ------------------------------------------------------------------
    # Dispatch, responsibility and escalation
    # ------------------------------------------------------------------
    def dispatch_search(
        self,
        order_id: str,
        person_id: str,
        team_id: str,
        at: float,
        *,
        command_group: str = "primary-command",
        area: Optional[str] = None,
        accepted_by: Optional[str] = None,
        accepted_at: Optional[float] = None,
        timeout_minutes: Optional[float] = 15,
        escalate_at: Optional[float] = None,
        escalation_owner: str = "incident-commander",
        medical_team: Optional[str] = None,
        medical_receiver: Optional[str] = None,
        task: str = "locate_and_recover",
        reinforcement: bool = False,
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        if order_id in self.assignments:
            old = self.assignments[order_id]
            return {"decision": "reused" if old["person_id"] == person_id else "order_id_conflict", "order_id": order_id}

        report = self.person_report(person_id)
        if report["accounted"]:
            return {"decision": "person_already_accounted", "person_id": person_id, "status": report["status"]}

        area = area or (report["last_credible_location"] or {}).get("area")
        if not area:
            return {"decision": "last_credible_location_required", "person_id": person_id}

        if escalate_at is None:
            if timeout_minutes is None:
                return {"decision": "escalation_deadline_required", "person_id": person_id}
            escalate_at = at + timeout_minutes

        active_id = self.active_assignment_id.get(person_id)
        if active_id and not reinforcement:
            duplicate = {
                "order_id": order_id,
                "person_id": person_id,
                "team_id": team_id,
                "command_group": command_group,
                "requested_at": at,
                "status": "duplicate_dispatch",
                "duplicate_of": active_id,
            }
            self.assignments[order_id] = duplicate
            dispute = self.open_dispute(
                None,
                person_id=person_id,
                kind="duplicate_command_dispatch",
                evidence_ids=[],
                opened_at=at,
                opened_by=command_group,
                question=f"Orders {active_id} and {order_id} both claim responsibility for {person_id}.",
                investigator="incident-commander",
            )
            duplicate["dispute_id"] = dispute.get("dispute_id")
            result = {
                "decision": "duplicate_dispatch",
                "order_id": order_id,
                "active_order_id": active_id,
                "dispute_id": dispute.get("dispute_id"),
            }
            self._journal_event("dispatch_search", {"order_id": order_id, "person_id": person_id}, result)
            return self._public_result(result)

        assignment = {
            "order_id": order_id,
            "person_id": person_id,
            "team_id": team_id,
            "command_group": command_group,
            "area": area,
            "task": task,
            "ordered_at": at,
            "escalate_at": escalate_at,
            "escalation_owner": escalation_owner,
            "status": "accepted" if accepted_by else "assigned",
            "accepted_by": accepted_by,
            "accepted_at": accepted_at if accepted_at is not None else (at if accepted_by else None),
            "medical_team": medical_team,
            "medical_receiver": medical_receiver,
            "medical_staging": bool(medical_team or medical_receiver),
            "reinforcement": reinforcement,
            "escalations": [],
            "recovery_id": None,
            "handoff_id": None,
        }
        self.assignments[order_id] = assignment
        if not reinforcement:
            self.active_assignment_id[person_id] = order_id
        result = {"decision": "search_dispatched", "order_id": order_id, "person_id": person_id, "escalate_at": escalate_at}
        self._journal_event("dispatch_search", {"order_id": order_id, "person_id": person_id}, result)
        return self._public_result(result)

    def accept_dispatch(self, order_id: str, accepted_by: str, at: float) -> dict[str, Any]:
        assignment = self.assignments.get(order_id)
        if not assignment:
            return {"decision": "unknown_order", "order_id": order_id}
        if assignment["status"] == "duplicate_dispatch":
            return {"decision": "duplicate_order_must_be_deconflicted", "order_id": order_id}
        assignment["accepted_by"] = accepted_by
        assignment["accepted_at"] = at
        assignment["status"] = "accepted"
        result = {"decision": "dispatch_accepted", "order_id": order_id, "accepted_by": accepted_by}
        self._journal_event("accept_dispatch", {"order_id": order_id}, result)
        return self._public_result(result)

    def deconflict_dispatch(
        self,
        person_id: str,
        lead_order_id: str,
        *,
        deconflicted_by: str,
        at: float,
        rationale: str = "single_responsible_team_required",
    ) -> dict[str, Any]:
        lead = self.assignments.get(lead_order_id)
        if not lead or lead["person_id"] != person_id or lead["status"] == "duplicate_dispatch":
            return {"decision": "invalid_lead_order", "order_id": lead_order_id}

        previous = self.active_assignment_id.get(person_id)
        self.active_assignment_id[person_id] = lead_order_id
        stood_down: list[str] = []
        for order in self.assignments.values():
            if (
                order["person_id"] == person_id
                and order["order_id"] != lead_order_id
                and order.get("status") == "duplicate_dispatch"
            ):
                order["status"] = "stood_down_duplicate"
                order["stood_down_at"] = at
                order["deconflicted_by"] = deconflicted_by
                stood_down.append(order["order_id"])
                dispute_id = order.get("dispute_id")
                if dispute_id and dispute_id in self.disputes:
                    self.resolve_dispute(
                        dispute_id,
                        investigation_id=f"deconflict-{lead_order_id}",
                        investigator=deconflicted_by,
                        at=at,
                        finding="dispatch_deconflicted_single_lead",
                        note=rationale,
                    )

        if lead.get("status") == "escalated" or lead.get("escalations"):
            lead["status"] = "escalated"
        else:
            lead["status"] = "accepted" if lead.get("accepted_by") else "designated_lead"
        result = {
            "decision": "dispatch_deconflicted",
            "person_id": person_id,
            "lead_order_id": lead_order_id,
            "previous_order_id": previous,
            "stood_down": stood_down,
        }
        self._journal_event("deconflict_dispatch", {"person_id": person_id}, result)
        return self._public_result(result)

    def process_escalations(self, now: float) -> dict[str, Any]:
        due: list[dict[str, Any]] = []
        for order_id in self.active_assignment_id.values():
            assignment = self.assignments[order_id]
            if assignment["status"] in {"complete", "closed_assembled", "medical_handed_over"}:
                continue
            if assignment["escalate_at"] <= now and assignment["status"] != "escalated":
                assignment["status"] = "escalated"
                escalation = {"at": now, "to": assignment["escalation_owner"], "deadline_was": assignment["escalate_at"]}
                assignment["escalations"].append(escalation)
                due.append({"order_id": order_id, "person_id": assignment["person_id"], **escalation})
        result = {"decision": "escalations_processed", "at": now, "due": due}
        self._journal_event("process_escalations", {"now": now}, result)
        return self._public_result(result)

    # ------------------------------------------------------------------
    # Safe dispositions: personal assembly and rescue/medical handover
    # ------------------------------------------------------------------
    def confirm_assembly(
        self,
        receipt_id: str,
        person_id: str,
        at: float,
        *,
        method: str = "signature",
        witness: Optional[str] = None,
        identity_verified: bool = True,
        self_present: bool = True,
        verification_ref: Optional[str] = None,
        recorded_at: Optional[float] = None,
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        if receipt_id in self.evidence:
            return {"decision": "reused", "receipt_id": receipt_id}
        if not self_present or not identity_verified:
            added = self._add_evidence(
                receipt_id,
                "assembly_signin",
                at,
                person_id=person_id,
                area="assembly-point",
                recorded_at=recorded_at,
                direct_identity=False,
                location_credible=False,
                details={"method": method, "witness": witness, "self_present": self_present, "rejected": True},
            )
            if added["decision"] == "duplicate_evidence_id":
                return {"decision": "reused", "receipt_id": receipt_id}
            result = {"decision": "proxy_assembly_signin_rejected", "receipt_id": receipt_id, "person_id": person_id}
            self._journal_event("confirm_assembly", {"receipt_id": receipt_id}, result)
            return self._public_result(result)

        if not witness and method not in {"biometric", "official_id_match"} and not verification_ref:
            return {
                "decision": "assembly_verification_incomplete",
                "reason": "witness_or_strong_verification_required",
                "person_id": person_id,
            }

        added = self._add_evidence(
            receipt_id,
            "assembly_signin",
            at,
            person_id=person_id,
            area="assembly-point",
            recorded_at=recorded_at,
            direct_identity=True,
            location_credible=True,
            details={"method": method, "witness": witness, "verification_ref": verification_ref, "receipt_id": receipt_id},
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "receipt_id": receipt_id}

        episode = self.episodes.setdefault(
            person_id,
            {"number": 1, "state": "open", "disposition": PENDING, "entered_at": at, "area": "unknown", "events": []},
        )
        episode["state"] = "accounted"
        episode["disposition"] = ASSEMBLED
        episode["accounted_at"] = at
        episode["assembly_receipt_id"] = receipt_id
        episode["events"].append({"at": at, "kind": "assembly_confirmed", "receipt_id": receipt_id})

        order_id = self.active_assignment_id.pop(person_id, None)
        if order_id and order_id in self.assignments:
            self.assignments[order_id]["status"] = "closed_assembled"
            self.assignments[order_id]["closed_at"] = at

        result = {"decision": "assembly_confirmed", "receipt_id": receipt_id, "person_id": person_id, "status": ASSEMBLED}
        self._journal_event("confirm_assembly", {"receipt_id": receipt_id, "person_id": person_id}, result)
        return self._public_result(result)

    def confirm_rescue_recovery(
        self,
        recovery_id: str,
        person_id: str,
        at: float,
        *,
        order_id: Optional[str] = None,
        rescued_by: str,
        condition: Optional[str] = None,
        recorded_at: Optional[float] = None,
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        order_id = order_id or self.active_assignment_id.get(person_id)
        assignment = self.assignments.get(order_id) if order_id else None
        if not assignment or assignment.get("person_id") != person_id:
            return {"decision": "active_rescue_order_required", "person_id": person_id}

        added = self._add_evidence(
            recovery_id,
            "rescue_recovery",
            at,
            person_id=person_id,
            area=assignment.get("area"),
            recorded_at=recorded_at,
            direct_identity=True,
            location_credible=True,
            details={"order_id": order_id, "rescued_by": rescued_by, "condition": condition, "recovery_id": recovery_id},
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "recovery_id": recovery_id}

        assignment["status"] = "recovered_pending_medical_handover"
        assignment["recovery_id"] = recovery_id
        episode = self.episodes.get(person_id)
        if episode:
            episode["rescue_custody"] = {"at": at, "by": rescued_by, "order_id": order_id, "recovery_id": recovery_id}
            episode["events"].append({"at": at, "kind": "rescue_recovered", "recovery_id": recovery_id})

        result = {"decision": "rescue_recovery_confirmed", "recovery_id": recovery_id, "order_id": order_id}
        self._journal_event("confirm_rescue_recovery", {"recovery_id": recovery_id}, result)
        return self._public_result(result)

    def complete_medical_handoff(
        self,
        handoff_id: str,
        person_id: str,
        handoff_at: float,
        *,
        received_by: str,
        medical_team: str,
        rescue_order_id: Optional[str] = None,
        recovery_id: Optional[str] = None,
        rescued_by: Optional[str] = None,
        transport_requested_at: Optional[float] = None,
        transport_arrived_at: Optional[float] = None,
        deadline: Optional[float] = None,
        recorded_at: Optional[float] = None,
        destination: str = "medical-receiving",
    ) -> dict[str, Any]:
        if person_id not in self.people:
            return self._unknown(person_id)
        if handoff_id in self.evidence:
            return {"decision": "reused", "handoff_id": handoff_id}

        rescue_order_id = rescue_order_id or self.active_assignment_id.get(person_id)
        assignment = self.assignments.get(rescue_order_id) if rescue_order_id else None
        recovery = None
        if recovery_id and recovery_id in self.evidence:
            recovery = self.evidence[recovery_id]
        elif assignment and assignment.get("recovery_id"):
            recovery_id = assignment["recovery_id"]
            recovery = self.evidence.get(recovery_id)

        rescue_side_verified = bool(
            recovery
            and recovery.get("person_id") == person_id
            and (not rescue_order_id or recovery["details"].get("order_id") == rescue_order_id)
        ) or bool(rescued_by and assignment)

        if not rescue_side_verified or not received_by or not medical_team:
            added = self._add_evidence(
                handoff_id,
                "medical_handoff",
                handoff_at,
                person_id=person_id,
                area=destination,
                recorded_at=recorded_at,
                direct_identity=True,
                location_credible=True,
                details={
                    "handoff_id": handoff_id,
                    "received_by": received_by,
                    "medical_team": medical_team,
                    "rescue_order_id": rescue_order_id,
                    "incomplete": True,
                },
            )
            if added["decision"] == "duplicate_evidence_id":
                return {"decision": "reused", "handoff_id": handoff_id}
            return {
                "decision": "incomplete_verifiable_handoff",
                "handoff_id": handoff_id,
                "rescue_side_verified": rescue_side_verified,
                "medical_receiver_present": bool(received_by and medical_team),
            }

        recorded_at = handoff_at if recorded_at is None else recorded_at
        transport_response_seconds = None
        if transport_requested_at is not None and transport_arrived_at is not None:
            transport_response_seconds = transport_arrived_at - transport_requested_at
        transport_late = deadline is not None and handoff_at > deadline
        recorded_late_seconds = max(0, recorded_at - handoff_at)

        added = self._add_evidence(
            handoff_id,
            "medical_handoff",
            handoff_at,
            person_id=person_id,
            area=destination,
            recorded_at=recorded_at,
            direct_identity=True,
            location_credible=True,
            details={
                "handoff_id": handoff_id,
                "received_by": received_by,
                "medical_team": medical_team,
                "rescue_order_id": rescue_order_id,
                "recovery_id": recovery_id,
                "rescued_by": rescued_by or (recovery["details"].get("rescued_by") if recovery else None),
                "destination": destination,
                "transport_requested_at": transport_requested_at,
                "transport_arrived_at": transport_arrived_at,
                "transport_response_seconds": transport_response_seconds,
                "deadline": deadline,
                "transport_late": transport_late,
                "recorded_late_seconds": recorded_late_seconds,
            },
        )
        if added["decision"] == "duplicate_evidence_id":
            return {"decision": "reused", "handoff_id": handoff_id}

        episode = self.episodes.setdefault(
            person_id,
            {"number": 1, "state": "open", "disposition": PENDING, "entered_at": handoff_at, "area": "unknown", "events": []},
        )
        episode["state"] = "accounted"
        episode["disposition"] = MEDICAL_HANDOVER
        episode["accounted_at"] = handoff_at
        episode["medical_handoff_id"] = handoff_id
        episode["events"].append({"at": handoff_at, "kind": "medical_handoff_verified", "handoff_id": handoff_id})

        if assignment:
            assignment["status"] = "medical_handed_over"
            assignment["handoff_id"] = handoff_id
            assignment["closed_at"] = handoff_at
        self.active_assignment_id.pop(person_id, None)

        result = {
            "decision": "medical_handoff_verified",
            "handoff_id": handoff_id,
            "person_id": person_id,
            "status": MEDICAL_HANDOVER,
            "transport_late": transport_late,
            "recorded_late_seconds": recorded_late_seconds,
            "transport_response_seconds": transport_response_seconds,
        }
        self._journal_event("complete_medical_handoff", {"handoff_id": handoff_id}, result)
        return self._public_result(result)

    # ------------------------------------------------------------------
    # Projections and reports
    # ------------------------------------------------------------------
    def _evidence_is_open_disputed(self, evidence_id: str) -> bool:
        ev = self.evidence.get(evidence_id)
        if not ev:
            return False
        for dispute_id in ev.get("dispute_ids", []):
            if self.disputes.get(dispute_id, {}).get("status") != "resolved":
                return True
        return False

    def _person_evidence_records(self, person_id: str) -> list[dict[str, Any]]:
        records = []
        for ev in self.evidence.values():
            if ev.get("person_id") == person_id or ev.get("adjudicated_actual_person_id") == person_id:
                records.append(ev)
        return sorted(records, key=lambda e: (self.effective_time(e), e["seq"]))

    def _current_episode_evidence(self, person_id: str) -> list[dict[str, Any]]:
        episode = self.episodes.get(person_id)
        records = self._person_evidence_records(person_id)
        if not episode:
            return records
        result = []
        for ev in records:
            if ev.get("episode_number") == episode["number"] and ev.get("person_id") == person_id:
                result.append(ev)
            elif self.effective_time(ev) >= episode["entered_at"]:
                result.append(ev)
        return result

    def last_credible_location(self, person_id: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
        episode = self.episodes.get(person_id)
        candidates: list[tuple[float, int, dict[str, Any]]] = []
        # Terminal safe-disposition locations are retained separately.  The
        # "last credible location" needed by search teams is the last body
        # location in or near the risk area before assembly/medical custody.
        terminal_sources = {"assembly_signin", "medical_handoff"}
        for ev in self._current_episode_evidence(person_id):
            if ev.get("source") in terminal_sources:
                continue
            if not ev.get("area") or not ev.get("location_credible"):
                continue
            if ev.get("adjudication") == "rejected":
                continue
            if self._evidence_is_open_disputed(ev["evidence_id"]):
                continue
            effective_at = self.effective_time(ev)
            if now is not None and effective_at > now:
                continue
            candidates.append((effective_at, ev["seq"], ev))

        if candidates:
            _, _, ev = max(candidates, key=lambda item: (item[0], item[1]))
            return {
                "area": ev["area"],
                "at": self.effective_time(ev),
                "recorded_at": ev["recorded_at"],
                "source": ev["source"],
                "evidence_id": ev["evidence_id"],
                "clock_adjusted": ev["source"] == "camera",
                "raw_displayed_at": ev.get("details", {}).get("displayed_at"),
            }

        if episode:
            return {
                "area": episode["area"],
                "at": episode["entered_at"],
                "source": episode["source"],
                "evidence_id": episode.get("evidence_id"),
                "basis": "fixed_entry_or_last_assigned_area",
            }
        return None

    def _status_as_of(self, person_id: str, now: Optional[float]) -> str:
        episode = self.episodes.get(person_id)
        if now is None:
            return episode.get("disposition", PENDING) if episode else PENDING

        terminal: Optional[tuple[float, int, str]] = None
        for ev in self._person_evidence_records(person_id):
            if ev["observed_at"] > now:
                continue
            if ev["source"] == "assembly_signin" and not ev.get("details", {}).get("rejected"):
                candidate = (ev["observed_at"], ev["seq"], ASSEMBLED)
            elif ev["source"] == "medical_handoff" and not ev.get("details", {}).get("incomplete"):
                candidate = (ev["observed_at"], ev["seq"], MEDICAL_HANDOVER)
            else:
                continue
            if terminal is None or (candidate[0], candidate[1]) > (terminal[0], terminal[1]):
                terminal = candidate

        if terminal is None:
            return PENDING
        terminal_at, _, status = terminal
        # A verified new risk-area entry after the terminal fact reopens the
        # census at the requested as-of time.
        if episode and episode.get("reentry") and episode["entered_at"] <= now and episode["entered_at"] > terminal_at:
            return PENDING
        return status

    def person_report(self, person_id: str, now: Optional[float] = None) -> dict[str, Any]:
        person = self.people.get(person_id)
        if not person:
            return self._unknown(person_id)

        episode = self.episodes.get(person_id)
        status = self._status_as_of(person_id, now)
        assignment_id = self.active_assignment_id.get(person_id)
        assignment = self.assignments.get(assignment_id) if assignment_id else None
        if now is not None:
            temporal_assignments = [
                order
                for order in self.assignments.values()
                if order.get("person_id") == person_id
                and order.get("status") not in {"duplicate_dispatch", "stood_down_duplicate"}
                and order.get("ordered_at", order.get("requested_at", 0)) <= now
                and not (order.get("closed_at") is not None and order["closed_at"] <= now)
            ]
            if temporal_assignments:
                assignment = max(
                    temporal_assignments,
                    key=lambda order: (order.get("ordered_at", order.get("requested_at", 0)), order["order_id"]),
                )
        location = self.last_credible_location(person_id, now=now)

        assignment_status = assignment.get("status") if assignment else None
        escalated_by_now = False
        if assignment and now is not None:
            escalated_by_now = any(item.get("at", float("inf")) <= now for item in assignment.get("escalations", []))
            recovery_evidence = self.evidence.get(assignment.get("recovery_id")) if assignment.get("recovery_id") else None
            closed_by_now = assignment.get("closed_at") is not None and assignment["closed_at"] <= now
            recovery_by_now = bool(recovery_evidence and recovery_evidence["observed_at"] <= now)
            if closed_by_now:
                assignment_status = assignment.get("status")
            elif recovery_by_now:
                assignment_status = "recovered_pending_medical_handover"
            elif escalated_by_now:
                assignment_status = "escalated"
            elif assignment.get("accepted_at") is not None and assignment["accepted_at"] <= now:
                assignment_status = "accepted"
            else:
                assignment_status = "assigned"

        disputes = [
            copy.deepcopy(d)
            for d in self.disputes.values()
            if d.get("person_id") == person_id and d.get("status") != "resolved"
        ]
        all_disputes = [copy.deepcopy(d) for d in self.disputes.values() if d.get("person_id") == person_id]

        overdue = bool(
            assignment
            and now is not None
            and assignment["escalate_at"] <= now
            and not escalated_by_now
            and not (assignment.get("closed_at") is not None and assignment["closed_at"] <= now)
        )

        timeline = []
        for ev in self._person_evidence_records(person_id):
            timeline.append(
                {
                    "evidence_id": ev["evidence_id"],
                    "source": ev["source"],
                    "observed_at": ev["observed_at"],
                    "effective_at": self.effective_time(ev),
                    "recorded_at": ev["recorded_at"],
                    "area": ev.get("area"),
                    "direct_identity": ev.get("direct_identity"),
                    "adjudication": ev.get("adjudication"),
                    "proxy": ev.get("proxy"),
                    "adjudicated_actual_person_id": ev.get("adjudicated_actual_person_id"),
                    "dispute_ids": ev.get("dispute_ids"),
                    "details": copy.deepcopy(ev.get("details", {})),
                }
            )

        current_location: Optional[dict[str, Any]] = location
        assembly_facts = [
            item for item in timeline if item["source"] == "assembly_signin" and (now is None or item["observed_at"] <= now)
        ]
        medical_facts = [
            item
            for item in timeline
            if item["source"] == "medical_handoff"
            and not item["details"].get("incomplete")
            and (now is None or item["observed_at"] <= now)
        ]
        if status == ASSEMBLED and assembly_facts:
            fact = assembly_facts[-1]
            current_location = {
                "area": "assembly-point",
                "at": fact["observed_at"],
                "source": "personal_assembly_signin",
                "evidence_id": fact["evidence_id"],
                "receipt_id": fact["details"].get("receipt_id", fact["evidence_id"]),
            }
        elif status == MEDICAL_HANDOVER and medical_facts:
            fact = medical_facts[-1]
            current_location = {
                "area": fact["details"].get("destination") or fact.get("area") or "medical-receiving",
                "at": fact["observed_at"],
                "source": "verified_medical_handover",
                "evidence_id": fact["evidence_id"],
                "handoff_id": fact["details"].get("handoff_id", fact["evidence_id"]),
                "medical_team": fact["details"].get("medical_team"),
                "received_by": fact["details"].get("received_by"),
            }

        episode_view = copy.deepcopy(episode)
        if episode_view is not None and now is not None:
            episode_view["disposition"] = status
            episode_view["state"] = "accounted" if status in SAFE_STATUSES else "open"

        return {
            "person_id": person_id,
            "full_name": person["full_name"],
            "role": person["role"],
            "employer": person.get("employer"),
            "shift": person.get("shift"),
            "entry_area": person.get("entry_area"),
            "escort_id": person.get("escort_id"),
            "escort_authorized": person.get("escort_authorized"),
            "status": status,
            "status_zh": STATUS_ZH[status],
            "accounted": status in SAFE_STATUSES,
            "safe_disposition": status if status in SAFE_STATUSES else None,
            "current_episode": episode_view,
            "last_credible_location": location,
            "current_location": current_location,
            "responsible_team": (
                {
                    "order_id": assignment["order_id"],
                    "team_id": assignment["team_id"],
                    "command_group": assignment["command_group"],
                    "accepted_by": assignment.get("accepted_by"),
                    "accepted_at": assignment.get("accepted_at"),
                    "escalate_at": assignment["escalate_at"],
                    "escalation_owner": assignment["escalation_owner"],
                    "status": assignment_status,
                    "escalated_by_now": escalated_by_now,
                    "medical_team": assignment.get("medical_team"),
                    "medical_receiver": assignment.get("medical_receiver"),
                }
                if assignment
                else None
            ),
            "overdue": overdue,
            "open_disputes": disputes,
            "disputes": all_disputes,
            "evidence_timeline": timeline,
            "cards": [copy.deepcopy(c) for c in self.cards.values() if c["person_id"] == person_id],
        }

    def _census_ids(self) -> list[str]:
        return [pid for pid, p in self.people.items() if p.get("census_required", True)]

    def muster_report(self, now: Optional[float] = None) -> dict[str, Any]:
        people = [self.person_report(pid, now=now) for pid in self._census_ids()]
        pending = [r for r in people if not r["accounted"]]
        accounted = [r for r in people if r["accounted"]]
        missing_location = [r["person_id"] for r in pending if not r["last_credible_location"]]
        missing_owner = [
            r["person_id"]
            for r in pending
            if not r["responsible_team"] or not r["responsible_team"].get("accepted_by")
        ]
        deadlines_missing = [r["person_id"] for r in pending if not (r["responsible_team"] or {}).get("escalate_at")]
        open_disputes = [
            {"dispute_id": d["dispute_id"], "person_id": d.get("person_id"), "kind": d["kind"], "status": d["status"]}
            for d in self.disputes.values()
            if d["status"] != "resolved"
        ]
        duplicate_orders = [
            o["order_id"] for o in self.assignments.values() if o.get("status") == "duplicate_dispatch"
        ]
        overdue = [r["person_id"] for r in pending if r.get("overdue")] if now is not None else []

        valid_final = [
            c
            for c in self.certificates.values()
            if c.get("scope") == "final"
            and c.get("currently_valid")
            and c.get("valid")
            and (now is None or c.get("signed_at", 0) <= now)
        ]
        return {
            "incident_id": self.incident_id,
            "total": len(people),
            "accounted_count": len(accounted),
            "pending_count": len(pending),
            "all_accounted": not pending,
            "pending": [{"person_id": r["person_id"], "full_name": r["full_name"], "status": r["status"]} for r in pending],
            "assembled": [r["person_id"] for r in people if r["status"] == ASSEMBLED],
            "medical_handover": [r["person_id"] for r in people if r["status"] == MEDICAL_HANDOVER],
            "missing_last_credible_location": missing_location,
            "missing_accepted_owner": missing_owner,
            "missing_escalation_deadline": deadlines_missing,
            "overdue_not_escalated": overdue,
            "open_disputes": open_disputes,
            "unresolved_duplicate_orders": duplicate_orders,
            "valid_final_clearance_id": valid_final[-1]["certificate_id"] if valid_final else None,
            "journal_intact": self.verify_journal_chain(),
            "people": people,
        }

    # ------------------------------------------------------------------
    # Commander clearance and correction
    # ------------------------------------------------------------------
    def _clearance_blockers(self, scope: str, at: float) -> list[str]:
        report = self.muster_report(now=at)
        blockers: list[str] = []

        bad_disputes = [d for d in report["open_disputes"] if d["status"] == "open"]
        if bad_disputes:
            blockers.append("unassigned_contradiction_investigation")
        if report["unresolved_duplicate_orders"]:
            blockers.append("duplicate_command_dispatch_not_deconflicted")

        if scope == "final":
            if not report["all_accounted"]:
                blockers.append("unconfirmed_personnel")
            if [d for d in report["open_disputes"] if d["status"] != "resolved"]:
                blockers.append("unresolved_evidence_dispute")
            if self.active_assignment_id:
                blockers.append("active_search_or_rescue_assignment")
            corrections_required = [
                cid for cid, c in self.certificates.items() if c.get("status") == "correction_required"
            ]
            if corrections_required:
                blockers.append("signed_clearance_requires_recorded_correction")
            return blockers

        if scope == "controlled":
            if not self.isolation or self.isolation.get("status") != "active":
                blockers.append("controlled_isolation_order_required")
            for person in report["people"]:
                if person["accounted"]:
                    continue
                if not person["last_credible_location"]:
                    blockers.append(f"missing_last_credible_location:{person['person_id']}")
                owner = person["responsible_team"]
                if not owner or not owner.get("team_id"):
                    blockers.append(f"missing_responsible_team:{person['person_id']}")
                elif not owner.get("accepted_by"):
                    blockers.append(f"real_handover_not_accepted:{person['person_id']}")
                if not owner or not owner.get("escalate_at"):
                    blockers.append(f"missing_escalation_deadline:{person['person_id']}")
                elif owner["escalate_at"] <= at and owner.get("status") not in {"escalated", "recovered_pending_medical_handover"}:
                    blockers.append(f"deadline_due_without_escalation:{person['person_id']}")
            return blockers

        blockers.append(f"unknown_clearance_scope:{scope}")
        return blockers

    def sign_clearance(
        self,
        certificate_id: str,
        commander_id: str,
        at: float,
        *,
        commander_name: Optional[str] = None,
        commander_role: str = "incident_commander",
        scope: str = "final",
        statement: Optional[str] = None,
        corrects: Optional[str] = None,
        force_record: bool = False,
    ) -> dict[str, Any]:
        scope = self._normalize_scope(scope)
        if certificate_id in self.certificates:
            return {"decision": "reused", "certificate_id": certificate_id}
        if commander_role != "incident_commander":
            return {"decision": "clearance_denied", "reason": "only_incident_commander_can_sign"}
        if corrects is not None and corrects not in self.certificates:
            return {"decision": "unknown_corrected_certificate", "corrects": corrects}

        blockers = self._clearance_blockers(scope, at)
        eligible = not blockers
        if blockers and not force_record:
            result = {"decision": "clearance_blocked", "certificate_id": certificate_id, "blockers": blockers}
            self._journal_event("sign_clearance", {"certificate_id": certificate_id, "scope": scope}, result)
            return self._public_result(result)

        if corrects and self.certificates[corrects].get("status") in {"correction_required", "signed"}:
            self.certificates[corrects]["status"] = "superseded_corrected"
            self.certificates[corrects]["currently_valid"] = False

        certificate = {
            "certificate_id": certificate_id,
            "commander_id": commander_id,
            "commander_name": commander_name,
            "commander_role": commander_role,
            "scope": scope,
            "signed_at": at,
            "statement": statement,
            "corrects": corrects,
            "blockers_at_signing": blockers,
            "valid": eligible,
            "currently_valid": eligible,
            "status": "signed" if eligible else "signed_but_unsupported",
            "corrections": [],
            "snapshot": {
                "total": self.muster_report(now=at)["total"],
                "accounted_count": self.muster_report(now=at)["accounted_count"],
                "pending_count": self.muster_report(now=at)["pending_count"],
                "pending": self.muster_report(now=at)["pending"],
            },
        }
        self.certificates[certificate_id] = certificate
        result = {
            "decision": "clearance_signed" if eligible else "unsupported_clearance_recorded",
            "certificate_id": certificate_id,
            "valid": eligible,
            "blockers": blockers,
        }
        self._journal_event("sign_clearance", {"certificate_id": certificate_id, "scope": scope}, result)
        return self._public_result(result)

    def record_signed_clearance(
        self,
        certificate_id: str,
        commander_id: str,
        at: float,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Record an already-signed paper/electronic signoff.

        The certificate is retained even when board guards prove it was
        unsupported; its ``valid`` flag and blockers preserve that distinction.
        """
        kwargs.setdefault("scope", "all_clear")
        kwargs["force_record"] = True
        return self.sign_clearance(certificate_id, commander_id, at, **kwargs)

    def correct_clearance(
        self,
        correction_id: str,
        certificate_id: str,
        at: float,
        *,
        corrected_by: str,
        reason: str,
        corrected_statement: Optional[str] = None,
        outcome: str = "corrected",
    ) -> dict[str, Any]:
        certificate = self.certificates.get(certificate_id)
        if not certificate:
            return {"decision": "unknown_certificate", "certificate_id": certificate_id}
        correction = {
            "correction_id": correction_id,
            "certificate_id": certificate_id,
            "at": at,
            "corrected_by": corrected_by,
            "reason": reason,
            "corrected_statement": corrected_statement,
            "outcome": outcome,
        }
        certificate["corrections"].append(correction)
        certificate["status"] = "rescinded" if outcome == "rescinded" else "corrected"
        certificate["valid"] = False
        certificate["currently_valid"] = False
        result = {"decision": "clearance_correction_recorded", **correction}
        self._journal_event("correct_clearance", {"correction_id": correction_id}, result)
        return self._public_result(result)

    def clearance_history(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self.certificates.values()))

    # ------------------------------------------------------------------
    # Safety reset / restart and briefings
    # ------------------------------------------------------------------
    def record_safety_reset(
        self,
        reset_id: str,
        at: float,
        *,
        authorized_by: str,
        leak_isolated: bool = True,
        ventilation_verified: bool = True,
        electrical_safe: bool = True,
        note: Optional[str] = None,
    ) -> dict[str, Any]:
        conditions = {
            "leak_isolated": leak_isolated,
            "ventilation_verified": ventilation_verified,
            "electrical_safe": electrical_safe,
        }
        self.safety_reset = {
            "reset_id": reset_id,
            "at": at,
            "authorized_by": authorized_by,
            "conditions": conditions,
            "reset_complete": all(conditions.values()),
            "note": note,
        }
        result = {"decision": "safety_reset_recorded", "reset_complete": self.safety_reset["reset_complete"]}
        self._journal_event("record_safety_reset", {"reset_id": reset_id}, result)
        return self._public_result(result)

    def restart_assessment(self, now: Optional[float] = None) -> dict[str, Any]:
        report = self.muster_report(now=now)
        blockers: list[str] = []
        if not report["all_accounted"]:
            blockers.append("personnel_muster_incomplete")
        if report["open_disputes"]:
            blockers.append("evidence_disputes_open")
        if self.active_assignment_id:
            blockers.append("active_rescue_mission")
        final_cert = next(
            (
                c
                for c in reversed(list(self.certificates.values()))
                if c["scope"] == "final"
                and c.get("currently_valid")
                and (now is None or c.get("signed_at", 0) <= now)
            ),
            None,
        )
        reset_available = bool(
            self.safety_reset
            and self.safety_reset.get("reset_complete")
            and (now is None or self.safety_reset.get("at", 0) <= now)
        )
        if not final_cert:
            blockers.append("valid_commander_final_clearance_missing")
        if not reset_available:
            blockers.append("leak_safety_reset_incomplete")

        earliest = None
        if final_cert and reset_available:
            earliest = max(final_cert["signed_at"], self.safety_reset["at"])
        return {
            "restart_authorized": not blockers,
            "blockers": blockers,
            "earliest_restart_at": earliest if not blockers else None,
            "final_clearance_id": final_cert["certificate_id"] if final_cert else None,
            "safety_reset": copy.deepcopy(self.safety_reset),
        }

    def person_dossier(self, person_id: str, now: Optional[float] = None) -> dict[str, Any]:
        report = self.person_report(person_id, now=now)
        if report.get("decision") == "unknown_person":
            return report
        refs = [item["evidence_id"] for item in report["evidence_timeline"]]
        if report["responsible_team"]:
            refs.append(report["responsible_team"]["order_id"])
        return {
            "dossier_type": "verifiable_personnel_whereabouts_and_handover",
            "identity": {
                "person_id": report["person_id"],
                "full_name": report["full_name"],
                "role": report["role"],
                "employer": report["employer"],
                "shift": report["shift"],
                "entry_area": report["entry_area"],
                "escort_id": report["escort_id"],
            },
            "current_status": {
                "code": report["status"],
                "label_zh": report["status_zh"],
                "accounted": report["accounted"],
            },
            "last_credible_location": report["last_credible_location"],
            "responsible_team": report["responsible_team"],
            "open_disputes": report["open_disputes"],
            "evidence_timeline": report["evidence_timeline"],
            "verification_refs": refs,
            "journal_intact": self.verify_journal_chain(),
        }

    def briefing(self, audience: str, person_id: str, now: Optional[float] = None) -> dict[str, Any]:
        dossier = self.person_dossier(person_id, now=now)
        if dossier.get("decision") == "unknown_person":
            return dossier
        location = dossier["last_credible_location"] or {}
        team = dossier["responsible_team"] or {}
        refs = dossier["verification_refs"]

        if dossier["current_status"]["code"] == ASSEMBLED:
            headline = "当事人已在集合点由本人完成签认；该事实可凭签收回执核验。"
            may_state = ["本人已到集合点并亲自签认", "当前人员清点状态已确认"]
            must_not_state: list[str] = []
        elif dossier["current_status"]["code"] == MEDICAL_HANDOVER:
            handoff = next(
                (e for e in reversed(dossier["evidence_timeline"]) if e["source"] == "medical_handoff"),
                None,
            )
            details = (handoff or {}).get("details", {})
            headline = "当事人已完成救援组与医疗组交接，交接有双方与转运回执可核验。"
            may_state = [
                f"医疗接收：{details.get('medical_team')} / {details.get('received_by')}",
                f"交接编号：{details.get('handoff_id')}",
            ]
            must_not_state = []
        else:
            loc_text = f"{location.get('area')}（{location.get('at')}，来源 {location.get('source')} / {location.get('evidence_id')}）" if location else "尚未固定可信位置"
            headline = "尚未完成本人安全确认；不得宣称其已出门或安全。"
            may_state = [
                f"最后可信位置：{loc_text}",
                f"负责查找：{team.get('command_group')} / {team.get('team_id')}，接收人 {team.get('accepted_by')}",
                f"升级期限：{team.get('escalate_at')}，升级对象 {team.get('escalation_owner')}",
            ]
            if team.get("medical_team"):
                may_state.append(f"医疗接应：{team.get('medical_team')} / {team.get('medical_receiver')}")
            must_not_state = ["不得仅凭代刷门禁 out 宣布本人离场或安全", "不得给出未经本人或交接回执证实的死亡/受伤结论"]

        if audience == "family":
            audience_note = "家属版：只披露该当事人的去向、查找责任和可核验交接，不展开无关处置细节。"
        elif audience == "employer":
            audience_note = "承包单位版：核对实名雇佣、班次、入场区域及其员工/承包商交接责任。"
            may_state = [f"雇佣单位：{dossier['identity'].get('employer')}", *may_state]
        elif audience in {"commander", "ic", "command"}:
            audience_note = "现场指挥版：同时使用 muster_report 的争议、超时、路线和清场阻塞项。"
        else:
            return {"decision": "unknown_audience", "audience": audience}

        return {
            "audience": audience,
            "person_id": person_id,
            "headline_zh": headline,
            "audience_note_zh": audience_note,
            "may_state_zh": may_state,
            "must_not_state_zh": must_not_state,
            "verification_refs": refs,
            "dossier": dossier,
        }


def legacy_balance_clear(db: Any, person: str) -> bool:
    """Run the old exit-counts-against-entry-counts algorithm."""
    from tenurecourt.muster_note import provisional_clear

    return bool(provisional_clear(db, person))


def comparison_snapshot(db: Any, person: str, board: PersonnelMuster, person_id: str) -> dict[str, Any]:
    """Compare the legacy flat verdict with the verifiable personnel verdict."""
    report = board.person_report(person_id)
    return {
        "person": person,
        "legacy_balance_clear": legacy_balance_clear(db, person),
        "actual_status": report["status"],
        "actual_status_zh": report["status_zh"],
        "actual_accounted": report["accounted"],
    }


_PERSONNEL_BOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS personnel_muster_boards(
    incident_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at REAL
);
"""


def _load_board(db: Any, incident_id: str) -> PersonnelMuster:
    db.execute(_PERSONNEL_BOARD_SCHEMA)
    row = db.execute(
        "SELECT state FROM personnel_muster_boards WHERE incident_id=?", (incident_id,)
    ).fetchone()
    if row is None:
        return PersonnelMuster(incident_id=incident_id)
    state = json.loads(row[0] if not isinstance(row, dict) else row["state"])
    board = PersonnelMuster.__new__(PersonnelMuster)
    board.__dict__.update(state)
    return board


def _save_board(db: Any, board: PersonnelMuster) -> None:
    state = json.dumps(board.__dict__, sort_keys=True, ensure_ascii=False, default=str)
    db.execute(
        """
        INSERT INTO personnel_muster_boards(incident_id, state, updated_at)
        VALUES(?, ?, ?)
        ON CONFLICT(incident_id) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at
        """,
        (board.incident_id, state, board._seq),
    )
    # tenurecourt.store uses isolation_level=None plus an explicit transaction;
    # committing here would close that outer transaction prematurely.
    if getattr(db, "isolation_level", None) is not None:
        db.commit()


def run(db: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """Project-style entry point matching the other tenurecourt modules.

    ``payload['op']`` is the name of a :class:`PersonnelMuster` method.  The
    board is serialized as JSON in SQLite so command handlers remain small.
    """
    command = dict(payload)
    op = command.pop("op", None)
    incident_id = command.pop("incident_id", "muster-1")
    if not op or not hasattr(PersonnelMuster, op):
        raise ValueError(f"unknown personnel_muster op: {op!r}")

    board = _load_board(db, incident_id)
    method = getattr(board, op)
    result = method(**command)
    if isinstance(result, dict):
        _save_board(db, board)
    return result


# Convenient aliases for callers that prefer ledger/board vocabulary.
MusterBoard = PersonnelMuster
MusterLedger = PersonnelMuster
PersonnelMusterLedger = PersonnelMuster
