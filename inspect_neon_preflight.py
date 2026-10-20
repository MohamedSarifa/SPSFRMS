"""Read-only SPSFRMS database preflight. This module never imports ``app``."""

import json
import os
import re
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from database_config import environment_database_url
from workflow_routing import CATEGORY_DEPARTMENTS


EXPECTED_COLUMNS = {
    "user": {
        "id", "name", "email", "phone", "password", "role", "registered_date", "department_id",
    },
    "department": {"id", "name", "email", "head_id"},
    "complaint": {
        "id", "complaint_text", "category", "location", "image", "keywords", "priority",
        "status", "created_date", "updated_date", "citizen_id", "department_id",
        "assigned_staff_id", "resolution_note",
    },
    "complaint_department": {"id", "complaint_id", "department_id", "assigned_date"},
    "complaint_assignment": {
        "id", "complaint_id", "staff_id", "assigned_by", "assigned_date", "remarks",
    },
    "complaint_status_history": {
        "id", "complaint_id", "old_status", "new_status", "updated_by", "timestamp", "remarks",
    },
}

EXPECTED_DEPARTMENTS = sorted(set(CATEGORY_DEPARTMENTS.values()))


def _select(connection, statement, parameters=None):
    """Fail closed unless this helper is given a single SELECT statement."""
    normalized = statement.strip()
    if (
        not re.match(r"^SELECT\b", normalized, re.IGNORECASE)
        or ";" in normalized
        or re.search(r"\bFOR\s+(UPDATE|SHARE)\b", normalized, re.IGNORECASE)
    ):
        raise ValueError("Preflight permits SELECT statements only.")
    return connection.execute(text(normalized), parameters or {})


def _schema(connection, dialect):
    if dialect == "postgresql":
        rows = _select(
            connection,
            "SELECT table_name, column_name "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "ORDER BY table_name, ordinal_position",
        )
        columns = {}
        for table_name, column_name in rows:
            if table_name in EXPECTED_COLUMNS:
                columns.setdefault(table_name, []).append(column_name)
    elif dialect == "sqlite":
        tables = {
            row[0] for row in _select(
                connection,
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name",
            )
        }
        columns = {}
        for table_name in EXPECTED_COLUMNS.keys() & tables:
            columns[table_name] = [
                row.name for row in _select(
                    connection,
                    "SELECT name FROM pragma_table_info(:table_name) ORDER BY cid",
                    {"table_name": table_name},
                )
            ]
    else:
        raise RuntimeError("Preflight supports PostgreSQL and SQLite only.")

    return {
        table_name: sorted(column_names)
        for table_name, column_names in columns.items()
    }


def _has(columns, table_name, *required):
    return table_name in columns and set(required) <= set(columns[table_name])


def inspect_database(engine):
    """Inspect schema and initializer-relevant counts using SELECT queries only."""
    dialect = engine.dialect.name
    with engine.connect() as connection:
        columns = _schema(connection, dialect)
        tables_present = sorted(columns)
        missing_tables = sorted(set(EXPECTED_COLUMNS) - set(columns))
        missing_columns = {
            table: sorted(EXPECTED_COLUMNS[table] - set(columns.get(table, [])))
            for table in EXPECTED_COLUMNS
            if EXPECTED_COLUMNS[table] - set(columns.get(table, []))
        }
        report = {
            "database_dialect": dialect,
            "schema": {
                "tables_present": tables_present,
                "tables_missing": missing_tables,
                "columns_present": columns,
                "columns_missing": missing_columns,
            },
            "departments": [],
            "department_name_duplicates_case_insensitive": [],
            "expected_departments_missing": [],
            "user_role_counts": {},
            "legacy_officer_role_count": None,
            "demo_admin_accounts_present": None,
            "complaint_mappings": [],
            "complaint_mapping_discrepancies": [],
            "complaint_categories_without_mapping": [],
            "assignment_history_gaps": None,
            "status_history_gaps": None,
            "initializer_effect_preview": {},
            "optional_seed_data_enabled": os.getenv("SPSFRMS_SEED_DEMO_DATA") == "1",
        }

        department_rows = []
        if _has(columns, "department", "id", "name"):
            department_rows = list(_select(
                connection,
                'SELECT id, name FROM "department" ORDER BY id',
            ))
            report["departments"] = [
                {"id": row.id, "name": row.name}
                for row in department_rows
            ]
            names_by_key = {}
            for row in department_rows:
                names_by_key.setdefault(row.name.casefold(), []).append(row.name)
            report["department_name_duplicates_case_insensitive"] = [
                sorted(set(names)) for names in names_by_key.values() if len(names) > 1
            ]
            department_ids_by_name = {}
            for row in department_rows:
                department_ids_by_name.setdefault(row.name.casefold(), []).append(row.id)
            department_ids = {
                name_key: ids[0] for name_key, ids in department_ids_by_name.items() if len(ids) == 1
            }
            report["expected_departments_missing"] = [
                name for name in EXPECTED_DEPARTMENTS
                if name.casefold() not in department_ids_by_name
            ]
        else:
            department_ids = {}
            department_ids_by_name = {}

        role_counts = {}
        if _has(columns, "user", "role"):
            role_counts = {
                row.role: row.account_count
                for row in _select(
                    connection,
                    'SELECT role, COUNT(*) AS account_count FROM "user" GROUP BY role ORDER BY role',
                )
            }
            report["user_role_counts"] = role_counts
            report["legacy_officer_role_count"] = role_counts.get("officer", 0)
        if _has(columns, "user", "email"):
            report["demo_admin_accounts_present"] = _select(
                connection,
                'SELECT COUNT(*) FROM "user" WHERE lower(email) = :email',
                {"email": "admin@spsfrms.com"},
            ).scalar_one()

        complaint_counts = []
        if _has(columns, "complaint", "category", "department_id"):
            if _has(columns, "department", "id", "name"):
                complaint_counts = list(_select(
                    connection,
                    "SELECT c.category, c.department_id, d.name AS department_name, COUNT(*) AS complaint_count "
                    "FROM complaint AS c LEFT JOIN department AS d ON d.id = c.department_id "
                    "GROUP BY c.category, c.department_id, d.name "
                    "ORDER BY c.category, c.department_id",
                ))
            else:
                complaint_counts = list(_select(
                    connection,
                    "SELECT category, department_id, NULL AS department_name, COUNT(*) AS complaint_count "
                    "FROM complaint GROUP BY category, department_id ORDER BY category, department_id",
                ))

        mapping_discrepancies = []
        unmapped_categories = {}
        total_department_id_updates = 0
        uncertain_department_id_updates = 0
        for row in complaint_counts:
            expected_name = CATEGORY_DEPARTMENTS.get(row.category)
            if not expected_name:
                unmapped_categories[row.category] = unmapped_categories.get(row.category, 0) + row.complaint_count
                continue
            expected_key = expected_name.casefold()
            expected_ids = department_ids_by_name.get(expected_key, [])
            expected_id = expected_ids[0] if len(expected_ids) == 1 else None
            if len(expected_ids) > 1:
                would_update = None
                uncertain_department_id_updates += row.complaint_count
            elif not expected_ids:
                # initialize_database() creates this department before routing complaints.
                would_update = True
                total_department_id_updates += row.complaint_count
            else:
                would_update = row.department_id != expected_id
                if would_update:
                    total_department_id_updates += row.complaint_count
            mapping = {
                "category": row.category,
                "current_department_id": row.department_id,
                "current_department_name": row.department_name,
                "expected_department_id": expected_id,
                "expected_department_name": expected_name,
                "complaint_count": row.complaint_count,
                "expected_department_name_is_duplicated": len(expected_ids) > 1,
                "would_update_department_id": would_update,
            }
            report["complaint_mappings"].append(mapping)
            if would_update is not False:
                mapping_discrepancies.append(mapping)
        report["complaint_mapping_discrepancies"] = mapping_discrepancies
        report["complaint_categories_without_mapping"] = [
            {"category": category, "complaint_count": count}
            for category, count in sorted(unmapped_categories.items())
        ]

        if _has(columns, "complaint", "id", "assigned_staff_id") and _has(
            columns, "complaint_assignment", "complaint_id"
        ):
            report["assignment_history_gaps"] = _select(
                connection,
                "SELECT COUNT(*) FROM complaint AS c "
                "WHERE c.assigned_staff_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM complaint_assignment AS a WHERE a.complaint_id = c.id)",
            ).scalar_one()
        if _has(columns, "complaint", "id") and _has(
            columns, "complaint_status_history", "complaint_id"
        ):
            report["status_history_gaps"] = _select(
                connection,
                "SELECT COUNT(*) FROM complaint AS c "
                "WHERE NOT EXISTS (SELECT 1 FROM complaint_status_history AS h WHERE h.complaint_id = c.id)",
            ).scalar_one()

        report["initializer_effect_preview"] = {
            "departments_that_would_be_seeded": len(report["expected_departments_missing"]),
            "complaint_department_ids_that_would_change": total_department_id_updates,
            "complaint_department_id_update_count_uncertain_due_to_duplicate_departments": uncertain_department_id_updates,
            "assignment_history_rows_that_would_be_backfilled": report["assignment_history_gaps"],
            "status_history_rows_that_would_be_backfilled": report["status_history_gaps"],
            "legacy_officer_roles_that_would_be_normalized": report["legacy_officer_role_count"],
            "demo_admin_account_would_be_created": (
                report["demo_admin_accounts_present"] == 0
                if report["demo_admin_accounts_present"] is not None else None
            ),
        }
        if report["optional_seed_data_enabled"]:
            report["initializer_effect_preview"]["optional_seed_data_review_required"] = True

    return report


def create_inspection_engine(database_url):
    engine_options = {"pool_pre_ping": True, "pool_recycle": 300}
    if database_url.startswith("postgresql"):
        # PostgreSQL enforces read-only transactions for this connection.
        engine_options["connect_args"] = {
            "options": "-c default_transaction_read_only=on",
        }
    return create_engine(database_url, **engine_options)


def main():
    load_dotenv()
    try:
        database_url = environment_database_url(required=True)
        engine = create_inspection_engine(database_url)
        try:
            report = inspect_database(engine)
        finally:
            engine.dispose()
    except Exception as error:
        print(
            json.dumps({
                "status": "inspection_failed",
                "error_type": type(error).__name__,
                "details": "Connection details are suppressed.",
            }),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
