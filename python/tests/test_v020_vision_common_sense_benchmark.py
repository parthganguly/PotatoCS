from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
from PIL import Image

from odysseus_desktop_backend.vision_benchmarks.reports import write_markdown_report, write_results_jsonl
from odysseus_desktop_backend.vision_benchmarks.routes import check_static_route_availability
from odysseus_desktop_backend.vision_benchmarks.runner import run_benchmark
from odysseus_desktop_backend.vision_benchmarks.schema import DEFAULT_ROUTES_PATH, DEFAULT_SUITE_PATH, load_suite
from odysseus_desktop_backend.vision_benchmarks.scoring import aggregate_results, parse_manual_scores


def test_starter_suite_loads_single_and_multi_turn_cases():
    suite = load_suite(DEFAULT_SUITE_PATH)

    assert suite["suite_name"] == "Odysseus Visual Common Sense Benchmark"
    assert len(suite["images"]) == 4
    assert sum(len(case["turns"]) for case in suite["cases"]) == 20
    assert any(len(case["turns"]) > 1 for case in suite["cases"])
    assert any(turn["followup_should_reuse_evidence"] for case in suite["cases"] for turn in case["turns"])
    assert any(turn["correct_abstention"] for case in suite["cases"] for turn in case["turns"])


def test_missing_image_can_be_loaded_for_skip_but_required_validation_fails(tmp_path: Path):
    suite_path = write_minimal_suite(tmp_path, image_path="fixtures/missing.png")

    suite = load_suite(suite_path, require_images=False)

    assert suite["images"]["tiny"]["path"] == "fixtures/missing.png"
    with pytest.raises(FileNotFoundError):
        load_suite(suite_path, require_images=True)


def test_missing_model_route_is_skipped_without_installing_or_pulling():
    route = {
        "route_id": "florence_llama_1b",
        "requires_florence": True,
        "requires_ollama_models": ["llama3.2:1b"],
        "final_model": "llama3.2:1b",
    }

    missing_model = check_static_route_availability(route, installed_models=[], florence_ready=True)
    missing_backend = check_static_route_availability(route, installed_models=["llama3.2:1b"], florence_ready=False)

    assert missing_model["status"] == "skipped_missing_model"
    assert "llama3.2:1b" in missing_model["reason"]
    assert missing_backend["status"] == "skipped_missing_backend"


def test_manual_score_parsing_and_aggregation(tmp_path: Path):
    results = [
        result("r1", score_expected_abstention=False, elapsed=1000),
        result("r2", score_expected_abstention=True, elapsed=200),
        {**result("r3", score_expected_abstention=False, elapsed=50), "status": "skipped_missing_model"},
    ]
    scores_path = tmp_path / "manual_scores.csv"
    with scores_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["result_id", "score", "score_notes"])
        writer.writeheader()
        writer.writerow({"result_id": "r1", "score": "H", "score_notes": "invented a city"})
        writer.writerow({"result_id": "r2", "score": "A", "score_notes": "correctly refused identity"})

    scores = parse_manual_scores(scores_path)
    summary = aggregate_results(results, scores)

    assert scores["r1"]["score"] == "H"
    assert summary["manual_score_counts"]["H"] == 1
    assert summary["manual_score_counts"]["A"] == 1
    assert summary["manual_score_counts"]["S"] == 1
    assert summary["hallucination_count"] == 1
    assert summary["correct_abstentions"] == {"scored": 1, "expected": 1}
    assert summary["timing"]["average_first_image_answer_ms"] == 1000
    assert summary["timing"]["average_followup_answer_ms"] == 125


def test_jsonl_and_markdown_report_generation(tmp_path: Path):
    results = [result("r1"), {**result("r2"), "answer_text": "I cannot determine the city.", "correct_abstention_expected": True}]
    run = {"run_id": "run-test", "route_id": "smoke_stub", "route": {"label": "Smoke"}}

    jsonl = write_results_jsonl(results, tmp_path / "results.vision-run.jsonl")
    report = write_markdown_report(run, results, tmp_path / "report.md")

    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 2
    text = report.read_text(encoding="utf-8")
    assert "Odysseus Visual Common Sense Benchmark" in text
    assert "Worst Failures" in text
    assert "Best Examples" in text
    assert "Manual Scoring Labels" in text


def test_smoke_runner_writes_reports_and_followup_metadata(tmp_path: Path):
    output = tmp_path / "smoke"

    run = run_benchmark(
        suite_path=DEFAULT_SUITE_PATH,
        routes_path=DEFAULT_ROUTES_PATH,
        route_id="smoke_stub",
        out_dir=output,
        smoke=True,
    )

    assert run["summary"]["completed"] == 5
    assert Path(run["paths"]["results_jsonl"]).exists()
    assert Path(run["paths"]["manual_scores_csv"]).exists()
    assert Path(run["paths"]["markdown_report"]).exists()
    assert Path(run["paths"]["html_report"]).exists()
    first, second = run["results"][0], run["results"][1]
    assert first["raw_evidence_reused"] is False
    assert second["followup_should_reuse_evidence"] is True
    assert second["raw_evidence_reused"] is True
    assert second["curated_evidence_recomputed"] is True
    assert second["vision_rerun"] is False


def test_local_private_image_path_is_not_copied_or_reported_by_default(tmp_path: Path):
    local_image = tmp_path / "private.png"
    Image.new("RGB", (24, 24), (10, 20, 30)).save(local_image)
    suite_path = write_minimal_suite(tmp_path, image_path=str(local_image))
    output = tmp_path / "out"

    run = run_benchmark(
        suite_path=suite_path,
        routes_path=DEFAULT_ROUTES_PATH,
        route_id="smoke_stub",
        out_dir=output,
        smoke=True,
    )

    assert run["results"][0]["image_path"] == "local_or_private_image:tiny"
    copied_images = list(output.rglob("*.png")) + list(output.rglob("*.jpg")) + list(output.rglob("*.webp"))
    assert copied_images == []


def test_route_metadata_fields_are_present_in_smoke_results(tmp_path: Path):
    run = run_benchmark(
        suite_path=DEFAULT_SUITE_PATH,
        routes_path=DEFAULT_ROUTES_PATH,
        route_id="smoke_stub",
        out_dir=tmp_path / "smoke",
        smoke=True,
    )

    item = run["results"][0]
    for key in (
        "route_id",
        "vision_backend_requested",
        "vision_backend_actual",
        "vision_model_actual",
        "final_model_actual",
        "perception_completed",
        "synthesis_completed",
        "raw_evidence_reused",
        "curated_evidence_recomputed",
        "vision_rerun",
    ):
        assert key in item


def write_minimal_suite(tmp_path: Path, *, image_path: str) -> Path:
    suite = {
        "suite_id": "tmp_suite",
        "suite_name": "Odysseus Visual Common Sense Benchmark",
        "suite_version": "test",
        "images": [
            {
                "id": "tiny",
                "path": image_path,
                "license": "generated test image",
                "source_note": "unit test",
                "safe_to_thumbnail": False,
            }
        ],
        "cases": [
            {
                "id": "tiny_case",
                "image_id": "tiny",
                "category": "object close-up",
                "turns": [
                    {
                        "id": "tiny_turn",
                        "question": "What color is the square?",
                        "expected_good": ["blue"],
                        "acceptable": ["dark square"],
                        "must_not_include": ["person"],
                        "correct_abstention": False,
                    }
                ],
            }
        ],
    }
    if not Path(image_path).is_absolute() and not image_path.endswith("missing.png"):
        fixture = tmp_path / image_path
        fixture.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (24, 24), (10, 20, 30)).save(fixture)
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(suite), encoding="utf-8")
    return path


def result(result_id: str, *, score_expected_abstention: bool = False, elapsed: int = 100) -> dict[str, object]:
    return {
        "result_id": result_id,
        "case_id": "case",
        "turn_id": result_id,
        "route_id": "route",
        "category": "category",
        "question_text": "Question?",
        "answer_text": "Answer.",
        "expected_good": ["answer"],
        "acceptable": ["answer"],
        "must_not_include": ["bad"],
        "correct_abstention_expected": score_expected_abstention,
        "status": "completed",
        "turn_index": 0 if result_id == "r1" else 1,
        "total_wall_time_ms": elapsed,
    }
