"""Conservative short-lived translation-memory reranking, no extra model.

History is never sent to the sentence NMT encoder and never appended to output.
This only selects among existing NMT hypotheses. It is not document-level NMT.
Parameters are fixed before held-out evaluation; no evaluation cases are read.
"""

from __future__ import annotations

import math
import re
from collections import Counter


VERSION = "lexical-memory-v1"
MAX_SCORE_GAP = 0.18
MAX_BONUS = 0.18
WORDS = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
NUMBERS = re.compile(r"\d+(?:[.,]\d+)*%?")
CJK = re.compile(r"[\u3400-\u9fff]+")
STOP = set("""a an the this that these those it its they're their there here
i me my mine we us our you your he him his she her they them theirs
am is are was were be been being do does did done have has had having
will would shall should can could may might must need needs needed
to of in on at by for from with about as into out over under between
and or but if then than so because while when where what which who
how why all any some each every both either another other same such
not no never don't doesn't didn't won't isn't aren't wasn't weren't
hasn't haven't hadn't can't couldn't wouldn't shouldn't mustn't
yes please just already still yet now later before after first last
get gets got getting make makes made making use uses used using
go goes went going come comes came coming put puts take takes took
taken want wants wanted know knows known think thinks thought say
says said see sees seen look looks looking also too very more most
less much many little few good better best bad worse only even
up down off again back away through today tomorrow yesterday
one two three four five six seven eight nine ten something anything
everything nothing someone anyone everyone things thing time times
new old well right wrong way ways part parts change changes changed
""".split())
NEGATION = {"not", "no", "never", "don't", "doesn't", "didn't", "won't",
            "isn't", "aren't", "wasn't", "weren't", "hasn't", "haven't",
            "hadn't", "can't", "cannot", "couldn't", "wouldn't", "shouldn't"}
MODALS = {"may", "might", "could", "must", "should", "will", "would", "can"}
PRONOUNS = {"he", "she", "they", "we", "i", "you", "him", "her", "them", "us", "me"}
GENERIC_ANCHORS = {"这个", "那个", "这些", "那些", "我们", "你们", "他们", "她们",
                   "需要", "可以", "已经", "应该", "一下", "问题", "没有", "什么",
                   "还是", "不是", "现在", "今天", "明天", "昨天", "时候", "然后",
                   "这样", "那样", "这边", "那边", "怎么", "事情"}


def _words(text: str) -> list[str]:
    return WORDS.findall(text.lower().replace("’", "'"))


def _content(text: str) -> set[str]:
    # Deliberately avoid stemming names and negations. Exact lexical recurrence
    # misses inflections, but does not invent an alignment or change a word.
    return {w for w in _words(text) if w not in STOP and len(w) > 2}


def _anchors(text: str) -> set[str]:
    return {run[i:i + 2] for run in CJK.findall(text)
            for i in range(len(run) - 1) if run[i:i + 2] not in GENERIC_ANCHORS}


def _signature(text: str) -> tuple:
    words = _words(text)
    return (Counter(NUMBERS.findall(text)), bool(set(words) & NEGATION),
            frozenset(set(words) & MODALS), frozenset(set(words) & PRONOUNS))


def chinese_history_candidates(source: str, history: list[dict]) -> list[int]:
    """Indices of at most two relevant ordinary Chinese commits to translate.

The worker can obtain provisional English lexical memory with the *same* NMT;
it must distinguish that from a user-selected English translation. No external
history is read and translation is useful only for shared source content.
"""
    anchors = _anchors(source)
    indices = []
    for index in range(len(history) - 1, max(-1, len(history) - 6), -1):
        entry = history[index]
        text = entry.get("committed")
        if (entry.get("language") in ("zh", "und") and isinstance(text, str)
                and len(text.encode("utf-8")) <= 512 and anchors & _anchors(text)
                and not WORDS.search(text)):
            indices.append(index)
            if len(indices) == 2:
                break
    return indices


def rerank(source: str, history: list[dict], hypotheses: list[dict]) -> dict:
    """Return an exact hypothesis, with diagnostics that contain no input text.

Only English *committed* history with known Chinese source participates. Source
overlap gates lexical reuse. A small score margin plus number/negation/modal/
pronoun guards avoids letting memory override substantially better NMT output.
These guards are heuristic; they cannot prove semantic equivalence.
"""
    if not hypotheses:
        raise ValueError("empty_hypotheses")
    for item in hypotheses:
        if (not isinstance(item.get("text"), str) or not item["text"].strip()
                or not isinstance(item.get("score"), (int, float))
                or not math.isfinite(item["score"])):
            raise ValueError("invalid_hypothesis")
    best = max(range(len(hypotheses)), key=lambda i: hypotheses[i]["score"])
    baseline = hypotheses[best]
    result = {"text": baseline["text"], "selected_index": best,
              "context_applied": False, "reason": "no_reusable_lexical_memory"}
    source_anchors = _anchors(source)
    if not source_anchors:
        return result
    memories, provisional_memories = [], []
    for age, entry in enumerate(reversed(history[-5:])):
        if (entry.get("language") != "en" or not isinstance(entry.get("source"), str)
                or not isinstance(entry.get("committed"), str)):
            continue
        overlap = source_anchors & _anchors(entry["source"])
        terms = _content(entry["committed"])
        if overlap and terms:
            weight = (0.8 ** age) * min(1.0, len(overlap) / max(1, len(source_anchors) * 0.4))
            if entry.get("provisional") is True:
                provisional_memories.append((terms, weight * 0.5))
            else:
                memories.append((terms, weight))
    # A model's provisional translation is weaker evidence than English the
    # user actually selected and committed. It never overrides that memory.
    memories = memories or provisional_memories
    if not memories:
        return result
    base_signature = _signature(baseline["text"])
    candidates = []
    for i, item in enumerate(hypotheses):
        if (baseline["score"] - item["score"] > MAX_SCORE_GAP
                or _signature(item["text"]) != base_signature):
            continue
        terms = _content(item["text"])
        # Reward at most two recurring content words. Long histories or repeated
        # words cannot accumulate unbounded bias. Latest relevant history wins.
        relevance = max((min(1.0, len(terms & known) / 2) * weight
                         for known, weight in memories), default=0.0)
        candidates.append((item["score"] + MAX_BONUS * relevance, -i, i))
    chosen = max(candidates)[2] if candidates else best
    result.update(text=hypotheses[chosen]["text"], selected_index=chosen,
                  context_applied=chosen != best,
                  reason="lexical_memory_rerank" if chosen != best else "baseline_retained")
    return result
