"""Canonical account identity helpers."""


def normalize_email(email):
    """Use the application's case-insensitive email identity consistently."""
    return (email or "").strip().lower()
