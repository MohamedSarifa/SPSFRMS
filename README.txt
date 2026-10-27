# SPSFRMS - Public Safety Feedback & Response Management System

A beginner-friendly Flask mini project for public complaint submission, department routing, officer status updates and citizen tracking.

## Technology
- Python Flask
- Flask-SQLAlchemy
- PostgreSQL
- HTML, CSS, JavaScript
- Bootstrap 5
- Gunicorn for Render

## 1. Local PostgreSQL setup

Install PostgreSQL and create a database named:

CREATE DATABASE spsfrms_db;

Copy `.env.example` to `.env` and set:

DATABASE_URL=postgresql://postgres:YOUR_PASSWORD@localhost:5432/spsfrms_db
SECRET_KEY=your-local-secret

The application normalizes this URL to use the installed psycopg 3 driver.

Do not upload `.env` to GitHub.

## 2. Install packages

Windows:

python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt

## 3. Run

python app.py

Open:
https://spsfrms.vercel.app

Ordinary application startup does not create tables or update database records. Before
running the app, use a database that has already been initialized by an operator.

## Account setup

There are no built-in demo passwords. Citizens choose a password of at least six
characters during registration. Administrators create staff and department-head
accounts with individual passwords of at least 12 characters and should deliver
each initial password through a secure channel. The application checks candidate
staff/head passwords against existing salted hashes, but that check is
best-effort and is not guaranteed across concurrent application workers. Use a
distinct password for each account regardless. The normal application import and
the one-off schema initialization command do not create demo accounts or seed
data. Optional demo account creation requires explicit operator invocation and
environment settings; the example seed file contains no
passwords. To explicitly create the demo administrator, set
SPSFRMS_CREATE_DEMO_ADMIN=1 and provide SPSFRMS_DEMO_ADMIN_PASSWORD through a
secret environment source. Optional seed accounts require a per-email password
map in SPSFRMS_SEED_PASSWORDS_JSON; do not put those values in the seed file.
Officer/staff CSV imports likewise require individual passwords in
SPSFRMS_IMPORT_PASSWORDS_JSON; any password column in a CSV is ignored.
The former all-account demo password reset utility is retired. Password reuse
checks are best-effort; they cannot guarantee global uniqueness under
concurrent requests, so operators must still issue a different password to
each account.

Any signed-in citizen, staff member, department head, or administrator can
change their own password from the Change password link in the navigation.
The form requires the current password, a matching confirmation, and a new
password of at least 12 characters. It changes only the account associated
with the authenticated session. Administrators can continue to use the separate
targeted reset action for eligible staff and department-head accounts.

## 4. Render deployment

Push the project to GitHub.

Option A - easiest:
1. Create a PostgreSQL database in Render.
2. Create a Python Web Service from the GitHub repository.
3. Build Command:
   pip install -r requirements.txt
4. Start Command:
   gunicorn app:app
5. Add environment variable:
   DATABASE_URL = your Render PostgreSQL INTERNAL database URL
6. Add:
   SECRET_KEY = a secure random value (Render can generate one).

Option B:
The included `render.yaml` can create a web service and a PostgreSQL database together. Review the generated resource names in Render before deployment.

## Important Render note

Render provides an internal database URL for services hosted in the same region. Use that URL for the deployed Flask service. The external URL is for connections from outside Render, such as your local computer.

Uploaded images are stored in the local `uploads` folder. Render's default web filesystem is not persistent across deploys/restarts, so this mini project treats image upload as optional. If permanent image storage is required later, add a dedicated object-storage service.

## Health check

Open `/health` after deployment. A successful response reports that the database is connected.

## Project flow

Citizen -> Register/Login -> Submit Complaint -> Basic Keyword Check -> Department -> Officer Review -> Status Update -> Citizen Tracking

Complaint classification still uses predefined phrase rules; it does not use an AI model.

## Complaint triage and work ordering

Complaint triage uses explicit phrase rules, not a trained machine-learning model. A specific drainage phrase takes precedence over generic mentions of water or roads; drinking-water contamination maps to Water Supply, and explicit outbreak terms map to Public Health. Ambiguous or unmatched descriptions are marked for human review in the Public Health review queue. The current database stores the selected category and department but does not persist a separate review flag.

Work is ordered deterministically: explicit emergency signals first, then stored priority (High, Medium, Low), then the oldest submission timestamp, then complaint ID. Emergency signals use the same transparent phrase rules and are not inferred from priority alone. Staff assignment recommendations are advisory and never assign automatically. See the ML staff-recommendation section below for the optional TF-IDF model and workload fallback.

The existing status and status-history fields also support Ignore and Restore without new columns. Ignoring retains the complaint, records the previous/new status, actor, timestamp and required reason in status history, and removes it from normal department and staff queues. A Department Head can restore it to the prior status.

## Database connection and startup behavior

Importing `app.py`, running `python app.py`, and starting `gunicorn app:app` do
not initialize the database. `/health` reports the connection and initialization
state; an uninitialized database is reported as degraded. The application expects
its schema and required department records to have been prepared separately.

For an intentionally selected **local disposable PostgreSQL database only**, set
`SPSFRMS_DISPOSABLE_DATABASE_URL` using a secure local environment or secret
source. Do not put the URL in a command argument, source file, or command history.
Then run this one-off command from the project directory:

`python initialize_disposable_database.py --confirm-disposable-database`

The command accepts only PostgreSQL URLs using a loopback host, asks the operator
to type `INITIALIZE`, and never prints the URL. The existing initializer can create
missing application tables and departments, add legacy workflow fields, normalize
legacy roles, update complaint department links, and add reconstructed assignment
or status-history snapshots. Review and back up the disposable database first.
This command does not create demo users or seed data. It is not a migration tool
for production and must not be pointed at Neon or another live database.

SQLAlchemy checks pooled connections before use and recycles them periodically.
Errors from the explicit setup command are reported without printing connection
strings or raw database exception details. No database setup or migration is
performed by the test suite; tests use an isolated in-memory SQLite database.

No SLA, complaint priority-source, or staff availability fields exist in the current schema. The application does not infer that a priority was manually approved, and workload recommendations count active assignments only.

## Optional ML staff recommendations

The dashboard can use an offline TF-IDF plus Logistic Regression model to rank
present-day eligible staff based on historical assignment choices. The label
means “staff selected in a past assignment”; it does not measure staff quality
or successful complaint resolution. Current workload adjusts ranking, and the
recommendation remains advisory. Current role and department are used to filter
present-day recommendation candidates only. They do not prove historical
eligibility and do not filter historical training rows. Training rows require
reviewed historical eligibility evidence in the manifest.

Training is fully offline. The script reads only an explicitly supplied local
CSV or JSON file and does not import Flask, `app.py`, `database_config.py`, or
SQLAlchemy. The input contains complaint text and staff IDs and is sensitive;
keep the dataset, review manifest, and evidence private. Reports contain
aggregate counts and metrics, not complaint text or staff identifiers. Do not
use real data until the required data-governance and evidence approvals exist.

CSV must have exactly the trainer's 12 fields: `assignment_id`, `complaint_id`,
`staff_id`, `assigned_at`, `complaint_text`, `category`,
`complaint_department_id`, `complaint_department`, `staff_role`,
`staff_department_id`, `staff_department`, and `staff_is_active`. IDs and
categories are validated, and category must agree with the project routing map.
`assigned_at` must be an ISO 8601 timestamp with an explicit timezone; naive
timestamps are rejected instead of guessed to be UTC. The role, active flag,
and staff department fields describe current account data and are retained for
the fixed CSV schema. They are not evidence of historical eligibility.

Every run requires one reviewed JSON manifest. CSV input uses
`--review-manifest`; JSON input may use that option or embed `review_manifest`,
but must not do both. Its top level has exactly `format_version: 2`,
`dataset_fingerprint`, `source_identity`, and `records`.
The fingerprint is a deterministic SHA-256 hash of the exact assignment rows;
the `source_identity` is a non-secret source label independently established
by the operator (letters, digits, dot, underscore, colon, and hyphen only; do
not use a URL, password, or database credential). Pass that same label with
`--source-identity`. The trainer checks both values before accepting any
manifest record. A fingerprint detects
dataset changes and accidental cross-dataset reuse; it does not authenticate
the data source, staffing records, or evidence.

Each manifest record binds `assignment_id`, `complaint_id`, `staff_id`, and
`assigned_at` to the dataset and declares `provenance`,
`reconstruction_status` (`not_reconstructed`, `known_reconstructed`, or
`unknown`), `provenance_evidence_ref`, `eligibility_verified`,
`eligibility_evidence_ref`, and `label_kind`. Only `verified_assignment`,
`reconstruction_status: not_reconstructed`, `eligibility_verified: true`, and
`historical_assignment_choice` are admitted. Unknown and known reconstructed
history such as `migration_backfill` is excluded even when a reference is
provided; no policy currently authorizes reconstructed history. Evidence
references and review flags are operator attestations unless a trusted
independent verification mechanism checks the underlying records. They are not
authenticated merely by the trainer.

Evaluation records additionally require `candidate_staff_ids`,
`candidate_roster_department_id`, `candidate_roster_at`,
`candidate_roster_reviewed: true`, `candidate_staff_eligibility_review`, and
`candidate_roster_evidence_ref`. The eligibility-review object must name every
candidate ID exactly once with a true reviewed assertion. The roster department
must match the complaint department and its explicitly zoned timestamp must
match the assignment decision timestamp. The roster evidence reference is an
attestation that the roster covers that department, time, and eligible
membership; timestamp equality or a reference string alone proves none of
these facts. Records with missing, wrong-department, or unreviewed rosters are
skipped from evaluation.

The evaluator keeps each complaint in one partition and uses a strict
chronological boundary. It reports held-out eligible, evaluated, skipped, and
pre-training excluded counts. Evaluation requires at least 80% coverage of
eligible held-out rows and at least five distinct evaluated complaints. Five is
a technical minimum only, not evidence of statistical reliability; small or
selectively covered samples must not be presented as model validation. If
either gate fails, status is `insufficient_data` and no artifact is written.

Example validation and training commands for a future, separately authorized
offline run:

```powershell
.\venv\Scripts\python.exe train_staff_recommendation.py `
  --dataset "D:\SPSFRMS-private\historical-assignments.csv" `
  --review-manifest "D:\SPSFRMS-private\historical-assignments-review.json" `
  --source-identity "approved-export-batch-01" `
  --validate-only
```

```powershell
.\venv\Scripts\python.exe train_staff_recommendation.py `
  --dataset "D:\SPSFRMS-private\historical-assignments.csv" `
  --review-manifest "D:\SPSFRMS-private\historical-assignments-review.json" `
  --source-identity "approved-export-batch-01" `
  --artifact-path "D:\SPSFRMS-private\staff-recommendation.joblib"
```

Validation-only never fits or writes an artifact. Its JSON `status` and
`data_sufficient_for_training_attempt` fields are authoritative: exit code 0
means parsing and hard validation succeeded, and can still accompany an
`insufficient_data` status. Training returns exit code 0 only when it saved an
artifact; insufficient data, invalid input, or a failed operation returns
nonzero. Check `artifact_saved` and `status` rather than assuming a model was
produced. Training refuses an existing artifact path and publishes a new
artifact with exclusive same-directory creation. Choose a fresh destination
for each authorized training run; the existing artifact is never replaced.

Artifacts use joblib/pickle. The loader validates metadata and model structure
after loading, so only load artifacts from a protected, trusted source; those
checks do not make an untrusted pickle safe. Incompatible or corrupt artifacts
are rejected with a generic warning and recommendations use the explicitly
identified workload-only fallback. Keep artifacts outside version control;
learned vocabulary and weights may encode information from complaint text.
Status-history JSON extensions can support a workload baseline only when their
timestamps and completeness are independently reviewed; this extension does
not establish historical assignment provenance or staff eligibility.

### Separate text-classification demonstration

The standalone demonstration below uses a small, fixed set of explicitly
synthetic example texts labeled with department names. It uses the already
declared scikit-learn dependency (TF-IDF plus Logistic Regression), makes a
fixed-seed stratified holdout split, and prints a JSON summary with metrics,
predicted department labels, aggregate split counts, and demo-scope limitations.
Accuracy is the share of the six held-out
synthetic examples whose department label matched; macro-F1 averages the three
department-level F1 scores with equal weight. The metrics describe only these
synthetic examples; they are not evidence of real-world routing performance,
generalization, or model quality. The script
does not import the Flask app, connect to a database, write an artifact, change
production routing, use assignment history, or recommend staff.

```powershell
.\venv\Scripts\python.exe -B demo_complaint_text_classifier.py
```

This demonstrates new-data text analysis only. It is separate from the
historical staff-recommendation model, which predicts past staff selection and
still requires approved assignment data and its evidence manifest to pass the
existing validation and evaluation gates.

### Demonstration checklist

The offline behavior can be demonstrated without a database or real records
with the focused synthetic test command:

```powershell
.\venv\Scripts\python.exe -B -m unittest discover -s tests -p test_staff_recommendation_offline.py -v
```

This demonstrates validation and advisory fallback behavior; it does not train
a model. A UI fallback demonstration requires a separately approved demo
environment: leave the model path unconfigured or point it at an absent or
invalid artifact, then confirm the dashboard labels the result as
`Lowest active workload (fallback)`. The user must still make the assignment
through the existing workflow.

A trained-ML demonstration is a separate stage. Use only a genuine, approved
dataset and its reviewed manifest, run the validation-only command above, and
proceed only if its aggregate report says the data is sufficient. Then train
to a new artifact path, verify the command reports that an artifact was saved,
configure `SPSFRMS_STAFF_MODEL_PATH` in the authorized demo environment, and
confirm the dashboard labels recommendations `ML + workload ranking`. This
demonstrates prediction of historical assignment choices, not successful
resolution. No approved dataset or trained artifact is currently documented
as available.

### Preparing a private dataset from PostgreSQL

`export_staff_training_data.py` is a separate, explicit PostgreSQL export
utility. It does not import the Flask app or read `DATABASE_URL` from the
environment. A future operator supplies an output path outside the project,
`--confirm-read-only-export`, and two protected review files. The approved
PostgreSQL URL is requested interactively with terminal echo disabled; it is
never accepted as a command-line argument or read from the environment. If the
terminal cannot hide input, the prompt fails closed. The utility configures
PostgreSQL transactions read-only, verifies that state
before data queries, and refuses to overwrite an existing output file. It
prints aggregate counts and warning categories only.

The exporter checks destination access controls before prompting and again
immediately before creating the CSV. On POSIX systems the destination
directory must have no group or other permission bits. On Windows it uses the
built-in PowerShell/.NET ACL API and proceeds only when the directory owner is
the current Windows user and the DACL grants full control only to that user,
LocalSystem, and the built-in Administrators group, with rules inherited by
files created in the directory. Any other trustee, deny entry, non-inheritable
rule, ambiguous ACL, unavailable PowerShell, or verification error causes a
fail-closed refusal. Windows file creation mode `0600` alone is not considered
a privacy control. Configure the destination directory with approved Windows
ACL tooling before export; the exporter does not change ACLs. The ACL checks in
the test suite use mocks and do not establish that a particular Windows host's
ACL is suitable.

Example command (run only after the database, eligibility, and privacy review):

```sh
python export_staff_training_data.py \
  --output "/srv/spsfrms-private/historical-assignments.csv" \
  --confirm-read-only-export \
  --provenance-evidence "/srv/spsfrms-private/assignment-provenance-review.json" \
  --eligibility-evidence "/srv/spsfrms-private/historical-eligibility-review.json"
```

Each review file uses this structure: `format_version: 1`, `database_identity`,
and `records`. The database identity is the lowercase SHA-256 digest of the
PostgreSQL cluster system identifier, server address and port, database name
and OID, and current schema. Hash input is the UTF-8 encoding of
`SPSFRMS-PG-IDENTITY-v1`, a NUL byte, and those six values in that order with
NUL separators. It does not contain credentials. The exporter obtains these
values with a SELECT from `pg_control_system()` after read-only mode is
verified; if PostgreSQL does not permit the query or any identity component is
unavailable, export stops. Reviewers must derive the digest through an
approved, independent process and place the same value in both evidence files.
Failover to a server with a different address or database recreation may change
the identity and require fresh review evidence.
This fingerprint is not a cryptographic proof against a cloned PostgreSQL
cluster that preserves its system identifier and database metadata. If the
environment can contain such clones, independently bind the review evidence to
the intended endpoint using an operator-managed immutable instance identity;
otherwise do not reuse the evidence.

Each evidence record must contain exactly `assignment_id`, `complaint_id`,
`staff_id`, `assigned_at`, `verified: true`, and a non-empty `evidence_ref`.
The three identifiers and timestamp must identify the same source assignment
in both files. Do not include rows bearing the known migration marker; such
evidence does not match the eligible source set and blocks the export. Evidence
timestamps require an explicit timezone and are
normalized to UTC; naive database timestamps follow the application's stored
UTC convention. Duplicate or conflicting keys, malformed evidence, a database
identity mismatch, disagreement between the two evidence files, or evidence
that matches no assignment blocks the export. Source assignments without both
evidence records are excluded and counted in aggregate diagnostics.

Include a record only when independent, dated records establish its assignment
provenance and the staff member's eligibility at that assignment time. Current
role, active status, and department fields are still exported to satisfy the
trainer's fixed 12-column input contract. The trainer does not use those current
fields to include or exclude historical rows; they do not prove eligibility at
the assignment time.
Suitable evidence could include an auditable assignment ledger and
effective-dated employment, role, and department-membership records. Current
user role and department are not evidence of historical eligibility. The
evidence references are operator attestations; the exporter cannot inspect or
authenticate the underlying documents.

The private dataset necessarily contains complaint text and staff identifiers,
because the existing offline trainer requires both. Keep it outside the
project, logs, reports, and version control. On POSIX systems the destination
directory must have no group or other permission bits; the output is created
exclusively with owner-only mode. On Windows, the DACL must restrict access to
the current user, LocalSystem, and built-in Administrators as described above;
the exporter fails closed if it cannot verify those rules. Place evidence files
and datasets only in directories restricted to the authorized operator. The
schema's exact migration
remark identifies one known class of synthesized history and those rows are
excluded. Other reconstructed records may be indistinguishable from genuine
history in the available schema; they require independent record-level
provenance evidence or remain excluded. This does not establish that all
unmarked rows are genuine. Human review is required before using assignments
as labels.

Training excludes rows whose provenance, label type, or historical eligibility
does not meet the reviewed manifest policy, and stops without an artifact if
reliable evaluation is insufficient. Evaluation keeps each complaint in one
chronological partition and reports top-1/top-k accuracy, macro F1, and mean
reciprocal rank only after both its coverage and complaint-count floors pass.
These technical floors do not establish statistical reliability. Historical
assignment-choice labels, changing staff eligibility, and human selection bias
can make rankings unreliable. Automated test fixtures are synthetic only.

Top-1 accuracy is the share of held-out assignments whose historically chosen
staff member ranks first. Top-k accuracy is the share whose chosen staff
member appears in the first k results. Macro F1 is the unweighted mean of
per-staff F1 scores. Mean reciprocal rank is the average of `1/rank` for the
historically chosen staff member, using zero when that member is absent from
the ranking.

Create the protected artifact directory before running the command. The script
requires an absolute output path outside the project directory and version
control.

Set `SPSFRMS_STAFF_MODEL_PATH` to the protected artifact path for the dashboard.
Artifacts are version-checked; incompatible or unreadable artifacts are
rejected with a generic warning and the workload-only fallback remains active.
Joblib uses pickle internally, so only load files from protected trusted
locations. Do not commit artifacts; their vocabulary and weights can encode
information from complaint text. Small, imbalanced, or historically biased
assignment data may not support a useful model.
