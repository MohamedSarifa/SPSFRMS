from datetime import datetime, timedelta, timezone

import pytest
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, Integer, Table

from assignment_audit import (
    AuditValidationError,
    build_assignment_event,
    eligibility_is_verified_for,
    legacy_assignment_snapshot,
    normalize_utc_timestamp,
    resolve_unambiguous_eligibility,
    validate_assignment_event,
    validate_eligibility_period,
)
from assignment_audit_models import register_assignment_audit_models


UTC = timezone.utc
NOW = datetime(2025, 4, 5, 12, 0, tzinfo=UTC)


def valid_event(**overrides):
    event = {
        "complaint_id": 11,
        "department_id": 4,
        "event_type": "initial_assignment",
        "previous_staff_id": None,
        "selected_staff_id": 21,
        "actor_type": "user",
        "actor_user_id": 31,
        "actor_key": None,
        "event_at": NOW,
        "provenance": "reviewed_legacy",
        "evidence_ref": "review-2025-04",
        "eligibility_verified": None,
        "eligibility_period_id": None,
    }
    event.update(overrides)
    return event


def valid_period(**overrides):
    period = {
        "staff_id": 21,
        "role": "staff",
        "department_id": 4,
        "employment_status": "active",
        "effective_start": NOW - timedelta(days=1),
        "effective_end": NOW + timedelta(days=1),
        "provenance": "verified_import",
        "evidence_ref": "hr-record-17",
    }
    period.update(overrides)
    return period


def test_valid_initial_reassignment_and_unassignment_shapes():
    assert validate_assignment_event(valid_event())["event_type"] == "initial_assignment"
    assert validate_assignment_event(valid_event(
        event_type="reassignment", previous_staff_id=19
    ))["previous_staff_id"] == 19
    assert validate_assignment_event(valid_event(
        event_type="unassignment", previous_staff_id=21, selected_staff_id=None
    ))["selected_staff_id"] is None


@pytest.mark.parametrize("changes", [
    {"event_type": "initial_assignment", "previous_staff_id": 9},
    {"event_type": "reassignment", "previous_staff_id": 21},
    {"event_type": "reassignment", "previous_staff_id": None},
    {"event_type": "unassignment", "previous_staff_id": None, "selected_staff_id": None},
    {"selected_staff_id": 0},
    {"actor_user_id": None},
])
def test_invalid_event_shapes_and_missing_actor_are_rejected(changes):
    with pytest.raises(AuditValidationError):
        validate_assignment_event(valid_event(**changes))


def test_unknown_event_provenance_cannot_authorize_an_assignment():
    with pytest.raises(AuditValidationError, match="explicit provenance"):
        validate_assignment_event(valid_event(provenance="unknown", evidence_ref=None))


def test_eligibility_period_bounds_and_verified_status():
    checked = validate_eligibility_period(valid_period())
    assert checked["effective_start"].tzinfo == UTC
    assert eligibility_is_verified_for(
        valid_period(), staff_id=21, department_id=4, at=NOW
    ) is True
    with pytest.raises(AuditValidationError, match="later than"):
        validate_eligibility_period(valid_period(effective_end=NOW - timedelta(days=2)))
    with pytest.raises(AuditValidationError, match="requires a department"):
        validate_eligibility_period(valid_period(department_id=None))
    with pytest.raises(AuditValidationError, match="evidence reference"):
        validate_eligibility_period(valid_period(evidence_ref=None))


def test_unknown_or_mismatched_eligibility_is_never_verified():
    unknown = valid_period(provenance="unknown", evidence_ref=None)
    assert eligibility_is_verified_for(
        unknown, staff_id=21, department_id=4, at=NOW
    ) is False
    assert eligibility_is_verified_for(
        valid_period(), staff_id=21, department_id=99, at=NOW
    ) is False
    assert eligibility_is_verified_for(
        valid_period(), staff_id=21, department_id=4, at=NOW + timedelta(days=2)
    ) is False
    with pytest.raises(AuditValidationError, match="ambiguous"):
        resolve_unambiguous_eligibility(
            [valid_period(), valid_period(provenance="unknown", evidence_ref=None)],
            staff_id=21,
            department_id=4,
            at=NOW,
        )
    with pytest.raises(AuditValidationError, match="missing"):
        resolve_unambiguous_eligibility([], staff_id=21, department_id=4, at=NOW)


def test_service_builds_only_new_eligible_assignment_events():
    event = build_assignment_event(
        complaint_id=11,
        department_id=4,
        previous_staff_id=None,
        selected_staff_id=21,
        actor_user_id=31,
        event_at=NOW,
        eligibility_period_id=7,
        eligibility_periods=[valid_period()],
        request_id="request-1",
    )
    assert event["event_type"] == "initial_assignment"
    assert event["eligibility_verified"] is True
    with pytest.raises(AuditValidationError, match="repeated assignment"):
        build_assignment_event(
            complaint_id=11,
            department_id=4,
            previous_staff_id=21,
            selected_staff_id=21,
            actor_user_id=31,
            event_at=NOW,
            eligibility_period_id=7,
            eligibility_periods=[valid_period()],
        )
    with pytest.raises(AuditValidationError, match="unknown or does not cover"):
        build_assignment_event(
            complaint_id=11,
            department_id=4,
            previous_staff_id=None,
            selected_staff_id=21,
            actor_user_id=31,
            event_at=NOW,
            eligibility_period_id=8,
            eligibility_periods=[valid_period(provenance="unknown", evidence_ref=None)],
        )


def test_timestamps_are_converted_to_utc_and_naive_values_rejected():
    local = datetime(2025, 4, 5, 17, 30, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert normalize_utc_timestamp(local) == NOW
    with pytest.raises(AuditValidationError, match="include a timezone"):
        normalize_utc_timestamp(datetime(2025, 4, 5, 12, 0))


def test_legacy_assignment_is_only_an_unverified_snapshot():
    snapshot = legacy_assignment_snapshot(
        complaint_id=11, department_id=4, selected_staff_id=21, observed_at=NOW
    )
    assert snapshot["event_type"] == "legacy_snapshot"
    assert snapshot["provenance"] == "migration_backfill"
    assert snapshot["actor_type"] == "legacy_unknown"
    assert snapshot["eligibility_verified"] is None
    with pytest.raises(AuditValidationError):
        validate_assignment_event({**snapshot, "eligibility_verified": True})


def test_models_register_on_supplied_extension_without_creating_tables():
    db = SQLAlchemy()
    eligibility_model, event_model = register_assignment_audit_models(db)
    assert eligibility_model.__tablename__ == "staff_eligibility_period"
    assert event_model.__tablename__ == "assignment_event"
    assert "event_at" in event_model.__table__.columns
    assert "eligibility_period_id" in event_model.__table__.columns
    assert any(item.name == "ck_assignment_event_shape" for item in event_model.__table__.constraints)
    assert any(item.name == "ck_staff_eligibility_period_order" for item in eligibility_model.__table__.constraints)
    existing_table = Table(
        "existing_application_table", db.metadata,
        Column("id", Integer, primary_key=True),
    )
    from assignment_audit_models import application_tables_excluding_audit
    startup_tables = application_tables_excluding_audit(db.metadata)
    assert event_model.__table__ not in startup_tables
    assert eligibility_model.__table__ not in startup_tables
    assert existing_table in startup_tables
