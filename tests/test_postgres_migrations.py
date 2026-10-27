"""Destructive integration tests requiring an explicitly disposable local PostgreSQL DB.

These tests are never part of the ordinary offline test run. They refuse to
connect unless three explicit gates are supplied and the target is loopback
with a dedicated spsfrms_test_ database name.
"""

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
import subprocess
import sys
import unittest

from sqlalchemy.engine import make_url

from migration_runner import (
    Migration,
    apply_migrations,
    canonical_postgresql_url,
    discover_migrations,
)


RUN_FLAG = "SPSFRMS_RUN_POSTGRES_TESTS"
URL_ENV = "SPSFRMS_TEST_POSTGRES_URL"
DISPOSABLE_CONFIRMATION = "I_CONFIRM_DROP_SPSFRMS_TEST_SCHEMA"


def _authorized_target():
    if os.environ.get(RUN_FLAG) != "1":
        raise unittest.SkipTest("PostgreSQL integration tests require explicit opt-in.")
    if os.environ.get("SPSFRMS_TEST_POSTGRES_DISPOSABLE") != DISPOSABLE_CONFIRMATION:
        raise unittest.SkipTest("Disposable-database confirmation is missing.")
    raw_url = os.environ.get(URL_ENV)
    if not raw_url:
        raise unittest.SkipTest("No dedicated PostgreSQL test URL was supplied.")
    parsed = make_url(raw_url)
    host = (parsed.host or "").strip("[]").lower()
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise unittest.SkipTest("PostgreSQL integration tests accept loopback hosts only.")
    if not (parsed.database or "").startswith("spsfrms_test_"):
        raise unittest.SkipTest("PostgreSQL integration tests require an spsfrms_test_ database name.")
    canonical_url, database_name = canonical_postgresql_url(raw_url, target="disposable-local")
    return canonical_url, database_name


class PostgreSQLMigrationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.database_url, cls.database_name = _authorized_target()
        import psycopg

        cls.psycopg = psycopg
        cls.connection = psycopg.connect(cls.database_url, autocommit=True)
        with cls.connection.cursor() as cursor:
            cursor.execute("DROP SCHEMA public CASCADE")
            cursor.execute("CREATE SCHEMA public")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "connection"):
            try:
                cls.connection.close()
            except Exception:
                pass

    def test_migration_and_department_assignment_database_contract(self):
        migrations = discover_migrations()
        self.connection.autocommit = False

        # Build the baseline, then place representative legacy data before 0002.
        self.assertEqual(
            apply_migrations(self.connection, migrations[:1], expected_database=self.database_name),
            ("0001",),
        )
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT id FROM department WHERE name = 'Water Supply'")
            water_department_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO "user" (name, email, phone, password, role, department_id)
                VALUES ('Synthetic legacy staff', 'legacy-staff@example.test', '1', 'x', 'staff', %s)
                RETURNING id
            """, (water_department_id,))
            legacy_staff_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO "user" (name, email, phone, password, role)
                VALUES ('Synthetic actor', 'legacy-actor@example.test', '2', 'x', 'admin')
                RETURNING id
            """)
            actor_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO complaint
                    (complaint_text, category, location, status, citizen_id, department_id)
                VALUES ('Synthetic legacy fixture', 'Water Supply', 'Test', 'In Progress', %s, %s)
                RETURNING id
            """, (actor_id, water_department_id))
            legacy_complaint_id = cursor.fetchone()[0]
            cursor.execute("""
                INSERT INTO complaint_assignment
                    (complaint_id, staff_id, assigned_by, assigned_date, remarks)
                VALUES (%s, %s, %s, TIMESTAMP '2025-01-01 00:00:00', 'synthetic legacy fixture')
            """, (legacy_complaint_id, legacy_staff_id, actor_id))
            cursor.execute("""
                INSERT INTO complaint_status_history
                    (complaint_id, old_status, new_status, updated_by, timestamp, remarks)
                VALUES (%s, 'Assigned', 'In Progress', %s, TIMESTAMP '2025-01-02 00:00:00', 'synthetic legacy fixture')
            """, (legacy_complaint_id, actor_id))
        self.connection.commit()

        self.assertEqual(
            apply_migrations(self.connection, migrations, expected_database=self.database_name),
            ("0001", "0002"),
        )
        # A second runner call is a no-op when checksums and schema agree.
        self.assertEqual(
            apply_migrations(self.connection, migrations, expected_database=self.database_name),
            ("0001", "0002"),
        )
        with self.connection.cursor() as cursor:
            cursor.execute('SELECT is_active FROM "user" WHERE id = %s', (legacy_staff_id,))
            self.assertFalse(cursor.fetchone()[0])
            cursor.execute("""
                SELECT status FROM complaint_department
                WHERE complaint_id = %s AND department_id = %s
            """, (legacy_complaint_id, water_department_id))
            self.assertEqual(cursor.fetchone()[0], "In Progress")
            cursor.execute("SELECT count(*) FROM complaint_assignment WHERE complaint_id = %s", (legacy_complaint_id,))
            self.assertEqual(cursor.fetchone()[0], 1)
            cursor.execute("SELECT department_id FROM complaint_assignment WHERE complaint_id = %s", (legacy_complaint_id,))
            self.assertIsNone(cursor.fetchone()[0])

            department_ids = []
            for name in ("Water Supply", "Drainage", "Public Health"):
                cursor.execute("SELECT id FROM department WHERE name = %s", (name,))
                department_ids.append(cursor.fetchone()[0])
            staff_ids = []
            staff_email_number = 1
            for department_index, department_id in enumerate(department_ids):
                department_staff = []
                staff_count = 2 if department_index == 0 else 1
                for _ in range(staff_count):
                    cursor.execute("""
                        INSERT INTO "user" (name, email, phone, password, role, department_id, is_active)
                        VALUES (%s, %s, %s, 'x', 'staff', %s, TRUE) RETURNING id
                    """, (
                        f"Synthetic staff {staff_email_number}",
                        f"staff-{staff_email_number}@example.test",
                        str(staff_email_number),
                        department_id,
                    ))
                    department_staff.append(cursor.fetchone()[0])
                    staff_email_number += 1
                staff_ids.append(department_staff)
            cursor.execute("""
                INSERT INTO complaint
                    (complaint_text, category, location, status, citizen_id, department_id)
                VALUES ('Synthetic three-department fixture', 'Water Supply', 'Test', 'Submitted', %s, %s)
                RETURNING id
            """, (actor_id, department_ids[0]))
            multidepartment_complaint_id = cursor.fetchone()[0]
            for department_id, department_staff in zip(department_ids, staff_ids):
                cursor.execute("""
                    INSERT INTO complaint_department
                        (complaint_id, department_id, assigned_staff_id, assigned_by,
                         staff_assigned_date, status)
                    VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, 'Assigned')
                """, (multidepartment_complaint_id, department_id, department_staff[0], actor_id))
        self.connection.commit()

        with self.connection.cursor() as cursor:
            # Reassignment/status updates remain department-scoped.
            cursor.execute("""
                UPDATE complaint_department SET assigned_staff_id = %s
                WHERE complaint_id = %s AND department_id = %s
            """, (staff_ids[0][1], multidepartment_complaint_id, department_ids[0]))
            cursor.execute("""
                UPDATE complaint_department SET status = 'Resolved', resolution_note = 'synthetic test'
                WHERE complaint_id = %s AND department_id = %s
            """, (multidepartment_complaint_id, department_ids[0]))
        self.connection.commit()

        with self.connection.cursor() as cursor:
            cursor.execute("""
                SELECT department_id, assigned_staff_id, status FROM complaint_department
                WHERE complaint_id = %s ORDER BY department_id
            """, (multidepartment_complaint_id,))
            rows = cursor.fetchall()
            self.assertEqual(len(rows), 3)
            states = {row[0]: (row[1], row[2]) for row in rows}
            self.assertEqual(states[department_ids[0]], (staff_ids[0][1], "Resolved"))
            self.assertEqual(states[department_ids[1]], (staff_ids[1][0], "Assigned"))
            self.assertEqual(states[department_ids[2]], (staff_ids[2][0], "Assigned"))

            with self.assertRaises(self.psycopg.errors.ForeignKeyViolation):
                with self.connection.transaction():
                    cursor.execute("""
                        INSERT INTO complaint_department (complaint_id, department_id)
                        VALUES (%s, 99999999)
                    """, (multidepartment_complaint_id,))

        self.connection.commit()
        bad_migration = Migration(
            version="0003",
            name="0003_test_failure.sql",
            sql="CREATE TABLE public.spsfrms_rollback_probe (id INTEGER); SELECT 1 / 0;",
            checksum="0" * 64,
        )
        with self.assertRaises(Exception):
            apply_migrations(
                self.connection, migrations + (bad_migration,), expected_database=self.database_name
            )
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.spsfrms_rollback_probe')")
            self.assertIsNone(cursor.fetchone()[0])
            cursor.execute("SELECT version FROM spsfrms_schema_migration ORDER BY version")
            self.assertEqual([row[0] for row in cursor.fetchall()], ["0001", "0002"])
        self.connection.commit()

        # PostgreSQL row locks serialize concurrent workflow writers. This only
        # verifies the DB primitive, not the Flask route's complete transaction.
        competing = self.psycopg.connect(self.database_url, autocommit=False)
        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SELECT id FROM complaint WHERE id = %s FOR UPDATE", (multidepartment_complaint_id,))
            with competing.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '100ms'")
                with self.assertRaises(self.psycopg.errors.LockNotAvailable):
                    cursor.execute("SELECT id FROM complaint WHERE id = %s FOR UPDATE", (multidepartment_complaint_id,))
            competing.rollback()
        finally:
            self.connection.rollback()
            competing.close()

        # Exercise the staff-row lock protocol used by assignment requests.
        # A second connection waits for the first, then performs a fresh
        # READ COMMITTED workload count before deciding whether to assign.
        concurrency_staff_id = staff_ids[1][0]
        candidate_complaints = []
        with self.connection.cursor() as cursor:
            for _ in range(3):
                cursor.execute("""
                    INSERT INTO complaint
                        (complaint_text, category, location, priority, status, citizen_id, department_id)
                    VALUES ('Synthetic workload concurrency fixture', 'Water Supply', 'Test', 'Low',
                            'Submitted', %s, %s)
                    RETURNING id
                """, (actor_id, department_ids[1]))
                candidate_id = cursor.fetchone()[0]
                candidate_complaints.append(candidate_id)
                cursor.execute("""
                    INSERT INTO complaint_department (complaint_id, department_id, status)
                    VALUES (%s, %s, 'Submitted')
                """, (candidate_id, department_ids[1]))
            # One active assignment leaves exactly one of the two slots open.
            cursor.execute("""
                SELECT id FROM complaint_department
                WHERE complaint_id = %s AND department_id = %s
            """, (multidepartment_complaint_id, department_ids[1]))
            existing_state_id = cursor.fetchone()[0]
            cursor.execute("""
                UPDATE complaint_department SET assigned_staff_id = %s, status = 'Assigned'
                WHERE id = %s
            """, (concurrency_staff_id, existing_state_id))
        self.connection.commit()

        barrier = Barrier(2)

        def compete(complaint_id):
            connection = self.psycopg.connect(self.database_url, autocommit=False)
            try:
                barrier.wait(timeout=5)
                with connection.cursor() as cursor:
                    cursor.execute('SELECT id FROM "user" WHERE id = %s FOR UPDATE', (concurrency_staff_id,))
                    cursor.execute("""
                        SELECT count(*) FROM complaint_department AS cd
                        JOIN complaint AS c ON c.id = cd.complaint_id
                        WHERE cd.assigned_staff_id = %s
                          AND (cd.status IS NULL OR cd.status NOT IN ('Resolved', 'Ignored'))
                          AND (c.status IS NULL OR c.status <> 'Ignored')
                    """, (concurrency_staff_id,))
                    current_workload = cursor.fetchone()[0]
                    if current_workload >= 2:
                        connection.rollback()
                        return "capacity_reached"
                    cursor.execute("""
                        UPDATE complaint_department
                        SET assigned_staff_id = %s, status = 'Assigned'
                        WHERE complaint_id = %s AND department_id = %s
                          AND assigned_staff_id IS NULL
                    """, (concurrency_staff_id, complaint_id, department_ids[1]))
                    assigned = cursor.rowcount == 1
                connection.commit()
                return "assigned" if assigned else "not_assigned"
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, candidate_complaints[:2]))
        self.assertCountEqual(results, ["assigned", "capacity_reached"])
        with self.connection.cursor() as cursor:
            cursor.execute("""
                SELECT count(*) FROM complaint_department
                WHERE assigned_staff_id = %s
                  AND status NOT IN ('Resolved', 'Ignored')
            """, (concurrency_staff_id,))
            self.assertEqual(cursor.fetchone()[0], 2)
        self.connection.commit()

    def test_real_flask_assignment_requests_serialize_at_two_active_work_items(self):
        """Race two actual assignment routes with one capacity slot remaining."""
        project_root = Path(__file__).resolve().parents[1]
        request_check = r"""
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
import uuid

os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['SPSFRMS_STARTUP_MODE'] = 'skip'
os.environ['SPSFRMS_SKIP_STARTUP_DB_INIT'] = '1'
os.environ['SECRET_KEY'] = 'synthetic-postgres-route-test-key-0123456789'

import app as application
from app import (
    Complaint, ComplaintAssignment, ComplaintDepartment, Department, User,
    active_assignment_workloads, app, db, utcnow,
)

app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
suffix = uuid.uuid4().hex
with app.app_context():
    department = Department.query.filter_by(name='Water Supply').one()
    admin = User(name='Synthetic test admin', email=f'admin-{suffix}@example.test',
                 phone='1', password='synthetic-test-value', role='admin')
    citizen = User(name='Synthetic test citizen', email=f'citizen-{suffix}@example.test',
                   phone='2', password='synthetic-test-value', role='citizen')
    staff = User(name='Synthetic test staff', email=f'staff-{suffix}@example.test',
                 phone='3', password='synthetic-test-value', role='staff',
                 department_id=department.id, is_active=True)
    db.session.add_all([admin, citizen, staff])
    db.session.flush()

    first = Complaint(complaint_text='Synthetic initial assignment', category='Water Supply',
                      location='Test', priority='Low', status='Assigned',
                      citizen_id=citizen.id, department_id=department.id,
                      assigned_staff_id=staff.id)
    first.department_assignments.append(ComplaintDepartment(
        department_id=department.id, assigned_staff_id=staff.id,
        assigned_by=admin.id, staff_assigned_date=utcnow(), status='Assigned'))
    candidates = []
    for _ in range(2):
        complaint = Complaint(complaint_text='Synthetic concurrent assignment',
                              category='Water Supply', location='Test', priority='Low',
                              status='Submitted', citizen_id=citizen.id,
                              department_id=department.id)
        complaint.department_assignments.append(ComplaintDepartment(
            department_id=department.id, status='Submitted'))
        candidates.append(complaint)
    db.session.add_all([first, *candidates])
    db.session.commit()
    admin_id, staff_id = admin.id, staff.id
    department_id = department.id
    candidate_ids = [candidate.id for candidate in candidates]

barrier = Barrier(2)
original_lock = application.get_locked_complaint_or_404
def synchronized_complaint_lock(complaint_id):
    complaint = original_lock(complaint_id)
    barrier.wait(timeout=15)
    return complaint

def submit(complaint_id):
    client = app.test_client()
    with client.session_transaction() as state:
        state['user_id'] = admin_id
        state['role'] = 'admin'
        state['csrf_token'] = 'synthetic-postgres-csrf'
    return client.post(
        f'/assign-complaint/{complaint_id}',
        data={'csrf_token': 'synthetic-postgres-csrf',
              'department_id': str(department_id), 'staff_id': str(staff_id)},
        follow_redirects=True,
    )

with patch.object(application, 'get_locked_complaint_or_404',
                  side_effect=synchronized_complaint_lock):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(submit, candidate_ids))

with app.app_context():
    assigned = ComplaintDepartment.query.filter(
        ComplaintDepartment.complaint_id.in_(candidate_ids),
        ComplaintDepartment.assigned_staff_id == staff_id,
    ).count()
    workload = active_assignment_workloads([staff_id])[staff_id]
    history_count = ComplaintAssignment.query.filter(
        ComplaintAssignment.complaint_id.in_(candidate_ids),
        ComplaintAssignment.staff_id == staff_id,
    ).count()
    assert assigned == 1
    assert workload == 2
    assert history_count == 1
assert all(response.status_code == 200 for response in responses)
assert any(b'already has 2 active assignments' in response.data
           and b'Please select another eligible staff member' in response.data
           for response in responses)
"""
        safe_environment = {
            key: os.environ[key]
            for key in ("PATH", "SYSTEMROOT", "WINDIR", "PATHEXT", "TEMP", "TMP")
            if key in os.environ
        }
        safe_environment.update({
            "DATABASE_URL": os.environ[URL_ENV],
            "SECRET_KEY": "synthetic-postgres-route-test-key-0123456789",
            "PYTHONPATH": str(project_root),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHON_DOTENV_DISABLED": "1",
            "SPSFRMS_STARTUP_MODE": "skip",
            "SPSFRMS_SKIP_STARTUP_DB_INIT": "1",
            "SPSFRMS_IMAGE_UPLOADS_ENABLED": "0",
        })
        result = subprocess.run(
            [sys.executable, "-c", request_check],
            cwd=project_root,
            env=safe_environment,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            result.returncode, 0,
            "The isolated Flask assignment concurrency subprocess failed.",
        )


if __name__ == "__main__":
    unittest.main()
