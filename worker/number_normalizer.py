"""Preserve explicit Arabic numeric literals after NMT, or decline the result.

This is a format/fidelity guard, not a proof of units, ordering, or meaning. It
never changes a numeric value to make a translation pass. Chinese written-out
numbers are outside its scope. Ambiguous fractions/ordinals remain unsupported.
"""
from __future__ import annotations

from collections import Counter, defaultdict, deque
from decimal import Decimal
import re

NUMBER = re.compile(r"(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?", re.ASCII)
SMALL = dict(zip("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(), range(20)))
TENS = dict(zip("twenty thirty forty fifty sixty seventy eighty ninety".split(), range(20, 100, 10)))
SCALES = {"thousand": 1000, "million": 1000000, "billion": 1000000000}
WORD = "(?:" + "|".join([*SMALL, *TENS, *SCALES, "hundred"]) + ")"
CARDINAL = re.compile(r"\b" + WORD + r"(?:[ -]+(?:and[ -]+)?" + WORD + r")*\b", re.IGNORECASE)
PERCENT = re.compile(r"(\d+(?:\.\d+)?)[ -]+(?:per[ -]+cent|percent)\b", re.IGNORECASE)


def canonical(token: str) -> tuple[Decimal, bool]:
    return Decimal(token.rstrip("%").replace(",", "")), token.endswith("%")


def cardinal_value(text: str) -> int | None:
    words = text.lower().replace("-", " ").split()
    total = group = 0
    previous = None
    last_scale = 10**12
    for word in words:
        if word == "and":
            if previous not in ("hundred", "scale"):
                return None
            previous = "and"
        elif word in SMALL:
            value = SMALL[word]
            if previous not in (None, "hundred", "scale", "and", "tens"):
                return None
            if previous == "tens" and not 0 < value < 10:
                return None
            group += value
            previous = "small"
        elif word in TENS:
            if previous not in (None, "hundred", "scale", "and"):
                return None
            group += TENS[word]
            previous = "tens"
        elif word == "hundred":
            if previous != "small" or not 0 < group < 10:
                return None
            group *= 100
            previous = "hundred"
        elif word in SCALES:
            scale = SCALES[word]
            if not 0 < group < 1000 or scale >= last_scale:
                return None
            total += group * scale
            group = 0
            last_scale = scale
            previous = "scale"
        else:
            return None
    return total + group if previous != "and" else None


def normalize_numbers(source: str, translation: str) -> str:
    literals = NUMBER.findall(source)
    if not literals:
        return translation
    expected = Counter(canonical(token) for token in literals)
    def replace_cardinal(match):
        value = cardinal_value(match.group())
        # Check additional written-out quantities too. For mixed Chinese
        # number spellings or a pronominal "one", this can conservatively
        # decline a correct translation; it must not hide an added quantity.
        return str(value) if value is not None else match.group()
    normalized = CARDINAL.sub(replace_cardinal, translation)
    normalized = PERCENT.sub(r"\1%", normalized)
    actual = Counter(canonical(token) for token in NUMBER.findall(normalized))
    if actual != expected:
        raise ValueError("numeric_mismatch")
    # Preserve source spellings (leading zeros, decimal precision, percentage
    # signs, thousands separators) only after the exact values/counts match.
    spellings = defaultdict(deque)
    for token in literals:
        spellings[canonical(token)].append(token)
    return NUMBER.sub(lambda m: spellings[canonical(m.group())].popleft(), normalized)
