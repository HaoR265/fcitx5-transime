#!/usr/bin/env python3
"""Offline CPU NMT worker. Stdout is length-framed JSON only; no text logging.

PyTorch and Transformers are conversion-time dependencies and are never imported
here. Model files must already exist locally. A source-only Marian model does
not understand conversation history; optional local memory may reorder its
candidates or adapt one aligned content word, with bounded model rescoring.
"""

from __future__ import annotations

import argparse
import gc
import importlib.util
import json
import math
import os
import re
import struct
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, BinaryIO

MAX_FRAME_BYTES = 65536
MAX_SOURCE_BYTES = 4096
MAX_TRANSLATION_BYTES = 4096
MAX_HISTORY_BYTES = 2048
MAX_HISTORY_ENTRIES = 5
MAX_ID = (1 << 64) - 1
MAX_SOURCE_TOKENS = 512
MAX_TARGET_TOKENS = 256
MAX_FIDELITY_SCORE_GAP = 0.30


class ProtocolError(Exception):
    """Only fixed error codes are emitted, never exception text or user content."""


class TranslationError(Exception):
    pass


def valid_text(value: Any, limit: int) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value):
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= limit
    except UnicodeError:
        return False


def valid_id(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_ID


def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ProtocolError("duplicate_json_key")
        value[key] = item
    return value


def reject_constant(_value: str) -> None:
    raise ProtocolError("nonfinite_json_number")


def read_exact(stream: BinaryIO, count: int, *, allow_eof: bool = False) -> bytes | None:
    data = bytearray()
    while len(data) < count:
        chunk = stream.read(count - len(data))
        if not chunk:
            if allow_eof and not data:
                return None
            raise ProtocolError("truncated_frame")
        data.extend(chunk)
    return bytes(data)


def read_frame(stream: BinaryIO) -> dict[str, Any] | None:
    header = read_exact(stream, 4, allow_eof=True)
    if header is None:
        return None
    length = struct.unpack(">I", header)[0]
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ProtocolError("invalid_frame_length")
    payload = read_exact(stream, length)
    try:
        value = json.loads(payload.decode("utf-8", errors="strict"),
                           object_pairs_hook=reject_duplicate_keys, parse_constant=reject_constant)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise ProtocolError("invalid_json") from error
    if not isinstance(value, dict):
        raise ProtocolError("invalid_request_object")
    return value


def write_frame(stream: BinaryIO, value: dict[str, Any]) -> None:
    payload = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if not 0 < len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("response_size_limit")
    stream.write(struct.pack(">I", len(payload)))
    stream.write(payload)
    stream.flush()


def validate_request(request: dict[str, Any]) -> None:
    if not valid_id(request.get("id")):
        raise ProtocolError("invalid_id")
    operation = request.get("op", "translate")
    if operation == "clear":
        return
    if operation != "translate":
        raise ProtocolError("unsupported_operation")
    if not valid_text(request.get("source"), MAX_SOURCE_BYTES):
        raise ProtocolError("invalid_source")
    if type(request.get("context", False)) is not bool:
        raise ProtocolError("invalid_context_flag")
    history = request.get("history", [])
    if not isinstance(history, list) or len(history) > MAX_HISTORY_ENTRIES:
        raise ProtocolError("invalid_history")
    byte_count = 0
    for record in history:
        if not isinstance(record, dict):
            raise ProtocolError("invalid_history")
        source = record.get("source")
        committed = record.get("committed")
        language = record.get("language")
        if source is not None and not valid_text(source, MAX_HISTORY_BYTES):
            raise ProtocolError("invalid_history")
        if not valid_text(committed, MAX_HISTORY_BYTES):
            raise ProtocolError("invalid_history")
        if not isinstance(language, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,32}", language) is None:
            raise ProtocolError("invalid_history")
        byte_count += len((source or "").encode("utf-8")) + len(committed.encode("utf-8")) + len(language.encode("ascii"))
    if byte_count > MAX_HISTORY_BYTES:
        raise ProtocolError("history_size_limit")


class Engine:
    """A lazy, source-only Marian encoder/decoder using CTranslate2 CPU INT8."""

    def __init__(self, model_dir: Path, threads: int = 2, beam_size: int = 4):
        self.model_dir = Path(model_dir)
        self.threads = threads
        self.beam_size = beam_size
        self.translator = None
        self.source_spm = None
        self.target_spm = None
        self.normalizer = None
        self.last_diagnostics: dict[str, Any] = {}

    def load(self) -> None:
        if self.translator is not None:
            return
        required = ("model.bin", "config.json", "source.spm", "target.spm")
        if not all((self.model_dir / filename).is_file() for filename in required):
            raise TranslationError("model_files_missing")
        started = time.perf_counter()
        # CPU-only, local-file APIs: no Hugging Face client or network import.
        import ctranslate2
        import sentencepiece
        from sacremoses import MosesPunctNormalizer

        if "int8" not in ctranslate2.get_supported_compute_types("cpu"):
            raise TranslationError("cpu_int8_unavailable")
        source_spm = sentencepiece.SentencePieceProcessor(model_file=str(self.model_dir / "source.spm"))
        target_spm = sentencepiece.SentencePieceProcessor(model_file=str(self.model_dir / "target.spm"))
        translator = ctranslate2.Translator(str(self.model_dir), device="cpu", compute_type="int8",
                                           inter_threads=1, intra_threads=self.threads, max_queued_batches=1)
        self.source_spm = source_spm
        self.target_spm = target_spm
        self.normalizer = MosesPunctNormalizer(lang="zho").normalize
        self.translator = translator
        self.last_diagnostics["model_load_ms"] = round((time.perf_counter() - started) * 1000, 3)

    def translate_candidates(self, source: str) -> list[dict[str, Any]]:
        cold = self.translator is None
        self.last_diagnostics = {"cold_start": cold, "model_load_ms": 0.0,
                                 "compute_type_requested": "int8", "device": "cpu",
                                 "threads": self.threads, "beam_size": self.beam_size}
        self.load()
        tokens = self.source_spm.encode(self.normalizer(source), out_type=str) + ["</s>"]
        if len(tokens) > MAX_SOURCE_TOKENS:
            raise TranslationError("source_token_limit")
        started = time.perf_counter()
        result = self.translator.translate_batch(
            [tokens], beam_size=self.beam_size, num_hypotheses=self.beam_size,
            max_input_length=0, max_decoding_length=MAX_TARGET_TOKENS,
            return_scores=True, return_end_token=True, replace_unknowns=False)[0]
        candidates: list[dict[str, Any]] = []
        for token_list, score in zip(result.hypotheses, result.scores):
            # Never silently commit a translation truncated by the generation cap.
            if not token_list or token_list[-1] != "</s>" or "<unk>" in token_list:
                continue
            filtered = [token for token in token_list if token not in {"</s>", "<s>", "<pad>"}]
            text = self.target_spm.decode(filtered).strip()
            if valid_text(text, MAX_TRANSLATION_BYTES) and math.isfinite(float(score)):
                candidates.append({"text": text, "score": float(score)})
        self.last_diagnostics.update({"inference_ms": round((time.perf_counter() - started) * 1000, 3),
                                      "source_tokens": len(tokens), "hypothesis_count": len(candidates),
                                      "compute_type": self.translator.compute_type})
        if not candidates:
            raise TranslationError("no_valid_translation")
        candidates.sort(key=lambda candidate: candidate["score"], reverse=True)
        return candidates

    def clear(self) -> None:
        if self.translator is not None:
            self.translator.unload_model()
        self.translator = None
        self.source_spm = None
        self.target_spm = None
        self.normalizer = None
        self.last_diagnostics = {}
        gc.collect()


def load_local_module(name: str):
    path = Path(__file__).with_name(name + ".py")
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("transime_local_" + name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Worker:
    def __init__(self, engine: Any, reranker=None, history_selector=None, number_normalizer=None,
                 terminology_adapter=None, fidelity_guard=None, *, context_policy="aligned"):
        if context_policy not in {"aligned", "lexical"}:
            raise ValueError("invalid_context_policy")
        self.engine = engine
        self.context_policy = context_policy
        self.reranker = reranker
        self.history_selector = history_selector
        self.number_normalizer = number_normalizer
        self.terminology_adapter = terminology_adapter
        self.fidelity_guard = fidelity_guard

    def prepare_history(self, source: str, history: list[dict[str, Any]]):
        """Translate at most two relevant Chinese commits, without retaining them.

        These provisional hints were not chosen by the user. Context rules must
        keep that distinction; the raw history and the current source stay intact.
        """
        prepared = list(history)
        count = 0
        started = time.perf_counter()
        if self.history_selector is not None:
            indices = self.history_selector(source, history)
            if not isinstance(indices, list) or len(indices) > 2:
                raise TranslationError("invalid_history_selection")
            for index in dict.fromkeys(indices):
                if type(index) is not int or not 0 <= index < len(history):
                    raise TranslationError("invalid_history_selection")
                entry = history[index]
                if (entry["language"] not in {"zh", "und"}
                        or len(entry["committed"].encode("utf-8")) > 512):
                    raise TranslationError("invalid_history_selection")
                try:
                    candidates = self.engine.translate_candidates(entry["committed"])
                    text = candidates[0]["text"]
                    if not valid_text(text, MAX_TRANSLATION_BYTES):
                        continue
                    prepared[index] = {"source": entry["committed"], "committed": text,
                                       "language": "en", "provisional": True}
                    count += 1
                except Exception:
                    # An unusable historical hint must not break current NMT.
                    continue
        return prepared, count, round((time.perf_counter() - started) * 1000, 3)

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        identifier = request.get("id") if valid_id(request.get("id")) else 0
        try:
            validate_request(request)
            if request.get("op") == "clear":
                self.engine.clear()
                return {"id": identifier, "translation": "", "error": None,
                        "diagnostics": {"cleared": True, "model_loaded": False}}
            candidates = self.engine.translate_candidates(request["source"])
            if not candidates or not valid_text(candidates[0].get("text"), MAX_TRANSLATION_BYTES):
                raise TranslationError("no_valid_translation")
            fidelity_rejected = 0
            fidelity_replaced = False
            if self.fidelity_guard is not None:
                original = candidates
                candidates = []
                for candidate in original:
                    try:
                        self.fidelity_guard(request["source"], candidate["text"])
                    except ValueError:
                        fidelity_rejected += 1
                        continue
                    candidates.append(candidate)
                if not candidates:
                    raise TranslationError("negation_missing")
                if candidates[0] is not original[0]:
                    candidates = [candidate for candidate in candidates
                                  if original[0]["score"] - candidate["score"] <= MAX_FIDELITY_SCORE_GAP]
                    if not candidates:
                        raise TranslationError("negation_missing")
                    fidelity_replaced = True
            chosen = 0
            context_applied = False
            diagnostics = dict(self.engine.last_diagnostics)
            history_count, history_ms = 0, 0.0
            history = request.get("history", [])
            if request.get("context", False) and self.reranker is not None:
                # Continuous composition already supplies the entire editable
                # paragraph to the encoder. Reuse known committed terminology
                # cheaply; do not retranslate past Chinese for each row/press.
                if self.context_policy == "aligned":
                    history, history_count, history_ms = self.prepare_history(
                        request["source"], request.get("history", []))
                selected = self.reranker(request["source"], history, candidates)
                if not isinstance(selected, dict) or type(selected.get("selected_index")) is not int:
                    raise TranslationError("invalid_context_result")
                chosen = selected["selected_index"]
                if not 0 <= chosen < len(candidates) or selected.get("text") != candidates[chosen]["text"]:
                    raise TranslationError("invalid_context_result")
                context_applied = bool(selected.get("context_applied", False))
            diagnostics.update({"context_requested": request.get("context", False),
                                "context_policy": self.context_policy,
                                "context_available": self.reranker is not None,
                                "context_applied": context_applied,
                                "selected_index": chosen, "text_cache_entries": 0,
                                "provisional_history_count": history_count,
                                "history_inference_ms": history_ms,
                                "fidelity_candidates_rejected": fidelity_rejected,
                                "fidelity_baseline_replaced": fidelity_replaced})
            translation = candidates[chosen]["text"]
            diagnostics.update(terminology_available=self.terminology_adapter is not None,
                               terminology_applied=False)
            if (request.get("context", False) and self.terminology_adapter is not None
                    and self.context_policy == "aligned"):
                try:
                    adapted = self.terminology_adapter(request["source"], history, translation, self.engine)
                except Exception:
                    # Optional memory failure preserves the valid current NMT.
                    adapted = {"text": translation, "applied": False, "reason": "adapter_failed"}
                replacement = adapted.get("text") if isinstance(adapted, dict) else None
                if not valid_text(replacement, MAX_TRANSLATION_BYTES):
                    raise TranslationError("invalid_terminology_result")
                if replacement != translation:
                    before = re.split(r"([A-Za-z]+)", translation)
                    after = re.split(r"([A-Za-z]+)", replacement)
                    changes = [(a, b) for a, b in zip(before, after) if a != b]
                    if (not adapted.get("applied", False) or len(before) != len(after)
                            or len(changes) != 1
                            or not all(re.fullmatch(r"[A-Za-z]{3,24}", text) for text in changes[0])):
                        raise TranslationError("invalid_terminology_result")
                    translation = replacement
                    diagnostics["context_applied"] = True
                    diagnostics["terminology_applied"] = True
                allowed_reasons = {"no_relevant_terminology", "source_limit", "alignment_limit",
                                   "aligned_word_memory", "proposal_only", "alignment_or_score_guard", "adapter_failed"}
                reason = adapted.get("reason")
                diagnostics["terminology_reason"] = reason if reason in allowed_reasons else "unknown"
                for field in ("score_gap", "elapsed_ms"):
                    value = adapted.get(field)
                    if type(value) in (int, float) and math.isfinite(value):
                        diagnostics["terminology_" + field] = value
            if re.search(r"[0-9]", request["source"]):
                if self.number_normalizer is None:
                    raise TranslationError("numeric_guard_unavailable")
                try:
                    translation = self.number_normalizer(request["source"], translation)
                except ValueError as error:
                    raise TranslationError("numeric_mismatch") from error
                if not valid_text(translation, MAX_TRANSLATION_BYTES):
                    raise TranslationError("invalid_normalized_translation")
            if self.fidelity_guard is not None:
                try:
                    self.fidelity_guard(request["source"], translation)
                except ValueError as error:
                    raise TranslationError("negation_missing") from error
            return {"id": identifier, "translation": translation,
                    "error": None, "diagnostics": diagnostics}
        except (ProtocolError, TranslationError) as error:
            return {"id": identifier, "translation": "", "error": str(error)}
        except Exception:
            # Third-party exception strings can contain input text or file paths.
            return {"id": identifier, "translation": "", "error": "inference_failed"}

    def serve(self, incoming: BinaryIO, outgoing: BinaryIO) -> int:
        try:
            while True:
                request = read_frame(incoming)
                if request is None:
                    return 0
                response = self.handle(request)
                write_frame(outgoing, response)
                del response
                del request
        except ProtocolError:
            # Bad framing loses request identity: terminate rather than guess IDs.
            return 2
        except (BrokenPipeError, OSError):
            return 3
        finally:
            self.engine.clear()


def main() -> int:
    # -I ignores PYTHONDONTWRITEBYTECODE; keep installed source directories clean.
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", "--model", dest="model_dir", type=Path, required=True)
    parser.add_argument("--threads", type=int, choices=(1, 2), default=2)
    parser.add_argument("--beam-size", type=int, choices=(1, 2, 4), default=4)
    parser.add_argument("--context-policy", choices=("aligned", "lexical"), default="aligned")
    args = parser.parse_args()
    # No online fallback. These also prevent accidental remote library behavior.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["CUDA_VISIBLE_DEVICES"] = ""
    os.environ["OMP_WAIT_POLICY"] = "PASSIVE"
    os.environ["KMP_BLOCKTIME"] = "0"
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    try:
        context_module = load_local_module("context")
    except Exception:
        context_module = None
    try:
        # Avoid shadowing Python's standard-library ``numbers`` module. NumPy
        # imports it while CTranslate2 starts, so a local numbers.py breaks the
        # real model on a clean Windows Python installation.
        numbers_module = load_local_module("number_normalizer")
    except Exception:
        numbers_module = None
    try:
        terminology_module = load_local_module("terminology")
        adapt = getattr(terminology_module, "adapt", None)
        terminology_adapter = (lambda source, history, baseline, engine:
                               adapt(source, history, baseline, engine, context_module, apply=True)) \
            if callable(adapt) and context_module is not None else None
    except Exception:
        terminology_adapter = None
    try:
        fidelity_module = load_local_module("fidelity")
        fidelity_guard = getattr(fidelity_module, "validate_negation", None)
    except Exception:
        fidelity_guard = None
    if not callable(fidelity_guard):
        def fidelity_guard(_source, _translation):
            raise TranslationError("fidelity_guard_unavailable")
    worker = Worker(Engine(args.model_dir, args.threads, args.beam_size),
                    reranker=getattr(context_module, "rerank", None),
                    history_selector=getattr(context_module, "chinese_history_candidates", None),
                    number_normalizer=getattr(numbers_module, "normalize_numbers", None),
                    terminology_adapter=terminology_adapter, fidelity_guard=fidelity_guard,
                    context_policy=args.context_policy)
    return worker.serve(sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    sys.exit(main())
