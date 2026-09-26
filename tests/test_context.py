"""Behavioral guards for lexical memory; synthetic scores, not quality tests."""
import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location("context", Path(__file__).parents[1] / "worker/context.py")
context = importlib.util.module_from_spec(spec)
spec.loader.exec_module(context)


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.source = "插头坏了。"
        self.history = [{"source": "这个插头有点松。", "committed": "This plug is a bit loose.", "language": "en"}]
        self.hypotheses = [{"text": "The connector broke.", "score": -0.50},
                           {"text": "The plug broke.", "score": -0.53}]

    def test_reuses_term_only_by_selecting_model_candidate(self):
        result = context.rerank(self.source, self.history, self.hypotheses)
        self.assertEqual(result["selected_index"], 1)
        self.assertEqual(result["text"], self.hypotheses[1]["text"])
        self.assertTrue(result["context_applied"])

    def test_empty_history(self):
        self.assertEqual(context.rerank(self.source, [], self.hypotheses)["selected_index"], 0)

    def test_unknown_source_not_inferred(self):
        self.history[0]["source"] = None
        self.assertEqual(context.rerank(self.source, self.history, self.hypotheses)["selected_index"], 0)

    def test_english_history_requires_relevant_chinese_anchor(self):
        self.history[0]["source"] = "我们谈论晚饭。"
        self.assertEqual(context.rerank(self.source, self.history, self.hypotheses)["selected_index"], 0)

    def test_cannot_override_low_likelihood(self):
        self.hypotheses[1]["score"] = -1.0
        self.assertEqual(context.rerank(self.source, self.history, self.hypotheses)["selected_index"], 0)

    def test_number_negation_modality_pronoun_changes_blocked(self):
        for text in ("The plug did not break.", "The 3 plugs broke.",
                     "The plug might break.", "He broke the plug."):
            with self.subTest(text=text):
                self.hypotheses[1]["text"] = text
                self.assertEqual(context.rerank(self.source, self.history, self.hypotheses)["selected_index"], 0)

    def test_source_and_history_not_inserted_into_output(self):
        self.history[0]["committed"] += " Unrelated historical details never belong in the current output."
        result = context.rerank(self.source, self.history, self.hypotheses)
        self.assertIn(result["text"], [h["text"] for h in self.hypotheses])

    def test_nonfinite_scores_rejected(self):
        self.hypotheses[0]["score"] = float("nan")
        with self.assertRaises(ValueError):
            context.rerank(self.source, self.history, self.hypotheses)

    def test_ordinary_chinese_history_is_relevant_without_source_pair(self):
        rows = [{"source": None, "committed": "插头没有接好。", "language": "und"},
                {"source": None, "committed": "晚饭准备好了。", "language": "und"}]
        self.assertEqual(context.chinese_history_candidates(self.source, rows), [0])

    def test_derived_history_is_bounded_and_skips_mixed_text(self):
        rows = [{"source": None, "committed": "插头没有接好。", "language": "zh"}] * 5
        self.assertEqual(context.chinese_history_candidates(self.source, rows), [4, 3])
        rows[-1] = {"source": None, "committed": "插头 USB test", "language": "und"}
        self.assertEqual(context.chinese_history_candidates(self.source, rows), [3, 2])

    def test_used_english_precedes_provisional_model_translation(self):
        self.history.append({"source": "插头需要更换。", "committed": "The connector needs replacing.",
                             "language": "en", "provisional": True})
        self.assertEqual(context.rerank(self.source, self.history, self.hypotheses)["selected_index"], 1)


if __name__ == "__main__":
    unittest.main()
