"""Safely import department heads and staff from the fictional CSV dataset.

Run from the project root after placing SPSFRMS_officers_staff_50.csv there:
    .\\venv\\Scripts\\python.exe import_officers_staff.py

The importer uses DATABASE_URL from .env (or the process environment), reuses
existing departments, skips existing email addresses, and never prints any
password or database connection details.
"""

import csv
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from password_policy import (
    MIN_NEW_ACCOUNT_PASSWORD_LENGTH,
    individual_passwords_for,
    parse_password_map_json,
    require_unique_passwords,
)
from account_identity import normalize_email

PROJECT_DIR = Path(__file__).resolve().parent
CSV_PATH = PROJECT_DIR / "SPSFRMS_officers_staff_50.csv"

# Importing app invokes its normal startup setup. Explicit .env path is needed
# when running this standalone script from contexts without a Python file frame.
load_dotenv(dotenv_path=PROJECT_DIR / ".env")
if not os.getenv("DATABASE_URL"):
    print("ERROR: DATABASE_URL is not set in the environment or project .env.")
    sys.exit(2)

from app import Department, User, app, db, find_user_by_email  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402


REQUIRED_COLUMNS = {"name", "email", "phone", "role"}
DEPARTMENT_COLUMNS = ("department_name", "department")
ROLE_MAP = {"department_head": "department_head", "head": "department_head", "staff": "staff"}


def main():
    if not CSV_PATH.is_file():
        print(f"ERROR: CSV not found at {CSV_PATH}")
        return 2

    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            print("ERROR: CSV has no header row.")
            return 2
        reader.fieldnames = [header.strip().lower() for header in reader.fieldnames]
        missing = REQUIRED_COLUMNS - set(reader.fieldnames)
        if not any(column in reader.fieldnames for column in DEPARTMENT_COLUMNS):
            missing.add("department (or department_name)")
        if missing:
            print("ERROR: CSV is missing required columns: " + ", ".join(sorted(missing)))
            return 2

        try:
            import_passwords = parse_password_map_json(
                os.getenv("SPSFRMS_IMPORT_PASSWORDS_JSON", "{}")
            )
        except ValueError:
            print("ERROR: Per-account import passwords are not configured as a valid bounded JSON object.")
            return 2

        rows = []
        for line_number, raw in enumerate(reader, start=2):
            # Ignore password data in CSV files entirely; credentials come
            # from the process environment as an individual email map.
            row = {
                str(key).strip().lower(): (value or "").strip()
                for key, value in raw.items()
                if key and str(key).strip().lower() != "password"
            }
            if not any(row.values()):
                continue
            # Accept either header; prefer the application's existing
            # canonical `department` field when both are present and filled.
            row["department"] = next(
                (row.get(column, "") for column in DEPARTMENT_COLUMNS if row.get(column, "")),
                "",
            )
            role = ROLE_MAP.get(row.get("role", "").strip().lower())
            row["email"] = normalize_email(row.get("email", ""))
            if not all(row.get(field) for field in REQUIRED_COLUMNS):
                print(f"ERROR: Row {line_number} has an empty required value.")
                return 2
            if role is None:
                print(f"ERROR: Row {line_number} has an unsupported role.")
                return 2
            row["role"] = role
            row["line_number"] = line_number
            rows.append(row)

    if len(rows) != 40:
        print(f"ERROR: Expected 40 accounts (10 department heads and 30 staff), found {len(rows)}.")
        return 2
    if sum(row["role"] == "department_head" for row in rows) != 10 or sum(row["role"] == "staff" for row in rows) != 30:
        print("ERROR: CSV must contain exactly 10 department_head and 30 staff records.")
        return 2
    try:
        passwords = individual_passwords_for(
            [row["email"] for row in rows], import_passwords,
            minimum_length=MIN_NEW_ACCOUNT_PASSWORD_LENGTH,
        )
        for row, password in zip(rows, passwords):
            row["password"] = password
    except ValueError:
        print("ERROR: Each imported account requires its own password of at least 12 characters; no records were changed.")
        return 2

    with app.app_context():
        try:
            # One transaction covers department creation, users and head links.
            # A rollback prevents a failed import from leaving partial records.
            department_created = 0
            department_reused = 0
            staff_created = 0
            staff_skipped = 0
            head_created = 0
            head_skipped = 0
            skip_reasons = []
            with db.session.begin():
                try:
                    require_unique_passwords(
                        (row["password"] for row in rows),
                        (row[0] for row in User.query.with_entities(User.password).all()),
                    )
                except ValueError:
                    raise ValueError("New accounts require individual passwords.") from None
                # Cache distinct incoming names case-insensitively so repeated
                # CSV rows and casing variants resolve to one Department.
                incoming_departments = {}
                for row in rows:
                    name = row["department"].strip()
                    key = name.casefold()
                    incoming_departments.setdefault(key, name)

                dept_by_name = {}
                for key, name in incoming_departments.items():
                    department = Department.query.filter(
                        db.func.lower(Department.name) == name.lower()
                    ).first()
                    if department:
                        department_reused += 1
                    else:
                        # Department requires only a unique, non-null name;
                        # email and head_id are optional and remain unset.
                        department = Department(name=name)
                        db.session.add(department)
                        db.session.flush()
                        department_created += 1
                    dept_by_name[key] = department

                for row in rows:
                    is_head = row["role"] == "department_head"
                    label = "department-head" if is_head else "staff"
                    existing = find_user_by_email(row["email"])
                    if existing:
                        if is_head:
                            head_skipped += 1
                        else:
                            staff_skipped += 1
                        skip_reasons.append(
                            f"{label} {row['email']}: email already exists"
                        )
                        continue
                    department = dept_by_name[row["department"].strip().casefold()]
                    if is_head and department.head_id is not None:
                        # The application links one head to each department.
                        # Preserve the current head and skip this CSV account.
                        head_skipped += 1
                        skip_reasons.append(
                            f"department-head {row['email']}: department "
                            f"'{department.name}' already has a different head"
                        )
                        continue
                    user = User(
                        name=row["name"],
                        email=row["email"],
                        phone=row["phone"],
                        password=generate_password_hash(row["password"]),
                        role=row["role"],
                        department_id=department.id,
                    )
                    db.session.add(user)
                    db.session.flush()
                    if row["role"] == "department_head" and department.head_id is None:
                        department.head_id = user.id
                        head_created += 1
                    else:
                        staff_created += 1

            print(
                "Import complete. "
                f"Departments created: {department_created}; reused: {department_reused}. "
                f"Staff created: {staff_created}; skipped: {staff_skipped}. "
                f"Department-head accounts created: {head_created}; skipped: {head_skipped}."
            )
            if skip_reasons:
                print("Skip reasons:")
                for reason in skip_reasons:
                    print(f"  - {reason}")
            return 0
        except Exception as error:
            db.session.rollback()
            # Do not print exception strings; database drivers can include
            # connection information in them.
            print(f"ERROR: Import failed and was rolled back ({type(error).__name__}).")
            return 1


if __name__ == "__main__":
    sys.exit(main())
