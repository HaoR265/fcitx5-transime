"""Decline translations that drop a small set of explicit Chinese negations.

This guard checks the presence of a negative expression, not its scope or full
meaning. It can reject valid paraphrases, and it cannot detect all mistranslation.
It neither repairs English nor uses evaluation IDs or expected translations.
"""

from __future__ import annotations

import re

VERSION = "explicit-negation-presence-v1"
EXPLICIT = re.compile(r"不要(?!紧)|没有|还没|尚未|还未|未能|未曾|不能|不会|不得|不是|不应(?:该)?|不允许|不需要|不再|不曾|禁止|不支持|不包含|不可用|不可以|不确定|不知道|不打算|不想")
# 别 is also present in lexical words: 特别、分别、区别、识别、别人、别名.
# Standalone commands may have a subject or common grammatical material before it.
BARE_BIE = re.compile(r"(?:^|[\s，,。.!！?？；;：:]|先|请|你|您|们|还|再|可|都|就|也|且|并|千万|暂时)别(?![人的处样扭致墅号名])(?=[\u3400-\u9fff])")
NEGATIVE_ENGLISH = re.compile(
    r"\b(?:not|no|never|nothing|nobody|none|neither|nor|without|cannot|"
    r"unable|unavailable|impossible|unnecessary|unsupported|unsure|uncertain|unknown|absent|lack|lacks|lacked|lacking|"
    r"prohibited|forbidden|disallowed)\b|\b[a-z]+n't\b|\bno[ -]longer\b",
    re.IGNORECASE)


def requires_negation(source: str) -> bool:
    # A-not-A questions do not assert a negative proposition.
    source = re.sub(r"要不要|有没有|能不能|会不会|是不是|需不需要|可不可以|支不支持|包不包含|确不确定|知不知道|想不想|打不打算", "", source)
    return bool(EXPLICIT.search(source) or BARE_BIE.search(source))


def validate_negation(source: str, translation: str) -> None:
    if requires_negation(source) and not NEGATIVE_ENGLISH.search(translation.replace("’", "'")):
        raise ValueError("negation_missing")
