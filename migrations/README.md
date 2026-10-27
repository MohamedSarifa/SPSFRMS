# SPSFRMS PostgreSQL migrations

## Reviewed schema boundary

`0001_initial_baseline.sql` is the application's legacy base schema immediately
before migration 0002. It was compared with the SQLAlchemy models and startup
schema additions in the source. The baseline includes:

- `user`: `id`, `name`, `email`, `phone`, `password`, `role`,
  `registered_date`, and `department_id`.
- `department`: `id`, `name`, `email`, and `head_id`.
- `complaint`: text, category, location, optional image and keywords, priority,
  status, created/updated timestamps, citizen, primary department, legacy
  assignee, and resolution note.
- `complaint_department`: `id`, `complaint_id`, `department_id`, and
  `assigned_date`, with unique complaint/department membership.
- `complaint_assignment`: `id`, complaint, staff, assigning user, assignment
  timestamp, and remarks.
- `complaint_status_history`: `id`, complaint, old/new status, actor, timestamp,
  and remarks.

Primary and foreign keys, uniqueness, delete behavior, and indexes are declared
in the SQL file. The `department`/`user` foreign-key cycle is created in two
steps. SQLAlchemy Python-side defaults are not relied on as database defaults.
The baseline inserts the canonical department reference rows already used by
`workflow_routing.py`; it creates no accounts and no complaint or assignment
history. It deliberately excludes `User.is_active` and the department-scoped
assignment/progress fields because migration 0002 adds them. The prospective
assignment audit tables are not used by the running app and are not part of
this migration chain.

Migration 0002's starting assumptions match the baseline: the six tables and
legacy fields above exist; complaint/department membership is unique; legacy
`assigned_by` may be null; and assignment/status-history rows may not have a
department. 0002 then adds the active flag, per-department state, scoped history
columns, constraints, indexes, functions, and triggers. Existing account rows
receive `is_active = false`; an authorized staffing owner must review current
accounts before activating staff. The backfill only copies complaint-level
current state to its primary department. It does not turn legacy assignment
rows into verified events or infer secondary-department state.

## Version tracking and execution

Run SQL files only through `migration_runner.py`. It requires the PostgreSQL
URL in `SPSFRMS_MIGRATION_DATABASE_URL` (never `DATABASE_URL`), an explicit
`--apply`, a target label, an expected database name, and an exact typed
confirmation before connecting. Do not put credentials in command arguments.
For example, with a separately provisioned local database:

```powershell
python migration_runner.py --apply --target disposable-local --expected-database-name spsfrms_test_local
```

Use `--target staging` only after the database operator has approved the
separate staging database and verified its backup. The runner requires
PostgreSQL 12+, checks the connected database name/version, serializes runs
with a session advisory lock, and creates `spsfrms_schema_migration`. Each
migration and its version, SHA-256 file checksum, and schema fingerprint are
committed in the same PostgreSQL transaction. The fingerprint covers the
application tables' columns, constraints, indexes, triggers, and trigger
functions. At the next run, altered migration files, out-of-order versions, or
unrecorded schema edits stop the process.

The runner applies 0001 then 0002 on an empty database. It refuses an existing
unversioned schema: there is no automatic baseline adoption. This prevents a
partial or unknown database from being mislabeled as version 0001. Existing
deployments need a separate operator-reviewed baseline comparison/adoption
procedure before 0002 can be applied. Do not manually insert migration-ledger
rows or edit their checksums/fingerprints.

### Offline schema validation for an existing deployment

Do not point `migration_runner.py` at Neon or another live database to discover
its schema. Before considering an existing deployment, have the authorized
database operator provide an approved schema-only dump or reviewed schema
inventory through the project's controlled channel. Do not include row data,
credentials, or connection URLs in the review copy. Restore a schema-only dump
to a newly created, isolated local PostgreSQL database with no network route to
the live service, then compare its tables, columns, types, nullability, defaults,
keys, constraints, indexes, and trigger/function definitions against the
pre-0002 baseline and the post-0002 schema expected by the migration files and
models. Check migration-ledger state separately if the reviewed inventory
includes it. Record differences as an operator-reviewed report; a textual match
does not itself authorize adoption.

The runner intentionally refuses to adopt a nonempty unversioned schema. If
the existing deployment is not already recorded at a matching version with a
matching checksum and fingerprint, an approved adoption procedure must be
designed and tested against the isolated copy first. Never create ledger rows
manually, and never apply this test setup to Neon. A successful fresh-schema
migration test validates only a fresh database; it does not establish that an
existing deployment is compatible.

Each SQL file omits transaction wrappers because the runner owns the
transaction boundary. A failed migration rolls back its DDL, data changes, and
ledger row together. Earlier committed versions remain intact. There is no
automatic down migration. Recovery is a reviewed forward repair or restore
from a verified backup; do not delete the ledger or rerun edited SQL.

## Required pre-migration review

Before any migration, the authorized database operator must verify a restorable
backup and review existing schema shape, row counts, orphan references, duplicate
department memberships, legacy assignment/history rows, and the account
deactivation impact of 0002. A migration-capable database role needs permission
to create and alter the application schema, indexes, functions, triggers, and
ledger. Runtime-role separation and exact grants remain pending database-owner
approval; this migration does not establish roles or grants.

## Disposable PostgreSQL integration tests

PostgreSQL integration tests are opt-in and destructive to the test database's
`public` schema. They require all three of these independent gates:

1. `SPSFRMS_RUN_POSTGRES_TESTS=1`.
2. `SPSFRMS_TEST_POSTGRES_URL` supplied by an approved local secret source.
3. `SPSFRMS_TEST_POSTGRES_DISPOSABLE=I_CONFIRM_DROP_SPSFRMS_TEST_SCHEMA`.

The URL must point to a loopback host and a database whose name starts with
`spsfrms_test_`. The test suite refuses remote hosts or other database names
before connecting. Provision a new empty database specifically for this suite;
never use Neon or a shared local database. Then run:

```powershell
venv\Scripts\python.exe -m unittest discover -s tests -p test_postgres_migrations.py -v
```

That suite covers fresh migrations, representative legacy rows between 0001
and 0002, data preservation, idempotent runner reruns, rollback of a failing
version, and key/foreign-key/trigger constraints. It also includes an isolated
Flask test-client race between two assignment requests when one workload slot
remains; the test starts a subprocess with only the explicitly authorized
disposable URL and uses a barrier to make both requests reach the complaint
lock before they contend on the same staff row. Its assertions require one
success, one capacity warning, and a final workload of two. Run it only after
the three PostgreSQL safety gates pass. SQLite workflow tests alone do not
prove PostgreSQL locking. No PostgreSQL integration run has been performed in
this environment.
