"""Database-free synthetic coverage for the offline recommendation pipeline."""

from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import warnings
from unittest.mock import patch

import staff_recommendation as recommendation
import train_staff_recommendation as trainer


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
SOURCE_IDENTITY = "synthetic-source-v1"


def make_record(assignment_id=1, complaint_id=101, staff_id=1, **overrides):
    row = {
        "assignment_id": assignment_id,
        "complaint_id": complaint_id,
        "staff_id": staff_id,
        "assigned_at": NOW + timedelta(days=complaint_id),
        "complaint_text": f"synthetic fixture {complaint_id}",
        "category": "Drainage",
        "complaint_department_id": 1,
        "complaint_department": "Drainage",
        "staff_role": "staff",
        "staff_department_id": 1,
        "staff_department": "Drainage",
        "staff_is_active": True,
        "record_provenance": "verified_assignment",
        "reconstruction_status": "not_reconstructed",
        "provenance_evidence_ref": "synthetic-provenance-evidence",
        "eligibility_verified": True,
        "eligibility_evidence_ref": "synthetic-eligibility-evidence",
        "label_kind": "historical_assignment_choice",
        "candidate_staff_ids": [1, 2],
        "candidate_roster_department_id": 1,
        "candidate_roster_at": NOW + timedelta(days=complaint_id),
        "candidate_roster_reviewed": True,
        "candidate_staff_eligibility_review": {1: True, 2: True},
        "candidate_roster_evidence_ref": "synthetic-candidate-roster-evidence",
    }
    row.update(overrides)
    return row


def manifest_record(row, **overrides):
    item = {
        "assignment_id": row["assignment_id"],
        "complaint_id": row["complaint_id"],
        "staff_id": row["staff_id"],
        "assigned_at": row["assigned_at"].isoformat(),
        "provenance": row["record_provenance"],
        "reconstruction_status": row["reconstruction_status"],
        "provenance_evidence_ref": row["provenance_evidence_ref"],
        "eligibility_verified": row["eligibility_verified"],
        "eligibility_evidence_ref": row["eligibility_evidence_ref"],
        "label_kind": row["label_kind"],
        "candidate_staff_ids": row["candidate_staff_ids"],
        "candidate_roster_department_id": row["candidate_roster_department_id"],
        "candidate_roster_at": row["candidate_roster_at"].isoformat()
        if row["candidate_roster_at"] else None,
        "candidate_roster_reviewed": row["candidate_roster_reviewed"],
        "candidate_staff_eligibility_review": {
            str(key): value for key, value in row["candidate_staff_eligibility_review"].items()
        } if row["candidate_staff_eligibility_review"] is not None else None,
        "candidate_roster_evidence_ref": row["candidate_roster_evidence_ref"],
    }
    item.update(overrides)
    return item


def assignment_payload(row):
    return {name: row[name] for name in trainer.ASSIGNMENT_FIELDS}


def raw_manifest_row(row):
    raw = assignment_payload(row)
    raw["assigned_at"] = row["assigned_at"].isoformat()
    return raw


def manifest_envelope(raw_rows, reviewed_rows, *, source_identity=SOURCE_IDENTITY):
    return {
        "format_version": 2,
        "dataset_fingerprint": trainer.dataset_fingerprint(raw_rows),
        "source_identity": source_identity,
        "records": [manifest_record(row) for row in reviewed_rows],
    }


class _FakeClassifier:
    classes_ = [1, 2, 3]


class _FakeModel:
    named_steps = {"classifier": _FakeClassifier()}

    def predict_proba(self, _rows):
        # Staff 3 is highly scored but is deliberately absent from the reviewed roster.
        return [[0.2, 0.7, 0.99]]


def make_valid_artifact():
    rows = [make_record(i, 2000 + i, 1 + (i % 2)) for i in range(1, 31)]
    train_rows, test_rows = recommendation.chronological_group_split(rows, 0.2)
    evaluation_model = recommendation.train_staff_models(train_rows, require_evaluation=False)
    rosters = {row["assignment_id"]: {
        "staff_ids": [1, 2], "department_id": 1, "as_of": row["assigned_at"],
        "reviewed": True, "evidence_ref": "synthetic-roster-evidence",
        "eligibility_review": {1: True, 2: True},
    } for row in test_rows}
    evaluation = recommendation.evaluate_staff_rankings(
        evaluation_model, test_rows, rosters, None, top_k=2,
    )
    primary = evaluation["ml_without_historical_workload_adjustment"]
    return recommendation.train_staff_models(rows, training_metadata={
        "training_data_fingerprint": recommendation.training_data_fingerprint(rows),
        "source_dataset_fingerprint": trainer.dataset_fingerprint(
            [raw_manifest_row(row) for row in rows]
        ),
        "source_identity": SOURCE_IDENTITY,
        "evaluation_metrics": {
            "evaluated_count": primary["evaluated_count"],
            "eligible_count": primary["eligible_count"],
            "skipped_count": primary["skipped_count"],
            "coverage": primary["coverage"],
            "primary": primary,
            "skipped_counts": evaluation["skipped_counts"],
            "top_k": 2,
        },
        "training_config": {
            "split_policy": "complaint-grouped-strict-chronological-v1",
            "minimum_evaluation_coverage": recommendation.MIN_EVALUATION_COVERAGE,
            "minimum_distinct_evaluation_complaints": recommendation.MIN_EVALUATION_COMPLAINTS,
        },
    })


class OfflineRecommendationTests(unittest.TestCase):
    def test_data_fingerprint_is_stable_for_order_and_sensitive_to_data(self):
        rows = [make_record(1, 101, 1), make_record(2, 102, 2)]
        self.assertEqual(
            recommendation.training_data_fingerprint(rows),
            recommendation.training_data_fingerprint(list(reversed(rows))),
        )
        changed = [dict(rows[0], complaint_text="different synthetic fixture"), rows[1]]
        self.assertNotEqual(
            recommendation.training_data_fingerprint(rows),
            recommendation.training_data_fingerprint(changed),
        )
        raw_rows = [raw_manifest_row(row) for row in rows]
        self.assertEqual(
            trainer.dataset_fingerprint(raw_rows),
            trainer.dataset_fingerprint(list(reversed(raw_rows))),
        )
        changed_raw = [dict(raw_rows[0], complaint_text="changed synthetic fixture"), raw_rows[1]]
        self.assertNotEqual(trainer.dataset_fingerprint(raw_rows), trainer.dataset_fingerprint(changed_raw))

    def test_unknown_provenance_and_unsupported_labels_are_excluded(self):
        valid = make_record(1, 101, 1)
        legacy = make_record(2, 102, 2, record_provenance="migration_backfill")
        outcome_label = make_record(3, 103, 1, label_kind="resolution_success")
        admitted, excluded = recommendation.validate_assignment_records([valid, legacy, outcome_label])
        self.assertEqual([row["assignment_id"] for row in admitted], [1])
        self.assertEqual(excluded, {
            "unknown_or_unverified_provenance": 1,
            "unsupported_training_label": 1,
        })

    def test_known_reconstructed_provenance_is_excluded_even_with_references(self):
        reconstructed = make_record(
            1, 101, 1, reconstruction_status="known_reconstructed",
            provenance_evidence_ref="synthetic-claimed-evidence",
            eligibility_verified=True,
        )
        admitted, excluded = recommendation.validate_assignment_records([reconstructed])
        self.assertEqual(admitted, [])
        self.assertEqual(excluded["reconstructed_or_unknown_history"], 1)

    def test_historical_inclusion_does_not_depend_on_later_current_account_fields(self):
        current_account_changed = make_record(
            staff_role="former_staff", staff_is_active=False,
            staff_department_id=99, staff_department="Different current unit",
        )
        admitted, excluded = recommendation.validate_assignment_records([current_account_changed])
        self.assertEqual(len(admitted), 1)
        self.assertEqual(excluded, {})

    def test_naive_assignment_timestamps_are_rejected(self):
        row = assignment_payload(make_record())
        row["assigned_at"] = "2026-01-02T00:00:00"
        with self.assertRaisesRegex(ValueError, "invalid_assignment_timestamp"):
            trainer._normalize_assignment(row)

    def test_missing_historical_eligibility_and_missing_review_are_excluded(self):
        missing_eligibility = make_record(1, 101, 1, eligibility_verified=False)
        missing_review = make_record(2, 102, 2, _review_missing=True)
        admitted, excluded = recommendation.validate_assignment_records([missing_eligibility, missing_review])
        self.assertEqual(admitted, [])
        self.assertEqual(excluded["historical_eligibility_unverified"], 1)
        self.assertEqual(excluded["review_record_missing"], 1)

    def test_review_manifest_matches_full_assignment_tuple_and_roster_time(self):
        row = make_record()
        raw_row = raw_manifest_row(row)
        manifest = manifest_envelope([raw_row], [row])
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "synthetic.json"
            dataset.write_text(json.dumps({
                "format_version": 1,
                "assignments": [raw_row],
                "review_manifest": manifest,
            }), encoding="utf-8")
            records, *_ = trainer._read_local_dataset(str(dataset), source_identity=SOURCE_IDENTITY)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["candidate_staff_ids"], [1, 2])

        wrong_tuple = manifest_record(row, staff_id=99)
        with self.assertRaisesRegex(ValueError, "review_assignment_tuple_mismatch"):
            trainer._load_review_manifest(
                None, manifest_envelope([raw_row], [row]) | {"records": [wrong_tuple]},
                [raw_row], SOURCE_IDENTITY, trainer.dataset_fingerprint([raw_row]),
            )
        wrong_time = manifest_record(
            row, assigned_at=(row["assigned_at"] + timedelta(seconds=1)).isoformat(),
        )
        with self.assertRaisesRegex(ValueError, "review_assignment_tuple_mismatch"):
            trainer._load_review_manifest(
                None, manifest_envelope([raw_row], [row]) | {"records": [wrong_time]},
                [raw_row], SOURCE_IDENTITY, trainer.dataset_fingerprint([raw_row]),
            )

    def test_duplicate_or_unmatched_review_entries_fail_closed(self):
        row = make_record()
        raw = [raw_manifest_row(row)]
        review = manifest_record(row)
        envelope = manifest_envelope(raw, [row])
        with self.assertRaisesRegex(ValueError, "duplicate_review_assignment_id"):
            trainer._load_review_manifest(
                None, envelope | {"records": [review, review]}, raw,
                SOURCE_IDENTITY, trainer.dataset_fingerprint(raw),
            )
        with self.assertRaisesRegex(ValueError, "unmatched_review_assignment_id"):
            trainer._load_review_manifest(
                None, envelope | {"records": [manifest_record(row, assignment_id=2)]}, raw,
                SOURCE_IDENTITY, trainer.dataset_fingerprint(raw),
            )
        with self.assertRaisesRegex(ValueError, "review_manifest_missing_or_malformed"):
            trainer._load_review_manifest(None, None, raw, SOURCE_IDENTITY, trainer.dataset_fingerprint(raw))
        with self.assertRaisesRegex(ValueError, "invalid_review_record"):
            trainer._load_review_manifest(
                None,
                envelope | {"records": [manifest_record(row, eligibility_verified="true")]},
                raw, SOURCE_IDENTITY, trainer.dataset_fingerprint(raw),
            )

    def test_manifest_dataset_source_and_tuple_collisions_fail_closed(self):
        row = make_record()
        raw = [raw_manifest_row(row)]
        envelope = manifest_envelope(raw, [row])
        with self.assertRaisesRegex(ValueError, "review_manifest_source_identity_mismatch"):
            trainer._load_review_manifest(
                None, envelope, raw, "different-synthetic-source", trainer.dataset_fingerprint(raw),
            )
        changed_source_row = dict(raw[0], complaint_text="other synthetic fixture")
        with self.assertRaisesRegex(ValueError, "review_manifest_dataset_fingerprint_mismatch"):
            trainer._load_review_manifest(
                None, envelope, [changed_source_row], SOURCE_IDENTITY,
                trainer.dataset_fingerprint([changed_source_row]),
            )

    def test_candidate_roster_requires_review_and_matching_department(self):
        row = make_record()
        raw = [raw_manifest_row(row)]
        unreviewed = manifest_envelope(raw, [row])
        unreviewed["records"] = [manifest_record(row, candidate_roster_reviewed=False)]
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "synthetic.json"
            dataset.write_text(json.dumps({
                "format_version": 1, "assignments": raw, "review_manifest": unreviewed,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid_candidate_roster"):
                trainer._read_local_dataset(str(dataset), source_identity=SOURCE_IDENTITY)

        wrong_department = manifest_envelope(raw, [row])
        wrong_department["records"] = [manifest_record(row, candidate_roster_department_id=2)]
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "synthetic.json"
            dataset.write_text(json.dumps({
                "format_version": 1, "assignments": raw, "review_manifest": wrong_department,
            }), encoding="utf-8")
            records, *rest = trainer._read_local_dataset(str(dataset), source_identity=SOURCE_IDENTITY)
        self.assertIsNone(records[0].get("candidate_staff_ids"))
        self.assertIn("candidate_roster_department_mismatch", rest[5])

        ineligible = manifest_envelope(raw, [row])
        ineligible["records"] = [manifest_record(
            row, candidate_staff_eligibility_review={"1": True, "2": False},
        )]
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "synthetic.json"
            dataset.write_text(json.dumps({
                "format_version": 1, "assignments": raw, "review_manifest": ineligible,
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid_candidate_roster"):
                trainer._read_local_dataset(str(dataset), source_identity=SOURCE_IDENTITY)

    def test_reader_counts_unknown_provenance_as_exclusion(self):
        approved = make_record(1, 101, 1)
        legacy = make_record(2, 102, 2, record_provenance="migration_backfill",
                             candidate_staff_ids=None, candidate_roster_at=None,
                             candidate_roster_department_id=None,
                             candidate_roster_reviewed=False,
                             candidate_staff_eligibility_review=None,
                             candidate_roster_evidence_ref=None)
        raw_rows = [raw_manifest_row(row) for row in (approved, legacy)]
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "synthetic.json"
            manifest = manifest_envelope(raw_rows, [approved, legacy])
            dataset.write_text(json.dumps({
                "format_version": 1, "assignments": raw_rows, "review_manifest": manifest,
            }), encoding="utf-8")
            records, _, _, _, _, _, excluded, _ = trainer._read_local_dataset(
                str(dataset), source_identity=SOURCE_IDENTITY,
            )
        self.assertEqual(len(records), 1)
        self.assertEqual(excluded["unknown_or_unverified_provenance"], 1)

    def test_evaluation_uses_only_time_matched_reviewed_candidate_roster(self):
        rows = [make_record(i, 100 + i, 2) for i in range(1, 6)]
        candidate_map = {row["assignment_id"]: {
            "staff_ids": [1, 2], "department_id": 1, "as_of": row["assigned_at"],
            "reviewed": True,
            "eligibility_review": {1: True, 2: True},
            "evidence_ref": "synthetic-roster-evidence",
        } for row in rows}
        artifact = {"models": {"1": _FakeModel()}}
        first = recommendation.evaluate_staff_rankings(artifact, rows, candidate_map, {}, top_k=2)
        second = recommendation.evaluate_staff_rankings(artifact, rows, candidate_map, {}, top_k=2)
        self.assertEqual(first, second)
        self.assertEqual(first["ml_plus_workload"]["evaluated_count"], 5)
        self.assertEqual(first["ml_plus_workload"]["top1_accuracy"], 1.0)

        unavailable = {row["assignment_id"]: {
            "staff_ids": [1], "department_id": 1, "as_of": row["assigned_at"],
            "reviewed": True,
            "eligibility_review": {1: True},
            "evidence_ref": "synthetic-roster-evidence",
        } for row in rows}
        excluded = recommendation.evaluate_staff_rankings(artifact, rows, unavailable, {}, top_k=2)
        self.assertEqual(excluded["ml_plus_workload"]["evaluated_count"], 0)
        self.assertEqual(excluded["skipped_counts"]["held_out_label_not_in_reviewed_candidate_roster"], 5)
        self.assertIsNone(excluded["ml_plus_workload"]["top1_accuracy"])

        future_roster = {row["assignment_id"]: {
            "staff_ids": [1, 2], "department_id": 1,
            "as_of": row["assigned_at"] + timedelta(seconds=1), "reviewed": True,
            "eligibility_review": {1: True, 2: True},
            "evidence_ref": "synthetic-roster-evidence",
        } for row in rows}
        excluded = recommendation.evaluate_staff_rankings(artifact, rows, future_roster, {}, top_k=2)
        self.assertEqual(excluded["ml_plus_workload"]["evaluated_count"], 0)
        self.assertEqual(excluded["skipped_counts"]["candidate_roster_missing_or_invalid"], 5)

        wrong_department = {row["assignment_id"]: {
            "staff_ids": [1, 2], "department_id": 99, "as_of": row["assigned_at"],
            "reviewed": True, "evidence_ref": "synthetic-roster-evidence",
            "eligibility_review": {1: True, 2: True},
        } for row in rows}
        excluded = recommendation.evaluate_staff_rankings(artifact, rows, wrong_department, {}, top_k=2)
        self.assertEqual(excluded["skipped_counts"]["candidate_roster_missing_or_invalid"], 5)

    def test_evaluation_coverage_floor_is_separate_from_complaint_floor(self):
        rows = [make_record(i, 500 + i, 2) for i in range(1, 11)]
        rosters = {row["assignment_id"]: {
            "staff_ids": [1, 2], "department_id": 1, "as_of": row["assigned_at"],
            "reviewed": True, "eligibility_review": {1: True, 2: True},
            "evidence_ref": "synthetic-roster-evidence",
        } for row in rows[:7]}
        result = recommendation.evaluate_staff_rankings(
            {"models": {"1": _FakeModel()}}, rows, rosters, None, top_k=2,
        )["ml_without_historical_workload_adjustment"]
        self.assertEqual(result["evaluated_complaint_count"], 7)
        self.assertEqual(result["evaluated_count"], 7)
        self.assertEqual(result["eligible_count"], 10)
        self.assertEqual(result["skipped_count"], 3)
        self.assertEqual(result["coverage"], 0.7)
        self.assertEqual(result["status"], "insufficient_data")
        self.assertIsNone(result["top1_accuracy"])

    def test_validation_preflight_checks_roster_label_and_coverage_readiness(self):
        rows = [make_record(i, 3000 + i, 1 + (i % 2)) for i in range(1, 31)]
        raw_rows = [raw_manifest_row(row) for row in rows]
        _, held_out = recommendation.chronological_group_split(rows, 0.2)
        excluded_assignment_ids = {row["assignment_id"] for row in held_out[:2]}
        reviews = []
        for row in rows:
            review = manifest_record(row)
            if row["assignment_id"] in excluded_assignment_ids:
                review["candidate_staff_ids"] = [3]
                review["candidate_staff_eligibility_review"] = {"3": True}
            reviews.append(review)
        manifest = {
            "format_version": 2,
            "dataset_fingerprint": trainer.dataset_fingerprint(raw_rows),
            "source_identity": SOURCE_IDENTITY,
            "records": reviews,
        }
        with TemporaryDirectory() as directory:
            dataset = Path(directory) / "synthetic.json"
            dataset.write_text(json.dumps({
                "format_version": 1, "assignments": raw_rows,
                "review_manifest": manifest,
            }), encoding="utf-8")
            args = trainer.parse_args([
                "--dataset", str(dataset), "--source-identity", SOURCE_IDENTITY,
                "--validate-only",
            ])
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertTrue(trainer.run_validation(args))
        report = json.loads(output.getvalue())
        readiness = report["held_out_evaluation_preflight"]
        self.assertEqual(readiness["eligible_count"], len(held_out))
        self.assertEqual(readiness["candidate_compatible_count"], len(held_out) - 2)
        self.assertEqual(readiness["skipped_count"], 2)
        self.assertFalse(report["data_sufficient_for_training_attempt"])
        self.assertEqual(report["status"], "insufficient_data")

    def test_split_keeps_complaints_together_and_enforces_strict_time_order(self):
        rows = [make_record(i, 100 + i, 1, assigned_at=NOW + timedelta(days=i)) for i in range(1, 6)]
        rows.extend([make_record(10 + i, 100 + i, 2,
                                 assigned_at=NOW + timedelta(days=i, minutes=1)) for i in range(1, 6)])
        train, test = recommendation.chronological_group_split(rows, 0.2)
        self.assertTrue(
            {row["complaint_id"] for row in train}.isdisjoint(
                {row["complaint_id"] for row in test}
            )
        )
        self.assertLess(max(row["assigned_at"] for row in train), min(row["assigned_at"] for row in test))

        overlapping = [
            make_record(1, 201, 1, assigned_at=NOW),
            make_record(2, 201, 2, assigned_at=NOW + timedelta(days=3)),
            make_record(3, 202, 1, assigned_at=NOW + timedelta(days=1)),
            make_record(4, 203, 2, assigned_at=NOW + timedelta(days=2)),
        ]
        with self.assertRaises(recommendation.InsufficientTrainingData):
            recommendation.chronological_group_split(overlapping, 0.34)
        tied = [make_record(i, 300 + i, i % 2 + 1, assigned_at=NOW) for i in range(1, 7)]
        with self.assertRaises(recommendation.InsufficientTrainingData):
            recommendation.chronological_group_split(tied, 0.2)

    def test_train_without_enough_trusted_data_is_explicitly_insufficient(self):
        with self.assertRaises(recommendation.InsufficientTrainingData):
            recommendation.train_staff_models([make_record()])

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is unavailable")
    def test_artifact_records_fingerprint_evaluation_exclusions_and_configuration(self):
        rows = [make_record(i, 1000 + i, 1 + (i % 2)) for i in range(1, 31)]
        train_rows, test_rows = recommendation.chronological_group_split(rows, 0.2)
        evaluation_model = recommendation.train_staff_models(train_rows, require_evaluation=False)
        rosters = {row["assignment_id"]: {
            "staff_ids": [1, 2], "department_id": 1,
            "as_of": row["assigned_at"], "reviewed": True,
            "eligibility_review": {1: True, 2: True},
            "evidence_ref": "synthetic-roster-evidence",
        } for row in test_rows}
        evaluation = recommendation.evaluate_staff_rankings(
            evaluation_model, test_rows, rosters, {}, top_k=2,
        )
        metrics = evaluation["ml_plus_workload"]
        self.assertEqual(metrics["status"], "evaluated")
        artifact = recommendation.train_staff_models(rows, training_metadata={
            "training_data_fingerprint": recommendation.training_data_fingerprint(rows),
            "source_dataset_fingerprint": trainer.dataset_fingerprint(
                [raw_manifest_row(row) for row in rows]
            ),
            "source_identity": SOURCE_IDENTITY,
            "evaluation_metrics": {
                "evaluated_count": metrics["evaluated_count"],
                "eligible_count": metrics["eligible_count"],
                "skipped_count": metrics["skipped_count"],
                "coverage": metrics["coverage"],
                "primary": metrics,
                "skipped_counts": evaluation["skipped_counts"],
                "top_k": 2,
            },
            "excluded_record_counts": {"unknown_or_unverified_provenance": 2},
            "training_config": {
                "split_policy": "complaint-grouped-strict-chronological-v1",
                "minimum_evaluation_coverage": recommendation.MIN_EVALUATION_COVERAGE,
                "minimum_distinct_evaluation_complaints": recommendation.MIN_EVALUATION_COMPLAINTS,
            },
        })
        metadata = artifact["metadata"]
        self.assertEqual(metadata["format_version"], recommendation.ARTIFACT_FORMAT_VERSION)
        self.assertEqual(metadata["feature_schema"], recommendation.FEATURE_SCHEMA)
        self.assertEqual(metadata["training_data_fingerprint"], recommendation.training_data_fingerprint(rows))
        self.assertEqual(metadata["source_identity"], SOURCE_IDENTITY)
        self.assertEqual(metadata["source_dataset_fingerprint"], trainer.dataset_fingerprint(
            [raw_manifest_row(row) for row in rows]
        ))
        self.assertEqual(metadata["evaluation_metrics"]["evaluated_count"], len(test_rows))
        self.assertEqual(metadata["rejected_record_counts"], {"unknown_or_unverified_provenance": 2})
        self.assertEqual(metadata["training_config"]["split_policy"], "complaint-grouped-strict-chronological-v1")
        self.assertTrue(recommendation._validate_loaded_artifact(artifact))

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is unavailable")
    def test_legacy_artifact_metadata_is_rejected(self):
        self.assertFalse(recommendation._validate_loaded_artifact({
            "metadata": {"format_version": 1}, "models": {},
        }))

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is unavailable")
    def test_corrupt_and_incompatible_artifacts_fall_back_safely(self):
        artifact = make_valid_artifact()
        incompatible = dict(artifact)
        incompatible["metadata"] = dict(artifact["metadata"], feature_schema="unknown-schema")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "artifact.joblib"
            path.write_bytes(b"synthetic artifact placeholder")
            recommendation._load_artifact_cached.cache_clear()
            with patch("joblib.load", return_value=incompatible), warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertIsNone(recommendation.load_model_artifact(str(path)))
            self.assertTrue(caught)
            recommendation._load_artifact_cached.cache_clear()
            with patch("joblib.load", side_effect=ValueError("synthetic corrupt artifact")), \
                    warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                self.assertIsNone(recommendation.load_model_artifact(str(path)))
            self.assertTrue(caught)
            recommendation._load_artifact_cached.cache_clear()

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "scikit-learn is unavailable")
    def test_artifact_creation_never_overwrites_and_publish_failure_leaves_no_target(self):
        artifact = make_valid_artifact()
        with TemporaryDirectory() as directory:
            target = Path(directory) / "artifact.joblib"
            original = b"existing trusted artifact"
            target.write_bytes(original)
            with patch("joblib.dump") as dump:
                with self.assertRaises(FileExistsError):
                    recommendation.save_model_artifact(artifact, str(target))
            dump.assert_not_called()
            self.assertEqual(target.read_bytes(), original)

            def write_staged_file(_artifact, staged_path, compress):
                del compress
                Path(staged_path).write_bytes(b"staged synthetic artifact")

            target.unlink()
            with patch("joblib.dump", side_effect=write_staged_file), \
                    patch("staff_recommendation.os.link", side_effect=OSError("synthetic publish failure")):
                with self.assertRaises(OSError):
                    recommendation.save_model_artifact(artifact, str(target))
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(directory).glob(".spsfrms-staff-model-*.tmp")), [])

            saved = recommendation.save_model_artifact(artifact, str(target))
            self.assertEqual(saved, str(target))
            self.assertGreater(target.stat().st_size, 0)
            with self.assertRaises(FileExistsError):
                recommendation.save_model_artifact(artifact, str(target))

    def test_training_artifact_path_refuses_existing_target(self):
        with TemporaryDirectory() as directory:
            target = Path(directory) / "existing.joblib"
            target.write_bytes(b"synthetic artifact")
            with self.assertRaises(FileExistsError):
                trainer._artifact_path(str(target))

    def test_training_cli_returns_nonzero_when_no_artifact_is_produced(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "missing.csv"
            self.assertEqual(trainer.main(["--dataset", str(missing)]), 1)

    def test_recommendation_is_advisory_and_does_not_assign(self):
        staff = [
            SimpleNamespace(id=1, name="Synthetic A", role="staff", department_id=1),
            SimpleNamespace(id=2, name="Synthetic B", role="staff", department_id=1),
            SimpleNamespace(id=3, name="Wrong department", role="staff", department_id=2),
            SimpleNamespace(id=4, name="Wrong role", role="department_head", department_id=1),
            SimpleNamespace(id=5, name="Explicitly inactive", role="staff", department_id=1,
                            is_active=False),
        ]
        workloads = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}

        # Missing artifacts and structurally unusable model maps both use the
        # deterministic workload baseline without changing assignment state.
        for artifact in (None, {"metadata": {}, "models": {}}, {"models": {"1": object()}}):
            result = recommendation.recommend_staff(
                "synthetic fixture", "Drainage", 1, staff, workloads, artifact,
            )
            self.assertEqual(result["staff_id"], 1)
            self.assertEqual(result["method"], "workload_fallback")
            self.assertNotIn("assignment_id", result)
            self.assertFalse(hasattr(staff[0], "assigned_complaints"))

        # Ordering is independent of the incoming candidate order on equal load.
        reversed_result = recommendation.recommend_staff(
            "synthetic fixture", "Drainage", 1, list(reversed(staff)), workloads,
        )
        self.assertEqual(reversed_result["staff_id"], 1)

    def test_trainer_schema_remains_twelve_fields(self):
        self.assertEqual(len(trainer.ASSIGNMENT_FIELDS), 12)


if __name__ == "__main__":
    unittest.main()
