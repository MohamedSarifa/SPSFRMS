"""Environment-based database URL normalization shared without importing Flask."""

import os


def normalize_database_url(database_url):
    if database_url and database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url and database_url.startswith("postgresql://"):
        database_url = database_url.replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
    return database_url


def environment_database_url(*, required=False):
    """Return the configured URL without logging or exposing it."""
    database_url = os.getenv("DATABASE_URL")
    if not database_url and required:
        raise RuntimeError("DATABASE_URL is required for database inspection.")
    return normalize_database_url(database_url)
