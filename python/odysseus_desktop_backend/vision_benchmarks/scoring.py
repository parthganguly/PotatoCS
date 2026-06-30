from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


SCORE_LABELS = {
    "2": "Good",
    "1": "Partly right",
    "0": "Wrong / useless",
    "H": "Hallucination",
    "A": "Correct abstention",
    "S": "Skipped",
}


def parse_manual_scores(path: str | Path) -> dict[str, dict[str, str]]:
    score_path = Path(path)
    if score_path.suffix.lower() == ".jsonl":
        return parse_manual_scores_jsonl(score_path)
    return parse_manual_scores_csv(score_path)


def parse_manual_scores_csv(path: Path) -> dict[str, dict[str, str]]:
    scores: dict[str, dict[str, str]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            result_id = str(row.get("result_id") or "").strip()
            label = normalize_score_label(row.get("score") or "")
            if result_id and label:
                scores[result_id] = {"score": label, "notes": str(row.get("score_notes") or "")}
    return scores


def parse_manual_scores_jsonl(path: Path) -> dict[str, dict[str, str]]:
    scores: dict[str, dict[str, str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        result_id = str(row.get("result_id") or "").strip()
        label = normalize_score_label(row.get("score") or "")
        if result_id and label:
            scores[result_id] = {"score": label, "notes": str(row.get("score_notes") or row.get("notes") or "")}
    return scores


def normalize_score_label(value: str) -> str:
    label = str(value or "").strip().upper()
    if not label:
        return ""
    if label not in SCORE_LABELS:
        raise ValueError(f"unknown manual score label: {value}")
    return label


def write_manual_score_csv(results: list[dict[str, Any]], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "result_id",
        "case_id",
        "turn_id",
        "route_id",
        "category",
        "question",
        "answer",
        "expected_good",
        "acceptable",
        "must_not_include",
        "correct_abstention_expected",
        "score",
        "score_notes",
    ]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for result in results:
            writer.writerow(
                {
                    "result_id": result["result_id"],
                    "case_id": result["case_id"],
                    "turn_id": result["turn_id"],
                    "route_id": result["route_id"],
                    "category": result["category"],
                    "question": result["question_text"],
                    "answer": result.get("answer_text") or "",
                    "expected_good": "; ".join(result.get("expected_good") or []),
                    "acceptable": "; ".join(result.get("acceptable") or []),
                    "must_not_include": "; ".join(result.get("must_not_include") or []),
                    "correct_abstention_expected": "yes" if result.get("correct_abstention_expected") else "no",
                    "score": "S" if str(result.get("status") or "").startswith("skipped") else "",
                    "score_notes": "",
                }
            )
    return output


def aggregate_results(
    results: list[dict[str, Any]],
    manual_scores: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    manual_scores = manual_scores or {}
    labels = Counter()
    status_counts = Counter(str(result.get("status") or "unknown") for result in results)
    expected_abstentions = 0
    scored_correct_abstentions = 0
    scored_hallucinations = 0
    first_turn_times: list[float] = []
    followup_times: list[float] = []
    for result in results:
        score = manual_scores.get(str(result.get("result_id") or ""), {}).get("score")
        if not score and str(result.get("status") or "").startswith("skipped"):
            score = "S"
        if score:
            labels[score] += 1
        if result.get("correct_abstention_expected"):
            expected_abstentions += 1
            if score == "A":
                scored_correct_abstentions += 1
        if score == "H":
            scored_hallucinations += 1
        elapsed = float(result.get("total_wall_time_ms") or 0)
        if elapsed > 0:
            if int(result.get("turn_index") or 0) == 0:
                first_turn_times.append(elapsed)
            else:
                followup_times.append(elapsed)
    return {
        "total": len(results),
        "completed": sum(1 for result in results if result.get("status") == "completed"),
        "skipped": sum(1 for result in results if str(result.get("status") or "").startswith("skipped")),
        "status_counts": dict(status_counts),
        "manual_score_counts": {label: labels.get(label, 0) for label in SCORE_LABELS},
        "hallucination_count": scored_hallucinations,
        "correct_abstentions": {
            "scored": scored_correct_abstentions,
            "expected": expected_abstentions,
        },
        "timing": {
            "average_first_image_answer_ms": average(first_turn_times),
            "average_followup_answer_ms": average(followup_times),
        },
        "manual_scores_pending": sum(1 for result in results if result.get("status") == "completed" and str(result.get("result_id") or "") not in manual_scores),
    }


def average(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 2)
