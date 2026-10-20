# SPSFRMS Implementation Audit

## Scope and execution safety

The Flask application, templates, static assets, and isolated tests were inspected and updated. No Neon setup, migration, seed, account import, password reset, table recreation, or live database write was run. Tests set `SPSFRMS_SKIP_STARTUP_DB_INIT=1` before importing the application and use an isolated SQLite database.

## Findings, fixes, and verification

| Area | Issue and cause | Fix | Files | Verification |
|---|---|---|---|---|
| Text analysis and routing | Broad or overlapping terms could produce unstable routing. Categories and destination departments were mixed in the analysis explanation. Preview truncated at 5,000 characters while a crafted submission could exceed that limit. | Kept transparent phrase rules, used longer specific matches before overlapping generic phrases, distinguished category from destination, sent weak/conflicting evidence to a review queue, and routed clear drainage blockage/overflow/sewage to Drainage. Preview and submit call the same analysis function; server-side submission now enforces the same 5,000-character limit as the form/preview. | `app.py`, `templates/submit_complaint.html`, `static/js/script.js`, `templates/track_complaint.html`, `tests/test_workflow.py` | Classification examples, preview-versus-saved-department, and oversized-submission rejection tests. |
| Department identity | Exact-case lookups could fail to reuse a department whose name differed only by case. | Department lookup/seeding uses case-insensitive name matching; submission resolves the same canonical mapping as preview. No new schema constraint was added. | `app.py` | Existing department lookup behavior covered by the routing tests; no live seed was run. |
| Queue ordering | There was no deterministic tie-break rule and emergency text could be hidden by stored priority. | Sort explicit emergency signals first, then stored High/Medium/Low priority, oldest submission, and complaint ID. No SLA fields exist, so no deadline ordering is claimed. | `app.py`, `README.txt` | Tests cover emergency precedence, same-priority oldest-first order, then ID. |
| Staff recommendation | Assignment UI lacked a workload-based reason and could include staff from another department if the route did not validate. | Recommend the eligible department staff member with the fewest active assigned complaints, then name as a deterministic tie-breaker. Display workload and recommendation reason; the head confirms assignment. Server route enforces staff role and department. Skills and availability are not modeled. | `app.py`, `templates/officer_dashboard.html`, `README.txt` | Tests verify lower workload recommendation and reject cross-department staff. |
| Department-head assignment UI | Assignment controls were crowded and repeated submission to the same staff member could add duplicate assignment history. | Use a compact row and per-complaint modal with eligible staff, workload, current assignee, assignment note, and separate Ignore/Restore actions. Repeating the same assignment is now a no-op with feedback. Resolved/ignored/in-progress states explain unavailable assignment. | `app.py`, `templates/officer_dashboard.html`, `static/css/style.css` | Dashboard rendering, valid assignment, resolved rejection, and repeated-assignment tests. |
| Ignore workflow | The reason was optional; a fallback string could make an unreasoned action look documented. | Require a non-empty reason in the form and route. Only an authorized head can ignore a complaint in their department. Resolved or already ignored complaints are rejected. The complaint is retained and history records prior/new status, actor, timestamp, and reason. Restore returns to the prior status and adds a history row. | `app.py`, `templates/officer_dashboard.html`, `templates/track_complaint.html` | Tests cover missing reason, valid ignore/restore, repeated ignore, resolved complaint, and removal from normal queues. |
| Start Work and status transitions | Stale or invalid transitions could produce confusing outcomes. | Validate each transition on the server. Ignored items cannot be started; resolved items cannot be assigned or started again; resolution requires a note. Invalid actions redirect with feedback and do not add history. | `app.py`, `templates/officer_dashboard.html`, `templates/track_complaint.html` | Tests cover Start Work, resolution, invalid/stale transitions, and resolved Start Work. |
| Forms and authorization | Route/form agreement was not covered systematically; assignment and workflow actions needed server-side department/role checks. | Reviewed login, registration, submission, preview, assignment, ignore, restore, status, tracking, and admin routes. Required values are validated where actions depend on them; protected workflow routes enforce role and ownership/department checks. | `app.py`, relevant templates, `tests/test_workflow.py` | Tests exercise login role, missing staff, invalid status, unauthorized role, and cross-department access. |
| Neon connection handling | The reported SSL-close error is consistent with a connection dropped by the remote service or network while pooled; the supplied log excerpt does not prove a single cause. Default pooling had no stale-connection preflight. Startup caught initialization failures but health only reported whether a later `SELECT 1` worked, which could imply initialization succeeded. | Added SQLAlchemy `pool_pre_ping` and a 300-second `pool_recycle`. Startup state is tracked; `/health` reports degraded/503 if the DB is reachable but setup was skipped or never run. The status indicator distinguishes this from a database outage. Error messages expose neither credentials nor exception text. No automatic retry of writes was added. Flask-SQLAlchemy removes scoped sessions at app-context teardown. | `app.py`, `static/js/script.js`, `README.txt` | Isolated test simulates a database failure and checks 503 without error-detail leakage. No Neon connection attempt was made in this task. |

## Current analysis and data limits

Complaint classification is a deterministic, explainable phrase-rule system. There is no trained machine-learning model. Strong conflicting matches or insufficient evidence become `Other` and are flagged for human review in the Public Health review queue. The category and destination department remain separate values. The review flag is computed from complaint text and is not a persisted schema field.

Priority is derived from the configured emergency and medium phrase lists. Queue order is emergency signal, stored priority, wait time, then complaint ID. Workload counts active assignments (excluding Resolved and Ignored). The current schema has no SLA/deadline, staff skills, staff availability, or priority-source fields, so those are not invented or used.

Ignore and Restore use the existing complaint status and `complaint_status_history` fields. No schema change was needed for these features.

### `Multiple Departments` category review

`Multiple Departments` is not in the active `CATEGORIES`, `CATEGORY_DEPARTMENTS`, or phrase classifier. Current complaint submission always persists one classifier category and one primary `department_id`; it cannot create a multi-department complaint. The existing database count therefore identifies an unsupported legacy/manual value. The code and aggregate preflight count cannot establish whether that row was entered accidentally or intentionally, and this audit did not reconnect to Neon to inspect the row.

The `complaint_department` association table can store links to multiple departments, but the live workflow still has one primary department, one `assigned_staff_id`, one status, and complaint-wide assignment/status history. It cannot track independent work or status for each department. It is therefore not safe to treat the association links as independent assignments.

The safe application behavior is read-only manual review: active classification and routing remain unchanged; a department head may view the record only when their department is its primary or explicitly linked department; all department-head Assign, Ignore, and Restore actions are unavailable and rejected server-side; an already assigned staff member cannot Start Work or change its global status. Tracking displays the primary and linked department names. No schema or live data was changed. SQLite regression tests verify visibility, authorization, and no-mutation behavior.

The same status-change guard rejects Resolve attempts for this category, including admin status-route submissions. Tracking remains available only to the owning citizen, an explicitly linked/primary department head, the explicitly assigned staff member, or an admin. Tests cover an unrelated citizen, unassigned staff, unlinked head, and rejected action/history invariance.

An earlier version of the read-only preflight included individual multi-department complaint identifiers, statuses, and department links. The final preflight no longer returns complaint-level details; it reports only aggregate counts of categories without routing mappings. The user-supplied prior report identifies complaint ID 1 as a resolved `Multiple Departments` record, but its intended resolving department remains unknown. Preserve that record and history. Do not infer an owner from its primary or linked departments. Any correction requires separate authorized evidence and review; independent departmental work would require a separately designed workflow.

### Resolved multi-department complaint from the supplied preflight

The user-provided read-only preflight findings identify complaint ID 1 as `Multiple Departments`, status `Resolved`, primary department Road Damage (ID 1), with Road Damage (ID 1) and Traffic (ID 7) in `complaint_department`. The aggregate history-gap counts are both zero. The status-history check establishes that this complaint has at least one status-history row, but not that the rows document a Resolved transition or who performed it. The assignment-history check only looks for a missing history row when a complaint currently has an assigned staff ID, so it does not by itself establish that this complaint has an assignment-history row. Neither aggregate includes the assignee, status-change actor, event sequence, or department membership of the actor at resolution time. `ComplaintAssignment` records staff/assigner IDs but no department snapshot; `ComplaintStatusHistory` records an updater ID and status event but no department ID. The application therefore cannot establish from the supplied evidence whether Road Damage or Traffic performed the resolution. The intended owner is **undetermined**.

Neither current initialization routing nor the category map should change this complaint's category or primary department: `Multiple Departments` is not a key in `CATEGORY_DEPARTMENTS`, so the initializer's category-to-department loop skips it. The supplied preview also reports zero department seeds, department-ID changes, history backfills, and legacy-role conversions. This is not a guarantee about unrelated schema setup performed by ordinary startup; no startup was run for this review.

Recommendation: retain complaint ID 1 as a resolved, manually reviewed legacy record for now. Do not reclassify it to Road Damage or Traffic based only on the primary link or the association rows. If contemporaneous records or an authorized department review establish one owner, prepare a separate reviewed correction to the category and primary department while retaining the historical link/status evidence as appropriate. If the intended record represents independent work by both departments, design a separate per-department workflow before attempting a data conversion. No live record was changed and no Neon reconnection was made for this review.

## Database and safe verification notes

### Exact normal startup and initializer effects

Importing `app.py` calls `run_startup_database_setup()`. By default, `SPSFRMS_STARTUP_MODE=initialize` is selected and startup is write-capable. Set `SPSFRMS_STARTUP_MODE=skip` to serve the Flask app without calling `initialize_database()` or `create_demo_users()`. If the new variable is unset, the legacy `SPSFRMS_SKIP_STARTUP_DB_INIT=1` still selects skip mode. An invalid explicit mode fails closed as `configuration_error`; it does not initialize. Repository search confirms these initializer functions are called only inside `run_startup_database_setup()`.

Skip mode performs no database operation during app import. Requests can still perform their ordinary application reads and writes; `/health` deliberately performs a read-only `SELECT 1`. Tests set skip mode and SQLite before importing the app, assert the engine dialect is SQLite and startup state is `not_run`, and verify that no tables exist before isolated test setup.

`initialize_database()` performs the following operations across DDL and data transactions; they are not one global all-or-nothing transaction:

1. `db.create_all()` creates missing model tables: `user`, `department`, `complaint`, `complaint_department`, `complaint_assignment`, and `complaint_status_history`. It does not alter columns in existing tables.
2. `migrate_schema()` runs these PostgreSQL statements:
   - Add nullable `user.department_id` referencing `department(id)` with `ON DELETE SET NULL` if missing.
   - Add nullable `department.head_id` referencing `user(id)` with `ON DELETE SET NULL` if missing.
   - Add nullable `complaint.assigned_staff_id` referencing `user(id)` with `ON DELETE SET NULL` if missing.
   - Add nullable `complaint.resolution_note TEXT` if missing.
   - Drop `NOT NULL` from `complaint_assignment.assigned_by`.
   - Create a partial unique index on non-null `department.head_id`.
   - Create indexes on `user.department_id` and `complaint.assigned_staff_id`.
   - Update existing `user.role='officer'` values to `department_head`.
3. For each canonical department in `DEPARTMENT_NAMES`, do a case-insensitive name lookup. If absent, insert one with a generated local email (`lowercase name with spaces removed` + `@spsfrms.local`).
4. For every existing complaint whose category is in `CATEGORY_DEPARTMENTS`, look up the canonical department by case-insensitive name and update `complaint.department_id` when it differs. This can re-route existing complaints, including legacy category/department pairs.
5. For existing complaints with `assigned_staff_id` but no assignment-history row, insert a backfill `complaint_assignment` row. For complaints with no status-history rows, insert an initial `complaint_status_history` row.
6. Commit the ORM department/routing/history changes. DDL from `create_all()` and the explicit migration transaction may already be committed if a later startup step fails.

`create_demo_users()` follows successful initialization. It checks for the demo admin using normalized, case-insensitive email identity and only creates it when the explicit creation flag is enabled and no logical match exists. If `SPSFRMS_SEED_DEMO_DATA=1`, it reads seed JSON, validates an exact bounded per-email password map for missing accounts, and creates only missing users; an existing matching account is not modified. New staff/head, seed, and imported account passwords require at least 12 characters. Password-reuse checks verify candidate values against Werkzeug salted hashes, but are best-effort and are not globally serialized across concurrent application workers. The bulk all-account password reset utility is retired and performs no database access.

The `/health` route performs only `SELECT 1`. It reports HTTP 200 with `status=ok`, `database=connected`, `initialization=initialized` only when the database responds and startup setup completed. If the database responds but setup was skipped, failed, or had an invalid mode, it returns HTTP 503 with `status=degraded`, `database=connected`, and the actual initialization state (`not_run`, `failed`, or `configuration_error`). If the DB check fails, it returns HTTP 503 with `status=error`, `database=not connected`, and the current initialization state. Startup and health logs include only the exception class, not exception text, URLs, credentials, or personal data.

No startup or seed operation ran against Neon during this task. A startup failure can leave previously committed DDL or earlier initialization writes in place; `db.session.rollback()` only rolls back the current ORM transaction. No live startup or Neon SSL recovery has been verified.

### Read-only preflight command

`inspect_neon_preflight.py` is a separate entry point. It imports only the database URL helper and the shared routing constants; it does not import `app.py`, create a Flask app, or call `initialize_database()` / `create_demo_users()`. It requires `DATABASE_URL` from the environment or `.env` and intentionally has no fallback URL. The shared `database_config.py` normalizes PostgreSQL URLs for psycopg, and `app.py` now uses the same helper so URL handling does not drift. The category-to-department map lives in `workflow_routing.py`, which both the Flask app and preflight import without startup side effects.

The inspector issues only `SELECT` statements. Its query helper rejects non-SELECT statements, stacked SQL statements, and row-locking clauses. For PostgreSQL, it also requests read-only transactions using the connection startup option `default_transaction_read_only=on`. Its SQLite and PostgreSQL schema discovery uses `SELECT`; it has no write SQL or initializer import. It reports expected table/column presence, department IDs/names, case-insensitive department-name duplicates, missing canonical departments, role counts and legacy `officer` count, the demo-admin existence flag, complaint category/current/expected department mapping counts and discrepancies, assignment/status history gaps, and a count preview of initializer effects. Where duplicate canonical department names make the initializer's `.first()` target indeterminate, it marks the affected complaint update count uncertain. It never selects complaint text or password values and does not output emails, tokens, or the database URL. If optional seed data is enabled, it flags that the seed file requires separate review rather than printing its contents.

The preflight reports complaints with unmapped categories only as aggregate category/count pairs. It does not retrieve individual complaint IDs, statuses, department links, or any complaint-level metadata. The separate prior user-provided findings for complaint ID 1 remain the only evidence recorded here; this audit did not reconnect to Neon or retrieve additional details.

The only required setting is `DATABASE_URL`, available either in the project `.env` file or in the PowerShell process environment. The script has no fallback URL and never prints the configured value. Review the script before running it. From PowerShell, run:

```powershell
Set-Location E:\SPSFRMS
.\venv\Scripts\python.exe .\inspect_neon_preflight.py
```

The script has **not** been run against Neon during this task. It does not import the Flask app, initialize it, or modify database data. Do not add the URL to the command or share environment dumps.

Review `schema.tables_missing` and `schema.columns_missing`, department duplicate/missing lists, complaint mapping discrepancies, `complaint_categories_without_mapping`, assignment/status history gaps, and initializer effect counts. A missing table or column means the related check may be unavailable; consult schema missing lists before interpreting a null count as zero. Categories without mappings are aggregate category/count pairs, not individual complaint records. Do not infer a complaint's owner from its primary department or association links.

If the command exits nonzero or reports `inspection_failed`, stop and report only the JSON `status` and `error_type`. The script intentionally suppresses driver details and connection strings; do not print environment variables, paste `.env`, or retry with a URL in the command. Resolve configuration/connectivity separately before another reviewed attempt. Do not start the application or run its initializer as a response to a preflight error.

For a later connection check in approved `initialize` mode, use the configured `DATABASE_URL` in the environment and confirm `/health` returns HTTP 200 with `initialization: initialized`. In skip mode, use the documented `not_run`/503 expectation instead. Inspect only generic startup error types if setup fails. Never paste or log the connection URL. The app does not claim initialization succeeded and does not retry writes automatically.

## Test results

Command run from the project root:

```powershell
.\venv\Scripts\python.exe -m unittest discover -s tests -v
```

The latest complete suite ran **50 tests and passed**. The workflow suite sets skip mode and SQLite before importing the app, then asserts the SQLite dialect and absence of tables before creating isolated test tables. The preflight suite uses independent in-memory SQLite engines, asserts SQLite before creating tables, verifies every emitted inspection statement starts with `SELECT`, verifies the entry point never imports `app.py`, and checks PostgreSQL read-only connection configuration without connecting. This review also verifies unmapped categories are reported as aggregate category/count pairs without complaint-level identifiers or statuses. Neither suite connects to Neon. Python compilation and `git diff --check` passed; Git emitted only line-ending conversion warnings during the latter.

The previous warnings were inspected and addressed: the `department.head_id` foreign key now uses SQLAlchemy's `use_alter=True` with an explicit constraint name to break the DDL dependency cycle; UTC defaults use a compatibility helper that preserves naive-UTC `DateTime` semantics; and `db.get_or_404()` replaces legacy `Query.get()`-based lookups. No warnings appeared in the latest test run. The explicit name affects only newly created schemas; this task did not alter the existing Neon schema.

### Controlled startup commands

For a later manual app run that must skip initialization and demo-user creation, set skip mode before the Flask CLI imports the app:

```powershell
Set-Location E:\SPSFRMS
$env:SPSFRMS_STARTUP_MODE = "skip"
.\venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1
```

`DATABASE_URL` must already be configured in `.env` or the PowerShell process environment; never place it in the command or print it. The app loads `.env`, and a process-level startup mode takes precedence. Skip mode avoids startup database setup but does not prevent ordinary application routes from writing data.

The normal write-capable mode is explicit:

```powershell
$env:SPSFRMS_STARTUP_MODE = "initialize"
.\venv\Scripts\python.exe -m flask --app app run --host 127.0.0.1
```

Do not use `initialize` against Neon until the schema/data effects above have been separately approved. Omitting the mode defaults to `initialize`, unless the legacy skip flag is set to `1`.

In skip mode, `/health` should return HTTP 503 with `status=degraded`, `database=connected`, `initialization=not_run` when the database responds. If it cannot connect, expect HTTP 503 with `status=error`, `database=not connected`, `initialization=not_run`. A 200 response must never claim success while setup was skipped. Normal mode returns 200 only after DB connectivity and setup both succeed. If setup fails, health remains 503 and reports `initialization=failed` (or `error` if the DB check also fails).

## Manual review checklist

1. **Citizen:** sign in with role `citizen`; preview a blocked drain and confirm Drainage; submit and verify the tracking page shows the saved department and analysis explanation; submit an ambiguous description and confirm it is marked for review; verify complaint text is unchanged.
2. **Department Head:** sign in with role `department_head`; confirm only own-department items appear; compare equal-priority ages; inspect workload recommendation; assign to eligible staff; try a stale resolved assignment; ignore with a reason and confirm it leaves the active queue; restore and verify status history and prior status.
3. **Staff:** sign in with role `staff`; confirm only assigned active work appears; start work; resolve with a note; verify repeated or invalid transitions produce feedback and do not duplicate history.
4. **Health:** after a controlled application startup against the intended database, check `/health`. HTTP 200 means DB connectivity and startup initialization both succeeded. A 503 means the connection or initialization is not healthy; inspect logs without exposing environment values.
5. **Multi-department legacy complaint:** the preflight reports only the aggregate count for unmapped categories; it does not identify individual complaint records or their departments. Preserve complaint ID 1 and its history. Its intended resolver remains unknown, and requires separate authorized evidence before any correction is considered. Workflow safeguards remain covered by isolated tests.

## Citizen timeline and local timestamps

The receipt/tracking page previously formatted `Complaint.created_date` and `ComplaintStatusHistory.timestamp` directly with `strftime`. The application creates these values with `utcnow()`, which returns naive UTC for the existing timezone-naive SQLAlchemy `DateTime` columns. Direct formatting treated that UTC wall time as local time and omitted the conversion to India time, which caused the displayed receipt time to be wrong. No JavaScript date conversion was involved, and there is no separate receipt template.

The `kolkata_datetime` Jinja filter now treats naive stored datetimes as UTC, converts aware datetimes by their represented instant, and formats them in `Asia/Kolkata` with `IST`. Missing values display `Date/time unavailable`. Complaint submission and its initial `Submitted` event now receive the same explicit UTC timestamp. Status transitions, assignment, ignore, and restore events explicitly use `utcnow()`. Status history ordering is chronological by timestamp, then ID for ties. The tracking page displays the submission timestamp and every available history event using this formatter. The initializer no longer substitutes the current time when a legacy complaint has no stored submission/update timestamp; it leaves the historical event unbackfilled rather than inventing a date.

No database columns or schema changes were added. Timeline authorization and multi-department workflow restrictions were not changed. Tests use isolated SQLite and deterministic naive/aware datetime fixtures, including submitted/In Progress/Resolved events, out-of-order insertion, missing timestamps, and the India-time receipt display. The frontend health indicator says `Database connected` when connectivity works but initialization is `not_run`, while `/health` continues to report HTTP 503/degraded and keeps its separate initialization state. Connection failure and initialization failure remain distinct. Neon was not accessed for this change.

Verification for this change: full test suite **54 tests passed**; Python compilation passed; `git diff --check` passed with line-ending conversion notices only.
