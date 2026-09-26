#!/usr/bin/env python3
"""Validate hand-authored TransIME cases using only the Python standard library."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any


def has_controls(value: str) -> bool:
    """Reject Unicode control, formatting, and surrogate code points."""
    return any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value)


def is_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not has_controls(value)


def read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    errors.append(f"line {number}: blank JSONL line")
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append(f"line {number}: invalid JSON: {exc.msg}")
                    continue
                if not isinstance(row, dict):
                    errors.append(f"line {number}: expected JSON object")
                    continue
                rows.append(row)
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read {path.name}: {type(exc).__name__}")
    return rows, errors


def validate_cases(rows: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    pairs: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not rows:
        errors.append("case file must contain at least one case")
    for index, row in enumerate(rows, 1):
        label = f"case {index}"
        for key in ("id", "source", "human_review_notes"):
            if not is_text(row.get(key)):
                errors.append(f"{label}: {key} must be nonempty text without controls")
        identifier = row.get("id")
        if isinstance(identifier, str):
            if identifier in seen:
                errors.append(f"{label}: duplicate id {identifier!r}")
            seen.add(identifier)
        history = row.get("history")
        if not isinstance(history, list):
            errors.append(f"{label}: history must be an array")
        else:
            for position, entry in enumerate(history, 1):
                if not isinstance(entry, dict):
                    errors.append(f"{label}: history {position} must be an object")
                    continue
                for key in ("source", "committed"):
                    if not is_text(entry.get(key)):
                        errors.append(f"{label}: history {position} {key} is invalid")
                if entry.get("language") not in ("zh", "en"):
                    errors.append(f"{label}: history {position} language must be zh or en")
        for key in ("acceptable_translations", "focus"):
            values = row.get(key)
            if not isinstance(values, list) or not values or not all(is_text(v) for v in values):
                errors.append(f"{label}: {key} must be a nonempty array of text")
        checks = row.get("checks")
        if not isinstance(checks, dict) or type(checks.get("preserve_numbers")) is not bool:
            errors.append(f"{label}: checks.preserve_numbers must be a boolean")
        if "pair_group" in row:
            if not is_text(row["pair_group"]):
                errors.append(f"{label}: pair_group must be nonempty text")
            else:
                pairs[row["pair_group"]].append(row)
    for group, members in pairs.items():
        if len(members) < 2:
            errors.append(f"pair group {group!r} needs at least two cases")
        sources = {json.dumps(member.get("source"), ensure_ascii=False) for member in members}
        histories = {json.dumps(member.get("history"), sort_keys=True) for member in members}
        if len(sources) != 1:
            errors.append(f"pair group {group!r} must share the same source")
        if len(histories) != len(members):
            errors.append(f"pair group {group!r} must have distinct histories")
    return errors


def load_cases(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows, errors = read_jsonl(path)
    return rows, errors + validate_cases(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path)
    args = parser.parse_args()
    rows, errors = load_cases(args.cases)
    print(json.dumps({"status": "invalid_cases" if errors else "schema_valid",
                      "case_count": len(rows), "translation_quality": "not_evaluated",
                      "errors": errors}, ensure_ascii=False, indent=2))
    return 2 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
