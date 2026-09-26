"""Protocol tests use a synthetic engine; no fixture is a real translation score."""

import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import unittest

PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("worker_under_test", PROJECT / "worker/transime_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class SyntheticEngine:
    def __init__(self):
        self.calls = []
        self.cleared = False
        self.last_diagnostics = {"fixture": "synthetic_engine"}

    def translate_candidates(self, source):
        self.calls.append(source)
        return [{"text": "Synthetic first.", "score": -0.1},
                {"text": "Synthetic second.", "score": -0.2}]

    def clear(self):
        self.calls.clear()
        self.cleared = True


def request(**updates):
    result = {"id": 7, "source": "测试。", "history": [], "context": False}
    result.update(updates)
    return result


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.engine = SyntheticEngine()
        self.worker = worker.Worker(self.engine)

    def test_frame_round_trip_with_non_ascii(self):
        stream = io.BytesIO()
        worker.write_frame(stream, request())
        stream.seek(0)
        self.assertEqual(request(), worker.read_frame(stream))
        self.assertIsNone(worker.read_frame(stream))

    def test_truncated_and_oversized_frames_are_rejected(self):
        for data in (b"\x00", struct.pack(">I", 12) + b"{}", struct.pack(">I", 0),
                     struct.pack(">I", worker.MAX_FRAME_BYTES + 1)):
            with self.subTest(data=data), self.assertRaises(worker.ProtocolError):
                worker.read_frame(io.BytesIO(data))

    def test_duplicate_keys_invalid_utf8_and_nonfinite_json_rejected(self):
        for payload in (b'{"id":1,"id":2}', b'{"id":NaN}', b'{"source":"\xff"}', b'[]'):
            with self.subTest(payload=payload), self.assertRaises(worker.ProtocolError):
                worker.read_frame(io.BytesIO(struct.pack(">I", len(payload)) + payload))

    def test_invalid_ids_do_not_reach_model(self):
        for identifier in (-1, True, 1.5, "7", 1 << 64):
            result = self.worker.handle(request(id=identifier))
            self.assertEqual("invalid_id", result["error"])
        self.assertEqual([], self.engine.calls)

    def test_source_limits_control_characters_and_unicode_whitespace(self):
        for source in ("", "\u00a0\u3000", "\ud800", "hello\nworld", "x\u202e", "中" * 1366):
            with self.subTest(source=repr(source)):
                self.assertEqual("invalid_source", self.worker.handle(request(source=source))["error"])
        self.assertEqual([], self.engine.calls)

    def test_history_limits_and_null_source(self):
        record = {"source": None, "committed": "Previous output.", "language": "en"}
        self.assertIsNone(self.worker.handle(request(history=[record]))["error"])
        self.assertEqual("invalid_history", self.worker.handle(request(history=[record] * 6))["error"])
        large = dict(record, committed="a" * 2048)
        self.assertEqual("history_size_limit", self.worker.handle(request(history=[large]))["error"])

    def test_baseline_does_not_append_history_to_source(self):
        result = self.worker.handle(request(context=True, history=[{"source": "以前的话。", "committed": "Earlier text.", "language": "en"}]))
        self.assertEqual(["测试。"], self.engine.calls)
        self.assertFalse(result["diagnostics"]["context_applied"])
        self.assertFalse(result["diagnostics"]["context_available"])

    def test_reranker_can_only_choose_existing_candidate(self):
        valid = lambda _s, _h, _c: {"text": "Synthetic second.", "selected_index": 1, "context_applied": True}
        result = worker.Worker(self.engine, valid).handle(request(context=True))
        self.assertEqual("Synthetic second.", result["translation"])
        malicious = lambda _s, _h, _c: {"text": "Invented content.", "selected_index": 1}
        result = worker.Worker(self.engine, malicious).handle(request(context=True))
        self.assertEqual("invalid_context_result", result["error"])

    def test_clear_does_not_translate_or_load_model(self):
        result = self.worker.handle({"id": 8, "op": "clear"})
        self.assertIsNone(result["error"])
        self.assertTrue(self.engine.cleared)
        self.assertEqual([], self.engine.calls)
        self.assertEqual("", result["translation"])

    def test_chinese_history_is_provisional_and_not_cached(self):
        seen = []
        def rerank(_source, history, candidates):
            seen.append(history)
            return {"text": candidates[0]["text"], "selected_index": 0,
                    "context_applied": False}
        selector = lambda _source, _history: [0]
        instance = worker.Worker(self.engine, rerank, selector)
        original = [{"source": None, "committed": "测试以前的提交。", "language": "und"}]
        result = instance.handle(request(context=True, history=original))
        self.assertIsNone(result["error"])
        self.assertEqual(["测试。", "测试以前的提交。"], self.engine.calls)
        self.assertIsNone(original[0]["source"])
        self.assertTrue(seen[0][0]["provisional"])
        self.assertEqual("测试以前的提交。", seen[0][0]["source"])
        self.assertEqual(1, result["diagnostics"]["provisional_history_count"])
        self.assertEqual(0, result["diagnostics"]["text_cache_entries"])
        instance.handle({"id": 8, "op": "clear"})
        self.assertEqual([], self.engine.calls)

    def test_context_false_does_not_translate_chinese_history(self):
        instance = worker.Worker(self.engine, lambda *_: None, lambda *_: [0])
        instance.handle(request(history=[{"source": None, "committed": "测试以前的提交。", "language": "und"}]))
        self.assertEqual(["测试。"], self.engine.calls)

    def test_lexical_skips_related_chinese_generation_and_alignment(self):
        rules = worker.load_local_module("context")
        source = "这个选项已经启用。"
        history = [{"source": None, "committed": "请检查这个选项。", "language": "und"}]
        # This history would really be selected by the aligned path's selector.
        self.assertEqual([0], rules.chinese_history_candidates(source, history))
        selector_calls, adapter_calls, seen = [], [], []
        def selector(*args):
            selector_calls.append(args)
            return [0]
        def adapter(*args):
            adapter_calls.append(args)
            raise RuntimeError("Synthetic alignment must be skipped")
        def rerank(current, memories, candidates):
            seen.append(json.loads(json.dumps(memories)))
            return rules.rerank(current, memories, candidates)
        instance = worker.Worker(self.engine, rerank, selector, terminology_adapter=adapter,
                                 context_policy="lexical")
        for identifier in (10, 11):
            result = instance.handle(request(id=identifier, source=source, context=True, history=history))
            self.assertIsNone(result["error"])
            self.assertEqual("Synthetic first.", result["translation"])
            diagnostics = result["diagnostics"]
            self.assertEqual("lexical", diagnostics["context_policy"])
            self.assertTrue(diagnostics["context_requested"])
            self.assertTrue(diagnostics["context_available"])
            self.assertFalse(diagnostics["context_applied"])
            self.assertEqual(0, diagnostics["provisional_history_count"])
            self.assertEqual(0, diagnostics["history_inference_ms"])
            # Availability means the adapter exists, not that it ran.
            self.assertTrue(diagnostics["terminology_available"])
            self.assertFalse(diagnostics["terminology_applied"])
            self.assertNotIn("terminology_elapsed_ms", diagnostics)
            self.assertNotIn("terminology_reason", diagnostics)
        self.assertEqual([source, source], self.engine.calls)
        self.assertEqual([], selector_calls)
        self.assertEqual([], adapter_calls)
        self.assertEqual([history, history], seen)
        self.assertEqual({"source": None, "committed": "请检查这个选项。", "language": "und"}, history[0])

    def test_lexical_uses_real_reranker_to_select_existing_english_term(self):
        rules = worker.load_local_module("context")
        calls = []
        def candidates(source):
            calls.append(source)
            return [{"text": "This feature is enabled.", "score": -0.1},
                    {"text": "This option is enabled.", "score": -0.15}]
        self.engine.translate_candidates = candidates
        instance = worker.Worker(self.engine, rules.rerank, rules.chinese_history_candidates,
                                 context_policy="lexical")
        source = "这个选项已经启用。"
        history = [{"source": "请检查这个选项。", "committed": "This option is enabled.", "language": "en"},
                   {"source": None, "committed": "请检查这个选项。", "language": "zh"}]
        baseline = instance.handle(request(source=source, context=False, history=history))
        result = instance.handle(request(source=source, context=True, history=history))
        self.assertEqual("This feature is enabled.", baseline["translation"])
        self.assertEqual("This option is enabled.", result["translation"])
        self.assertEqual([source, source], calls)
        self.assertIsNone(result["error"])
        self.assertTrue(result["diagnostics"]["context_applied"])
        self.assertEqual(1, result["diagnostics"]["selected_index"])
        self.assertFalse(result["diagnostics"]["terminology_applied"])
        self.assertEqual(0, result["diagnostics"]["provisional_history_count"])

    def test_default_aligned_keeps_provisional_history_and_adapter(self):
        seen = []
        def rerank(_source, history, candidates):
            return {"text": candidates[0]["text"], "selected_index": 0}
        def adapter(source, history, baseline, engine):
            seen.append((source, history, baseline, engine))
            return {"text": "Aligned first.", "applied": True, "reason": "aligned_word_memory"}
        instance = worker.Worker(self.engine, rerank, lambda *_: [0], terminology_adapter=adapter)
        result = instance.handle(request(context=True, history=[
            {"source": None, "committed": "测试以前的提交。", "language": "und"}]))
        self.assertIsNone(result["error"])
        self.assertEqual("Aligned first.", result["translation"])
        self.assertEqual(["测试。", "测试以前的提交。"], self.engine.calls)
        self.assertEqual(1, len(seen))
        self.assertTrue(seen[0][1][0]["provisional"])
        self.assertEqual("aligned", result["diagnostics"]["context_policy"])
        self.assertEqual(1, result["diagnostics"]["provisional_history_count"])
        self.assertTrue(result["diagnostics"]["terminology_applied"])
        self.assertTrue(result["diagnostics"]["context_applied"])

    def test_context_false_skips_every_context_hook_in_both_policies(self):
        for policy in ("aligned", "lexical"):
            with self.subTest(policy=policy):
                calls = []
                def unexpected(*args):
                    calls.append(args)
                    raise RuntimeError("Synthetic context hook must not run")
                engine = SyntheticEngine()
                instance = worker.Worker(engine, unexpected, unexpected,
                                         terminology_adapter=unexpected, context_policy=policy)
                result = instance.handle(request(context=False, history=[
                    {"source": None, "committed": "测试以前的提交。", "language": "zh"}]))
                self.assertIsNone(result["error"])
                self.assertEqual("Synthetic first.", result["translation"])
                self.assertEqual(["测试。"], engine.calls)
                self.assertEqual([], calls)
                self.assertEqual(policy, result["diagnostics"]["context_policy"])
                self.assertFalse(result["diagnostics"]["context_requested"])
                self.assertFalse(result["diagnostics"]["context_applied"])
                self.assertFalse(result["diagnostics"]["terminology_applied"])
                self.assertEqual(0, result["diagnostics"]["provisional_history_count"])

    def test_lexical_still_rejects_invented_reranker_output(self):
        invented = lambda *_: {"text": "Invented content.", "selected_index": 0, "context_applied": True}
        result = worker.Worker(self.engine, invented, context_policy="lexical").handle(request(context=True))
        self.assertEqual("invalid_context_result", result["error"])
        self.assertEqual("", result["translation"])

    def test_unknown_context_policy_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "^invalid_context_policy$"):
            worker.Worker(self.engine, context_policy="unknown-synthetic-policy")
        self.assertEqual([], self.engine.calls)

    def test_failed_history_does_not_discard_current_translation(self):
        def translate(source):
            if source != "测试。":
                raise RuntimeError("Private synthetic history must never be logged")
            return [{"text": "Synthetic current.", "score": -0.1}]
        self.engine.translate_candidates = translate
        rerank = lambda _s, _h, candidates: {"text": candidates[0]["text"], "selected_index": 0}
        instance = worker.Worker(self.engine, rerank, lambda *_: [0])
        result = instance.handle(request(context=True, history=[{"source": None, "committed": "测试历史。", "language": "zh"}]))
        self.assertEqual("Synthetic current.", result["translation"])
        self.assertIsNone(result["error"])
        self.assertEqual(0, result["diagnostics"]["provisional_history_count"])

    def test_third_party_exception_text_is_not_exposed(self):
        def fail(_source):
            raise RuntimeError("Synthetic private source must not be echoed")
        self.engine.translate_candidates = fail
        result = self.worker.handle(request())
        self.assertEqual("inference_failed", result["error"])
        self.assertNotIn("private", json.dumps(result))

    def test_numeric_guard_required_for_arabic_source(self):
        result = self.worker.handle(request(source="测试2个。"))
        self.assertEqual("numeric_guard_unavailable", result["error"])
        def fail(_source, _target):
            raise ValueError("Private content")
        result = worker.Worker(self.engine, number_normalizer=fail).handle(request(source="测试2个。"))
        self.assertEqual("numeric_mismatch", result["error"])
        self.assertNotIn("Private", json.dumps(result))

    def test_numeric_normalizer_output_is_revalidated(self):
        for text in ("bad\ntext", "\ud800", "a" * 4097):
            result = worker.Worker(self.engine, number_normalizer=lambda _s, _t: text).handle(request(source="测试2个。"))
            self.assertEqual("invalid_normalized_translation", result["error"])
        result = worker.Worker(self.engine, number_normalizer=lambda _s, _t: "Synthetic 2.").handle(request(source="测试2个。"))
        self.assertEqual("Synthetic 2.", result["translation"])

    def test_terminology_can_change_only_one_word(self):
        def adapter(_s, _h, _t, _e):
            return {"text": "Synthetic memory.", "applied": True, "reason": "aligned_word_memory"}
        result = worker.Worker(self.engine, terminology_adapter=adapter).handle(request(context=True))
        self.assertEqual("Synthetic memory.", result["translation"])
        self.assertTrue(result["diagnostics"]["context_applied"])
        def malicious(_s, _h, _t, _e):
            return {"text": "Entirely invented text.", "applied": True}
        result = worker.Worker(self.engine, terminology_adapter=malicious).handle(request(context=True))
        self.assertEqual("invalid_terminology_result", result["error"])

    def test_terminology_failure_preserves_baseline_without_logging(self):
        def fail(*_args):
            raise RuntimeError("Private synthetic source")
        instance = worker.Worker(self.engine, terminology_adapter=fail)
        result = instance.handle(request(context=True))
        self.assertEqual("Synthetic first.", result["translation"])
        self.assertEqual("adapter_failed", result["diagnostics"]["terminology_reason"])
        self.assertNotIn("Private", json.dumps(result))

    def test_context_false_never_runs_terminology(self):
        calls = []
        instance = worker.Worker(self.engine, terminology_adapter=lambda *args: calls.append(args))
        self.assertIsNone(instance.handle(request(context=False))["error"])
        self.assertEqual([], calls)

    def test_terminology_form_guard_is_conservative(self):
        spec = importlib.util.spec_from_file_location("term_under_test", PROJECT / "worker/terminology.py")
        terms = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(terms)
        for first, second in (("task", "missions"), ("turning", "switch"), ("connected", "connection")):
            self.assertFalse(terms.compatible_forms(first, second))
        self.assertTrue(terms.compatible_forms("task", "mission"))

    def load_fidelity(self):
        spec = importlib.util.spec_from_file_location("fidelity_under_test", PROJECT / "worker/fidelity.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_explicit_negation_requires_english_marker(self):
        fidelity = self.load_fidelity()
        for source in ("先别发送。", "请不要删除。", "他没有收到。", "还没结束。", "不能重试。",
                       "我不会参加。", "尚未确认。", "未能完成。", "这不是结果。", "不应该修改。",
                       "暂时不支持。", "其中不包含附件。", "现在不可用。", "不可以删除。",
                       "我不确定。", "我不知道。", "暂时不打算更新。", "我不想参加。"):
            self.assertTrue(fidelity.requires_negation(source), source)
            with self.assertRaises(ValueError):
                fidelity.validate_negation(source, "Synthetic affirmative result.")
        for output in ("Do not send it.", "It hasn't arrived.", "It isn’t ready.", "This is unavailable.", "Without confirmation.",
                       "I am unsure.", "The result is uncertain.", "The cause is unknown.", "This is unsupported."):
            fidelity.validate_negation("还没有结果。", output)

    def test_lexical_bie_and_positive_idioms_are_not_negation(self):
        fidelity = self.load_fidelity()
        for source in ("别人已经来了。", "这个地方特别安静。", "请分别检查。", "自动识别完成。",
                       "这个方案不错。", "请设置别名。", "这个不要紧。", "你要不要参加？",
                       "你能不能来？", "有没有空？", "会不会下雨？", "是不是这个？", "你知不知道？", "你支不支持？"):
            self.assertFalse(fidelity.requires_negation(source), source)
            fidelity.validate_negation(source, "Synthetic affirmative paraphrase.")

    def test_negation_guard_can_select_close_existing_candidate(self):
        fidelity = self.load_fidelity()
        self.engine.translate_candidates = lambda _s: [
            {"text": "Please send it.", "score": -0.1},
            {"text": "Do not send it.", "score": -0.2}]
        instance = worker.Worker(self.engine, fidelity_guard=fidelity.validate_negation)
        result = instance.handle(request(source="请不要发送。"))
        self.assertEqual("Do not send it.", result["translation"])
        self.assertTrue(result["diagnostics"]["fidelity_baseline_replaced"])
        self.assertEqual(1, result["diagnostics"]["fidelity_candidates_rejected"])

    def test_negation_guard_declines_distant_or_missing_candidate(self):
        fidelity = self.load_fidelity()
        instance = worker.Worker(self.engine, fidelity_guard=fidelity.validate_negation)
        for candidates in ([{"text": "Please send it.", "score": -0.1}],
                           [{"text": "Please send it.", "score": -0.1},
                            {"text": "Do not send it.", "score": -0.9}]):
            self.engine.translate_candidates = lambda _s: candidates
            result = instance.handle(request(source="请不要发送。"))
            self.assertEqual("negation_missing", result["error"])
            self.assertEqual("", result["translation"])

    def test_negation_is_checked_again_after_adaptation(self):
        fidelity = self.load_fidelity()
        self.engine.translate_candidates = lambda _s: [{"text": "Do not send it.", "score": -0.1}]
        adapter = lambda *_: {"text": "Do now send it.", "applied": True}
        instance = worker.Worker(self.engine, fidelity_guard=fidelity.validate_negation,
                                 terminology_adapter=adapter)
        result = instance.handle(request(source="请不要发送。", context=True))
        self.assertEqual("negation_missing", result["error"])

    def test_serving_clears_references_on_eof(self):
        incoming, outgoing = io.BytesIO(), io.BytesIO()
        worker.write_frame(incoming, request())
        incoming.seek(0)
        self.assertEqual(0, self.worker.serve(incoming, outgoing))
        self.assertTrue(self.engine.cleared)
        outgoing.seek(0)
        self.assertEqual(7, worker.read_frame(outgoing)["id"])

    def test_cli_isolated_mode_handles_clear_without_runtime_packages(self):
        incoming = io.BytesIO()
        worker.write_frame(incoming, {"id": 9, "op": "clear"})
        result = subprocess.run([sys.executable, "-I", "-u", str(PROJECT / "worker/transime_worker.py"),
                                 "--model-dir", "/nonexistent-transime-model", "--threads", "1"],
                                input=incoming.getvalue(), capture_output=True)
        self.assertEqual(0, result.returncode)
        self.assertEqual(b"", result.stderr)
        self.assertTrue(worker.read_frame(io.BytesIO(result.stdout))["diagnostics"]["cleared"])


if __name__ == "__main__":
    unittest.main()
