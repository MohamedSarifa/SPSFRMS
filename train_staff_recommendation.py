"""Train/evaluate staff recommendations from an explicitly supplied local file.

This module is intentionally independent of Flask and every database library.
It reads only a caller-selected CSV or JSON file and writes an artifact only
after validation, evaluation, and successful training.
"""

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
import sys

from staff_recommendation import (
    InsufficientTrainingData,
    MIN_ASSIGNMENTS_PER_STAFF,
    MIN_TRAINING_ASSIGNMENTS,
    MIN_TRAINING_COMPLAINTS,
    MIN_EVALUATION_COMPLAINTS,
    MIN_EVALUATION_COVERAGE,
    ScikitLearnUnavailable,
    chronological_group_split,
    evaluate_staff_rankings,
    reconstruct_historical_workloads,
    reviewed_candidate_roster,
    save_model_artifact,
    train_staff_models,
    training_data_fingerprint,
    validate_assignment_records,
)
from workflow_routing import CATEGORY_DEPARTMENTS


MAX_DATASET_BYTES = 256 * 1024 * 1024
MAX_COMPLAINT_TEXT_LENGTH = 100_000
ASSIGNMENT_FIELDS = {
    "assignment_id", "complaint_id", "staff_id", "assigned_at",
    "complaint_text", "category", "complaint_department_id",
    "complaint_department", "staff_role", "staff_department_id",
    "staff_department", "staff_is_active",
}
STATUS_EVENT_FIELDS = {"history_id", "complaint_id", "new_status", "timestamp"}
REVIEW_MANIFEST_FIELDS = {
    "format_version", "dataset_fingerprint", "source_identity", "records",
}
REVIEW_RECORD_FIELDS = {
    "assignment_id", "complaint_id", "staff_id", "assigned_at",
    "provenance", "reconstruction_status", "provenance_evidence_ref", "eligibility_verified",
    "eligibility_evidence_ref", "label_kind", "candidate_staff_ids",
    "candidate_roster_department_id", "candidate_roster_at",
    "candidate_roster_reviewed", "candidate_staff_eligibility_review",
    "candidate_roster_evidence_ref",
}
TRAINING_EXCLUSION_CODES = {
    "review_record_missing", "unknown_or_unverified_provenance",
    "reconstructed_or_unknown_history",
    "historical_eligibility_unverified", "unsupported_training_label",
    "candidate_roster_timestamp_mismatch", "candidate_roster_department_mismatch",
}


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Train/evaluate from a local CSV or JSON dataset; no database access is performed."
    )
    parser.add_argument("--dataset", help="Local CSV or JSON file in the documented format.")
    parser.add_argument("--review-manifest", help=(
        "Private reviewed JSON manifest bound to this dataset, its source identity, "
        "assignment evidence, and (for evaluation) a dated eligible candidate roster."
    ))
    parser.add_argument("--source-identity", help=(
        "Explicit non-secret source label independently established for this dataset; "
        "must exactly match the review manifest."
    ))
    parser.add_argument("--artifact-path", help="Absolute local artifact path outside the project.")
    parser.add_argument("--test-fraction", type=float, default=0.20)
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--validate-only", action="store_true",
                        help="Validate and summarize the local dataset without fitting or saving a model.")
    return parser.parse_args(argv)


def _report(payload):
    print(json.dumps(payload, indent=2, sort_keys=True, default=lambda value:
                      value.isoformat() if isinstance(value, datetime) else str(value)))


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _valid_source_identity(value):
    return (
        isinstance(value, str) and 1 <= len(value) <= 200
        and value[0].isalnum()
        and all(character.isalnum() or character in "._:-" for character in value)
    )


def _parse_datetime(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_assignment_timestamp")
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid_assignment_timestamp") from error


def _parse_aware_datetime(value, error_code="invalid_assignment_timestamp"):
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(error_code)
    return parsed.astimezone(timezone.utc)


def dataset_fingerprint(raw_assignments):
    """Hash the exact assignment payload as an order-independent row multiset."""
    try:
        encoded_rows = [json.dumps(
            row, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False,
        ) for row in raw_assignments]
        payload = json.dumps(
            {"schema": "spsfrms-assignment-dataset-v1", "rows": sorted(encoded_rows)},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("invalid_dataset_fingerprint_input") from error
    return hashlib.sha256(payload).hexdigest()


def _parse_positive_int(value, error_code):
    if isinstance(value, bool):
        raise ValueError(error_code)
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdecimal():
        parsed = int(value.strip())
    else:
        # In particular, reject fractional JSON numbers instead of truncating them.
        raise ValueError(error_code)
    if parsed < 1:
        raise ValueError(error_code)
    return parsed


def _normalize_assignment(row):
    if not isinstance(row, dict) or set(row) != ASSIGNMENT_FIELDS:
        raise ValueError("invalid_assignment_fields")
    normalized = dict(row)
    for field in ("assignment_id", "complaint_id", "staff_id",
                  "complaint_department_id"):
        normalized[field] = _parse_positive_int(normalized[field], "invalid_identifiers")
    normalized["assigned_at"] = _parse_aware_datetime(normalized["assigned_at"])
    # Current role, department, and active fields are retained only to preserve
    # the fixed trainer schema; their values do not gate historical rows.
    if not isinstance(normalized["complaint_text"], str) or not normalized["complaint_text"].strip():
        raise ValueError("missing_complaint_text")
    if len(normalized["complaint_text"]) > MAX_COMPLAINT_TEXT_LENGTH:
        raise ValueError("complaint_text_too_long")
    if not isinstance(normalized["category"], str) or not normalized["category"].strip():
        raise ValueError("invalid_category")
    category_lookup = {name.casefold(): name for name in CATEGORY_DEPARTMENTS}
    category_name = category_lookup.get(normalized["category"].strip().casefold())
    if category_name is None:
        raise ValueError("invalid_category")
    normalized["category"] = category_name
    expected_department = CATEGORY_DEPARTMENTS[category_name]
    complaint_department = normalized["complaint_department"]
    if (not isinstance(complaint_department, str)
            or complaint_department.strip().casefold() != expected_department.casefold()):
        raise ValueError("department_mismatch")
    return normalized


def _load_review_manifest(path, embedded_manifest, raw_assignments,
                          expected_source_identity, expected_dataset_fingerprint):
    if path and embedded_manifest is not None:
        raise ValueError("review_manifest_must_be_supplied_once")
    if path:
        with open(os.path.abspath(os.path.expanduser(path)), "r", encoding="utf-8") as handle:
            manifest = json.load(handle, object_pairs_hook=_unique_json_object)
    else:
        manifest = embedded_manifest
    if (not isinstance(manifest, dict) or set(manifest) != REVIEW_MANIFEST_FIELDS
            or isinstance(manifest.get("format_version"), bool)
            or manifest.get("format_version") != 2
            or not isinstance(manifest.get("records"), list)):
        raise ValueError("review_manifest_missing_or_malformed")
    source_identity = manifest.get("source_identity")
    if (not _valid_source_identity(expected_source_identity)
            or not _valid_source_identity(source_identity)
            or source_identity != expected_source_identity):
        raise ValueError("review_manifest_source_identity_mismatch")
    manifest_fingerprint = manifest.get("dataset_fingerprint")
    if (not isinstance(manifest_fingerprint, str) or len(manifest_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in manifest_fingerprint)
            or not isinstance(expected_dataset_fingerprint, str)
            or manifest_fingerprint != expected_dataset_fingerprint):
        raise ValueError("review_manifest_dataset_fingerprint_mismatch")

    source_tuples = {}
    for row in raw_assignments:
        if isinstance(row, dict):
            try:
                assignment_id = _parse_positive_int(row.get("assignment_id"), "invalid_identifiers")
                source_tuple = (
                    _parse_positive_int(row.get("complaint_id"), "invalid_identifiers"),
                    _parse_positive_int(row.get("staff_id"), "invalid_identifiers"),
                    _utc_naive(_parse_aware_datetime(row.get("assigned_at"))),
                )
            except ValueError:
                continue
            if assignment_id in source_tuples:
                raise ValueError("duplicate_dataset_assignment_id")
            source_tuples[assignment_id] = source_tuple
    reviews = {}
    for row in manifest["records"]:
        if not isinstance(row, dict) or set(row) != REVIEW_RECORD_FIELDS:
            raise ValueError("invalid_review_record")
        assignment_id = _parse_positive_int(row.get("assignment_id"), "invalid_review_record")
        if assignment_id in reviews:
            raise ValueError("duplicate_review_assignment_id")
        if assignment_id not in source_tuples:
            raise ValueError("unmatched_review_assignment_id")
        review_tuple = (
            _parse_positive_int(row.get("complaint_id"), "invalid_review_record"),
            _parse_positive_int(row.get("staff_id"), "invalid_review_record"),
            _utc_naive(_parse_aware_datetime(row.get("assigned_at"))),
        )
        if review_tuple != source_tuples[assignment_id]:
            raise ValueError("review_assignment_tuple_mismatch")
        provenance = row.get("provenance")
        reconstruction_status = row.get("reconstruction_status")
        provenance_ref = row.get("provenance_evidence_ref")
        label_kind = row.get("label_kind")
        eligibility_verified = row.get("eligibility_verified")
        eligibility_ref = row.get("eligibility_evidence_ref")
        if (not isinstance(provenance, str) or not provenance.strip()
                or reconstruction_status not in {"not_reconstructed", "known_reconstructed", "unknown"}
                or not isinstance(label_kind, str) or not label_kind.strip()
                or not isinstance(eligibility_verified, bool)):
            raise ValueError("invalid_review_record")
        if provenance == "verified_assignment" and (
                not isinstance(provenance_ref, str) or not provenance_ref.strip()):
            raise ValueError("invalid_review_record")
        if eligibility_verified and (
                not isinstance(eligibility_ref, str) or not eligibility_ref.strip()):
            raise ValueError("invalid_review_record")
        if provenance_ref is not None and (not isinstance(provenance_ref, str) or not provenance_ref.strip()):
            raise ValueError("invalid_review_record")
        if eligibility_ref is not None and (not isinstance(eligibility_ref, str) or not eligibility_ref.strip()):
            raise ValueError("invalid_review_record")
        candidate_ids = row.get("candidate_staff_ids")
        eligibility_review = row.get("candidate_staff_eligibility_review")
        candidate_department_id = row.get("candidate_roster_department_id")
        roster_at = row.get("candidate_roster_at")
        roster_reviewed = row.get("candidate_roster_reviewed")
        roster_ref = row.get("candidate_roster_evidence_ref")
        if candidate_ids is not None:
            if not isinstance(candidate_ids, list) or not candidate_ids:
                raise ValueError("invalid_candidate_roster")
            candidate_ids = [
                _parse_positive_int(value, "invalid_candidate_roster") for value in candidate_ids
            ]
            if len(candidate_ids) != len(set(candidate_ids)):
                raise ValueError("invalid_candidate_roster")
            if not isinstance(eligibility_review, dict):
                raise ValueError("invalid_candidate_roster")
            normalized_eligibility_review = {}
            for candidate_id, verified in eligibility_review.items():
                parsed_candidate_id = _parse_positive_int(candidate_id, "invalid_candidate_roster")
                if parsed_candidate_id in normalized_eligibility_review or verified is not True:
                    raise ValueError("invalid_candidate_roster")
                normalized_eligibility_review[parsed_candidate_id] = True
            if set(normalized_eligibility_review) != set(candidate_ids):
                raise ValueError("invalid_candidate_roster")
            candidate_department_id = _parse_positive_int(
                candidate_department_id, "invalid_candidate_roster",
            )
            roster_at = _parse_aware_datetime(roster_at, "invalid_candidate_roster")
            if roster_reviewed is not True:
                raise ValueError("invalid_candidate_roster")
            if not isinstance(roster_ref, str) or not roster_ref.strip():
                raise ValueError("invalid_candidate_roster")
        elif (candidate_department_id is not None or roster_at is not None
              or roster_reviewed is not False or eligibility_review is not None):
            raise ValueError("invalid_candidate_roster")
        elif roster_ref is not None:
            raise ValueError("invalid_candidate_roster")
        reviews[assignment_id] = {
            "record_provenance": provenance,
            "reconstruction_status": reconstruction_status,
            "provenance_evidence_ref": provenance_ref,
            "eligibility_verified": eligibility_verified,
            "eligibility_evidence_ref": eligibility_ref,
            "label_kind": label_kind,
            "candidate_staff_ids": candidate_ids,
            "candidate_staff_eligibility_review": normalized_eligibility_review if candidate_ids is not None else None,
            "candidate_roster_department_id": candidate_department_id,
            "candidate_roster_at": roster_at,
            "candidate_roster_reviewed": roster_reviewed,
            "candidate_roster_evidence_ref": roster_ref,
        }
    return reviews


def _read_local_dataset(path, review_manifest_path=None, source_identity=None):
    """Read and validate the explicit local data file without DB imports."""
    absolute_path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(absolute_path):
        raise FileNotFoundError("dataset_not_found")
    if os.path.getsize(absolute_path) > MAX_DATASET_BYTES:
        raise ValueError("dataset_too_large")
    extension = os.path.splitext(absolute_path)[1].lower()
    if extension == ".csv":
        with open(absolute_path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, strict=True)
            if (reader.fieldnames is None or len(reader.fieldnames) != len(set(reader.fieldnames))
                    or set(reader.fieldnames) != ASSIGNMENT_FIELDS):
                raise ValueError("invalid_csv_columns")
            raw_assignments = list(reader)
        status_events, current_statuses = [], None
        embedded_review_manifest = None
    elif extension == ".json":
        with open(absolute_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle, object_pairs_hook=_unique_json_object)
        if (not isinstance(payload, dict)
                or set(payload) - {"format_version", "assignments", "status_events", "current_statuses", "history_complete", "review_manifest"}
                or isinstance(payload.get("format_version"), bool)
                or not isinstance(payload.get("format_version"), int)
                or payload.get("format_version") != 1
                or not isinstance(payload.get("assignments"), list)):
            raise ValueError("invalid_json_dataset")
        raw_assignments = payload["assignments"]
        embedded_review_manifest = payload.get("review_manifest")
        raw_events = payload.get("status_events", [])
        if not isinstance(raw_events, list):
            raise ValueError("invalid_status_events")
        status_events = []
        for event in raw_events:
            if not isinstance(event, dict) or set(event) != STATUS_EVENT_FIELDS:
                raise ValueError("invalid_status_event_fields")
            normalized_event = dict(event)
            for field in ("history_id", "complaint_id"):
                normalized_event[field] = _parse_positive_int(
                    normalized_event[field], "invalid_status_event_identifier",
                )
            if not isinstance(normalized_event["new_status"], str) or not normalized_event["new_status"].strip():
                raise ValueError("invalid_status_event")
            normalized_event["timestamp"] = _parse_aware_datetime(
                normalized_event["timestamp"], "invalid_status_event",
            )
            status_events.append(normalized_event)
        current_statuses = payload.get("current_statuses")
        if current_statuses is not None:
            if not isinstance(current_statuses, dict):
                raise ValueError("invalid_current_statuses")
            normalized_statuses = {}
            for complaint_id, status in current_statuses.items():
                try:
                    parsed_id = _parse_positive_int(complaint_id, "invalid_current_statuses")
                except ValueError as error:
                    raise ValueError("invalid_current_statuses") from error
                if (parsed_id in normalized_statuses
                        or not isinstance(status, str) or not status.strip()):
                    raise ValueError("invalid_current_statuses")
                normalized_statuses[parsed_id] = status
            current_statuses = normalized_statuses
    else:
        raise ValueError("dataset_must_be_csv_or_json")

    source_dataset_fingerprint = dataset_fingerprint(raw_assignments)
    reviews = _load_review_manifest(
        review_manifest_path, embedded_review_manifest, raw_assignments,
        source_identity, source_dataset_fingerprint,
    )

    assignments = []
    rejected_counts = Counter()
    roster_mismatch_count = 0
    for row in raw_assignments:
        try:
            normalized = _normalize_assignment(row)
            assignment_id = normalized["assignment_id"]
            normalized.update(reviews.get(assignment_id, {
                "record_provenance": "unknown",
                "reconstruction_status": "unknown",
                "eligibility_verified": False,
                "label_kind": "unknown",
                "candidate_staff_ids": None,
                "candidate_roster_department_id": None,
                "candidate_roster_at": None,
                "candidate_roster_reviewed": False,
                "candidate_staff_eligibility_review": None,
                "_review_missing": True,
            }))
            roster_mismatch = (
                normalized.get("candidate_staff_ids") is not None
                and _utc_naive(normalized["candidate_roster_at"])
                != _utc_naive(normalized["assigned_at"])
            )
            roster_department_mismatch = (
                normalized.get("candidate_staff_ids") is not None
                and normalized.get("candidate_roster_department_id")
                != normalized.get("complaint_department_id")
            )
            if roster_mismatch or roster_department_mismatch:
                normalized["candidate_staff_ids"] = None
                normalized["candidate_roster_department_id"] = None
                normalized["candidate_roster_at"] = None
                normalized["candidate_roster_reviewed"] = False
                normalized["candidate_staff_eligibility_review"] = None
                normalized["candidate_roster_evidence_ref"] = None
                normalized["_candidate_roster_timestamp_mismatch"] = True
                if roster_mismatch:
                    roster_mismatch_count += 1
                if roster_department_mismatch:
                    rejected_counts["candidate_roster_department_mismatch"] += 1
            assignments.append(normalized)
        except (TypeError, ValueError) as error:
            code = str(error)
            if code not in {
                "invalid_assignment_fields", "invalid_identifiers", "invalid_assignment_timestamp",
                "missing_complaint_text",
                "complaint_text_too_long", "invalid_category", "department_mismatch",
            }:
                code = "invalid_assignment_row"
            rejected_counts[code] += 1
    parseable_complaint_ids = set()
    for row in raw_assignments:
        if isinstance(row, dict):
            try:
                parseable_complaint_ids.add(
                    _parse_positive_int(row.get("complaint_id"), "invalid_identifiers")
                )
            except ValueError:
                pass
    assignments, semantic_rejected = validate_assignment_records(assignments)
    rejected_counts.update(semantic_rejected)
    if roster_mismatch_count:
        rejected_counts["candidate_roster_timestamp_mismatch"] = roster_mismatch_count
    history_complete = payload.get("history_complete") is True if extension == ".json" else False
    return (assignments, status_events, current_statuses, history_complete,
            len(raw_assignments), len(parseable_complaint_ids), dict(rejected_counts),
            source_dataset_fingerprint)


def _utc_naive(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _evaluation_preflight(train_rows, test_rows):
    """Mirror evaluation's evidence, roster, label, and coverage gates without fitting."""
    by_department = {}
    for row in train_rows:
        department_id = row["complaint_department_id"]
        summary = by_department.setdefault(department_id, {"rows": 0, "complaints": set(), "staff": Counter()})
        summary["rows"] += 1
        summary["complaints"].add(row["complaint_id"])
        summary["staff"][row["staff_id"]] += 1
    model_labels = {}
    for department_id, summary in by_department.items():
        if (summary["rows"] >= MIN_TRAINING_ASSIGNMENTS
                and len(summary["complaints"]) >= MIN_TRAINING_COMPLAINTS
                and len(summary["staff"]) >= 2
                and min(summary["staff"].values(), default=0) >= MIN_ASSIGNMENTS_PER_STAFF):
            model_labels[department_id] = set(summary["staff"])

    compatible = []
    skipped = Counter()
    for row in test_rows:
        roster = {
            "staff_ids": row.get("candidate_staff_ids"),
            "department_id": row.get("candidate_roster_department_id"),
            "as_of": row.get("candidate_roster_at"),
            "reviewed": row.get("candidate_roster_reviewed"),
            "eligibility_review": row.get("candidate_staff_eligibility_review"),
            "evidence_ref": row.get("candidate_roster_evidence_ref"),
        }
        candidate_ids = reviewed_candidate_roster(row, roster)
        if candidate_ids is None:
            skipped["candidate_roster_missing_or_invalid"] += 1
        elif row["staff_id"] not in candidate_ids:
            skipped["held_out_label_not_in_reviewed_candidate_roster"] += 1
        elif row["staff_id"] not in model_labels.get(row["complaint_department_id"], set()):
            skipped["held_out_label_not_in_trained_model"] += 1
        else:
            compatible.append(row)
    count = len(compatible)
    coverage = count / len(test_rows) if test_rows else 0.0
    complaint_count = len({row["complaint_id"] for row in compatible})
    sufficient = (complaint_count >= MIN_EVALUATION_COMPLAINTS
                  and coverage >= MIN_EVALUATION_COVERAGE)
    return {
        "eligible_count": len(test_rows),
        "candidate_compatible_count": count,
        "skipped_count": len(test_rows) - count,
        "coverage": coverage,
        "minimum_coverage": MIN_EVALUATION_COVERAGE,
        "distinct_compatible_complaints": complaint_count,
        "minimum_distinct_complaints": MIN_EVALUATION_COMPLAINTS,
        "skipped_counts": dict(skipped),
        "sufficient": sufficient,
    }


def _validation_failure(args, status, code=None):
    report = {
        "mode": "validation_only",
        "status": status,
        "format_valid": False,
        "schema_valid": False,
        "validation_passed": False,
        "total_assignment_rows": None,
        "distinct_complaint_count": 0,
        "assignment_counts_by_department": {},
        "departments": {},
        "data_sufficient_for_training_attempt": False,
        "workload_evaluation_eligible": False,
        "workload_evaluation_reason": "dataset_unavailable_or_invalid",
        "validation_success_does_not_guarantee_model_accuracy": True,
    }
    if code:
        report["validation_error"] = code
    _report(report)
    return False


def run_validation(args):
    """Validate and summarize a local dataset without model fit/artifact calls."""
    if not args.dataset:
        return _validation_failure(args, "dataset_missing", "dataset_argument_required")
    if not os.path.isfile(os.path.abspath(os.path.expanduser(args.dataset))):
        return _validation_failure(args, "dataset_missing", "dataset_not_found")
    if args.top_k < 1 or not 0 < args.test_fraction < 1:
        return _validation_failure(args, "dataset_invalid", "invalid_evaluation_options")
    try:
        (records, status_events, current_statuses, history_complete, total_rows,
         distinct_complaints, rejected, source_dataset_fingerprint) = _read_local_dataset(
            args.dataset, args.review_manifest, args.source_identity,
        )
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError, TypeError, ValueError) as error:
        code = str(error)
        allowed_codes = {
            "dataset_too_large", "invalid_csv_columns", "invalid_json_dataset",
            "invalid_status_events", "invalid_status_event_fields", "invalid_status_event_identifier",
            "invalid_status_event", "invalid_current_statuses", "dataset_must_be_csv_or_json",
            "review_manifest_must_be_supplied_once", "review_manifest_missing_or_malformed",
            "invalid_review_record", "duplicate_review_assignment_id",
            "unmatched_review_assignment_id", "invalid_candidate_roster",
            "duplicate_dataset_assignment_id", "review_assignment_tuple_mismatch",
            "duplicate_json_key", "review_manifest_source_identity_mismatch",
            "review_manifest_dataset_fingerprint_mismatch",
        }
        return _validation_failure(args, "dataset_invalid", code if code in allowed_codes else "unreadable_or_malformed_dataset")

    schema_errors = {
        "invalid_assignment_fields", "invalid_csv_columns", "invalid_json_dataset",
        "review_manifest_missing_or_malformed", "invalid_review_record",
    }
    hard_rejections = {key: value for key, value in rejected.items()
                       if key not in TRAINING_EXCLUSION_CODES}
    schema_valid = not any(key in schema_errors for key in rejected)
    validation_passed = schema_valid and not hard_rejections
    assignment_counts = {}
    staff_counts = {}
    for record in records:
        department = record["complaint_department"]
        assignment_counts[department] = assignment_counts.get(department, 0) + 1
        by_staff = staff_counts.setdefault(department, {})
        by_staff[record["staff_id"]] = by_staff.get(record["staff_id"], 0) + 1

    try:
        train_rows, test_rows = chronological_group_split(records, args.test_fraction)
        split_available = True
    except InsufficientTrainingData:
        train_rows, test_rows = [], []
        split_available = False
    train_counts = {}
    for record in train_rows:
        department = record["complaint_department"]
        summary = train_counts.setdefault(department, {"assignments": 0, "complaints": set(), "staff": {}})
        summary["assignments"] += 1
        summary["complaints"].add(record["complaint_id"])
        summary["staff"][record["staff_id"]] = summary["staff"].get(record["staff_id"], 0) + 1

    department_names = set(assignment_counts) | set(train_counts)
    departments = {}
    for name in sorted(department_names):
        all_staff = staff_counts.get(name, {})
        split_summary = train_counts.get(name, {"assignments": 0, "complaints": set(), "staff": {}})
        train_staff_counts = list(split_summary["staff"].values())
        meets = (split_available
                 and split_summary["assignments"] >= MIN_TRAINING_ASSIGNMENTS
                 and len(split_summary["complaints"]) >= MIN_TRAINING_COMPLAINTS
                 and len(train_staff_counts) >= 2
                 and min(train_staff_counts, default=0) >= MIN_ASSIGNMENTS_PER_STAFF)
        departments[name] = {
            "assignment_count": assignment_counts.get(name, 0),
            "distinct_staff_labels": len(all_staff),
            "assignment_counts_per_staff_label": sorted(all_staff.values()),
            "training_split": {
                "available": split_available,
                "assignment_count": split_summary["assignments"],
                "distinct_complaint_count": len(split_summary["complaints"]),
                "distinct_staff_labels": len(train_staff_counts),
                "assignment_counts_per_staff_label": sorted(train_staff_counts),
                "minimum_requirements": {
                    "assignments": MIN_TRAINING_ASSIGNMENTS,
                    "distinct_complaints": MIN_TRAINING_COMPLAINTS,
                    "distinct_staff_labels": 2,
                    "assignments_per_staff_label": MIN_ASSIGNMENTS_PER_STAFF,
                },
                "meets_training_requirements": meets,
            },
        }

    workload_eligible = False
    workload_reason = "invalid_dataset" if not validation_passed else "status_history_not_available_or_attested"
    if validation_passed and records and history_complete and current_statuses is not None:
        complaint_ids = {row["complaint_id"] for row in records}
        if set(current_statuses) == complaint_ids:
            workloads, workload_reason = reconstruct_historical_workloads(
                records, status_events, current_statuses,
            )
            workload_eligible = workloads is not None
            if workload_eligible:
                workload_reason = "complete_attested_history_and_unambiguous_timestamps"
        else:
            workload_reason = "current_status_ids_do_not_match_assignment_complaints"
    elif validation_passed and os.path.splitext(args.dataset)[1].lower() == ".csv":
        workload_reason = "csv_does_not_include_status_history"

    evaluation_preflight = _evaluation_preflight(train_rows, test_rows)
    data_sufficient = validation_passed and evaluation_preflight["sufficient"] and any(
        department["training_split"]["meets_training_requirements"]
        for department in departments.values()
    )
    report = {
        "mode": "validation_only",
        "status": ("invalid_rows" if not validation_passed else
                   "valid" if data_sufficient else "insufficient_data"),
        "format_valid": True,
        "schema_valid": schema_valid,
        "validation_passed": validation_passed,
        "total_assignment_rows": total_rows,
        "valid_assignment_rows": len(records),
        "invalid_assignment_rows": sum(hard_rejections.values()),
        "excluded_assignment_rows": sum(rejected.get(code, 0) for code in TRAINING_EXCLUSION_CODES),
        "rejected_row_counts": rejected,
        "distinct_complaint_count": distinct_complaints,
        "distinct_complaint_count_scope": "positive, integer-convertible complaint IDs across parsed assignment rows",
        "assignment_counts_by_department": assignment_counts,
        "departments": departments,
        "data_sufficient_for_training_attempt": data_sufficient,
        "source_dataset_fingerprint": source_dataset_fingerprint,
        "held_out_evaluation_preflight": {
            key: value for key, value in evaluation_preflight.items()
            if key != "sufficient"
        },
        "evaluation_readiness_reason": (
            "coverage_and_complaint_floor_met" if evaluation_preflight["sufficient"]
            else "held_out_evidence_or_coverage_insufficient"
        ),
        "staff_label_counts_are_anonymized": True,
        "workload_evaluation_eligible": workload_eligible,
        "workload_evaluation_reason": workload_reason,
        "validation_success_does_not_guarantee_model_accuracy": True,
    }
    _report(report)
    return validation_passed


def _artifact_path(path):
    if not path or not os.path.isabs(path):
        raise ValueError("Use an absolute local artifact path outside the project directory.")
    project_root = os.path.abspath(os.path.dirname(__file__))
    absolute_path = os.path.abspath(os.path.expanduser(path))
    try:
        inside_project = os.path.commonpath([project_root, absolute_path]) == project_root
    except ValueError:
        inside_project = False
    if inside_project:
        raise ValueError("Store the model artifact outside the project directory and version control.")
    if not os.path.isdir(os.path.dirname(absolute_path)):
        raise ValueError("Create the protected artifact directory before training.")
    if os.path.lexists(absolute_path):
        raise FileExistsError("Refusing to overwrite an existing model artifact.")
    return absolute_path


def run_training(args):
    if not args.dataset:
        _report({"status": "model_not_produced", "artifact_saved": False,
                 "message": "Supply a local dataset with --dataset; workload fallback remains active."})
        return False
    if not os.path.isfile(os.path.abspath(os.path.expanduser(args.dataset))):
        _report({"status": "model_not_produced", "artifact_saved": False,
                 "message": "The supplied local dataset is unavailable; workload fallback remains active."})
        return False
    if args.top_k < 1 or not 0 < args.test_fraction < 1:
        raise ValueError("test-fraction must be between zero and one and top-k must be positive.")
    artifact_path = _artifact_path(args.artifact_path)
    try:
        (records, status_events, current_statuses, history_complete, _, _,
         parser_rejected, source_dataset_fingerprint) = _read_local_dataset(
            args.dataset, args.review_manifest, args.source_identity,
        )
    except (OSError, UnicodeError, csv.Error, json.JSONDecodeError, ValueError) as error:
        code = str(error) if str(error) in {
            "dataset_not_found", "dataset_too_large", "invalid_csv_columns",
            "invalid_json_dataset", "invalid_status_events", "invalid_status_event_fields",
            "invalid_status_event_identifier", "invalid_status_event", "invalid_current_statuses",
            "invalid_assignment_fields", "invalid_identifiers", "invalid_assignment_timestamp",
            "missing_complaint_text", "complaint_text_too_long", "invalid_category",
            "dataset_must_be_csv_or_json", "review_manifest_must_be_supplied_once",
            "review_manifest_missing_or_malformed", "invalid_review_record",
            "duplicate_review_assignment_id", "unmatched_review_assignment_id",
            "invalid_candidate_roster", "duplicate_dataset_assignment_id",
            "review_assignment_tuple_mismatch", "duplicate_json_key",
            "review_manifest_source_identity_mismatch",
            "review_manifest_dataset_fingerprint_mismatch",
        } else "invalid_dataset"
        _report({"status": "model_not_produced", "artifact_saved": False,
                 "validation_error": code,
                 "message": "The local dataset is invalid; workload fallback remains active."})
        return False

    valid, rejected = validate_assignment_records(records)
    for code, count in parser_rejected.items():
        rejected[code] = rejected.get(code, 0) + count
    hard_rejections = {key: value for key, value in rejected.items()
                       if key not in TRAINING_EXCLUSION_CODES}
    if hard_rejections or not valid:
        _report({"status": "insufficient_data", "artifact_saved": False,
                 "valid_assignment_count": len(valid),
                 "excluded_record_counts": {**parser_rejected, **rejected},
                 "reason": "insufficient_verified_training_rows",
                 "message": "Verified assignment-choice rows are missing or insufficient; workload fallback remains active."})
        return False
    try:
        train_rows, test_rows = chronological_group_split(valid, args.test_fraction)
        evaluation_model = train_staff_models(train_rows, require_evaluation=False)
    except (InsufficientTrainingData, ScikitLearnUnavailable):
        _report({"status": "insufficient_data", "artifact_saved": False,
                 "valid_assignment_count": len(valid),
                 "complaint_group_count": len({row["complaint_id"] for row in valid}),
                 "message": "The dataset is too small or training dependencies are unavailable; workload fallback remains active."})
        return False

    workloads, workload_error = (None, "historical_workload_metadata_not_supplied")
    assignment_complaints = {row["complaint_id"] for row in valid}
    if (history_complete and current_statuses is not None
            and set(current_statuses) == assignment_complaints):
        workloads, workload_error = reconstruct_historical_workloads(
            valid, status_events, current_statuses,
        )
    elif current_statuses is not None:
        workload_error = "history_not_attested_complete_or_current_statuses_incomplete"
    candidate_rosters = {
        row["assignment_id"]: {
            "staff_ids": row.get("candidate_staff_ids"),
            "department_id": row.get("candidate_roster_department_id"),
            "as_of": row.get("candidate_roster_at"),
            "reviewed": row.get("candidate_roster_reviewed"),
            "eligibility_review": row.get("candidate_staff_eligibility_review"),
            "evidence_ref": row.get("candidate_roster_evidence_ref"),
        }
        for row in test_rows
    }
    report = {
        "status": "evaluated",
        "valid_assignment_count": len(valid),
        "complaint_group_count": len({row["complaint_id"] for row in valid}),
        "training_assignment_count": len(train_rows),
        "evaluation_assignment_count": len(test_rows),
        "training_departments": {
            department_id: {
                "status": details["status"],
                "assignment_count": details["assignment_count"],
                "complaint_count": details["complaint_count"],
                "staff_label_count": len(details["staff_label_counts"]),
            }
            for department_id, details in evaluation_model["metadata"]["departments"].items()
        },
        "workload_history_reconstruction": "available" if workloads is not None else "unavailable",
        "workload_history_limitation": workload_error,
        "training_source": "operator-supplied local dataset",
        "label_limitation": "historical assignment choice, not resolution quality",
        "source_dataset_fingerprint": source_dataset_fingerprint,
        "source_identity_matched": True,
    }
    evaluation = evaluate_staff_rankings(
        evaluation_model, test_rows, candidate_rosters, workloads, args.top_k,
    )
    report["evaluation"] = evaluation
    metric_name = "ml_plus_workload" if workloads is not None else "ml_without_historical_workload_adjustment"
    primary_metrics = evaluation.get(metric_name, {})
    excluded_counts = Counter(parser_rejected)
    excluded_counts.update(rejected)
    report["excluded_record_counts"] = dict(excluded_counts)
    report["evaluation_record_counts"] = {
        "eligible": primary_metrics.get("eligible_count", len(test_rows)),
        "evaluated": primary_metrics.get("evaluated_count", 0),
        "skipped": primary_metrics.get("skipped_count", len(test_rows)),
        "excluded_before_split": sum(excluded_counts.values()),
    }
    if primary_metrics.get("status") != "evaluated":
        report.update({
            "status": "insufficient_data",
            "artifact_saved": False,
            "reason": "verified_held_out_evaluation_unavailable",
            "message": "Held-out evidence or evaluation coverage was insufficient; no model artifact was produced.",
        })
        _report(report)
        return False

    training_metadata = {
        "training_data_fingerprint": training_data_fingerprint(valid),
        "source_dataset_fingerprint": source_dataset_fingerprint,
        "source_identity": args.source_identity,
        "evaluation_metrics": {
            "evaluated_count": primary_metrics["evaluated_count"],
            "eligible_count": primary_metrics["eligible_count"],
            "skipped_count": primary_metrics["skipped_count"],
            "coverage": primary_metrics["coverage"],
            "primary": primary_metrics,
            "workload_baseline": evaluation.get("workload_baseline"),
            "skipped_counts": evaluation.get("skipped_counts", {}),
            "top_k": args.top_k,
        },
        "excluded_record_counts": dict(excluded_counts),
        "training_config": {
            "test_fraction": args.test_fraction,
            "top_k": args.top_k,
            "split_policy": "complaint-grouped-strict-chronological-v1",
            "minimum_assignments": MIN_TRAINING_ASSIGNMENTS,
            "minimum_complaints": MIN_TRAINING_COMPLAINTS,
            "minimum_assignments_per_staff_label": MIN_ASSIGNMENTS_PER_STAFF,
            "minimum_evaluation_coverage": MIN_EVALUATION_COVERAGE,
            "minimum_distinct_evaluation_complaints": MIN_EVALUATION_COMPLAINTS,
            "label_kind": "historical_assignment_choice",
        },
    }
    report["training_data_fingerprint"] = training_metadata["training_data_fingerprint"]
    report["excluded_record_counts"] = dict(excluded_counts)
    final_model = train_staff_models(valid, training_metadata=training_metadata)
    saved_path = save_model_artifact(final_model, artifact_path)
    report["artifact_saved"] = True
    report["artifact_path"] = saved_path
    _report(report)
    return True


def main(argv=None):
    args = parse_args(argv)
    if args.validate_only:
        try:
            return 0 if run_validation(args) else 1
        except Exception:
            # Do not surface parser internals that could contain input values.
            _validation_failure(args, "dataset_invalid", "unreadable_or_malformed_dataset")
            return 1
    try:
        return 0 if run_training(args) else 1
    except (InsufficientTrainingData, ScikitLearnUnavailable):
        _report({"status": "model_not_produced", "artifact_saved": False,
                 "message": "Training was insufficient or unavailable; workload fallback remains active."})
        return 1
    except Exception as error:
        # Error details are deliberately generic: malformed inputs may contain text/PII.
        print(f"Training stopped ({type(error).__name__}); no database access was attempted.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
