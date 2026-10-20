"""Conservative read-only PostgreSQL export for offline staff recommendation.

The private CSV contains complaint text and staff IDs because the trainer needs
them. This module never imports the Flask application or reads credentials from
the environment.
"""

import argparse
import base64
import csv
from datetime import datetime, timezone
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from urllib.parse import urlsplit
import warnings

from workflow_routing import CATEGORY_DEPARTMENTS


FIELDS = (
    "assignment_id", "complaint_id", "staff_id", "assigned_at",
    "complaint_text", "category", "complaint_department_id",
    "complaint_department", "staff_role", "staff_department_id",
    "staff_department", "staff_is_active",
)
SELECT_SQL = """SELECT
    a.id AS assignment_id, a.complaint_id AS complaint_id,
    a.staff_id AS staff_id, a.assigned_date AS assigned_at,
    c.complaint_text AS complaint_text, c.category AS category,
    c.department_id AS complaint_department_id,
    cd.name AS complaint_department, u.role AS staff_role,
    u.department_id AS staff_department_id, sd.name AS staff_department,
    (u.role = 'staff' AND u.department_id = c.department_id) AS staff_is_active,
    (c.id IS NOT NULL) AS _complaint_found,
    (u.id IS NOT NULL) AS _staff_found,
    (cd.id IS NOT NULL) AS _complaint_department_found,
    (u.department_id IS NOT NULL AND sd.id IS NOT NULL) AS _staff_department_relationship_valid,
    a.remarks AS _remarks
FROM complaint_assignment AS a
LEFT JOIN complaint AS c ON c.id = a.complaint_id
LEFT JOIN department AS cd ON cd.id = c.department_id
LEFT JOIN "user" AS u ON u.id = a.staff_id
LEFT JOIN department AS sd ON sd.id = u.department_id
ORDER BY a.assigned_date, a.id"""
_SELECT_ONLY = re.compile(r"^\s*SELECT\b", re.IGNORECASE)
_MIGRATION_REMARK = "Existing assignment recorded during workflow upgrade."
_EVIDENCE_FIELDS = {
    "assignment_id", "complaint_id", "staff_id", "assigned_at", "verified", "evidence_ref",
}


class ExportRejected(RuntimeError):
    def __init__(self, reason, warnings=None):
        super().__init__(reason)
        self.reason = reason
        self.warnings = warnings or {}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, help="New CSV path outside the project directory.")
    parser.add_argument("--confirm-read-only-export", action="store_true",
                        help="Confirm this explicit, read-only export operation.")
    parser.add_argument("--provenance-evidence", required=True, help="Private reviewed JSON evidence file.")
    parser.add_argument("--eligibility-evidence", required=True, help="Private reviewed JSON evidence file.")
    return parser.parse_args(argv)


def _validate_output(output_path, project_dir):
    output = Path(output_path).expanduser().resolve()
    project = Path(project_dir).resolve()
    if output == project or project in output.parents:
        raise ValueError("output_must_be_outside_project")
    if output.exists():
        raise FileExistsError("output_already_exists")
    if not output.parent.is_dir():
        raise FileNotFoundError("output_directory_missing")
    _ensure_private_destination(output.parent)
    return output


def _ensure_private_destination(directory):
    """Fail closed when destination privacy cannot be established locally."""
    if os.name == "nt":
        if not _verify_windows_private_acl(directory):
            raise PermissionError("windows_destination_acl_unverified")
        return
    mode = stat.S_IMODE(Path(directory).stat().st_mode)
    if mode & 0o077:
        raise PermissionError("destination_directory_not_private")


def _verify_windows_private_acl(directory, *, runner=None):
    """Verify a restrictive DACL using Windows' built-in PowerShell/.NET ACL API.

    Only the directory owner (the current user), LocalSystem, and the built-in
    Administrators group are allowed in the DACL. Each must have full control;
    any other trustee or deny ACE fails closed. The path is base64 encoded into
    a fixed PowerShell script to prevent command interpretation.
    """
    if os.name != "nt":
        return False
    if runner is None:
        runner = subprocess.run
    try:
        path_bytes = str(Path(directory).resolve()).encode("utf-8")
        encoded_path = base64.b64encode(path_bytes).decode("ascii")
        script = f"""
$ErrorActionPreference = 'Stop'
$target = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$acl = Get-Acl -LiteralPath $target
$userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$rules = @(
    foreach ($rule in $acl.Access) {{
        $sid = $rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
        [ordered]@{{
            sid = $sid
            type = $rule.AccessControlType.ToString()
            rights = [int64]$rule.FileSystemRights
            inheritance = [int]$rule.InheritanceFlags
        }}
    }}
)
[ordered]@{{
    owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    user = $userSid
    rules = $rules
}} | ConvertTo-Json -Compress -Depth 4
"""
        encoded_script = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        completed = runner(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
             "-EncodedCommand", encoded_script],
            capture_output=True, text=True, timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return False
        snapshot = json.loads(completed.stdout)
        return _windows_acl_snapshot_is_private(snapshot)
    except Exception:
        # Do not surface a path, account name, or PowerShell/ACL error.
        return False


def _windows_acl_snapshot_is_private(snapshot):
    """Validate the minimal, sanitized ACL snapshot returned by PowerShell."""
    if (not isinstance(snapshot, dict) or set(snapshot) != {"owner", "user", "rules"}
            or not isinstance(snapshot.get("owner"), str)
            or not isinstance(snapshot.get("user"), str)
            or not re.fullmatch(r"S-1-(?:\d+-)+\d+", snapshot["owner"])
            or not re.fullmatch(r"S-1-(?:\d+-)+\d+", snapshot["user"])
            or snapshot["owner"] != snapshot["user"]
            or not isinstance(snapshot.get("rules"), list)
            or not snapshot["rules"]):
        return False

    user_sid = snapshot["user"]
    authorized_sids = {user_sid, "S-1-5-18", "S-1-5-32-544"}
    full_control = 2032127  # System.Security.AccessControl.FileSystemRights.FullControl
    rights_by_sid = {sid: 0 for sid in authorized_sids}
    for rule in snapshot["rules"]:
        if (not isinstance(rule, dict)
                or set(rule) != {"sid", "type", "rights", "inheritance"}
                or rule.get("sid") not in authorized_sids
                or rule.get("type") != "Allow"
                or type(rule.get("rights")) is not int
                or rule["rights"] < 0
                or type(rule.get("inheritance")) is not int
                or rule["inheritance"] < 0
                or rule["inheritance"] & 2 != 2):
            return False
        rights_by_sid[rule["sid"]] |= rule["rights"]
    return all(rights_by_sid[sid] & full_control == full_control for sid in authorized_sids)


def _validate_url(database_url):
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgresql+psycopg"}:
        raise ValueError("postgresql_required")
    if not parsed.hostname or not parsed.path or parsed.path == "/":
        raise ValueError("invalid_database_configuration")


def _hidden_database_url():
    # Fail closed if getpass cannot hide input and would otherwise echo it.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass("Approved PostgreSQL URL (input hidden): ")


def _select(connection, statement, parameters=None):
    normalized = statement.strip()
    if not _SELECT_ONLY.match(normalized) or ";" in normalized:
        raise ValueError("select_statements_only")
    from sqlalchemy import text
    return connection.execute(text(normalized), parameters or {})


def _verify_read_only(connection):
    if connection.dialect.name != "postgresql":
        raise ExportRejected("postgresql_required")
    if not connection.in_transaction():
        raise ExportRejected("transaction_not_started")
    try:
        value = _select(connection, "SELECT current_setting('transaction_read_only')").scalar_one()
    except Exception as error:
        raise ExportRejected("read_only_verification_failed") from error
    if str(value).lower() not in {"on", "true", "1"}:
        raise ExportRejected("read_only_verification_failed")


def _normalize_timestamp(value):
    if not isinstance(value, datetime):
        raise ValueError("invalid_evidence_timestamp")
    if value.tzinfo is None or value.utcoffset() is None:
        # Existing SPSFRMS DateTime values are naive UTC by source convention.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _evidence_timestamp(value):
    if not isinstance(value, str) or not value:
        raise ValueError("invalid_evidence_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid_evidence_timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_evidence_timestamp")
    return _normalize_timestamp(parsed)


def _database_identity(connection):
    """Hash PostgreSQL cluster, endpoint, database, and schema identity."""
    try:
        row = _select(
            connection,
            "SELECT system_identifier::text, current_database(), current_schema(), "
            "inet_server_addr()::text, inet_server_port()::text, "
            "(SELECT oid::text FROM pg_database WHERE datname = current_database()) "
            "FROM pg_control_system()",
        ).one()
    except Exception as error:
        raise ExportRejected("database_identity_unavailable") from error
    try:
        system_id, database_name, schema_name, server_address, server_port, database_oid = tuple(row)
    except (TypeError, ValueError) as error:
        raise ExportRejected("database_identity_unavailable") from error
    if not all(isinstance(value, str) and value for value in (
        system_id, database_name, schema_name, server_address, server_port, database_oid,
    )):
        raise ExportRejected("database_identity_unavailable")
    identity_input = "SPSFRMS-PG-IDENTITY-v1\0" + "\0".join(
        (system_id, server_address, server_port, database_name, database_oid, schema_name),
    )
    return hashlib.sha256(identity_input.encode("utf-8")).hexdigest()


def _assignment_key(record):
    values = []
    for name in ("assignment_id", "complaint_id", "staff_id"):
        value = record.get(name)
        if type(value) is not int or value < 1:
            raise ValueError("invalid_evidence_record")
        values.append(value)
    return (*values, _evidence_timestamp(record.get("assigned_at")))


def _validate_evidence_payload(payload):
    if (not isinstance(payload, dict)
            or set(payload) != {"format_version", "database_identity", "records"}
            or type(payload.get("format_version")) is not int
            or payload["format_version"] != 1
            or not isinstance(payload.get("database_identity"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["database_identity"])
            or not isinstance(payload.get("records"), list)):
        raise ValueError("invalid_evidence_file")
    records = {}
    for record in payload["records"]:
        if (not isinstance(record, dict) or set(record) != _EVIDENCE_FIELDS
                or record.get("verified") is not True
                or not isinstance(record.get("evidence_ref"), str)
                or not record["evidence_ref"].strip()):
            raise ValueError("invalid_evidence_record")
        key = _assignment_key(record)
        if key in records:
            raise ValueError("duplicate_evidence_key")
        records[key] = record["evidence_ref"]
    return {"database_identity": payload["database_identity"], "records": records}


def _load_evidence(path):
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    return _validate_evidence_payload(payload)


def _row_mapping(row):
    mapping = row._mapping if hasattr(row, "_mapping") else row
    return dict(mapping)


def _source_key(row):
    values = []
    for name in ("assignment_id", "complaint_id", "staff_id"):
        value = row.get(name)
        if type(value) is not int or value < 1:
            raise ValueError("invalid_source_identifier")
        values.append(value)
    return (*values, _normalize_timestamp(row.get("assigned_at")))


def _aggregate_checks(raw_rows, provenance, eligibility):
    warnings = {}
    rows = []
    seen_ids, seen_keys, events = set(), set(), {}
    source_keys = set()
    mapped_rows = []
    for raw in raw_rows:
        row = _row_mapping(raw)
        try:
            key = _source_key(row)
        except (ValueError, TypeError, OverflowError):
            warnings["invalid_assignment_key"] = warnings.get("invalid_assignment_key", 0) + 1
            continue
        assignment_id = key[0]
        source_keys.add(key)
        if assignment_id in seen_ids or key in seen_keys:
            warnings["duplicate_assignment_identifier"] = warnings.get("duplicate_assignment_identifier", 0) + 1
        seen_ids.add(assignment_id)
        seen_keys.add(key)
        mapped_rows.append((row, key))
        problems = []
        if row.get("_complaint_found") is not True:
            problems.append("missing_complaint_relationship")
        if row.get("_staff_found") is not True:
            problems.append("missing_staff_relationship")
        if row.get("_complaint_department_found") is not True:
            problems.append("missing_complaint_department_relationship")
        if row.get("_staff_department_relationship_valid") is not True:
            problems.append("missing_staff_department_relationship")
        if row.get("staff_role") != "staff":
            problems.append("current_staff_role_not_trainable")
        category = row.get("category")
        if not isinstance(category, str) or category not in CATEGORY_DEPARTMENTS:
            problems.append("unsupported_category")
        else:
            expected = CATEGORY_DEPARTMENTS[category]
            if (str(row.get("complaint_department") or "").casefold() != expected.casefold()
                    or str(row.get("staff_department") or "").casefold() != expected.casefold()):
                problems.append("category_department_mismatch")
        provenance_ref = provenance["records"].get(key)
        eligibility_ref = eligibility["records"].get(key)
        if provenance_ref is None:
            problems.append("provenance_evidence_missing")
        if eligibility_ref is None:
            problems.append("eligibility_evidence_missing")
        for problem in problems:
            warnings[problem] = warnings.get(problem, 0) + 1
        if key[1:3] and key[3]:
            event = (key[1], key[2], key[3])
            events[event] = events.get(event, 0) + 1
        if not problems:
            normalized = {name: row.get(name) for name in FIELDS}
            normalized["assigned_at"] = key[3]
            # This label is authorized only by independent eligibility evidence.
            normalized["staff_is_active"] = True
            rows.append((key, normalized))
    for count in events.values():
        if count > 1:
            warnings["questionable_duplicate_assignment_event"] = (
                warnings.get("questionable_duplicate_assignment_event", 0) + count
            )
    if warnings.get("duplicate_assignment_identifier") or warnings.get("questionable_duplicate_assignment_event"):
        rows = []
    for evidence in (provenance, eligibility):
        unmatched = set(evidence["records"]) - source_keys
        if unmatched:
            raise ExportRejected("unmatched_evidence_records", {
                **warnings, "unmatched_evidence_records": len(unmatched),
            })
    return [row for _, row in rows], warnings, source_keys


def _write_new_csv(output, rows):
    # Recheck immediately before creating the file to reduce ACL-change races.
    _ensure_private_destination(output.parent)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS, extrasaction="raise")
            writer.writeheader()
            writer.writerows(rows)
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise


def _run_export(connection, output_path, project_dir, provenance, eligibility):
    output = _validate_output(output_path, project_dir)
    _verify_read_only(connection)
    identity = _database_identity(connection)
    if (provenance["database_identity"] != identity
            or eligibility["database_identity"] != identity):
        raise ExportRejected("evidence_database_identity_mismatch")
    marker_count = _select(
        connection,
        "SELECT COUNT(*) FROM complaint_assignment WHERE remarks = :marker",
        {"marker": _MIGRATION_REMARK},
    ).scalar_one()
    assignment_gaps = _select(
        connection,
        "SELECT COUNT(*) FROM complaint AS c "
        "WHERE c.assigned_staff_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM complaint_assignment AS a WHERE a.complaint_id = c.id)",
    ).scalar_one()
    raw_rows = [_row_mapping(row) for row in _select(connection, SELECT_SQL)]
    warnings = {"missing_assignment_history": int(assignment_gaps)} if assignment_gaps else {}
    if marker_count:
        warnings["known_migration_marker_excluded"] = int(marker_count)
    candidates = [row for row in raw_rows if row.get("_remarks") != _MIGRATION_REMARK]
    if len(candidates) != len(raw_rows):
        warnings["known_migration_marker_excluded"] = len(raw_rows) - len(candidates)
    provenance_keys = set(provenance["records"])
    eligibility_keys = set(eligibility["records"])
    if provenance_keys != eligibility_keys:
        raise ExportRejected("evidence_assignment_sets_disagree", warnings)
    rows, checks, _ = _aggregate_checks(candidates, provenance, eligibility)
    warnings.update(checks)
    blocking = set(checks) - {"provenance_evidence_missing", "eligibility_evidence_missing"}
    if rows and any(checks.get(name) for name in blocking):
        raise ExportRejected("data_quality_review_required", warnings)
    if not rows:
        raise ExportRejected("no_fully_verified_rows", warnings)
    _write_new_csv(output, rows)
    return {"status": "exported", "row_count": len(rows), "warnings": warnings,
            "private_dataset_contains_complaint_text_and_staff_ids": True}


def main(argv=None, *, engine_factory=None, password_prompt=None):
    args = parse_args(argv)
    if not args.confirm_read_only_export:
        print(json.dumps({"status": "aborted", "reason": "confirmation_required"}))
        return 2
    project_dir = Path(__file__).parent
    try:
        output = _validate_output(args.output, project_dir)
        provenance = _load_evidence(args.provenance_evidence)
        eligibility = _load_evidence(args.eligibility_evidence)
        if password_prompt is None:
            database_url = _hidden_database_url()
        else:
            database_url = password_prompt("Approved PostgreSQL URL (input hidden): ")
        _validate_url(database_url)
        if engine_factory is None:
            from sqlalchemy import create_engine
            engine_factory = create_engine
        engine = engine_factory(
            database_url, pool_pre_ping=True,
            connect_args={"options": "-c default_transaction_read_only=on"},
        )
        try:
            with engine.connect() as connection:
                with connection.begin():
                    report = _run_export(connection, output, project_dir, provenance, eligibility)
        finally:
            engine.dispose()
    except Exception as error:
        report = {"status": "export_failed", "reason": _safe_error(error)}
        if isinstance(error, ExportRejected):
            report["warnings"] = error.warnings
        print(json.dumps(report, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


def _safe_error(error):
    allowed = {
        "postgresql_required", "transaction_not_started", "read_only_verification_failed",
        "database_identity_unavailable", "evidence_database_identity_mismatch",
        "evidence_assignment_sets_disagree", "unmatched_evidence_records",
        "output_must_be_outside_project", "output_already_exists", "output_directory_missing",
        "destination_directory_not_private", "windows_destination_acl_unverified",
        "data_quality_review_required", "no_fully_verified_rows", "invalid_evidence_file",
        "invalid_evidence_record", "invalid_evidence_timestamp", "duplicate_evidence_key",
    }
    message = getattr(error, "reason", str(error))
    return message if message in allowed else type(error).__name__


if __name__ == "__main__":
    raise SystemExit(main())
