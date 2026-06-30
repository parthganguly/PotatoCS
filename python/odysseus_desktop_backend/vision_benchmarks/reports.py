from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.vision_benchmarks import SUITE_NAME
from odysseus_desktop_backend.vision_benchmarks.scoring import SCORE_LABELS, aggregate_results, write_manual_score_csv


def write_results_jsonl(results: list[dict[str, Any]], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for result in results:
            handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    return output


def write_summary_json(run: dict[str, Any], results: list[dict[str, Any]], path: str | Path) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {**run, "summary": aggregate_results(results), "result_count": len(results)}
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return output


def write_markdown_report(
    run: dict[str, Any],
    results: list[dict[str, Any]],
    path: str | Path,
    *,
    manual_scores: dict[str, dict[str, str]] | None = None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = aggregate_results(results, manual_scores)
    route = run.get("route") or {}
    lines = [
        f"# {SUITE_NAME}",
        "",
        "This local benchmark asks a plain product question: can Odysseus answer ordinary image questions with grounded local evidence?",
        "",
        "It is product smoke evidence, not a scientific leaderboard or GPT-4o comparison.",
        "",
        "## Summary",
        "",
        f"- Route: {route.get('label') or run.get('route_id')}",
        f"- Cases: {summary['total']} total, {summary['completed']} completed, {summary['skipped']} skipped",
        f"- Manual scores pending: {summary['manual_scores_pending']}",
        f"- Correct abstentions: {summary['correct_abstentions']['scored']} / {summary['correct_abstentions']['expected']}",
        f"- Hallucinations marked: {summary['hallucination_count']}",
        f"- Average first image answer: {format_ms(summary['timing']['average_first_image_answer_ms'])}",
        f"- Average follow-up answer: {format_ms(summary['timing']['average_followup_answer_ms'])}",
        "",
        "Plain-English verdict: manual scoring is required before drawing conclusions. Use this report to inspect useful answers, refusals, and hallucinations.",
        "",
        "## Manual Scoring Labels",
        "",
    ]
    for label, meaning in SCORE_LABELS.items():
        lines.append(f"- `{label}` = {meaning}")
    lines.extend(["", "## Score Counts", ""])
    score_counts = summary["manual_score_counts"]
    for label, meaning in SCORE_LABELS.items():
        lines.append(f"- `{label}` {meaning}: {score_counts.get(label, 0)}")
    lines.extend(["", "## Worst Failures", ""])
    worst = scored_examples(results, manual_scores or {}, {"0", "H"})
    lines.extend(example_block(worst[:5]) if worst else ["No wrong or hallucination scores have been entered yet."])
    lines.extend(["", "## Best Examples", ""])
    best = scored_examples(results, manual_scores or {}, {"2", "A"})
    lines.extend(example_block(best[:5]) if best else ["No good or correct-abstention scores have been entered yet."])
    lines.extend(["", "## Example Rows", ""])
    lines.extend(example_block(results[:10]))
    output.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return output


def write_html_report(
    run: dict[str, Any],
    results: list[dict[str, Any]],
    path: str | Path,
    *,
    manual_scores: dict[str, dict[str, str]] | None = None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    summary = aggregate_results(results, manual_scores)
    rows = []
    for result in results:
        score = (manual_scores or {}).get(result["result_id"], {}).get("score") or ("S" if str(result.get("status") or "").startswith("skipped") else "")
        rows.append(
            "<tr>"
            f"<td>{html.escape(result['case_id'])}</td>"
            f"<td>{html.escape(result['turn_id'])}</td>"
            f"<td>{html.escape(result.get('question_text') or '')}</td>"
            f"<td>{html.escape(result.get('answer_text') or '')}</td>"
            f"<td>{html.escape(score)}</td>"
            f"<td>{html.escape(result.get('status') or '')}</td>"
            "</tr>"
        )
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{html.escape(SUITE_NAME)}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1f2933; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d7d2c8; padding: 0.5rem; vertical-align: top; }}
    th {{ background: #f4f1ea; text-align: left; }}
    .summary {{ margin-bottom: 1rem; }}
  </style>
</head>
<body>
  <h1>{html.escape(SUITE_NAME)}</h1>
  <p>This report is human-readable local product smoke evidence, not a public leaderboard.</p>
  <div class="summary">
    <strong>Cases:</strong> {summary['total']} total, {summary['completed']} completed, {summary['skipped']} skipped<br>
    <strong>Manual scores pending:</strong> {summary['manual_scores_pending']}<br>
    <strong>Correct abstentions:</strong> {summary['correct_abstentions']['scored']} / {summary['correct_abstentions']['expected']}<br>
    <strong>Hallucinations marked:</strong> {summary['hallucination_count']}
  </div>
  <table>
    <thead><tr><th>Case</th><th>Turn</th><th>Question</th><th>Answer</th><th>Score</th><th>Status</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</body>
</html>
"""
    output.write_text(document, encoding="utf-8")
    return output


def write_report_bundle(
    run: dict[str, Any],
    results: list[dict[str, Any]],
    out_dir: str | Path,
) -> dict[str, str]:
    output = Path(out_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "results_jsonl": str(write_results_jsonl(results, output / "results.vision-run.jsonl")),
        "summary_json": str(write_summary_json(run, results, output / "summary.json")),
        "manual_scores_csv": str(write_manual_score_csv(results, output / "manual_scores.csv")),
        "markdown_report": str(write_markdown_report(run, results, output / "report.md")),
        "html_report": str(write_html_report(run, results, output / "report.html")),
    }
    return paths


def scored_examples(
    results: list[dict[str, Any]],
    manual_scores: dict[str, dict[str, str]],
    labels: set[str],
) -> list[dict[str, Any]]:
    return [result for result in results if manual_scores.get(str(result.get("result_id") or ""), {}).get("score") in labels]


def example_block(results: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for result in results:
        lines.extend(
            [
                f"### {result['case_id']} / {result['turn_id']}",
                "",
                f"Question: {result.get('question_text') or ''}",
                "",
                f"Answer: {result.get('answer_text') or '[no answer]'}",
                "",
                f"Expected: {'; '.join(result.get('expected_good') or [])}",
                "",
                f"Must not include: {'; '.join(result.get('must_not_include') or [])}",
                "",
                f"Manual score: {'S - skipped' if str(result.get('status') or '').startswith('skipped') else '[enter score]'}",
                "",
            ]
        )
    return lines


def format_ms(value: float | None) -> str:
    if value is None:
        return "not available"
    if value >= 1000:
        return f"{value / 1000:.1f}s"
    return f"{value:.0f}ms"
