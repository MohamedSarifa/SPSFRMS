import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, event

import inspect_neon_preflight
from inspect_neon_preflight import EXPECTED_COLUMNS, create_inspection_engine, inspect_database
from workflow_routing import CATEGORY_DEPARTMENTS, MULTI_DEPARTMENT_LEGACY_CATEGORY


class NeonPreflightTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://")
        if self.engine.dialect.name != "sqlite":
            raise RuntimeError("Preflight tests must use isolated SQLite.")
        self.metadata = MetaData()
        self.tables = {}
        for table_name, column_names in EXPECTED_COLUMNS.items():
            columns = []
            for column_name in sorted(column_names):
                is_id = column_name == "id"
                is_integer = is_id or column_name.endswith("_id")
                columns.append(Column(
                    column_name,
                    Integer if is_integer else String,
                    primary_key=is_id,
                ))
            self.tables[table_name] = Table(table_name, self.metadata, *columns)
        self.metadata.create_all(self.engine)
        self._seed_fixture_rows()

    def tearDown(self):
        self.engine.dispose()

    def _seed_fixture_rows(self):
        department_table = self.tables["department"]
        user_table = self.tables["user"]
        complaint_table = self.tables["complaint"]
        link_table = self.tables["complaint_department"]
        assignment_table = self.tables["complaint_assignment"]
        history_table = self.tables["complaint_status_history"]
        with self.engine.begin() as connection:
            department_ids = {}
            for department_id, department_name in enumerate(sorted(set(CATEGORY_DEPARTMENTS.values())), 1):
                connection.execute(department_table.insert().values(
                    id=department_id,
                    name=department_name,
                    email="internal-fixture@example.invalid",
                ))
                department_ids[department_name] = department_id
            connection.execute(user_table.insert().values(
                id=1, name="PRIVATE FIXTURE NAME", email="private@example.invalid",
                phone="private", password="FIXTURE HASH MUST NOT APPEAR", role="officer",
                department_id=department_ids["Drainage"],
            ))
            connection.execute(user_table.insert().values(
                id=2, name="Fixture Staff", email="staff@example.invalid",
                phone="private", password="FIXTURE HASH MUST NOT APPEAR", role="staff",
                department_id=department_ids["Drainage"],
            ))
            connection.execute(complaint_table.insert().values(
                id=1, complaint_text="PRIVATE COMPLAINT DESCRIPTION",
                category="Water Leakage", location="Private Location", priority="Low",
                status="Submitted", citizen_id=1,
                department_id=department_ids["Public Health"],
            ))
            connection.execute(complaint_table.insert().values(
                id=2, complaint_text="ANOTHER PRIVATE COMPLAINT",
                category="Drainage", location="Private Location", priority="Medium",
                status="Assigned", citizen_id=1,
                department_id=department_ids["Drainage"], assigned_staff_id=2,
            ))
            connection.execute(complaint_table.insert().values(
                id=3, complaint_text="DO NOT REPORT THIS TEXT", category=MULTI_DEPARTMENT_LEGACY_CATEGORY,
                location="DO NOT REPORT LOCATION", priority="High", status="Under Review", citizen_id=1,
                department_id=department_ids["Drainage"], assigned_staff_id=2,
            ))
            connection.execute(link_table.insert().values(
                id=1, complaint_id=3, department_id=department_ids["Police"],
            ))
            connection.execute(assignment_table.insert().values(
                id=1, complaint_id=3, staff_id=2, assigned_by=1,
            ))
            connection.execute(history_table.insert().values(
                id=1, complaint_id=3, old_status="Submitted", new_status="Under Review", updated_by=1,
            ))

    def test_inspection_uses_select_statements_and_reports_only_safe_aggregates(self):
        statements = []

        def require_select(connection, cursor, statement, parameters, context, executemany):
            statements.append(statement)
            if not statement.lstrip().upper().startswith("SELECT "):
                raise AssertionError("Preflight executed a non-SELECT statement")

        event.listen(self.engine, "before_cursor_execute", require_select)
        report = inspect_database(self.engine)
        event.remove(self.engine, "before_cursor_execute", require_select)

        self.assertTrue(statements)
        self.assertTrue(all(statement.lstrip().upper().startswith("SELECT ") for statement in statements))
        self.assertFalse(any("SELECT *" in statement.upper() for statement in statements))
        self.assertTrue(any("pragma_table_info" in statement for statement in statements))
        self.assertEqual(report["database_dialect"], "sqlite")
        self.assertEqual(report["legacy_officer_role_count"], 1)
        self.assertEqual(report["assignment_history_gaps"], 1)
        self.assertEqual(report["status_history_gaps"], 2)
        self.assertEqual(report["initializer_effect_preview"]["complaint_department_ids_that_would_change"], 1)
        unmapped = report["complaint_categories_without_mapping"]
        self.assertIn({"category": MULTI_DEPARTMENT_LEGACY_CATEGORY, "complaint_count": 1}, unmapped)
        self.assertNotIn("multi_department_complaint_metadata", report)
        self.assertNotIn("multi_department_metadata_status", report)
        self.assertNotIn("password", " ".join(statements).lower())
        self.assertNotIn("complaint_text", " ".join(statements).lower())
        discrepancy = report["complaint_mapping_discrepancies"][0]
        self.assertEqual(discrepancy["category"], "Water Leakage")
        self.assertEqual(discrepancy["current_department_name"], "Public Health")
        self.assertEqual(discrepancy["expected_department_name"], "Water Supply")
        rendered = str(report)
        for private_value in (
            "PRIVATE FIXTURE NAME", "private@example.invalid", "FIXTURE HASH MUST NOT APPEAR",
            "PRIVATE COMPLAINT DESCRIPTION", "ANOTHER PRIVATE COMPLAINT", "Private Location",
            "DO NOT REPORT THIS TEXT", "DO NOT REPORT LOCATION",
        ):
            self.assertNotIn(private_value, rendered)

    def test_inspection_entrypoint_does_not_import_application_or_run_startup(self):
        environment = os.environ.copy()
        environment["DATABASE_URL"] = "sqlite://"
        environment["SPSFRMS_SKIP_STARTUP_DB_INIT"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; import inspect_neon_preflight as p; "
                "assert p.main() == 0; assert 'app' not in sys.modules",
            ],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_schema_is_reported_without_database_changes(self):
        empty_engine = create_engine("sqlite://")
        try:
            self.assertEqual(empty_engine.dialect.name, "sqlite")
            report = inspect_database(empty_engine)
            self.assertEqual(report["schema"]["tables_missing"], sorted(EXPECTED_COLUMNS))
            self.assertEqual(report["initializer_effect_preview"]["assignment_history_rows_that_would_be_backfilled"], None)
            self.assertIn("complaint", report["schema"]["tables_missing"])
            self.assertEqual(report["complaint_categories_without_mapping"], [])
        finally:
            empty_engine.dispose()

    def test_missing_complaint_columns_marks_category_metadata_unavailable(self):
        partial_engine = create_engine("sqlite://")
        try:
            self.assertEqual(partial_engine.dialect.name, "sqlite")
            partial_metadata = MetaData()
            Table(
                "complaint", partial_metadata,
                Column("id", Integer, primary_key=True),
                Column("category", String),
            )
            partial_metadata.create_all(partial_engine)
            report = inspect_database(partial_engine)
            self.assertIn("department_id", report["schema"]["columns_missing"]["complaint"])
            self.assertEqual(report["complaint_categories_without_mapping"], [])
        finally:
            partial_engine.dispose()

    def test_unmapped_categories_are_aggregated_without_complaint_identifiers(self):
        with self.engine.begin() as connection:
            connection.execute(
                self.tables["complaint"].insert().values(
                    id=4, complaint_text="Sensitive text", category=MULTI_DEPARTMENT_LEGACY_CATEGORY,
                    location="Sensitive location", priority="Low", status="Resolved", citizen_id=1,
                )
            )
        report = inspect_database(self.engine)
        unmapped = [
            item for item in report["complaint_categories_without_mapping"]
            if item["category"] == MULTI_DEPARTMENT_LEGACY_CATEGORY
        ]
        self.assertEqual(unmapped, [{"category": MULTI_DEPARTMENT_LEGACY_CATEGORY, "complaint_count": 2}])
        rendered = str(unmapped)
        self.assertNotIn("complaint_id", rendered)
        self.assertNotIn("status", rendered)
        self.assertNotIn("department_id", rendered)

    def test_select_guard_rejects_writes_stacked_statements_and_locks(self):
        with self.engine.connect() as connection:
            for statement in (
                "UPDATE complaint SET status = 'Resolved'",
                "SELECT 1; DELETE FROM complaint",
                "SELECT 1 FOR UPDATE",
            ):
                with self.subTest(statement=statement), self.assertRaises(ValueError):
                    inspect_neon_preflight._select(connection, statement)

    def test_inspection_error_output_suppresses_connection_details(self):
        stderr = io.StringIO()
        with (
            patch.object(inspect_neon_preflight, "load_dotenv"),
            patch.object(inspect_neon_preflight, "environment_database_url", return_value="postgresql+psycopg://private"),
            patch.object(inspect_neon_preflight, "create_inspection_engine", side_effect=RuntimeError("private connection secret")),
            redirect_stderr(stderr),
        ):
            self.assertEqual(inspect_neon_preflight.main(), 1)
        self.assertIn('"status": "inspection_failed"', stderr.getvalue())
        self.assertNotIn("private", stderr.getvalue())
        self.assertNotIn("secret", stderr.getvalue())

    @patch("inspect_neon_preflight.create_engine")
    def test_postgresql_engine_is_configured_read_only_without_connecting(self, create_engine_mock):
        create_inspection_engine("postgresql+psycopg://localhost/spsfrms_test")
        create_engine_mock.assert_called_once()
        options = create_engine_mock.call_args.kwargs
        self.assertEqual(options["pool_pre_ping"], True)
        self.assertEqual(options["connect_args"]["options"], "-c default_transaction_read_only=on")

    def test_missing_or_duplicated_target_departments_are_called_out(self):
        department_table = self.tables["department"]
        with self.engine.begin() as connection:
            connection.execute(
                department_table.delete().where(department_table.c.name == "Water Supply")
            )
        missing_report = inspect_database(self.engine)
        self.assertIn("Water Supply", missing_report["expected_departments_missing"])
        self.assertEqual(missing_report["initializer_effect_preview"]["departments_that_would_be_seeded"], 1)
        self.assertEqual(missing_report["initializer_effect_preview"]["complaint_department_ids_that_would_change"], 1)

        with self.engine.begin() as connection:
            connection.execute(department_table.insert().values(
                id=100, name="Water Supply", email="duplicate-fixture@example.invalid",
            ))
            connection.execute(department_table.insert().values(
                id=101, name="water supply", email="duplicate-fixture@example.invalid",
            ))
        duplicate_report = inspect_database(self.engine)
        self.assertNotIn("Water Supply", duplicate_report["expected_departments_missing"])
        self.assertTrue(duplicate_report["department_name_duplicates_case_insensitive"])
        self.assertGreater(
            duplicate_report["initializer_effect_preview"][
                "complaint_department_id_update_count_uncertain_due_to_duplicate_departments"
            ],
            0,
        )


if __name__ == "__main__":
    unittest.main()
