import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import migration_runner


class MigrationRunnerTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.url = "postgresql+psycopg://synthetic-user:synthetic-password@localhost:5432/spsfrms_test_local"
        self.confirmation = "APPLY MIGRATIONS disposable-local spsfrms_test_local"

    def invoke(self, argv, *, answers=(), env=None, connect_fn=None):
        answer_iter = iter(answers)
        return migration_runner.main(
            argv,
            environ=env or {migration_runner.DATABASE_URL_ENV: self.url},
            input_fn=lambda _prompt: next(answer_iter),
            connect_fn=connect_fn,
            output=lambda message: self.output.write(str(message) + "\n"),
        )

    def test_discovered_migration_chain_and_checksums_are_deterministic(self):
        first = migration_runner.discover_migrations()
        second = migration_runner.discover_migrations()
        self.assertEqual([migration.version for migration in first], ["0001", "0002"])
        self.assertEqual([migration.checksum for migration in first], [migration.checksum for migration in second])

    def test_bad_migration_filename_is_rejected_without_database_access(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "bad.sql").write_text("SELECT 1", encoding="utf-8")
            with self.assertRaises(migration_runner.MigrationError):
                migration_runner.discover_migrations(directory)

    def test_migration_files_are_runner_transactional_and_fail_on_preexisting_objects(self):
        migrations = migration_runner.discover_migrations()
        self.assertNotRegex(migrations[0].sql.upper(), r"\bBEGIN\s*;")
        self.assertNotRegex(migrations[0].sql.upper(), r"\bCOMMIT\s*;")
        self.assertNotRegex(
            migrations[1].sql.upper(),
            r"\bCREATE\s+(?:TABLE|INDEX|TRIGGER|FUNCTION)\s+IF\s+NOT\s+EXISTS\b",
        )
        self.assertNotIn("CREATE OR REPLACE", migrations[1].sql.upper())
        self.assertIn('CREATE TABLE "user"', migrations[0].sql)
        self.assertIn("CREATE TABLE complaint_status_history", migrations[0].sql)

    def test_confirmation_flag_and_typed_target_are_required_before_connect(self):
        connector = Mock()
        result = self.invoke([], connect_fn=connector)
        self.assertEqual(result, 2)
        connector.assert_not_called()

        result = self.invoke(
            ["--apply", "--target", "disposable-local", "--expected-database-name", "spsfrms_test_local"],
            answers=("NO",),
            connect_fn=connector,
        )
        self.assertEqual(result, 2)
        connector.assert_not_called()

    def test_no_implicit_environment_fallback_or_connection(self):
        connector = Mock()
        result = self.invoke(
            ["--apply", "--target", "disposable-local", "--expected-database-name", "spsfrms_test_local"],
            answers=(self.confirmation,),
            env={"DATABASE_URL": self.url},
            connect_fn=connector,
        )
        self.assertEqual(result, 2)
        connector.assert_not_called()
        self.assertNotIn(self.url, self.output.getvalue())

    def test_local_disposable_target_rejects_remote_postgres_before_connect(self):
        connector = Mock()
        result = self.invoke(
            ["--apply", "--target", "disposable-local", "--expected-database-name", "spsfrms_test_local"],
            answers=(self.confirmation,),
            env={migration_runner.DATABASE_URL_ENV:
                 "postgresql+psycopg://synthetic-user:synthetic-password@remote.invalid/spsfrms_test_local"},
            connect_fn=connector,
        )
        self.assertEqual(result, 2)
        connector.assert_not_called()
        self.assertNotIn("remote.invalid", self.output.getvalue())

    def test_connection_errors_are_sanitized(self):
        connector = Mock(side_effect=RuntimeError(self.url))
        result = self.invoke(
            ["--apply", "--target", "disposable-local", "--expected-database-name", "spsfrms_test_local"],
            answers=(self.confirmation,),
            connect_fn=connector,
        )
        self.assertEqual(result, 1)
        self.assertNotIn(self.url, self.output.getvalue())
        self.assertNotIn("synthetic-password", self.output.getvalue())

    def test_successful_cli_delegates_only_after_confirmation(self):
        connection = Mock()
        connector = Mock(return_value=connection)
        with patch.object(migration_runner, "apply_migrations", return_value=("0001", "0002")) as apply:
            result = self.invoke(
                ["--apply", "--target", "disposable-local", "--expected-database-name", "spsfrms_test_local"],
                answers=(self.confirmation,),
                connect_fn=connector,
            )
        self.assertEqual(result, 0)
        connector.assert_called_once()
        apply.assert_called_once()
        connection.close.assert_called_once()

    def test_postgresql_target_mismatch_fails_before_advisory_lock(self):
        class Cursor:
            def __init__(self):
                self.query = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query, *_args, **_kwargs):
                self.query = query

            def fetchone(self):
                return ("unexpected_database", 160000)

        class Connection:
            def __init__(self):
                self.last_query = None
                self.current_cursor = Cursor()

            def cursor(self):
                self.current_cursor = Cursor()
                return self.current_cursor

            def commit(self):
                self.last_query = self.current_cursor.query

            def rollback(self):
                pass

        connection = Connection()
        with self.assertRaises(migration_runner.MigrationError):
            migration_runner.apply_migrations(
                connection, migration_runner.discover_migrations(),
                expected_database="spsfrms_test_local",
            )
        self.assertNotIn("pg_advisory_lock", connection.last_query or "")


if __name__ == "__main__":
    unittest.main()
