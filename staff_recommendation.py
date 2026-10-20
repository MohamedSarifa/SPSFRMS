"""Optional, offline-trained staff recommendation support.

No model is trained or loaded at import time. A configured, locally trusted
artifact is optional; every failure returns to the workload-only baseline.
"""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import json
import math
import os
import stat
import tempfile
import warnings

from workflow_routing import CATEGORY_DEPARTMENTS


ARTIFACT_FORMAT_VERSION = 2
FEATURE_SCHEMA = "tfidf:category+complaint_text:v2"
SUPPORTED_RECORD_PROVENANCE = "verified_assignment"
SUPPORTED_LABEL_KIND = "historical_assignment_choice"
MIN_TRAINING_ASSIGNMENTS = 20
MIN_TRAINING_COMPLAINTS = 10
MIN_ASSIGNMENTS_PER_STAFF = 3
MIN_EVALUATION_COMPLAINTS = 5
MIN_EVALUATION_COVERAGE = 0.80
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
WORKLOAD_PENALTY_WEIGHT = 0.20


class RecommendationDataError(ValueError):
    """Raised when assignment records cannot be used as supervised labels."""


class InsufficientTrainingData(RecommendationDataError):
    """Raised when valid historical assignments do not support a model."""


class ScikitLearnUnavailable(RuntimeError):
    """Raised by explicit training when the declared dependency is missing."""


def _as_utc_naive(value):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def validate_assignment_records(records):
    """Validate training labels and return admitted rows plus aggregate counts."""
    candidates = []
    rejected = Counter()
    for record in records:
        if not isinstance(record, dict):
            rejected["invalid_record"] += 1
            continue
        assignment_id = record.get("assignment_id")
        complaint_id = record.get("complaint_id")
        staff_id = record.get("staff_id")
        complaint_department_id = record.get("complaint_department_id")
        staff_department_id = record.get("staff_department_id")
        assigned_value = record.get("assigned_at")
        assigned_at = (_as_utc_naive(assigned_value)
                       if isinstance(assigned_value, datetime)
                       and assigned_value.tzinfo is not None
                       and assigned_value.utcoffset() is not None else None)
        text = record.get("complaint_text")
        category = record.get("category")

        if not all(isinstance(value, int) and not isinstance(value, bool) and value > 0
                   for value in (assignment_id, complaint_id, staff_id,
                                 complaint_department_id)):
            rejected["invalid_identifiers"] += 1
            continue
        candidates.append((record, assignment_id, assigned_at))

    counts = Counter(assignment_id for _, assignment_id, _ in candidates)
    valid = []
    seen_rejected_duplicates = set()
    for record, assignment_id, assigned_at in candidates:
        assigned_value = record.get("assigned_at")
        if counts[assignment_id] > 1:
            if assignment_id not in seen_rejected_duplicates:
                rejected["duplicate_assignment_id"] += counts[assignment_id]
                seen_rejected_duplicates.add(assignment_id)
            continue
        if record.get("_review_missing") is True:
            rejected["review_record_missing"] += 1
            continue
        if record.get("reconstruction_status") != "not_reconstructed":
            rejected["reconstructed_or_unknown_history"] += 1
            continue
        provenance = record.get("record_provenance")
        if (provenance != SUPPORTED_RECORD_PROVENANCE
                or not isinstance(record.get("provenance_evidence_ref"), str)
                or not record["provenance_evidence_ref"].strip()):
            rejected["unknown_or_unverified_provenance"] += 1
            continue
        if (record.get("eligibility_verified") is not True
                or not isinstance(record.get("eligibility_evidence_ref"), str)
                or not record["eligibility_evidence_ref"].strip()):
            rejected["historical_eligibility_unverified"] += 1
            continue
        if record.get("label_kind") != SUPPORTED_LABEL_KIND:
            rejected["unsupported_training_label"] += 1
            continue
        complaint_department_id = record.get("complaint_department_id")
        text = record.get("complaint_text")
        category = record.get("category")
        # Current account role, status, and department are observations only.
        # They must not determine historical label inclusion.
        if assigned_at is None:
            rejected["invalid_assignment_timestamp"] += 1
            continue
        if not isinstance(text, str) or not text.strip():
            rejected["missing_complaint_text"] += 1
            continue
        if not isinstance(category, str) or not category.strip() or category == "Multiple Departments":
            rejected["invalid_category"] += 1
            continue
        expected_department = CATEGORY_DEPARTMENTS.get(category.strip())
        if (expected_department is None
                or not isinstance(record.get("complaint_department"), str)
                or record["complaint_department"].strip().casefold() != expected_department.casefold()):
            rejected["category_department_mismatch"] += 1
            continue
        normalized = dict(record)
        normalized["assigned_at"] = assigned_value.astimezone(timezone.utc)
        normalized["complaint_text"] = text.strip()
        normalized["category"] = category.strip()
        valid.append(normalized)
    return valid, dict(rejected)


def training_data_fingerprint(records):
    """Return an order-independent SHA-256 fingerprint without exposing rows."""
    canonical_rows = []
    fields = (
        "assignment_id", "complaint_id", "staff_id", "assigned_at",
        "complaint_text", "category", "complaint_department_id",
        "complaint_department", "staff_role", "staff_department_id",
        "staff_department", "staff_is_active", "record_provenance",
        "reconstruction_status",
        "provenance_evidence_ref", "eligibility_verified", "eligibility_evidence_ref",
        "label_kind", "candidate_staff_ids", "candidate_roster_at",
        "candidate_roster_department_id", "candidate_roster_reviewed",
        "candidate_staff_eligibility_review",
        "candidate_roster_evidence_ref",
    )
    for record in records:
        row = {}
        for name in fields:
            value = record.get(name)
            if isinstance(value, datetime):
                utc_value = value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
                value = utc_value.isoformat().replace("+00:00", "Z")
            elif name == "candidate_staff_ids" and isinstance(value, list):
                value = sorted(value)
            elif name == "candidate_staff_eligibility_review" and isinstance(value, dict):
                value = {str(key): value[key] for key in sorted(value)}
            row[name] = value
        canonical_rows.append(row)
    canonical_rows.sort(key=lambda row: (row.get("assignment_id") or 0, row.get("complaint_id") or 0))
    encoded = json.dumps(
        {"feature_schema": FEATURE_SCHEMA, "records": canonical_rows},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def chronological_group_split(records, test_fraction=0.20):
    """Split by complaint and strict time boundary; ties/overlap never leak forward."""
    if not 0 < test_fraction < 1:
        raise ValueError("test_fraction must be between zero and one.")
    groups = defaultdict(list)
    for record in records:
        groups[record["complaint_id"]].append(record)
    if len(groups) < 2:
        raise InsufficientTrainingData("At least two complaint groups are required for evaluation.")
    intervals = []
    for complaint_id, rows in groups.items():
        times = [_as_utc_naive(row["assigned_at"]) for row in rows]
        if any(value is None for value in times):
            raise InsufficientTrainingData("Complaint group timestamps are incomplete.")
        intervals.append((complaint_id, rows, min(times), max(times)))
    intervals.sort(key=lambda item: (item[2], item[3], item[0]))
    target_test_count = max(1, math.ceil(len(intervals) * test_fraction))
    valid_splits = []
    for cut in range(1, len(intervals)):
        train_groups = intervals[:cut]
        test_groups = intervals[cut:]
        latest_train = max(group[3] for group in train_groups)
        earliest_test = min(group[2] for group in test_groups)
        if latest_train < earliest_test:
            test_count = len(test_groups)
            valid_splits.append((abs(test_count - target_test_count), -cut, train_groups, test_groups))
    if not valid_splits:
        raise InsufficientTrainingData(
            "No complaint-group split has a strictly later test window without timestamp ties or overlap."
        )
    _, _, train_groups, test_groups = min(valid_splits, key=lambda item: (item[0], item[1]))
    train_groups = [(group[0], group[1]) for group in train_groups]
    test_groups = [(group[0], group[1]) for group in test_groups]
    train = [row for _, rows in train_groups for row in rows]
    test = [row for _, rows in test_groups for row in rows]
    return train, test


def _feature_text(record):
    return f"category {record['category']} complaint {record['complaint_text']}"


def _build_pipeline():
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.linear_model import LogisticRegression
        from sklearn.pipeline import Pipeline
    except ImportError as error:
        raise ScikitLearnUnavailable(
            "Install the pinned scikit-learn dependency before training staff recommendations."
        ) from error
    return Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), max_features=20000, sublinear_tf=True)),
        ("classifier", LogisticRegression(
            max_iter=1000, class_weight="balanced", random_state=0,
        )),
    ])


def _sklearn_version():
    try:
        import sklearn
    except ImportError as error:
        raise ScikitLearnUnavailable(
            "Install the pinned scikit-learn dependency before training staff recommendations."
        ) from error
    return sklearn.__version__


def train_staff_models(records, *, training_metadata=None, require_evaluation=True):
    """Train department-specific TF-IDF + Logistic Regression models.

    The input must be validated historical assignment rows. No sample data is
    generated here, and departments without sufficient data are skipped.
    """
    valid, rejected = validate_assignment_records(records)
    by_department = defaultdict(list)
    for record in valid:
        by_department[record["complaint_department_id"]].append(record)

    eligible_departments = {}
    department_metadata = {}
    for department_id, rows in by_department.items():
        label_counts = Counter(row["staff_id"] for row in rows)
        complaint_count = len({row["complaint_id"] for row in rows})
        if (len(rows) < MIN_TRAINING_ASSIGNMENTS
                or complaint_count < MIN_TRAINING_COMPLAINTS
                or len(label_counts) < 2
                or min(label_counts.values(), default=0) < MIN_ASSIGNMENTS_PER_STAFF):
            department_metadata[str(department_id)] = {
                "status": "insufficient_data",
                "assignment_count": len(rows),
                "complaint_count": complaint_count,
                "staff_label_counts": dict(label_counts),
            }
        else:
            eligible_departments[department_id] = rows
    if not eligible_departments:
        raise InsufficientTrainingData(
            "No department has enough valid historical assignments to train a model."
        )

    version = _sklearn_version()
    models = {}
    for department_id, rows in eligible_departments.items():
        label_counts = Counter(row["staff_id"] for row in rows)
        complaint_count = len({row["complaint_id"] for row in rows})
        model = _build_pipeline()
        model.fit([_feature_text(row) for row in rows], [row["staff_id"] for row in rows])
        models[str(department_id)] = model
        department_metadata[str(department_id)] = {
            "status": "trained",
            "assignment_count": len(rows),
            "complaint_count": complaint_count,
            "staff_label_counts": dict(label_counts),
        }

    if not models:
        raise InsufficientTrainingData(
            "No department has enough valid historical assignments to train a model."
        )
    metadata = training_metadata or {}
    evaluation_metrics = metadata.get("evaluation_metrics")
    if require_evaluation:
        primary = evaluation_metrics.get("primary") if isinstance(evaluation_metrics, dict) else None
        source_identity = metadata.get("source_identity")
        source_fingerprint = metadata.get("source_dataset_fingerprint")
        if (not isinstance(primary, dict) or primary.get("status") != "evaluated"
                or not isinstance(primary.get("evaluated_complaint_count"), int)
                or primary.get("evaluated_complaint_count", 0) < MIN_EVALUATION_COMPLAINTS
                or not isinstance(primary.get("coverage"), (int, float))
                or primary.get("coverage", 0) < MIN_EVALUATION_COVERAGE
                or not isinstance(source_identity, str) or not source_identity.strip()
                or not isinstance(source_fingerprint, str) or len(source_fingerprint) != 64
                or any(ch not in "0123456789abcdef" for ch in source_fingerprint)):
            raise InsufficientTrainingData(
                "Verified source identity and sufficiently covered held-out evaluation are required."
            )
    fingerprint = metadata.get("training_data_fingerprint") or training_data_fingerprint(valid)
    if not isinstance(fingerprint, str) or len(fingerprint) != 64:
        raise RecommendationDataError("A valid training-data fingerprint is required.")
    return {
        "metadata": {
            "format_version": ARTIFACT_FORMAT_VERSION,
            "sklearn_version": version,
            "feature_schema": FEATURE_SCHEMA,
            "label_meaning": "historical staff assignment choice, not resolution success",
            "rejected_record_counts": metadata.get("excluded_record_counts", rejected),
            "training_data_fingerprint": fingerprint,
            "source_dataset_fingerprint": metadata.get("source_dataset_fingerprint"),
            "source_identity": metadata.get("source_identity"),
            "training_config": metadata.get("training_config", {}),
            "evaluation_metrics": evaluation_metrics or {},
            "departments": department_metadata,
        },
        "models": models,
    }


def _validate_loaded_artifact(artifact):
    import sklearn
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline

    if not isinstance(artifact, dict) or not isinstance(artifact.get("metadata"), dict):
        return False
    metadata = artifact["metadata"]
    evaluation = metadata.get("evaluation_metrics")
    primary_metrics = evaluation.get("primary") if isinstance(evaluation, dict) else None
    fingerprint = metadata.get("training_data_fingerprint")
    source_fingerprint = metadata.get("source_dataset_fingerprint")
    source_identity = metadata.get("source_identity")
    primary_coverage = primary_metrics.get("coverage") if isinstance(primary_metrics, dict) else None
    primary_complaints = (primary_metrics.get("evaluated_complaint_count")
                          if isinstance(primary_metrics, dict) else None)
    metrics_are_valid = (
        isinstance(evaluation, dict)
        and isinstance(evaluation.get("evaluated_count"), int)
        and not isinstance(evaluation.get("evaluated_count"), bool)
        and evaluation.get("evaluated_count", 0) >= MIN_EVALUATION_COMPLAINTS
        and isinstance(primary_metrics, dict)
        and evaluation.get("evaluated_count") == primary_metrics.get("evaluated_count")
        and evaluation.get("eligible_count") == primary_metrics.get("eligible_count")
        and evaluation.get("skipped_count") == primary_metrics.get("skipped_count")
        and evaluation.get("coverage") == primary_metrics.get("coverage")
        and primary_metrics.get("status") == "evaluated"
        and isinstance(primary_metrics.get("eligible_count"), int)
        and not isinstance(primary_metrics.get("eligible_count"), bool)
        and primary_metrics.get("eligible_count", 0) > 0
        and isinstance(primary_metrics.get("evaluated_count"), int)
        and not isinstance(primary_metrics.get("evaluated_count"), bool)
        and primary_metrics.get("evaluated_count", 0) >= MIN_EVALUATION_COMPLAINTS
        and isinstance(primary_metrics.get("skipped_count"), int)
        and primary_metrics.get("skipped_count", -1) >= 0
        and primary_metrics.get("eligible_count")
        == primary_metrics.get("evaluated_count") + primary_metrics.get("skipped_count")
        and isinstance(primary_complaints, int)
        and not isinstance(primary_complaints, bool)
        and primary_complaints >= MIN_EVALUATION_COMPLAINTS
        and isinstance(primary_coverage, (int, float))
        and math.isfinite(primary_coverage)
        and MIN_EVALUATION_COVERAGE <= primary_coverage <= 1.0
        and math.isclose(
            primary_coverage,
            primary_metrics.get("evaluated_count", 0) / primary_metrics.get("eligible_count", 1),
            rel_tol=0.0, abs_tol=1e-12,
        )
        and all(isinstance(primary_metrics.get(name), (int, float))
                and math.isfinite(primary_metrics[name])
                and 0.0 <= primary_metrics[name] <= 1.0
                for name in ("top1_accuracy", "topk_accuracy", "macro_f1", "mean_reciprocal_rank"))
    )
    rejected_counts = metadata.get("rejected_record_counts")
    rejected_counts_are_valid = (
        isinstance(rejected_counts, dict)
        and all(isinstance(name, str) and isinstance(count, int)
                and not isinstance(count, bool) and count >= 0
                for name, count in rejected_counts.items())
    )
    if (metadata.get("format_version") != ARTIFACT_FORMAT_VERSION
            or metadata.get("sklearn_version") != sklearn.__version__
            or metadata.get("feature_schema") != FEATURE_SCHEMA
            or metadata.get("label_meaning") != "historical staff assignment choice, not resolution success"
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
            or not isinstance(source_fingerprint, str)
            or len(source_fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in source_fingerprint)
            or not isinstance(source_identity, str)
            or not (1 <= len(source_identity) <= 200)
            or not source_identity[0].isalnum()
            or any(not (character.isalnum() or character in "._:-")
                   for character in source_identity)
            or not isinstance(metadata.get("training_config"), dict)
            or metadata["training_config"].get("split_policy") != "complaint-grouped-strict-chronological-v1"
            or metadata["training_config"].get("minimum_evaluation_coverage") != MIN_EVALUATION_COVERAGE
            or metadata["training_config"].get("minimum_distinct_evaluation_complaints") != MIN_EVALUATION_COMPLAINTS
            or not metrics_are_valid
            or not rejected_counts_are_valid
            or not isinstance(artifact.get("models"), dict)
            or not artifact["models"]
            or not isinstance(metadata.get("departments"), dict)):
        return False
    for department_id, model in artifact["models"].items():
        try:
            int(department_id)
        except (TypeError, ValueError):
            return False
        if (not isinstance(model, Pipeline)
                or not isinstance(model.named_steps.get("tfidf"), TfidfVectorizer)
                or not isinstance(model.named_steps.get("classifier"), LogisticRegression)
                or len(model.named_steps["classifier"].classes_) < 2):
            return False
        department_metadata = metadata["departments"].get(department_id)
        if not isinstance(department_metadata, dict):
            return False
        counts = department_metadata.get("staff_label_counts")
        if (department_metadata.get("status") != "trained"
                or department_metadata.get("assignment_count", 0) < MIN_TRAINING_ASSIGNMENTS
                or department_metadata.get("complaint_count", 0) < MIN_TRAINING_COMPLAINTS
                or not isinstance(counts, dict)
                or len(counts) < 2
                or min(counts.values(), default=0) < MIN_ASSIGNMENTS_PER_STAFF):
            return False
        try:
            if {int(label) for label in counts} != {int(label) for label in model.named_steps["classifier"].classes_}:
                return False
        except (TypeError, ValueError):
            return False
    return True


@lru_cache(maxsize=8)
def _load_artifact_cached(path, modified_ns, size):
    del modified_ns, size  # Included in the cache key so changed files are rechecked.
    import joblib
    artifact = joblib.load(path)
    if not _validate_loaded_artifact(artifact):
        raise ValueError("Model artifact metadata or estimator type is incompatible.")
    return artifact


def load_model_artifact(path):
    """Load only a configured local artifact; failures safely return no model.

    Joblib uses pickle internally. The path must point to an artifact created
    by this project's training script and protected from untrusted writers.
    """
    if not path:
        return None
    try:
        absolute_path = os.path.abspath(os.path.expanduser(path))
        if os.path.islink(absolute_path):
            return None
        details = os.stat(absolute_path)
        if (not stat.S_ISREG(details.st_mode) or details.st_size <= 0
                or details.st_size > MAX_ARTIFACT_BYTES):
            return None
        return _load_artifact_cached(absolute_path, details.st_mtime_ns, details.st_size)
    except Exception:
        warnings.warn(
            "Configured staff model artifact was rejected; using workload fallback.",
            RuntimeWarning,
            stacklevel=2,
        )
        return None


def save_model_artifact(artifact, path):
    """Create a new artifact exclusively; never replace an existing artifact."""
    if not path or not _validate_loaded_artifact(artifact):
        raise ValueError("A valid model artifact and explicit local path are required.")
    import joblib
    absolute_path = os.path.abspath(os.path.expanduser(path))
    directory = os.path.dirname(absolute_path)
    if not os.path.isdir(directory):
        raise ValueError("Create the configured artifact directory before training.")
    if os.path.lexists(absolute_path):
        raise FileExistsError("Refusing to overwrite an existing model artifact.")

    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".spsfrms-staff-model-", suffix=".tmp", dir=directory,
    )
    os.close(descriptor)
    try:
        joblib.dump(artifact, temporary_path, compress=3)
        try:
            os.chmod(temporary_path, 0o600)
        except OSError:
            pass
        # A same-directory hard link publishes atomically and fails if another
        # process created the destination after the initial existence check.
        os.link(temporary_path, absolute_path)
    finally:
        if os.path.exists(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass
    _load_artifact_cached.cache_clear()
    return absolute_path


def _eligible_staff(staff_members, department_id):
    eligible = []
    for staff in staff_members or []:
        if (getattr(staff, "role", None) != "staff"
                or getattr(staff, "department_id", None) != department_id
                or getattr(staff, "is_active", True) is False):
            continue
        eligible.append(staff)
    return sorted(eligible, key=lambda staff: (getattr(staff, "name", ""), staff.id))


def _rank_eligible_staff(complaint_text, category, department_id, candidates, workloads, artifact):
    model = artifact.get("models", {}).get(str(department_id)) if isinstance(artifact, dict) else None
    if model is None:
        return []
    classifier = model.named_steps["classifier"]
    text = f"category {category or ''} complaint {complaint_text or ''}"
    probabilities = model.predict_proba([text])[0]
    probabilities_by_staff = {
        int(staff_id): float(probability)
        for staff_id, probability in zip(classifier.classes_, probabilities)
    }
    eligible_predictions = [
        (staff, probabilities_by_staff[staff.id])
        for staff in candidates if staff.id in probabilities_by_staff
    ]
    max_workload = max((max(0, workloads.get(staff.id, 0)) for staff, _ in eligible_predictions), default=0)

    def rank_key(item):
        staff, confidence = item
        load = max(0, workloads.get(staff.id, 0))
        normalized_load = load / max_workload if max_workload else 0.0
        combined_score = confidence - WORKLOAD_PENALTY_WEIGHT * normalized_load
        return (-combined_score, -confidence, load, staff.name, staff.id)

    return sorted(eligible_predictions, key=rank_key)


def workload_recommendation(staff_members, department_id, workloads):
    """Deterministic baseline, restricted to current eligible department staff."""
    candidates = _eligible_staff(staff_members, department_id)
    if not candidates:
        return None
    selected = min(candidates, key=lambda staff: (workloads.get(staff.id, 0), staff.name, staff.id))
    return {
        "staff_id": selected.id,
        "staff_name": selected.name,
        "method": "workload_fallback",
        "method_label": "Lowest active workload (fallback)",
        "confidence": None,
        "confidence_label": None,
        "workload": workloads.get(selected.id, 0),
        "reason": "ML is unavailable or has no eligible trained staff label.",
    }


def recommend_staff(complaint_text, category, department_id, staff_members, workloads, artifact=None):
    """Return an advisory recommendation; never changes or assigns a complaint."""
    candidates = _eligible_staff(staff_members, department_id)
    if not candidates:
        return None
    fallback = workload_recommendation(candidates, department_id, workloads)
    if not artifact or not isinstance(artifact, dict):
        return fallback
    try:
        eligible_predictions = _rank_eligible_staff(
            complaint_text, category, department_id, candidates, workloads, artifact,
        )
        if not eligible_predictions:
            return fallback
        selected, confidence = eligible_predictions[0]
        return {
            "staff_id": selected.id,
            "staff_name": selected.name,
            "method": "ml_workload",
            "method_label": "ML + workload ranking",
            "confidence": confidence,
            "confidence_label": "Model confidence for matching a historical assignment choice",
            "workload": workloads.get(selected.id, 0),
            "reason": "The model ranks historical assignment matches among eligible staff; active workload adjusts ranking.",
        }
    except Exception:
        return fallback


def reconstruct_historical_workloads(assignment_records, status_events, current_statuses):
    """Reconstruct active workload immediately before each assignment decision.

    Returns (mapping, None) when the history is sufficient, or (None, reason)
    when timestamps or terminal status events make a fair comparison unsafe.
    Tied cross-record timestamps are treated as ambiguous rather than guessed.
    """
    assignments = []
    for record in assignment_records:
        assigned_at = _as_utc_naive(record.get("assigned_at"))
        if assigned_at is None or not isinstance(record.get("assignment_id"), int):
            return None, "assignment_timestamp_gap"
        assignments.append({**record, "assigned_at": assigned_at})
    statuses = []
    for event in status_events:
        timestamp = _as_utc_naive(event.get("timestamp"))
        if timestamp is None or not isinstance(event.get("history_id"), int):
            return None, "status_timestamp_gap"
        statuses.append({**event, "timestamp": timestamp})
    all_assignment_times = [row["assigned_at"] for row in assignments]
    if len(all_assignment_times) != len(set(all_assignment_times)):
        return None, "tied_assignment_timestamps"
    if set(all_assignment_times) & {event["timestamp"] for event in statuses}:
        return None, "ambiguous_assignment_status_event_order"
    for assignment in assignments:
        if any(event["complaint_id"] == assignment["complaint_id"]
               and event["timestamp"] == assignment["assigned_at"] for event in statuses):
            return None, "ambiguous_same_complaint_event_order"

    assignments_by_complaint = defaultdict(list)
    statuses_by_complaint = defaultdict(list)
    for row in assignments:
        assignments_by_complaint[row["complaint_id"]].append(row)
    for event in statuses:
        statuses_by_complaint[event["complaint_id"]].append(event)
    for complaint_id, status in current_statuses.items():
        events = statuses_by_complaint.get(complaint_id, [])
        if status in {"Resolved", "Ignored"} and not any(event["new_status"] == status for event in events):
            return None, "terminal_status_history_gap"
        if events:
            latest = max(events, key=lambda event: (event["timestamp"], event["history_id"]))
            if latest["new_status"] != status:
                return None, "current_status_history_mismatch"

    workload_before = {}
    for target in assignments:
        timestamp = target["assigned_at"]
        counts = Counter()
        for complaint_id, events in assignments_by_complaint.items():
            if complaint_id == target["complaint_id"]:
                continue
            prior_assignments = [event for event in events if event["assigned_at"] < timestamp]
            if not prior_assignments:
                continue
            latest_assignment = max(prior_assignments, key=lambda event: (event["assigned_at"], event["assignment_id"]))
            prior_statuses = [event for event in statuses_by_complaint.get(complaint_id, [])
                              if event["timestamp"] < timestamp]
            latest_status = max(prior_statuses, key=lambda event: (event["timestamp"], event["history_id"])) if prior_statuses else None
            if latest_status and latest_status["new_status"] in {"Resolved", "Ignored"}:
                continue
            counts[latest_assignment["staff_id"]] += 1
        workload_before[target["assignment_id"]] = dict(counts)
    return workload_before, None


def _ranking_metrics(actual, rankings, top_k):
    if not actual:
        return {"evaluated_count": 0, "top1_accuracy": None, "topk_accuracy": None,
                "macro_f1": None, "mean_reciprocal_rank": None}
    top1 = [ranking[0] if ranking else None for ranking in rankings]
    labels = sorted(set(actual) | {prediction for prediction in top1 if prediction is not None})
    f1_values = []
    for label in labels:
        true_positive = sum(expected == label and predicted == label for expected, predicted in zip(actual, top1))
        false_positive = sum(expected != label and predicted == label for expected, predicted in zip(actual, top1))
        false_negative = sum(expected == label and predicted != label for expected, predicted in zip(actual, top1))
        denominator = 2 * true_positive + false_positive + false_negative
        f1_values.append((2 * true_positive / denominator) if denominator else 0.0)
    reciprocal_ranks = []
    for expected, ranking in zip(actual, rankings):
        reciprocal_ranks.append(1 / (ranking.index(expected) + 1) if expected in ranking else 0.0)
    return {
        "evaluated_count": len(actual),
        "top1_accuracy": sum(expected == predicted for expected, predicted in zip(actual, top1)) / len(actual),
        "topk_accuracy": sum(expected in ranking[:top_k] for expected, ranking in zip(actual, rankings)) / len(actual),
        "macro_f1": sum(f1_values) / len(f1_values) if f1_values else 0.0,
        "mean_reciprocal_rank": sum(reciprocal_ranks) / len(reciprocal_ranks),
    }


def reviewed_candidate_roster(record, roster):
    """Return candidate IDs only for a reviewed, same-department, time-matched roster."""
    if not isinstance(roster, dict):
        return None
    candidate_ids = roster.get("staff_ids")
    assigned_at = _as_utc_naive(record.get("assigned_at"))
    roster_at = _as_utc_naive(roster.get("as_of"))
    evidence_ref = roster.get("evidence_ref")
    eligibility_review = roster.get("eligibility_review")
    if (not isinstance(candidate_ids, list) or not candidate_ids
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0
                   for value in candidate_ids)
            or len(candidate_ids) != len(set(candidate_ids))
            or not isinstance(eligibility_review, dict)
            or set(eligibility_review) != set(candidate_ids)
            or any(value is not True for value in eligibility_review.values())
            or roster.get("reviewed") is not True
            or roster.get("department_id") != record.get("complaint_department_id")
            or assigned_at is None or roster_at is None or roster_at != assigned_at
            or not isinstance(evidence_ref, str) or not evidence_ref.strip()):
        return None
    return candidate_ids


def evaluate_staff_rankings(artifact, records, candidate_rosters_by_assignment,
                            workloads_by_assignment=None, top_k=3):
    """Evaluate only against reviewed candidate rosters valid at each decision time.

    A union of staff observed in the dataset is not evidence that they were
    available for a held-out assignment.
    """
    workload_baseline_available = workloads_by_assignment is not None
    workloads_by_assignment = workloads_by_assignment or {}
    candidate_rosters = (candidate_rosters_by_assignment
                         if isinstance(candidate_rosters_by_assignment, dict) else {})
    model_actual, model_rankings = [], []
    baseline_actual, baseline_rankings = [], []
    evaluated_complaints = set()
    skipped = Counter()
    for record in records:
        department_id = record["complaint_department_id"]
        assignment_id = record["assignment_id"]
        roster = candidate_rosters.get(assignment_id)
        if not isinstance(roster, dict):
            skipped["candidate_roster_missing_or_invalid"] += 1
            continue
        candidate_ids = reviewed_candidate_roster(record, roster)
        if candidate_ids is None:
            skipped["candidate_roster_missing_or_invalid"] += 1
            continue
        actual_label = record["staff_id"]
        if actual_label not in candidate_ids:
            skipped["held_out_label_not_in_reviewed_candidate_roster"] += 1
            continue
        workload = workloads_by_assignment.get(record["assignment_id"], {})
        model = artifact.get("models", {}).get(str(department_id)) if isinstance(artifact, dict) else None
        if model is None:
            skipped["department_model_unavailable"] += 1
            continue
        try:
            classifier = model.named_steps["classifier"]
            probabilities = model.predict_proba([_feature_text(record)])[0]
            probability_by_staff = {
                int(staff_id): float(probability)
                for staff_id, probability in zip(classifier.classes_, probabilities)
            }
            ranked_candidates = [
                staff_id for staff_id in candidate_ids if staff_id in probability_by_staff
            ]
            max_workload = max((max(0, workload.get(staff_id, 0))
                                for staff_id in ranked_candidates), default=0)
            ranked_candidates.sort(key=lambda staff_id: (
                -(probability_by_staff[staff_id] - WORKLOAD_PENALTY_WEIGHT *
                  (max(0, workload.get(staff_id, 0)) / max_workload if max_workload else 0.0)),
                -probability_by_staff[staff_id],
                max(0, workload.get(staff_id, 0)),
                staff_id,
            ))
            ml_ranking = ranked_candidates
        except Exception:
            ml_ranking = []
        if actual_label not in ml_ranking:
            skipped["held_out_label_not_in_trained_model"] += 1
            continue
        model_actual.append(actual_label)
        model_rankings.append(ml_ranking)
        evaluated_complaints.add(record["complaint_id"])
        if workload_baseline_available:
            baseline_actual.append(actual_label)
            baseline_rankings.append(sorted(
                candidate_ids,
                key=lambda staff_id: (workload.get(staff_id, 0), staff_id),
            ))
    primary = _ranking_metrics(model_actual, model_rankings, top_k)
    eligible_count = len(records)
    evaluated_count = len(model_actual)
    coverage = evaluated_count / eligible_count if eligible_count else 0.0
    primary["evaluated_complaint_count"] = len(evaluated_complaints)
    primary["minimum_evaluation_complaints"] = MIN_EVALUATION_COMPLAINTS
    primary["eligible_count"] = eligible_count
    primary["evaluated_count"] = evaluated_count
    primary["skipped_count"] = eligible_count - evaluated_count
    primary["coverage"] = coverage
    primary["minimum_coverage"] = MIN_EVALUATION_COVERAGE
    if (len(evaluated_complaints) < MIN_EVALUATION_COMPLAINTS
            or coverage < MIN_EVALUATION_COVERAGE):
        primary.update({
            "status": "insufficient_data",
            "top1_accuracy": None,
            "topk_accuracy": None,
            "macro_f1": None,
            "mean_reciprocal_rank": None,
        })
    else:
        primary["status"] = "evaluated"
    baseline = (_ranking_metrics(baseline_actual, baseline_rankings, top_k)
                if workload_baseline_available else None)
    if baseline is not None:
        baseline["evaluated_complaint_count"] = len(evaluated_complaints)
        baseline["minimum_evaluation_complaints"] = MIN_EVALUATION_COMPLAINTS
        baseline["eligible_count"] = eligible_count
        baseline["skipped_count"] = eligible_count - len(baseline_actual)
        baseline["coverage"] = len(baseline_actual) / eligible_count if eligible_count else 0.0
        baseline["minimum_coverage"] = MIN_EVALUATION_COVERAGE
        if (len(evaluated_complaints) < MIN_EVALUATION_COMPLAINTS
                or baseline["coverage"] < MIN_EVALUATION_COVERAGE):
            baseline.update({
                "status": "insufficient_data",
                "top1_accuracy": None,
                "topk_accuracy": None,
                "macro_f1": None,
                "mean_reciprocal_rank": None,
            })
        else:
            baseline["status"] = "evaluated"
    return {
        "ml_plus_workload" if workload_baseline_available else "ml_without_historical_workload_adjustment": primary,
        "workload_baseline": baseline,
        "skipped_counts": dict(skipped),
        "top_k": top_k,
    }
