"""SQLAlchemy model factory for prospective assignment audit records.

The factory receives the application's existing Flask-SQLAlchemy extension so
this module never imports ``app`` and cannot create an import cycle.
"""

from datetime import datetime, timezone


AUDIT_TABLE_NAMES = frozenset({"assignment_event", "staff_eligibility_period"})


def utc_now():
    return datetime.now(timezone.utc)


def application_tables_excluding_audit(metadata):
    """Return existing application tables except audit tables awaiting migration."""
    return [table for table in metadata.tables.values() if table.name not in AUDIT_TABLE_NAMES]


def register_assignment_audit_models(db):
    class StaffEligibilityPeriod(db.Model):
        __tablename__ = "staff_eligibility_period"
        __table_args__ = (
            db.CheckConstraint(
                "effective_end IS NULL OR effective_end > effective_start",
                name="ck_staff_eligibility_period_order",
            ),
            db.CheckConstraint(
                "role IN ('staff', 'department_head', 'unknown')",
                name="ck_staff_eligibility_period_role",
            ),
            db.CheckConstraint(
                "role <> 'staff' OR department_id IS NOT NULL",
                name="ck_staff_eligibility_period_staff_department",
            ),
            db.CheckConstraint(
                "employment_status IN ('active', 'inactive', 'leave', 'terminated', 'unknown')",
                name="ck_staff_eligibility_period_status",
            ),
            db.CheckConstraint(
                "provenance IN ('live_application', 'verified_import', 'migration_backfill', 'unknown')",
                name="ck_staff_eligibility_period_provenance",
            ),
            db.CheckConstraint(
                "provenance <> 'verified_import' OR "
                "(evidence_ref IS NOT NULL AND length(trim(evidence_ref)) > 0)",
                name="ck_staff_eligibility_period_import_evidence",
            ),
            db.Index(
                "ix_staff_eligibility_period_staff_window",
                "staff_id", "effective_start", "effective_end",
            ),
        )

        id = db.Column(db.Integer, primary_key=True)
        staff_id = db.Column(
            db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False, index=True,
        )
        role = db.Column(db.String(20), nullable=False)
        department_id = db.Column(
            db.Integer, db.ForeignKey("department.id", ondelete="RESTRICT"),
        )
        employment_status = db.Column(db.String(20), nullable=False)
        effective_start = db.Column(db.DateTime(timezone=True), nullable=False)
        effective_end = db.Column(db.DateTime(timezone=True))
        provenance = db.Column(db.String(30), nullable=False, default="unknown")
        evidence_ref = db.Column(db.String(255))
        created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    class AssignmentEvent(db.Model):
        __tablename__ = "assignment_event"
        __table_args__ = (
            db.CheckConstraint(
                "event_type IN ('initial_assignment', 'reassignment', 'unassignment', 'legacy_snapshot')",
                name="ck_assignment_event_type",
            ),
            db.CheckConstraint(
                "(event_type = 'initial_assignment' AND previous_staff_id IS NULL "
                "AND selected_staff_id IS NOT NULL) OR "
                "(event_type = 'reassignment' AND previous_staff_id IS NOT NULL "
                "AND selected_staff_id IS NOT NULL AND previous_staff_id <> selected_staff_id) OR "
                "(event_type = 'unassignment' AND previous_staff_id IS NOT NULL "
                "AND selected_staff_id IS NULL) OR "
                "(event_type = 'legacy_snapshot' AND previous_staff_id IS NULL "
                "AND selected_staff_id IS NOT NULL)",
                name="ck_assignment_event_shape",
            ),
            db.CheckConstraint(
                "(actor_type = 'user' AND actor_user_id IS NOT NULL AND actor_key IS NULL) OR "
                "(actor_type = 'system' AND actor_user_id IS NULL "
                "AND actor_key IS NOT NULL AND length(trim(actor_key)) > 0) OR "
                "(actor_type = 'legacy_unknown' AND actor_user_id IS NULL AND actor_key IS NULL "
                "AND event_type = 'legacy_snapshot')",
                name="ck_assignment_event_actor",
            ),
            db.CheckConstraint(
                "provenance IN ('live_application', 'migration_backfill', 'reviewed_legacy', 'unknown')",
                name="ck_assignment_event_provenance",
            ),
            db.CheckConstraint(
                "(event_type = 'legacy_snapshot' AND provenance IN ('migration_backfill', 'unknown') "
                "AND eligibility_verified IS NULL) OR "
                "(event_type <> 'legacy_snapshot' AND provenance IN ('live_application', 'reviewed_legacy'))",
                name="ck_assignment_event_legacy_provenance",
            ),
            db.CheckConstraint(
                "(eligibility_verified IS NULL AND eligibility_period_id IS NULL "
                "AND eligibility_role_snapshot IS NULL "
                "AND eligibility_department_snapshot_id IS NULL "
                "AND eligibility_employment_status_snapshot IS NULL "
                "AND eligibility_valid_from_snapshot IS NULL "
                "AND eligibility_valid_to_snapshot IS NULL) OR "
                "(eligibility_verified IS TRUE AND eligibility_period_id IS NOT NULL "
                "AND eligibility_role_snapshot = 'staff' "
                "AND eligibility_department_snapshot_id = department_id "
                "AND eligibility_employment_status_snapshot = 'active' "
                "AND eligibility_valid_from_snapshot IS NOT NULL "
                "AND event_at >= eligibility_valid_from_snapshot "
                "AND (eligibility_valid_to_snapshot IS NULL "
                "OR event_at < eligibility_valid_to_snapshot))",
                name="ck_assignment_event_eligibility_snapshot",
            ),
            db.CheckConstraint(
                "provenance <> 'reviewed_legacy' OR "
                "(evidence_ref IS NOT NULL AND length(trim(evidence_ref)) > 0)",
                name="ck_assignment_event_review_evidence",
            ),
            db.CheckConstraint(
                "provenance <> 'live_application' OR eligibility_verified IS TRUE",
                name="ck_assignment_event_live_eligibility",
            ),
            db.Index("ix_assignment_event_complaint_time", "complaint_id", "event_at", "id"),
            db.Index("ix_assignment_event_selected_staff", "selected_staff_id", "event_at"),
        )

        id = db.Column(db.Integer, primary_key=True)
        complaint_id = db.Column(
            db.Integer, db.ForeignKey("complaint.id", ondelete="RESTRICT"),
            nullable=False, index=True,
        )
        department_id = db.Column(
            db.Integer, db.ForeignKey("department.id", ondelete="RESTRICT"),
            nullable=False,
        )
        event_type = db.Column(db.String(30), nullable=False)
        previous_staff_id = db.Column(
            db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"),
        )
        selected_staff_id = db.Column(
            db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"),
        )
        actor_type = db.Column(db.String(20), nullable=False, default="user")
        actor_user_id = db.Column(
            db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"),
        )
        actor_key = db.Column(db.String(100))
        event_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)
        provenance = db.Column(db.String(30), nullable=False, default="live_application")
        eligibility_period_id = db.Column(
            db.Integer,
            db.ForeignKey("staff_eligibility_period.id", ondelete="RESTRICT"),
        )
        eligibility_verified = db.Column(db.Boolean)
        eligibility_role_snapshot = db.Column(db.String(20))
        eligibility_department_snapshot_id = db.Column(db.Integer)
        eligibility_employment_status_snapshot = db.Column(db.String(20))
        eligibility_valid_from_snapshot = db.Column(db.DateTime(timezone=True))
        eligibility_valid_to_snapshot = db.Column(db.DateTime(timezone=True))
        request_id = db.Column(db.String(100), index=True)
        evidence_ref = db.Column(db.String(255))
        created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utc_now)

    return StaffEligibilityPeriod, AssignmentEvent
