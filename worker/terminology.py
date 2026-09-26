"""Experimental one-word translation memory, aligned and rescored by local NMT.

No case files, dictionaries of expected translations, or network services are
used. Source SentencePiece units propose shared Chinese terms; model attention
aligns them with an actual historical English word. This is a conservative
heuristic, not evidence of general contextual or grammatical understanding.
"""

from __future__ import annotations

import math
import re
import statistics
import time

VERSION = "attention-term-memory-v1"
MAX_SCORE_GAP = 0.30
MIN_ALIGNMENT = 0.50
MAX_HISTORY = 2
MAX_SOURCE_BYTES = 1024
MAX_MEMORY_BYTES = 512
MAX_TARGET_TOKENS = 128
GENERIC_VERBS = {"安装", "更新", "关闭", "打开", "完成", "创建", "删除", "修改",
                 "发送", "测试", "运行", "启动", "停止", "知道", "认为", "讨论",
                 "翻译", "处理", "进行", "开始", "结束", "使用", "帮助", "准备"}
NUMBER_WORDS = set("zero eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million billion percent".split())


def compatible_forms(current, historical):
    """Reject obvious suffix mismatches; this is not English morphology/POS."""
    current, historical = current.lower(), historical.lower()
    return all(current.endswith(suffix) == historical.endswith(suffix)
               for suffix in ("s", "ing", "ed"))


def _source_tokens(engine, source):
    normalized = engine.normalizer(source)
    return normalized, engine.source_spm.encode(normalized, out_type=str) + ["</s>"]


def _terms(engine, source, history_source, rules):
    source = engine.normalizer(source)
    history_source = engine.normalizer(history_source)
    found = []
    generic = rules.GENERIC_ANCHORS | GENERIC_VERBS
    for piece in engine.source_spm.encode(source, out_type=str):
        term = piece.lstrip("▁")
        # Trim grammatical material fused into a source vocabulary unit. This
        # does not claim SentencePiece is a Chinese word segmenter.
        term = re.sub(r"^(?:这个|那个|这些|那些)", "", term)
        term = term.strip("的了是也都把请")
        if (re.fullmatch(r"[\u3400-\u9fff]{2,4}", term) and term not in generic
                and source.count(term) == 1 and history_source.count(term) == 1
                and term not in found):
            found.append(term)
    return found[:3]


def _alignment(engine, source, target):
    """Teacher-force the existing sentence to obtain its attention, no rewrite."""
    normalized, source_tokens = _source_tokens(engine, source)
    if len(source_tokens) > 512:
        return None
    target_tokens = engine.target_spm.encode(target, out_type=str)
    if not target_tokens or len(target_tokens) > MAX_TARGET_TOKENS:
        return None
    prefix = target_tokens + ["</s>"]
    result = engine.translator.translate_batch(
        [source_tokens], target_prefix=[prefix], beam_size=1,
        max_input_length=0, max_decoding_length=len(prefix) + 1,
        return_attention=True, return_end_token=True)[0]
    if (result.hypotheses[0][:len(prefix)] != prefix
            or len(result.attention[0]) < len(target_tokens)):
        return None
    return normalized, target_tokens, result.attention[0][:len(target_tokens)]


def _aligned_word(engine, term, aligned, rules):
    source, target_tokens, attention = aligned
    begin = source.index(term)
    end = begin + len(term)
    source_pieces = engine.source_spm.encode(source, out_type="immutable_proto").pieces
    source_indices = [i for i, p in enumerate(source_pieces) if p.begin < end and p.end > begin]
    groups = []
    for i, token in enumerate(target_tokens):
        if token.startswith("▁") or not groups:
            groups.append([i])
        else:
            groups[-1].append(i)
    words = []
    for group in groups:
        word = engine.target_spm.decode([target_tokens[i] for i in group]).strip().rstrip(".,;:!?")
        if (re.fullmatch(r"[A-Za-z]{3,24}", word) is None
                or word.lower() in rules.STOP | NUMBER_WORDS):
            continue
        # Weight subwords by their lexical character count. Punctuation and
        # short inflection pieces must not dominate the stem's alignment.
        lexical = [(i, len(re.sub("[^A-Za-z]", "", target_tokens[i]))) for i in group]
        lexical = [(i, n) for i, n in lexical if n]
        strength = sum(n * sum(attention[i][j] for j in source_indices)
                       / max(1e-9, sum(attention[i][:-1])) for i, n in lexical) / sum(n for i, n in lexical)
        if math.isfinite(strength) and strength >= MIN_ALIGNMENT:
            words.append((strength, word))
    if not words:
        return None
    # Multiple different aligned content words mean this transfer is ambiguous.
    if len({word.lower() for _, word in words}) != 1:
        return None
    return max(words)[1]


def _score(engine, source, target):
    _, source_tokens = _source_tokens(engine, source)
    target_tokens = engine.target_spm.encode(target, out_type=str)
    # score_batch appends EOS itself; adding it here would score EOS twice.
    result = engine.translator.score_batch([source_tokens], [target_tokens], max_input_length=0)[0]
    score = statistics.mean(result.log_probs)
    return score if math.isfinite(score) else None


def adapt(source, history, baseline, engine, rules, *, apply=False):
    """Return text and non-content diagnostics; default only observes a proposal.

    At most one word can change. All other characters remain exactly baseline.
    An inferred Chinese-history hint gets half the score-gap budget and is used
    only when no relevant actual English pair exists. No text is retained.
    """
    result = {"text": baseline, "proposed": False, "applied": False,
              "reason": "no_relevant_terminology", "score_gap": None,
              "provisional": False, "elapsed_ms": 0.0}
    started = time.perf_counter()
    try:
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            result["reason"] = "source_limit"
            return result
        memories = []
        for entry in reversed(history[-5:]):
            if (entry.get("language") == "en" and isinstance(entry.get("source"), str)
                    and isinstance(entry.get("committed"), str)
                    and len(entry["source"].encode("utf-8")) <= MAX_MEMORY_BYTES
                    and len(entry["committed"].encode("utf-8")) <= MAX_MEMORY_BYTES):
                terms = _terms(engine, source, entry["source"], rules)
                if terms:
                    memories.append((entry, terms))
        actual = [m for m in memories if not m[0].get("provisional", False)]
        memories = (actual or memories)[:MAX_HISTORY]
        if not memories:
            return result
        current_alignment = _alignment(engine, source, baseline)
        if current_alignment is None:
            result["reason"] = "alignment_limit"
            return result
        baseline_score = None
        for entry, terms in memories:
            historical_alignment = _alignment(engine, entry["source"], entry["committed"])
            if historical_alignment is None:
                continue
            for term in terms:
                current_word = _aligned_word(engine, term, current_alignment, rules)
                history_word = _aligned_word(engine, term, historical_alignment, rules)
                if not current_word or not history_word or current_word.lower() == history_word.lower():
                    continue
                if not compatible_forms(current_word, history_word):
                    continue
                old_pattern = r"\b" + re.escape(current_word) + r"\b"
                if len(re.findall(old_pattern, baseline)) != 1:
                    continue
                if not re.search(r"\b" + re.escape(history_word) + r"\b", entry["committed"]):
                    continue
                if history_word[0].isupper() and current_word[0].islower():
                    continue  # Do not turn a generic word into a guessed name.
                replacement = history_word.capitalize() if current_word[0].isupper() else history_word
                candidate = re.sub(old_pattern, replacement, baseline, count=1)
                if rules._signature(candidate) != rules._signature(baseline):
                    continue
                if baseline_score is None:
                    baseline_score = _score(engine, source, baseline)
                candidate_score = _score(engine, source, candidate)
                if baseline_score is None or candidate_score is None:
                    continue
                gap = baseline_score - candidate_score
                provisional = bool(entry.get("provisional", False))
                if gap > MAX_SCORE_GAP * (0.5 if provisional else 1.0):
                    continue
                result.update(text=candidate if apply else baseline, proposed=True,
                              applied=apply, reason="aligned_word_memory" if apply else "proposal_only",
                              score_gap=round(gap, 6), provisional=provisional)
                return result
        result["reason"] = "alignment_or_score_guard"
        return result
    finally:
        result["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
