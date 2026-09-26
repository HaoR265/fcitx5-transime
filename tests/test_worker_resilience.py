"""Synthetic failure/privacy tests, not translation-quality or GUI claims."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import unittest

spec = importlib.util.spec_from_file_location(
    "resilience_worker", Path(__file__).resolve().parents[1] / "worker/transime_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Engine:
    def __init__(self):
        self.last_diagnostics = {}
        self.clear_count = 0
        self.fail = False

    def translate_candidates(self, source):
        if self.fail:
            raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION " + source)
        return [{"text": "Synthetic safe result.", "score": -0.1}]

    def clear(self):
        self.clear_count += 1


def request(identifier=1, **kwargs):
    return dict(id=identifier, source="合成测试文本", history=[], context=False, **kwargs)


class WorkerResilienceTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine()
        self.instance = worker.Worker(self.engine)

    def serve(self, data, output=None):
        out = output if output is not None else io.BytesIO()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = self.instance.serve(io.BytesIO(data), out)
        self.assertEqual("", stderr.getvalue())
        self.assertEqual(1, self.engine.clear_count)
        return code, out

    def frame(self, value):
        out = io.BytesIO()
        worker.write_frame(out, value)
        return out.getvalue()

    def test_malformed_frame_never_echoes_or_processes_following_request(self):
        malformed = b'{"source":"SYNTHETIC_PRIVATE_PAYLOAD",'
        code, output = self.serve(struct.pack(">I", len(malformed)) + malformed + self.frame(request()))
        self.assertEqual(2, code)
        self.assertEqual(b"", output.getvalue())

    def test_backend_exception_is_redacted_and_next_request_recovers(self):
        self.engine.fail = True
        result = self.instance.handle(request())
        self.assertEqual({"id": 1, "translation": "", "error": "inference_failed"}, result)
        self.engine.fail = False
        result = self.instance.handle(request(2))
        self.assertIsNone(result["error"])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_bad_request_identity_cannot_be_reused_from_previous_request(self):
        self.assertEqual(42, self.instance.handle(request(42))["id"])
        self.assertEqual(0, self.instance.handle(request(True))["id"])
        self.assertEqual(43, self.instance.handle(request(43))["id"])

    def test_context_is_per_request_and_clear_drops_engine_state(self):
        seen = []
        def rerank(source, history, candidates):
            seen.append([dict(record) for record in history])
            return {"text": candidates[0]["text"], "selected_index": 0}
        self.instance = worker.Worker(self.engine, rerank, context_policy="lexical")
        first = request()
        first.update(context=True, history=[{"source": None, "committed": "SYNTHETIC_PRIVATE_HISTORY", "language": "en"}])
        response = self.instance.handle(first)
        self.assertNotIn("PRIVATE", json.dumps(response))
        self.instance.handle({"id": 2, "op": "clear"})
        second = request(3)
        second["context"] = True
        self.instance.handle(second)
        self.assertEqual([], seen[1])
        self.assertEqual(1, self.engine.clear_count)
        self.assertNotIn("history", vars(self.instance))
        self.assertNotIn("source", vars(self.instance))

    def test_broken_output_clears_worker(self):
        class BrokenOutput:
            def write(self, data):
                raise BrokenPipeError("SYNTHETIC_PRIVATE_IO_ERROR")
        code, _ = self.serve(self.frame(request()), BrokenOutput())
        self.assertEqual(3, code)

    def test_many_requests_have_distinct_ids_and_no_text_cache(self):
        code, output = self.serve(b"".join(self.frame(request(i)) for i in range(100)))
        self.assertEqual(0, code)
        output.seek(0)
        for identifier in range(100):
            result = worker.read_frame(output)
            self.assertEqual(identifier, result["id"])
            self.assertEqual(0, result["diagnostics"]["text_cache_entries"])
        self.assertIsNone(worker.read_frame(output))


if __name__ == "__main__":
    unittest.main()
