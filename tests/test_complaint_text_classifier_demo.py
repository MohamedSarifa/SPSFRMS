"""Isolated checks for the synthetic complaint text-classification demo."""

import contextlib
import io
import unittest

import demo_complaint_text_classifier as demo


class ComplaintTextClassifierDemoTests(unittest.TestCase):
    def test_examples_are_explicitly_synthetic_and_labeled(self):
        self.assertEqual(len(demo.SYNTHETIC_EXAMPLES), 18)
        self.assertEqual(
            {label for _text, label in demo.SYNTHETIC_EXAMPLES},
            {"Water Supply", "Road Maintenance", "Waste Management"},
        )
        self.assertTrue(all("synthetic" in text.lower()
                            or "fictional" in text.lower()
                            or "example" in text.lower()
                            or "demo" in text.lower()
                            or "made-up" in text.lower()
                            for text, _label in demo.SYNTHETIC_EXAMPLES))

    def test_split_and_metrics_are_reproducible_and_clearly_limited(self):
        first = demo.run_demo()
        second = demo.run_demo()
        self.assertEqual(first, second)
        self.assertEqual(first["demo_status"], "completed")
        self.assertEqual(first["dataset_type"], "fixed synthetic demonstration examples")
        self.assertFalse(first["staff_recommendation_model"])
        self.assertFalse(first["production_routing_changed"])
        self.assertFalse(first["artifact_written"])
        self.assertEqual(first["training_example_count"], 12)
        self.assertEqual(first["evaluation_example_count"], 6)
        self.assertEqual(set(first["synthetic_holdout_metrics"]), {"accuracy", "macro_f1"})
        self.assertEqual(len(first["synthetic_example_predictions"]), 3)

    def test_cli_prints_only_demonstration_summary_and_writes_no_artifact(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = demo.main()
        self.assertEqual(result, 0)
        rendered = output.getvalue()
        self.assertIn('"artifact_written": false', rendered)
        self.assertIn('"staff_recommendation_model": false', rendered)
        self.assertIn('"dataset_type": "fixed synthetic demonstration examples"', rendered)


if __name__ == "__main__":
    unittest.main()
