"""Evaluator tests use synthetic fixtures only; they do not evaluate a model."""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from check_eval_cases import load_cases, validate_cases  # noqa: E402
from evaluate_translations import evaluate, number_literals  # noqa: E402


class EvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases, errors = load_cases(ROOT / "eval" / "cases.jsonl")
        if errors:
            raise AssertionError(errors)
        configured = os.environ.get("TRANSIME_WORK_DIR")
        if configured:
            cls.artifact_root = Path(configured).expanduser().resolve() / "evaluation-tests"
            cls.artifact_root.mkdir(parents=True, exist_ok=True)
        else:
            temporary = tempfile.TemporaryDirectory(prefix="transime-evaluation-")
            cls.addClassCleanup(temporary.cleanup)
            cls.artifact_root = Path(temporary.name)

    def synthetic_predictions(self):
        return [{"id": case["id"], "translation": case["acceptable_translations"][0],
                 "provenance": "synthetic_test_fixture"} for case in self.cases]

    def test_cases_have_sixteen_cases_and_three_context_pairs(self):
        self.assertEqual(16, len(self.cases))
        self.assertEqual({"agreement", "interface", "close"},
                         {case["pair_group"] for case in self.cases if "pair_group" in case})

    def test_synthetic_references_never_prove_semantic_quality(self):
        summary, rows = evaluate(self.cases, self.synthetic_predictions())
        self.assertEqual("needs_human_review", summary["status"])
        self.assertIsNone(summary["semantic_quality_score"])
        self.assertIsNone(summary["semantic_passed_cases"])
        self.assertEqual(1.0, summary["completion_ratio"])
        self.assertTrue(all(row["semantic_status"] == "needs_human_review" for row in rows))

    def test_missing_prediction_is_detected(self):
        summary, _ = evaluate(self.cases, self.synthetic_predictions()[1:])
        self.assertEqual(["agreement-plan"], summary["missing_ids"])
        self.assertEqual(15 / 16, summary["completion_ratio"])
        self.assertEqual("mechanical_checks_failed", summary["status"])

    def test_duplicate_id_is_not_silently_overwritten(self):
        predictions = self.synthetic_predictions()
        predictions.append(dict(predictions[0]))
        summary, rows = evaluate(self.cases, predictions)
        self.assertEqual(["agreement-plan"], summary["duplicate_ids"])
        self.assertEqual("duplicate_id", rows[0]["issues"])
        self.assertEqual(15 / 16, summary["completion_ratio"])

    def test_unknown_ids_fail_even_if_expected_cases_are_present(self):
        predictions = self.synthetic_predictions()
        predictions.append({"id": "unknown", "translation": "Synthetic output."})
        summary, _ = evaluate(self.cases, predictions)
        self.assertEqual(["unknown"], summary["unknown_ids"])
        self.assertEqual("mechanical_checks_failed", summary["status"])

    def test_control_characters_and_empty_values_are_rejected(self):
        for value in ("", "  ", "\u00a0", "\u3000", "\u00a0\u3000", None, 123,
                      "hello\nworld", "hello\tworld", "hello\u0000world",
                      "hello\u202eworld", "\ud800"):
            with self.subTest(value=repr(value)):
                predictions = self.synthetic_predictions()
                predictions[0]["translation"] = value
                summary, rows = evaluate(self.cases, predictions)
                self.assertEqual("mechanical_checks_failed", summary["status"])
                self.assertEqual("failed", rows[0]["mechanical_status"])

    def test_invalid_provenance_fails_both_summary_and_case(self):
        predictions = self.synthetic_predictions()
        predictions[0]["provenance"] = {"invalid": "metadata"}
        summary, rows = evaluate(self.cases, predictions)
        self.assertEqual("mechanical_checks_failed", summary["status"])
        self.assertEqual(1, summary["mechanical_failed_cases"])
        self.assertEqual(15, summary["mechanical_passed_cases"])
        self.assertEqual(15, summary["completed_cases"])
        self.assertEqual("failed", rows[0]["mechanical_status"])
        self.assertIn("invalid_provenance", rows[0]["issues"])
        self.assertEqual("invalid", rows[0]["provenance"])

    def test_changed_numeric_value_and_percent_are_detected(self):
        for value in ("The failure rate fell from 2.5% to 1.3%.",
                      "The failure rate fell from 2.5 to 1.2%."):
            predictions = self.synthetic_predictions()
            next(row for row in predictions if row["id"] == "rate-decimals")["translation"] = value
            summary, rows = evaluate(self.cases, predictions)
            self.assertEqual("mechanical_checks_failed", summary["status"])
            self.assertEqual("mismatch", next(row for row in rows if row["id"] == "rate-decimals")["numeric_literals"])

    def test_number_normalization_and_chinese_adjacency(self):
        self.assertEqual(number_literals("使用1024字节，等待30秒。"), number_literals("Use 1,024 bytes; wait 30.0 seconds."))
        self.assertNotEqual(number_literals("-2"), number_literals("2"))
        self.assertNotEqual(number_literals("2%"), number_literals("2"))

    def test_schema_rejects_duplicate_case_and_invalid_history(self):
        cases = copy.deepcopy(self.cases)
        cases[1]["id"] = cases[0]["id"]
        cases[0]["history"][0]["language"] = "unknown"
        errors = validate_cases(cases)
        self.assertTrue(any("duplicate id" in error for error in errors))
        self.assertTrue(any("language" in error for error in errors))

    def test_cli_without_predictions_never_evaluates_translation(self):
        with tempfile.TemporaryDirectory(dir=self.artifact_root) as folder:
            output = Path(folder) / "schema"
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "evaluate_translations.py"),
                                     "--cases", str(ROOT / "eval" / "cases.jsonl"),
                                     "--output-dir", str(output)], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("schema_valid_only", summary["status"])
            self.assertEqual("not_evaluated", summary["semantic_status"])
            self.assertTrue((output / "report.md").is_file())
            self.assertTrue((output / "metrics.csv").is_file())

    def test_cli_malformed_prediction_writes_failed_merged_report(self):
        with tempfile.TemporaryDirectory(dir=self.artifact_root) as folder:
            base = Path(folder)
            predictions = base / "synthetic-invalid.jsonl"
            predictions.write_text('{"id": invalid json}\n', encoding="utf-8")
            output = base / "report"
            result = subprocess.run([sys.executable, str(ROOT / "tools" / "evaluate_translations.py"),
                                     "--cases", str(ROOT / "eval" / "cases.jsonl"),
                                     "--predictions", str(predictions), "--output-dir", str(output)],
                                    capture_output=True, text=True)
            self.assertEqual(1, result.returncode, result.stderr)
            summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
            self.assertEqual("mechanical_checks_failed", summary["status"])
            self.assertEqual(16, len(summary["missing_ids"]))
            self.assertTrue(any("invalid JSON" in error for error in summary["errors"]))

    def test_json_escapes_do_not_bypass_empty_or_control_validation(self):
        for value in ("\ud800", "\u00a0", "\u3000", "hello\u0000world"):
            with self.subTest(value=repr(value)), tempfile.TemporaryDirectory(dir=self.artifact_root) as folder:
                base = Path(folder)
                predictions = self.synthetic_predictions()
                predictions[0]["translation"] = value
                input_file = base / "synthetic-escaped.jsonl"
                input_file.write_text("".join(json.dumps(row, ensure_ascii=True) + "\n" for row in predictions), encoding="utf-8")
                output = base / "report"
                result = subprocess.run([sys.executable, str(ROOT / "tools" / "evaluate_translations.py"),
                                         "--cases", str(ROOT / "eval" / "cases.jsonl"),
                                         "--predictions", str(input_file), "--output-dir", str(output)],
                                        capture_output=True, text=True)
                self.assertEqual(1, result.returncode, result.stderr)
                summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual("mechanical_checks_failed", summary["status"])
                self.assertEqual(1, summary["mechanical_failed_cases"])
                self.assertEqual(15, summary["completed_cases"])


if __name__ == "__main__":
    unittest.main()
