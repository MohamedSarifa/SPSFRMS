"""Compatibility entry point for explicitly migrating local disposable PostgreSQL.

The command does not import the Flask application or call ``db.create_all``.
It delegates to the versioned migration runner after explicit confirmation.
"""

import argparse
import os
import sys


CONFIRMATION_TEXT = "INITIALIZE"
DATABASE_URL_ENV = "SPSFRMS_DISPOSABLE_DATABASE_URL"


def validate_disposable_database_url(database_url):
    """Accept only PostgreSQL URLs targeting an explicit loopback host."""
    try:
        from sqlalchemy.engine import make_url

        parsed = make_url(database_url)
    except Exception as error:
        raise ValueError("A valid local PostgreSQL database URL is required.") from error

    host = (parsed.host or "").strip("[]").lower()
    if parsed.get_backend_name() != "postgresql" or host not in {
        "localhost", "127.0.0.1", "::1",
    }:
        raise ValueError("Only a loopback PostgreSQL database is allowed.")
    if not parsed.database:
        raise ValueError("The local PostgreSQL database name is required.")
    return database_url


def main(argv=None, *, environ=None, input_fn=input, migration_main=None):
    """Run versioned migrations on a loopback database after confirmation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-disposable-database",
        action="store_true",
        help="acknowledge that initialization may alter schema and existing rows",
    )
    arguments = parser.parse_args(argv)
    if not arguments.confirm_disposable_database:
        print("Refusing initialization without --confirm-disposable-database.")
        return 2

    environment = os.environ if environ is None else environ
    raw_url = environment.get(DATABASE_URL_ENV)
    if not raw_url:
        print(f"Set {DATABASE_URL_ENV} to an explicitly selected local disposable database.")
        return 2
    try:
        database_url = validate_disposable_database_url(raw_url)
    except ValueError as error:
        print(str(error))
        return 2

    try:
        response = input_fn(
            "Initialization may create or alter schema and update existing rows. "
            "Type INITIALIZE to continue: "
        )
    except (EOFError, KeyboardInterrupt):
        print("Initialization cancelled.")
        return 2
    if response.strip() != CONFIRMATION_TEXT:
        print("Initialization cancelled.")
        return 2

    try:
        from sqlalchemy.engine import make_url

        database_name = make_url(database_url).database
        runner = migration_main
        if runner is None:
            from migration_runner import main as runner
        migration_environment = dict(environment)
        migration_environment["SPSFRMS_MIGRATION_DATABASE_URL"] = database_url
        return runner(
            [
                "--apply", "--target", "disposable-local",
                "--expected-database-name", database_name,
            ],
            environ=migration_environment,
            input_fn=input_fn,
        )
    except Exception:
        print("Database migration failed. Details were suppressed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
