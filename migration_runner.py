"""Explicit, versioned PostgreSQL migrations for SPSFRMS.

This module never imports the Flask application and is never called at app
startup. Database credentials are read only from
``SPSFRMS_MIGRATION_DATABASE_URL`` after explicit CLI and typed confirmation.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sys

from sqlalchemy.engine import make_url


DATABASE_URL_ENV = "SPSFRMS_MIGRATION_DATABASE_URL"
LATEST_REQUIRED_VERSION = "0002"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
ADVISORY_LOCK_KEY = 6_241_283_289_412_204_955
MIGRATION_TABLE = "spsfrms_schema_migration"
APP_SCHEMA_TABLES = (
    "user",
    "department",
    "complaint",
    "complaint_department",
    "complaint_assignment",
    "complaint_status_history",
)
MIGRATION_NAME = re.compile(r"^(?P<version>[0-9]{4})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    """A safe-to-report migration validation or execution failure."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


def discover_migrations(directory=MIGRATIONS_DIR):
    """Load a contiguous, ordered migration chain without touching a database."""
    files = []
    for path in Path(directory).iterdir():
        if path.suffix.lower() != ".sql":
            continue
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise MigrationError("Migration filenames must use NNNN_description.sql.")
        data = path.read_bytes()
        files.append(Migration(
            version=match.group("version"),
            name=path.name,
            sql=data.decode("utf-8"),
            checksum=hashlib.sha256(data).hexdigest(),
        ))
    files.sort(key=lambda item: item.version)
    versions = [item.version for item in files]
    expected = [f"{number:04d}" for number in range(1, len(versions) + 1)]
    if versions != expected:
        raise MigrationError("Migration versions must be unique and contiguous from 0001.")
    if not files or files[-1].version != LATEST_REQUIRED_VERSION:
        raise MigrationError("The required application migration chain is incomplete.")
    return tuple(files)


def canonical_postgresql_url(raw_url, *, target):
    """Validate a PostgreSQL URL without returning or logging its credentials."""
    try:
        parsed = make_url(raw_url)
    except Exception as error:
        raise MigrationError("The explicitly supplied migration URL is invalid.") from error
    if parsed.get_backend_name() != "postgresql" or not parsed.database:
        raise MigrationError("A PostgreSQL URL with a database name is required.")
    if target == "disposable-local" and (parsed.host or "").strip("[]").lower() not in {
        "localhost", "127.0.0.1", "::1",
    }:
        raise MigrationError("The disposable-local target must use a loopback PostgreSQL host.")
    return parsed.set(drivername="postgresql").render_as_string(hide_password=False), parsed.database


def schema_fingerprint(connection):
    """Hash PostgreSQL columns, constraints, indexes, and triggers for app tables."""
    rows = []
    queries = (
        ("columns", """
            SELECT c.relname, a.attname, pg_catalog.format_type(a.atttypid, a.atttypmod),
                   a.attnotnull, pg_catalog.pg_get_expr(d.adbin, d.adrelid)
            FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            JOIN pg_catalog.pg_attribute AS a ON a.attrelid = c.oid
            LEFT JOIN pg_catalog.pg_attrdef AS d ON d.adrelid = c.oid AND d.adnum = a.attnum
            WHERE n.nspname = 'public' AND c.relname = ANY(%s)
              AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY c.relname, a.attnum
        """),
        ("constraints", """
            SELECT t.relname, con.conname, con.contype, pg_catalog.pg_get_constraintdef(con.oid, true)
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS t ON t.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND t.relname = ANY(%s)
            ORDER BY t.relname, con.conname
        """),
        ("indexes", """
            SELECT tablename, indexname, indexdef FROM pg_catalog.pg_indexes
            WHERE schemaname = 'public' AND tablename = ANY(%s)
            ORDER BY tablename, indexname
        """),
        ("triggers", """
            SELECT t.relname, tr.tgname, pg_catalog.pg_get_triggerdef(tr.oid, true),
                   p.proname, pg_catalog.pg_get_functiondef(p.oid)
            FROM pg_catalog.pg_trigger AS tr
            JOIN pg_catalog.pg_class AS t ON t.oid = tr.tgrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace
            JOIN pg_catalog.pg_proc AS p ON p.oid = tr.tgfoid
            WHERE n.nspname = 'public' AND t.relname = ANY(%s) AND NOT tr.tgisinternal
            ORDER BY t.relname, tr.tgname
        """),
    )
    with connection.cursor() as cursor:
        for label, query in queries:
            cursor.execute(query, (list(APP_SCHEMA_TABLES),))
            rows.append((label, cursor.fetchall()))
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_migration_table(connection):
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT column_name, data_type, character_maximum_length, is_nullable
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = %s
            ORDER BY ordinal_position
        """, (MIGRATION_TABLE,))
        columns = cursor.fetchall()
        cursor.execute("""
            SELECT con.contype, pg_catalog.pg_get_constraintdef(con.oid, true)
            FROM pg_catalog.pg_constraint AS con
            JOIN pg_catalog.pg_class AS t ON t.oid = con.conrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public' AND t.relname = %s
        """, (MIGRATION_TABLE,))
        constraints = cursor.fetchall()
    expected = [
        ("version", "character varying", 32, "NO"),
        ("checksum", "character", 64, "NO"),
        ("schema_fingerprint", "character", 64, "NO"),
        ("applied_at", "timestamp with time zone", None, "NO"),
    ]
    if columns != expected or not any(
        row[0] == "p" and "PRIMARY KEY (version)" in row[1] for row in constraints
    ):
        raise MigrationError("The migration ledger has an unexpected schema.")


def apply_migrations(connection, migrations, *, expected_database):
    """Apply pending files under one PostgreSQL session advisory lock.

    Each migration's DDL and ledger row share a transaction. Existing
    unversioned application schemas are deliberately refused; automatic
    baseline adoption needs separate schema review.
    """
    acquired_lock = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT current_database(), current_setting('server_version_num')::integer"
            )
            identity = cursor.fetchone()
        connection.commit()
        if not identity or identity[0] != expected_database or int(identity[1]) < 120000:
            raise MigrationError("The connected PostgreSQL target does not match the confirmed target.")

        with connection.cursor() as cursor:
            cursor.execute("SET search_path TO public")
            cursor.execute("SELECT pg_catalog.pg_advisory_lock(%s)", (ADVISORY_LOCK_KEY,))
        connection.commit()
        acquired_lock = True

        with connection.cursor() as cursor:
            cursor.execute("""
                SELECT tablename FROM pg_catalog.pg_tables
                WHERE schemaname = 'public' AND tablename <> %s
            """, (MIGRATION_TABLE,))
            existing_tables = {row[0] for row in cursor.fetchall()}
            cursor.execute("SELECT pg_catalog.to_regclass(%s)", (f"public.{MIGRATION_TABLE}",))
            has_ledger = cursor.fetchone()[0] is not None
        connection.commit()

        if not has_ledger:
            if existing_tables:
                raise MigrationError(
                    "An existing unversioned schema was found; automatic baseline adoption is disabled."
                )
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(f"""
                        CREATE TABLE public.{MIGRATION_TABLE} (
                            version VARCHAR(32) PRIMARY KEY,
                            checksum CHAR(64) NOT NULL,
                            schema_fingerprint CHAR(64) NOT NULL,
                            applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                        )
                    """)

        _validate_migration_table(connection)
        connection.commit()

        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT version, checksum, schema_fingerprint FROM public.{MIGRATION_TABLE} ORDER BY version"
            )
            applied_rows = cursor.fetchall()
        connection.commit()

        by_version = {migration.version: migration for migration in migrations}
        known_versions = [migration.version for migration in migrations]
        applied_versions = [row[0] for row in applied_rows]
        if applied_versions != known_versions[:len(applied_versions)]:
            raise MigrationError("The migration ledger contains an unknown or out-of-order version.")
        for version, checksum, fingerprint in applied_rows:
            migration = by_version[version]
            if checksum.strip() != migration.checksum:
                raise MigrationError(f"Applied migration {version} has a checksum mismatch.")
        if applied_rows and applied_rows[-1][2].strip() != schema_fingerprint(connection):
            connection.commit()
            raise MigrationError("The current schema differs from its recorded migration fingerprint.")
        connection.commit()

        applied = set(applied_versions)
        for migration in migrations:
            if migration.version in applied:
                continue
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(migration.sql, prepare=False)
                fingerprint = schema_fingerprint(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO public.{MIGRATION_TABLE} "
                        "(version, checksum, schema_fingerprint) VALUES (%s, %s, %s)",
                        (migration.version, migration.checksum, fingerprint),
                    )
            applied.add(migration.version)
        return tuple(sorted(applied))
    except MigrationError:
        raise
    except Exception as error:
        try:
            connection.rollback()
        except Exception:
            pass
        raise MigrationError("A migration operation failed; raw database details were suppressed.") from error
    finally:
        if acquired_lock:
            try:
                connection.rollback()
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_catalog.pg_advisory_unlock(%s)", (ADVISORY_LOCK_KEY,))
                connection.commit()
            except Exception:
                try:
                    connection.rollback()
                except Exception:
                    pass


def _connect(database_url):
    """Connect with psycopg 3 after caller authorization; never log this URL."""
    import psycopg

    parsed = make_url(database_url)
    canonical = parsed.set(drivername="postgresql").render_as_string(hide_password=False)
    return psycopg.connect(canonical, autocommit=False)


def main(argv=None, *, environ=None, input_fn=input, connect_fn=None, output=print):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="authorize the migration workflow")
    parser.add_argument("--target", choices=("disposable-local", "staging"))
    parser.add_argument("--expected-database-name")
    args = parser.parse_args(argv)
    if not args.apply or not args.target or not args.expected_database_name:
        output("Refusing migrations without --apply, an explicit target, and an expected database name.")
        return 2

    typed_confirmation = f"APPLY MIGRATIONS {args.target} {args.expected_database_name}"
    try:
        response = input_fn(f"Type {typed_confirmation} to continue: ")
    except (EOFError, KeyboardInterrupt):
        output("Migration cancelled.")
        return 2
    if response.strip() != typed_confirmation:
        output("Migration cancelled.")
        return 2

    environment = os.environ if environ is None else environ
    raw_url = environment.get(DATABASE_URL_ENV)
    if not raw_url:
        output(f"Set {DATABASE_URL_ENV} explicitly for the approved target; no fallback is used.")
        return 2
    try:
        canonical_url, parsed_database = canonical_postgresql_url(raw_url, target=args.target)
        if parsed_database != args.expected_database_name:
            raise MigrationError("The URL database does not match the confirmed database name.")
        migrations = discover_migrations()
    except MigrationError as error:
        output(str(error))
        return 2

    connection = None
    try:
        connection = (connect_fn or _connect)(canonical_url)
        versions = apply_migrations(
            connection, migrations, expected_database=args.expected_database_name
        )
        output(f"Migrations complete through version {versions[-1]}.")
        return 0
    except MigrationError as error:
        output(str(error))
        return 1
    except Exception:
        output("PostgreSQL connection failed; connection details were suppressed.")
        return 1
    finally:
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
