"""Database-free validation helpers for prospective assignment audit records.

This module deliberately imports no application or ORM code.  It validates
payloads before persistence; it does not provide database-level immutability.
Naive datetimes are rejected because their timezone cannot be established
reliably.  Legacy assignment snapshots describe reconstructed state, not a
verified assignment event.
"""

from datetime import datetime, timezone


class AuditValidationError(ValueError):
    """Raised when an assignment audit payload is incomplete or inconsistent."""


EVENT_TYPES = frozenset({
    "initial_assignment", "reassignment", "unassignment", "legacy_snapshot",
})
EVENT_PROVENANCE = frozenset({
    "live_application", "migration_backfill", "reviewed_legacy", "unknown",
})
ELIGIBILITY_PROVENANCE = frozenset({
    "live_application", "verified_import", "migration_backfill", "unknown",
})
EMPLOYMENT_STATUSES = frozenset({"active", "inactive", "leave", "terminated", "unknown"})


def normalize_utc_timestamp(value, field_name="timestamp"):
    """Return an aware UTC datetime; reject values with no explicit timezone."""
    if not isinstance(value, datetime):
        raise AuditValidationError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuditValidationError(f"{field_name} must include a timezone")
    return value.astimezone(timezone.utc)


def _required_id(record, key):
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AuditValidationError(f"{key} must be a positive integer")
    return value


def validate_eligibility_period(period):
    """Validate an effective-dated staff period and return normalized bounds."""
    if not isinstance(period, dict):
        raise AuditValidationError("eligibility period must be a mapping")
    _required_id(period, "staff_id")
    role = period.get("role")
    if not isinstance(role, str) or role not in {"staff", "department_head", "unknown"}:
        raise AuditValidationError("role is not supported")
    department_id = period.get("department_id")
    if department_id is not None and (
        isinstance(department_id, bool) or not isinstance(department_id, int) or department_id <= 0
    ):
        raise AuditValidationError("department_id must be a positive integer or null")
    if role == "staff" and department_id is None:
        raise AuditValidationError("staff eligibility requires a department")
    status = period.get("employment_status")
    if not isinstance(status, str) or status not in EMPLOYMENT_STATUSES:
        raise AuditValidationError("employment_status is not supported")
    provenance = period.get("provenance", "unknown")
    if not isinstance(provenance, str) or provenance not in ELIGIBILITY_PROVENANCE:
        raise AuditValidationError("eligibility provenance is not supported")
    evidence_ref = period.get("evidence_ref")
    if evidence_ref is not None and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
        raise AuditValidationError("evidence_ref must be a non-empty string or null")
    if provenance == "verified_import" and not evidence_ref:
        raise AuditValidationError("verified_import requires an evidence reference")

    start = normalize_utc_timestamp(period.get("effective_start"), "effective_start")
    end_value = period.get("effective_end")
    end = normalize_utc_timestamp(end_value, "effective_end") if end_value is not None else None
    if end is not None and end <= start:
        raise AuditValidationError("effective_end must be later than effective_start")
    return {**period, "effective_start": start, "effective_end": end, "provenance": provenance}


def eligibility_is_verified_for(period, *, staff_id, department_id, at):
    """Return true only for an evidenced, active staff period covering ``at``."""
    normalized = validate_eligibility_period(period)
    event_at = normalize_utc_timestamp(at, "event_at")
    if normalized["provenance"] not in {"live_application", "verified_import"}:
        return False
    return (
        normalized["staff_id"] == staff_id
        and normalized["role"] == "staff"
        and normalized["department_id"] == department_id
        and normalized["employment_status"] == "active"
        and normalized["effective_start"] <= event_at
        and (normalized["effective_end"] is None or event_at < normalized["effective_end"])
    )


def resolve_unambiguous_eligibility(periods, *, staff_id, department_id, at):
    """Require exactly one effective-dated period for the staff at event time."""
    if not isinstance(periods, (list, tuple)) or not periods:
        raise AuditValidationError("eligibility periods are missing")
    event_at = normalize_utc_timestamp(at, "event_at")
    normalized_periods = [validate_eligibility_period(period) for period in periods]
    if any(period["staff_id"] != staff_id for period in normalized_periods):
        raise AuditValidationError("eligibility records do not all belong to the selected staff member")

    covering = [
        period for period in normalized_periods
        if period["effective_start"] <= event_at
        and (period["effective_end"] is None or event_at < period["effective_end"])
    ]
    if not covering:
        raise AuditValidationError("no eligibility period covers this event")
    if len(covering) != 1:
        raise AuditValidationError("eligibility at this event time is ambiguous")
    selected = covering[0]
    if not eligibility_is_verified_for(
        selected, staff_id=staff_id, department_id=department_id, at=event_at
    ):
        raise AuditValidationError("selected staff eligibility is unknown or does not cover this event")
    return selected


def validate_assignment_event(event):
    """Validate an event payload; unknown historical eligibility remains unknown."""
    if not isinstance(event, dict):
        raise AuditValidationError("assignment event must be a mapping")
    _required_id(event, "complaint_id")
    _required_id(event, "department_id")
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise AuditValidationError("event_type is not supported")
    provenance = event.get("provenance", "unknown")
    if not isinstance(provenance, str) or provenance not in EVENT_PROVENANCE:
        raise AuditValidationError("event provenance is not supported")
    event_at = normalize_utc_timestamp(event.get("event_at"), "event_at")

    previous = event.get("previous_staff_id")
    selected = event.get("selected_staff_id")
    for name, value in (("previous_staff_id", previous), ("selected_staff_id", selected)):
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise AuditValidationError(f"{name} must be a positive integer or null")
    valid_shape = (
        (event_type == "initial_assignment" and previous is None and selected is not None)
        or (event_type == "reassignment" and previous is not None and selected is not None and previous != selected)
        or (event_type == "unassignment" and previous is not None and selected is None)
        or (event_type == "legacy_snapshot" and previous is None and selected is not None)
    )
    if not valid_shape:
        raise AuditValidationError("staff fields do not match event_type")

    actor_type = event.get("actor_type")
    actor_user_id = event.get("actor_user_id")
    actor_key = event.get("actor_key")
    if actor_type == "user":
        _required_id({"actor_user_id": actor_user_id}, "actor_user_id")
        if actor_key is not None:
            raise AuditValidationError("user actor cannot include actor_key")
    elif actor_type == "system":
        if actor_user_id is not None or not isinstance(actor_key, str) or not actor_key.strip():
            raise AuditValidationError("system actor requires actor_key and no user id")
    elif actor_type == "legacy_unknown":
        if actor_user_id is not None or actor_key is not None or event_type != "legacy_snapshot":
            raise AuditValidationError("unknown actor is allowed only for legacy snapshots")
    else:
        raise AuditValidationError("actor_type is not supported")

    evidence_ref = event.get("evidence_ref")
    if evidence_ref is not None and (not isinstance(evidence_ref, str) or not evidence_ref.strip()):
        raise AuditValidationError("evidence_ref must be a non-empty string or null")
    eligibility_verified = event.get("eligibility_verified")
    if eligibility_verified is not True and eligibility_verified is not None:
        raise AuditValidationError("eligibility_verified must be true or unknown")

    if event_type == "legacy_snapshot":
        if provenance not in {"migration_backfill", "unknown"} or actor_type != "legacy_unknown":
            raise AuditValidationError("legacy snapshots must retain unknown provenance and actor")
        if eligibility_verified is not None or event.get("eligibility_period_id") is not None:
            raise AuditValidationError("legacy snapshots cannot have verified eligibility")
    else:
        if provenance not in {"live_application", "reviewed_legacy"}:
            raise AuditValidationError("non-snapshot events need explicit provenance")
        if provenance == "reviewed_legacy" and not evidence_ref:
            raise AuditValidationError("reviewed legacy events require an evidence reference")

    if eligibility_verified is True:
        period_id = event.get("eligibility_period_id")
        if isinstance(period_id, bool) or not isinstance(period_id, int) or period_id <= 0:
            raise AuditValidationError("verified eligibility requires a period id")
        if event.get("eligibility_role_snapshot") != "staff":
            raise AuditValidationError("verified eligibility requires a staff role snapshot")
        snapshot_department = event.get("eligibility_department_snapshot_id")
        if (
            isinstance(snapshot_department, bool)
            or not isinstance(snapshot_department, int)
            or snapshot_department <= 0
            or snapshot_department != event["department_id"]
        ):
            raise AuditValidationError("eligibility department must match the event department")
        if event.get("eligibility_employment_status_snapshot") != "active":
            raise AuditValidationError("verified eligibility requires active employment")
        start = normalize_utc_timestamp(event.get("eligibility_valid_from_snapshot"), "eligibility_valid_from_snapshot")
        end_value = event.get("eligibility_valid_to_snapshot")
        end = normalize_utc_timestamp(end_value, "eligibility_valid_to_snapshot") if end_value is not None else None
        if start > event_at or (end is not None and (event_at >= end or end <= start)):
            raise AuditValidationError("event time must be within the eligibility snapshot")
    elif any(event.get(key) is not None for key in (
        "eligibility_period_id", "eligibility_role_snapshot",
        "eligibility_department_snapshot_id", "eligibility_employment_status_snapshot",
        "eligibility_valid_from_snapshot", "eligibility_valid_to_snapshot",
    )):
        raise AuditValidationError("unknown eligibility cannot carry a partial verification snapshot")

    if provenance == "live_application" and eligibility_verified is not True:
        raise AuditValidationError("live assignments require verified eligibility")
    return {**event, "event_at": event_at, "provenance": provenance}


def build_assignment_event(
    *, complaint_id, department_id, previous_staff_id, selected_staff_id,
    actor_user_id, event_at, eligibility_period_id, eligibility_periods,
    request_id=None,
):
    """Build a validated initial-assignment or reassignment payload.

    This is deliberately persistence-free. Callers must use it inside the
    future transaction that updates the complaint and writes related history.
    """
    if previous_staff_id == selected_staff_id:
        raise AuditValidationError("repeated assignment does not create a new event")
    if selected_staff_id is None:
        raise AuditValidationError("this service builds assignments, not unassignments")
    event_type = "initial_assignment" if previous_staff_id is None else "reassignment"
    normalized_at = normalize_utc_timestamp(event_at, "event_at")
    normalized_period = resolve_unambiguous_eligibility(
        eligibility_periods,
        staff_id=selected_staff_id,
        department_id=department_id,
        at=normalized_at,
    )
    payload = {
        "complaint_id": complaint_id,
        "department_id": department_id,
        "event_type": event_type,
        "previous_staff_id": previous_staff_id,
        "selected_staff_id": selected_staff_id,
        "actor_type": "user",
        "actor_user_id": actor_user_id,
        "actor_key": None,
        "event_at": normalized_at,
        "provenance": "live_application",
        "eligibility_period_id": eligibility_period_id,
        "eligibility_verified": True,
        "eligibility_role_snapshot": normalized_period["role"],
        "eligibility_department_snapshot_id": normalized_period["department_id"],
        "eligibility_employment_status_snapshot": normalized_period["employment_status"],
        "eligibility_valid_from_snapshot": normalized_period["effective_start"],
        "eligibility_valid_to_snapshot": normalized_period["effective_end"],
        "request_id": request_id,
    }
    return validate_assignment_event(payload)


def legacy_assignment_snapshot(*, complaint_id, department_id, selected_staff_id, observed_at):
    """Describe a backfill observation without asserting it is an assignment event.

    ``observed_at`` is when a legacy row was inspected, not a reconstructed
    assignment timestamp. The caller must never upgrade this snapshot to a
    verified event without independent, record-level evidence.
    """
    return validate_assignment_event({
        "complaint_id": complaint_id,
        "department_id": department_id,
        "event_type": "legacy_snapshot",
        "previous_staff_id": None,
        "selected_staff_id": selected_staff_id,
        "actor_type": "legacy_unknown",
        "actor_user_id": None,
        "actor_key": None,
        "event_at": observed_at,
        "provenance": "migration_backfill",
        "eligibility_verified": None,
        "eligibility_period_id": None,
    })
