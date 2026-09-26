#!/usr/bin/env python3
"""Check supplied predictions and write mechanical checks plus human-review status.

No network, model calls, reference matching, or model self-grading are performed.
Exit 0 means only that applicable machine checks ran without errors.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from check_eval_cases import has_controls, is_text, load_cases, read_jsonl


NUMBER = re.compile(r"(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?", re.ASCII)


def number_literals(text: str) -> Counter[str]:
    """Compare numeric values and percent marks; word-form numbers are not parsed."""
    result: Counter[str] = Counter()
    for match in NUMBER.finditer(text):
        token = match.group()
        percent = "%" if token.endswith("%") else ""
        normalized = str(Decimal(token.removesuffix("%").replace(",", "")).normalize())
        result[normalized + percent] += 1
    return result


def evaluate(cases: list[dict[str, Any]], predictions: list[dict[str, Any]],
             input_errors: list[str] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    errors = list(input_errors or [])
    expected_ids = {case["id"] for case in cases}
    for index, prediction in enumerate(predictions, 1):
        identifier = prediction.get("id")
        if not is_text(identifier):
            errors.append(f"prediction {index}: invalid id")
            continue
        grouped[identifier].append(prediction)
    unknown = sorted(set(grouped) - expected_ids)
    duplicate = sorted(identifier for identifier, values in grouped.items() if len(values) > 1)
    missing = sorted(expected_ids - set(grouped))
    errors.extend(f"unknown id: {identifier}" for identifier in unknown)
    errors.extend(f"duplicate id: {identifier}" for identifier in duplicate)
    errors.extend(f"missing prediction: {identifier}" for identifier in missing)
    # Validate every supplied row, including unknown IDs and duplicate rows.
    for identifier, values in grouped.items():
        for position, prediction in enumerate(values, 1):
            translation = prediction.get("translation")
            if "provenance" in prediction and not is_text(prediction["provenance"]):
                errors.append(f"{identifier} prediction {position}: invalid_provenance")
            if not isinstance(translation, str) or not translation.strip():
                errors.append(f"{identifier} prediction {position}: empty_or_invalid_translation")
            elif has_controls(translation):
                errors.append(f"{identifier} prediction {position}: control_characters")
    rows: list[dict[str, Any]] = []
    for case in cases:
        identifier = case["id"]
        values = grouped.get(identifier, [])
        issues: list[str] = []
        prediction = values[0] if len(values) == 1 else {}
        translation = prediction.get("translation")
        if not values:
            issues.append("missing_prediction")
        elif len(values) != 1:
            issues.append("duplicate_id")
        elif not isinstance(translation, str) or not translation.strip():
            issues.append("empty_or_invalid_translation")
        elif has_controls(translation):
            issues.append("control_characters")
        if any("provenance" in value and not is_text(value["provenance"]) for value in values):
            issues.append("invalid_provenance")
        numeric_status = "not_requested"
        if case["checks"]["preserve_numbers"]:
            numeric_status = "not_checked"
            if not issues:
                numeric_status = "matched" if number_literals(case["source"]) == number_literals(translation) else "mismatch"
                if numeric_status == "mismatch":
                    issues.append("number_literal_mismatch")
                    errors.append(f"{identifier}: number_literal_mismatch")
        rows.append({"id": identifier,
                     "prediction_count": len(values),
                     "mechanical_status": "failed" if issues else "checks_passed",
                     "issues": ";".join(issues),
                     "numeric_literals": numeric_status,
                     "semantic_status": "needs_human_review",
                     "focus": ";".join(case["focus"]),
                     "provenance": prediction.get("provenance", "unspecified") if is_text(prediction.get("provenance", "unspecified")) else "invalid"})
    completed = sum(row["prediction_count"] == 1
                    and is_text(grouped[row["id"]][0].get("translation"))
                    and is_text(grouped[row["id"]][0].get("provenance", "unspecified"))
                    for row in rows)
    mechanical_passed = sum(row["mechanical_status"] == "checks_passed" for row in rows)
    summary = {"status": "mechanical_checks_failed" if errors else "needs_human_review",
               "case_count": len(cases), "prediction_rows": len(predictions),
               "completed_cases": completed,
               "completion_ratio": completed / len(cases) if cases else 0,
               "mechanical_passed_cases": mechanical_passed,
               "mechanical_failed_cases": len(cases) - mechanical_passed,
               "missing_ids": missing, "duplicate_ids": duplicate, "unknown_ids": unknown,
               "errors": errors, "semantic_status": "needs_human_review",
               "semantic_passed_cases": None, "semantic_quality_score": None,
               "focus_coverage": dict(sorted(Counter(tag for case in cases for tag in case["focus"]).items())),
               "paired_context_groups": sorted({case["pair_group"] for case in cases if "pair_group" in case}),
               "limitations": ["References are examples, never exact-match grading targets.",
                               "Number checking covers Arabic numeral values and percent marks, not word-form numbers, units, direction, or semantic correctness.",
                               "No semantic accuracy claim is made, even when all mechanical checks pass.",
                               "Prediction provenance is supplied by the caller and is not independently verified."]}
    return summary, rows


def write_reports(output_dir: Path, summary: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fields = ["id", "prediction_count", "mechanical_status", "issues", "numeric_literals", "semantic_status", "focus", "provenance"]
    with (output_dir / "metrics.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    lines = ["# TransIME 翻译验收汇总", "", f"状态：`{summary['status']}`", "",
             "本报告只执行数据完整性与机械约束检查。未运行语义评分，不代表翻译质量通过。",
             "参考译文只是人工编写示例，不是唯一正确答案。", ""]
    if summary["status"] == "schema_valid_only":
        lines += ["未提供预测，仅验证了验收数据结构。翻译质量：`not_evaluated`。", ""]
    else:
        lines += [f"验收样例：{summary['case_count']}；预测行：{summary['prediction_rows']}。",
                  f"完整有效预测：{summary['completed_cases']} / {summary['case_count']}（{summary['completion_ratio']:.1%}）。",
                  f"逐例机械检查通过：{summary['mechanical_passed_cases']}；失败：{summary['mechanical_failed_cases']}。",
                  "语义判定：`needs_human_review`，语义通过数与准确率均未计算。", "",
                  "数字检查比较阿拉伯数字值与百分号的多重集合；单位、方向、语序与英文数字词需人工核查。",
                  "未知 ID 或输入文件错误也会令整份机械评测失败，不能只看逐例通过数。", ""]
    lines += ["## 数据覆盖", ""]
    for tag, count in summary.get("focus_coverage", {}).items():
        lines.append(f"- {tag}: {count}")
    lines += ["", "## 需要处理的问题", ""]
    lines.extend(f"- {error}" for error in summary.get("errors", []))
    if not summary.get("errors"):
        lines.append("- 未发现本次检查范围内的结构或机械约束问题；语义尚未评审。")
    lines += ["", "## 人工评审", "",
              "逐例参照原文、实际提交历史及 human_review_notes，确认原意、指代、省略、否定、不确定性、数字单位与方向。",
              "确认只输出当前内容，未重复历史；成对比较相同 source 在不同 history 下是否合理变化。",
              "应使用同一个模型分别生成无历史与有历史结果，再盲评两组；不能用参考译文冒充模型预测。",
              "本报告不保存原文、历史或完整预测文本。", ""]
    (output_dir / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    cases, errors = load_cases(args.cases)
    if errors:
        print(json.dumps({"status": "invalid_cases", "errors": errors}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    if args.predictions is None:
        summary = {"status": "schema_valid_only", "case_count": len(cases),
                   "semantic_status": "not_evaluated", "errors": [],
                   "focus_coverage": dict(sorted(Counter(tag for case in cases for tag in case["focus"]).items()))}
        rows = []
    else:
        predictions, errors = read_jsonl(args.predictions)
        summary, rows = evaluate(cases, predictions, errors)
    # Never let an output report overwrite either explicitly supplied input file.
    targets = {(args.output_dir / name).resolve() for name in ("summary.json", "metrics.csv", "report.md")}
    if args.cases.resolve() in targets or (args.predictions and args.predictions.resolve() in targets):
        print("output report path conflicts with an input file", file=sys.stderr)
        return 2
    write_reports(args.output_dir, summary, rows)
    print(json.dumps({"status": summary["status"], "case_count": len(cases),
                      "semantic_status": summary["semantic_status"],
                      "output_dir": str(args.output_dir)}, ensure_ascii=False, indent=2))
    return 1 if summary.get("errors") else 0


if __name__ == "__main__":
    sys.exit(main())
