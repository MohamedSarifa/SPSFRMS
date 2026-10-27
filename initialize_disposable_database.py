"""Explicitly initialize a local disposable SPSFRMS PostgreSQL database.

This command is deliberately separate from importing ``app``. It requires a
local PostgreSQL URL through SPSFRMS_DISPOSABLE_DATABASE_URL, an explicit CLI
flag, and an interactive confirmation before importing the application or
calling its existing initializer. It never creates demo users or seed data.
"""

import argparse
import importlib
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


def main(argv=None, *, environ=None, input_fn=input, app_loader=None):
    """Run the existing initializer after two explicit operator confirmations."""
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

    # Pass only the explicitly supplied disposable URL to app configuration;
    # never fall back to DATABASE_URL or print either value.
    environment["DATABASE_URL"] = database_url
    application = None
    try:
        application = (app_loader or (lambda: importlib.import_module("app")))()
        application.initialize_database()
    except Exception:
        rollback_failed = False
        if application is not None:
            try:
                application.db.session.rollback()
            except Exception:
                rollback_failed = True
        if rollback_failed:
            print("Database initialization failed and rollback could not be confirmed.")
        else:
            print("Database initialization failed. Details were suppressed to protect connection data.")
        return 1

    print("Database initialization completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
