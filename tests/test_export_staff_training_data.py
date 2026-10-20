import contextlib
import csv
import hashlib
import io
import json
import os
import tempfile
import unittest
import warnings
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import export_staff_training_data as exporter


IDENTITY = hashlib.sha256(
    b"SPSFRMS-PG-IDENTITY-v1\0" + b"\0".join((
        b"cluster-system-id", b"192.0.2.5", b"235432",
        b"fixture-database", b"41234", b"public",
    ))
).hexdigest()


def source_row(**updates):
    row = {
        "assignment_id": 11, "complaint_id": 22, "staff_id": 33,
        "assigned_at": datetime(2025, 1, 1, tzinfo=timezone.utc),
        "complaint_text": "fixture only", "category": "Road Damage",
        "complaint_department_id": 4, "complaint_department": "Road Maintenance",
        "staff_role": "staff", "staff_department_id": 4,
        "staff_department": "Road Maintenance", "staff_is_active": True,
        "_complaint_found": True, "_staff_found": True,
        "_complaint_department_found": True,
        "_staff_department_relationship_valid": True, "_remarks": None,
    }
    row.update(updates)
    return row


def evidence_record(row=None, **updates):
    row = row or source_row()
    record = {
        "assignment_id": row["assignment_id"],
        "complaint_id": row["complaint_id"],
        "staff_id": row["staff_id"],
        "assigned_at": row["assigned_at"].isoformat(),
        "verified": True,
        "evidence_ref": "review-record-1",
    }
    record.update(updates)
    return record


def evidence(records=None, identity=IDENTITY):
    return {"database_identity": identity, "records": records or [evidence_record()],
            "format_version": 1}


class FakeResult:
    def __init__(self, scalar=None, rows=()):
        self.scalar = scalar
        self.rows = rows

    def scalar_one(self):
        return self.scalar

    def one(self):
        return self.rows[0]

    def __iter__(self):
        return iter(self.rows)


class FakeConnection:
    def __init__(self, *, dialect="postgresql", in_tx=True, read_only="on",
                 identity=IDENTITY, rows=(), fail_read_only=False):
        self.dialect = SimpleNamespace(name=dialect)
        self.in_tx = in_tx
        self.read_only = read_only
        self.identity = identity
        self.rows = rows
        self.fail_read_only = fail_read_only
        self.statements = []

    def in_transaction(self):
        return self.in_tx

    def execute(self, statement, parameters):
        sql = str(statement)
        if not exporter._SELECT_ONLY.match(sql) or ";" in sql:
            raise AssertionError("workflow attempted a non-single-SELECT statement")
        self.statements.append(sql)
        if "current_setting('transaction_read_only')" in sql:
            if self.fail_read_only:
                raise RuntimeError("sensitive driver error")
            return FakeResult(self.read_only)
        if "FROM pg_control_system()" in sql:
            return FakeResult(rows=[("cluster-system-id", "fixture-database", "public",
                                     "192.0.2.5", "235432", "41234")])
        if "COUNT(*) FROM complaint_assignment" in sql:
            return FakeResult(0)
        if "COUNT(*) FROM complaint AS c" in sql:
            return FakeResult(0)
        if "FROM complaint_assignment AS a" in sql:
            return FakeResult(rows=self.rows)
        raise AssertionError("unexpected query")


class ExportTests(unittest.TestCase):
    def _paths(self, root):
        project, private = root / "project", root / "private"
        project.mkdir()
        private.mkdir()
        return project, private

    def _review_maps(self, row=None):
        record = evidence_record(row)
        return ({"database_identity": IDENTITY, "records": {
            (record["assignment_id"], record["complaint_id"], record["staff_id"],
             exporter._evidence_timestamp(record["assigned_at"])): record["evidence_ref"]}},
                {"database_identity": IDENTITY, "records": {
            (record["assignment_id"], record["complaint_id"], record["staff_id"],
             exporter._evidence_timestamp(record["assigned_at"])): record["evidence_ref"]}})

    def test_no_prompt_or_engine_before_confirmation(self):
        factory, prompt = Mock(), Mock()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = exporter.main(
                ["--output", "x", "--provenance-evidence", "x", "--eligibility-evidence", "x"],
                engine_factory=factory, password_prompt=prompt,
            )
        self.assertEqual(code, 2)
        factory.assert_not_called()
        prompt.assert_not_called()

    def test_duplicate_evidence_keys_rejected(self):
        record = evidence_record()
        with self.assertRaisesRegex(ValueError, "duplicate_evidence_key"):
            exporter._validate_evidence_payload(evidence([record, record]))
        conflict = evidence_record(evidence_ref="different-review-reference")
        with self.assertRaisesRegex(ValueError, "duplicate_evidence_key"):
            exporter._validate_evidence_payload(evidence([record, conflict]))

    def test_malformed_and_contradictory_evidence_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid_evidence_record"):
            exporter._validate_evidence_payload(evidence([evidence_record(verified=False)]))
        missing_field = evidence_record()
        missing_field.pop("evidence_ref")
        with self.assertRaisesRegex(ValueError, "invalid_evidence_record"):
            exporter._validate_evidence_payload(evidence([missing_field]))
        malformed = evidence([evidence_record(assigned_at="not-a-timestamp")])
        with self.assertRaisesRegex(ValueError, "invalid_evidence_timestamp"):
            exporter._validate_evidence_payload(malformed)

    def test_wrong_database_identity_rejected(self):
        provenance, eligibility = self._review_maps()
        wrong = {**eligibility, "database_identity": "b" * 64}
        with tempfile.TemporaryDirectory() as tmp:
            project, private = self._paths(Path(tmp))
            with patch.object(exporter, "_ensure_private_destination"):
                with self.assertRaisesRegex(exporter.ExportRejected, "evidence_database_identity_mismatch"):
                    exporter._run_export(FakeConnection(), private / "out.csv", project, provenance, wrong)

    def test_wrong_tuple_timestamp_and_unmatched_evidence_rejected(self):
        for field, bad_value in (("complaint_id", 999), ("staff_id", 998),
                                 ("assigned_at", "2024-01-01T00:00:00.000000Z")):
            with self.subTest(field=field):
                provenance, eligibility = self._review_maps()
                key = next(iter(provenance["records"]))
                wrong_values = list(key)
                wrong_values[{"complaint_id": 1, "staff_id": 2, "assigned_at": 3}[field]] = bad_value
                wrong_key = tuple(wrong_values)
                provenance["records"] = {wrong_key: "review-record-1"}
                eligibility["records"] = {wrong_key: "review-record-1"}
                with tempfile.TemporaryDirectory() as tmp:
                    project, private = self._paths(Path(tmp))
                    with patch.object(exporter, "_ensure_private_destination"):
                        with self.assertRaisesRegex(exporter.ExportRejected, "unmatched_evidence_records"):
                            exporter._run_export(FakeConnection(rows=[source_row()]),
                                                 private / "out.csv", project, provenance, eligibility)

    def test_missing_both_evidence_records_excludes_row_with_aggregate_counts(self):
        row = source_row()
        provenance = {"database_identity": IDENTITY, "records": {}}
        eligibility = {"database_identity": IDENTITY, "records": {}}
        rows, warnings, _ = exporter._aggregate_checks(
            [row], provenance, eligibility,
        )
        self.assertEqual(rows, [])
        self.assertEqual(warnings["provenance_evidence_missing"], 1)
        self.assertEqual(warnings["eligibility_evidence_missing"], 1)

    def test_current_role_does_not_substitute_for_historical_evidence(self):
        row = source_row(staff_role="department_head")
        provenance, eligibility = self._review_maps(row)
        rows, warnings, _ = exporter._aggregate_checks(
            [row], provenance, eligibility,
        )
        self.assertEqual(rows, [])
        self.assertEqual(warnings["current_staff_role_not_trainable"], 1)

    def test_evidence_files_must_refer_to_same_assignment_set(self):
        provenance, eligibility = self._review_maps()
        eligibility["records"] = {}
        with tempfile.TemporaryDirectory() as tmp:
            project, private = self._paths(Path(tmp))
            with patch.object(exporter, "_ensure_private_destination"):
                with self.assertRaisesRegex(exporter.ExportRejected, "evidence_assignment_sets_disagree"):
                    exporter._run_export(FakeConnection(rows=[source_row()]),
                                         private / "out.csv", project, provenance, eligibility)

    def test_non_postgresql_and_missing_transaction_fail_before_queries(self):
        for connection, reason in (
            (FakeConnection(dialect="sqlite"), "postgresql_required"),
            (FakeConnection(in_tx=False), "transaction_not_started"),
        ):
            with self.assertRaisesRegex(exporter.ExportRejected, reason):
                exporter._verify_read_only(connection)
            self.assertEqual(connection.statements, [])

    def test_read_only_query_failure_or_unexpected_result_fails_closed(self):
        for connection in (FakeConnection(fail_read_only=True), FakeConnection(read_only="unknown")):
            with self.assertRaisesRegex(exporter.ExportRejected, "read_only_verification_failed"):
                exporter._verify_read_only(connection)
            self.assertEqual(len(connection.statements), 1)

    def test_database_identity_unavailable_fails_closed(self):
        connection = FakeConnection()
        connection.execute = Mock(side_effect=RuntimeError("sensitive database error"))
        with self.assertRaisesRegex(exporter.ExportRejected, "database_identity_unavailable"):
            exporter._database_identity(connection)

    def test_prompt_and_connection_failures_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, private = self._paths(root)
            pfile, efile = private / "p.json", private / "e.json"
            pfile.write_text(json.dumps(evidence()), encoding="utf-8")
            efile.write_text(json.dumps(evidence()), encoding="utf-8")
            output = private / "out.csv"
            secret_text = "credential-and-url-must-not-leak"
            prompt_error = Mock(side_effect=RuntimeError(secret_text))
            stderr = io.StringIO()
            with patch.object(exporter, "_ensure_private_destination"), contextlib.redirect_stderr(stderr):
                code = exporter.main(
                    ["--output", str(output), "--confirm-read-only-export",
                     "--provenance-evidence", str(pfile), "--eligibility-evidence", str(efile)],
                    password_prompt=prompt_error,
                )
            self.assertEqual(code, 1)
            self.assertNotIn(secret_text, stderr.getvalue())

            stderr = io.StringIO()
            with patch.object(exporter, "_ensure_private_destination"), contextlib.redirect_stderr(stderr):
                code = exporter.main(
                    ["--output", str(output), "--confirm-read-only-export",
                     "--provenance-evidence", str(pfile), "--eligibility-evidence", str(efile)],
                    password_prompt=lambda _: "postgresql://secret-url/db",
                    engine_factory=Mock(side_effect=RuntimeError(secret_text)),
                )
            self.assertEqual(code, 1)
            self.assertNotIn(secret_text, stderr.getvalue())
            self.assertNotIn("secret-url", stderr.getvalue())

    def test_export_queries_all_pass_select_guard_and_csv_has_exact_fields(self):
        row = source_row()
        provenance, eligibility = self._review_maps(row)
        connection = FakeConnection(rows=[row])
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project, private = self._paths(root)
            target = private / "training.csv"
            with patch.object(exporter, "_ensure_private_destination"):
                report = exporter._run_export(connection, target, project, provenance, eligibility)
            self.assertEqual(report["row_count"], 1)
            self.assertTrue(all(exporter._SELECT_ONLY.match(sql) and ";" not in sql
                                for sql in connection.statements))
            with target.open(encoding="utf-8", newline="") as handle:
                self.assertEqual(next(csv.reader(handle)), list(exporter.FIELDS))
            with self.assertRaises(FileExistsError):
                with patch.object(exporter, "_ensure_private_destination"):
                    exporter._write_new_csv(target, [])

    def test_destination_privacy_fails_closed_when_unverifiable(self):
        with tempfile.TemporaryDirectory() as tmp:
            if exporter.os.name == "nt":
                with patch.object(exporter, "_verify_windows_private_acl", return_value=False):
                    with self.assertRaisesRegex(PermissionError, "windows_destination_acl_unverified"):
                        exporter._ensure_private_destination(Path(tmp))
                with patch.object(exporter, "_verify_windows_private_acl", return_value=True):
                    exporter._ensure_private_destination(Path(tmp))
            else:
                exposed = Path(tmp) / "exposed"
                exposed.mkdir()
                os.chmod(exposed, 0o755)
                with self.assertRaisesRegex(PermissionError, "destination_directory_not_private"):
                    exporter._ensure_private_destination(exposed)

    def test_windows_acl_snapshot_requires_owner_and_only_authorized_full_control(self):
        user = "S-1-5-21-100-200-300-1001"
        full_control = 2032127
        valid = {
            "owner": user,
            "user": user,
            "rules": [
                {"sid": user, "type": "Allow", "rights": full_control, "inheritance": 2},
                {"sid": "S-1-5-18", "type": "Allow", "rights": full_control, "inheritance": 2},
                {"sid": "S-1-5-32-544", "type": "Allow", "rights": full_control, "inheritance": 2},
            ],
        }
        self.assertTrue(exporter._windows_acl_snapshot_is_private(valid))
        for invalid in (
            {**valid, "owner": "S-1-5-32-544"},
            {**valid, "rules": valid["rules"] + [
                {"sid": "S-1-5-11", "type": "Allow", "rights": full_control, "inheritance": 2},
            ]},
            {**valid, "rules": valid["rules"] + [
                {"sid": user, "type": "Deny", "rights": 1, "inheritance": 2},
            ]},
            {**valid, "rules": valid["rules"][:-1]},
            {**valid, "rules": [{
                "sid": user, "type": "Allow", "rights": True, "inheritance": 2,
            }]},
            {**valid, "rules": [
                {**rule, "inheritance": 0} if rule["sid"] == user else rule
                for rule in valid["rules"]
            ]},
        ):
            with self.subTest(snapshot=invalid):
                self.assertFalse(exporter._windows_acl_snapshot_is_private(invalid))

    def test_windows_acl_inspector_uses_builtin_api_and_sanitizes_failures(self):
        user = "S-1-5-21-100-200-300-1001"
        full_control = 2032127
        snapshot = {
            "owner": user,
            "user": user,
            "rules": [
                {"sid": user, "type": "Allow", "rights": full_control, "inheritance": 2},
                {"sid": "S-1-5-18", "type": "Allow", "rights": full_control, "inheritance": 2},
                {"sid": "S-1-5-32-544", "type": "Allow", "rights": full_control, "inheritance": 2},
            ],
        }
        completed = SimpleNamespace(returncode=0, stdout=json.dumps(snapshot), stderr="private detail")
        with tempfile.TemporaryDirectory() as tmp:
            runner = Mock(return_value=completed)
            with patch.object(exporter.os, "name", "nt"):
                self.assertTrue(exporter._verify_windows_private_acl(Path(tmp), runner=runner))
            args, kwargs = runner.call_args
            self.assertEqual(args[0][0], "powershell.exe")
            self.assertEqual(args[0][-2], "-EncodedCommand")
            self.assertNotIn(str(Path(tmp)), " ".join(args[0]))
            self.assertTrue(kwargs["capture_output"])
            self.assertEqual(kwargs["timeout"], 10)

            with patch.object(exporter.os, "name", "nt"):
                self.assertFalse(exporter._verify_windows_private_acl(
                    Path(tmp), runner=Mock(side_effect=RuntimeError("private ACL detail")),
                ))
                self.assertFalse(exporter._verify_windows_private_acl(
                    Path(tmp), runner=Mock(return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr="private ACL detail",
                    )),
                ))

    def test_safe_error_does_not_return_exception_message(self):
        marker = "sensitive-url-or-record-marker"
        self.assertEqual(exporter._safe_error(RuntimeError(marker)), "RuntimeError")

    def test_getpass_echo_fallback_is_rejected(self):
        def echoed_fallback(_prompt):
            warnings.warn("terminal echo unavailable", exporter.getpass.GetPassWarning)
            return "sensitive-url"

        with patch.object(exporter.getpass, "getpass", side_effect=echoed_fallback):
            with self.assertRaises(exporter.getpass.GetPassWarning):
                exporter._hidden_database_url()


if __name__ == "__main__":
    unittest.main()
