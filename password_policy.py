"""Password checks shared by account creation paths."""

import json

from werkzeug.security import check_password_hash
from account_identity import normalize_email

MIN_NEW_ACCOUNT_PASSWORD_LENGTH = 12
MAX_NEW_ACCOUNT_PASSWORD_LENGTH = 1024
MAX_PASSWORD_MAP_JSON_BYTES = 64 * 1024


def _reject_duplicate_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Password configuration contains duplicate keys.")
        result[key] = value
    return result


def parse_password_map_json(raw_json):
    """Parse a bounded JSON object without echoing secret values in errors."""
    if not isinstance(raw_json, str):
        raise ValueError("Password configuration must be valid JSON.")
    try:
        encoded = raw_json.encode("utf-8")
    except UnicodeError:
        raise ValueError("Password configuration must be valid JSON.") from None
    if len(encoded) > MAX_PASSWORD_MAP_JSON_BYTES:
        raise ValueError("Password configuration exceeds the allowed size.")
    try:
        value = json.loads(raw_json, object_pairs_hook=_reject_duplicate_json_keys)
    except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
        raise ValueError("Password configuration must be a valid JSON object.") from None
    if not isinstance(value, dict):
        raise ValueError("Password configuration must be a JSON object.")
    return value


def _validate_password(password, minimum_length):
    return (
        isinstance(password, str)
        and minimum_length <= len(password) <= MAX_NEW_ACCOUNT_PASSWORD_LENGTH
        and bool(password.strip())
        and not any(ord(character) < 32 or ord(character) == 127 for character in password)
    )


def password_meets_policy(password, minimum_length=MIN_NEW_ACCOUNT_PASSWORD_LENGTH):
    """Return whether a password is a bounded, nonblank string of valid length."""
    return _validate_password(password, minimum_length)


def validate_password_map(password_map, emails, minimum_length=MIN_NEW_ACCOUNT_PASSWORD_LENGTH):
    """Require exactly one valid password entry for each normalized email."""
    if not isinstance(password_map, dict):
        raise ValueError("Per-account passwords must be provided as an object.")

    expected_emails = [normalize_email(email) for email in emails]
    if any(not email for email in expected_emails) or len(set(expected_emails)) != len(expected_emails):
        raise ValueError("Account email configuration is invalid or duplicated.")

    normalized_map = {}
    for email, password in password_map.items():
        if not isinstance(email, str):
            raise ValueError("Password configuration contains an invalid account key.")
        normalized_email = normalize_email(email)
        if not normalized_email or normalized_email in normalized_map:
            raise ValueError("Password configuration contains duplicate account keys.")
        normalized_map[normalized_email] = password

    if set(normalized_map) != set(expected_emails):
        raise ValueError("Password configuration must match the requested account list exactly.")
    passwords = [normalized_map[email] for email in expected_emails]
    if any(not password_meets_policy(password, minimum_length) for password in passwords):
        raise ValueError("Every new account requires a valid password of sufficient length.")
    require_unique_passwords(passwords)
    return normalized_map


def require_unique_passwords(passwords, existing_hashes=()):
    """Best-effort reuse check against salted hashes; not concurrency-safe globally."""
    candidates = list(passwords)
    if any(not isinstance(password, str) or not password for password in candidates):
        raise ValueError("Every new account must have an individual password.")
    if len(set(candidates)) != len(candidates):
        raise ValueError("New accounts must use individual passwords.")
    hashes = tuple(password_hash for password_hash in existing_hashes if password_hash)
    for password in candidates:
        if any(check_password_hash(password_hash, password) for password_hash in hashes):
            raise ValueError("Choose individual passwords that are not already in use.")
    return True


def individual_passwords_for(
    emails, password_map, minimum_length=MIN_NEW_ACCOUNT_PASSWORD_LENGTH,
):
    """Return passwords in email order after exact-map and policy validation."""
    emails = list(emails)
    validated = validate_password_map(password_map, emails, minimum_length)
    return [validated[normalize_email(email)] for email in emails]
