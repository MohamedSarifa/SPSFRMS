# Assignment audit Stage 1

Stage 1 adds ORM declarations and database-free validation for future audit
records. It does not migrate a database, change assignment workflows, or turn
existing `ComplaintAssignment` rows into verified events.

The model classes are registered in the application's existing SQLAlchemy
metadata. The existing startup table-creation path now excludes these two
audit tables until an explicit reviewed migration is provided; other current
application tables remain in the startup create-all set. No startup was run
for this change. There is no data migration or legacy-row backfill in Stage 1.

`AssignmentEvent` represents a prospective assignment, reassignment,
unassignment, or explicitly unverified legacy snapshot. The snapshot's
`event_at` is an observation time supplied by the caller; it is not evidence
of when the historical assignment happened. Live event validation requires a
staff actor and an eligibility snapshot. Historical eligibility remains
unknown unless an effective-dated period with accepted provenance and evidence
covers the event time. Missing, expired, or overlapping eligibility periods
are rejected by the database-free assignment service.

`StaffEligibilityPeriod` records effective-dated role, department, and
employment status. `unknown` provenance is valid data but never establishes
eligibility. `verified_import` requires an evidence reference. The Python
validators are input checks, not a replacement for database constraints or
independent review of source evidence.

The models have check constraints and restrictive foreign keys, but ORM
declarations alone do not make an append-only ledger. A later database
migration must add and verify database-level protections against update and
delete, define overlap/concurrency rules for eligibility periods, and ensure
the eligibility-period reference belongs to the selected staff member and
department. Stage 2 must also write assignment events and status history in
one transaction with rollback behavior and tests. Until then, the models are
not a deployed audit trail and existing assignment records must not be treated
as verified training labels.
