from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

from odysseus_desktop_backend.services import report_service as report_module
from odysseus_desktop_backend.services.campaign_service import CampaignService
from odysseus_desktop_backend.services.eval_service import (
    aggregate_score,
    benchmark_comparison,
    diagnose_end_to_end,
    evaluate_answer,
    pipeline_taxonomy_counts,
    primary_case_bucket,
)
from odysseus_desktop_backend.services.report_service import ReportService, render_case_details, report_recommendations
from odysseus_desktop_backend.storage import Database

from test_v0110_campaign_reports import PNG_DATA_URL, create_completed_campaign, fail_pdf, monkeypatch_models


def absence_case() -> dict:
    return {
        "expected_facts": [
            {
                "label": "no approver",
                "type": "absence_or_abstention",
                "target_aliases": ["approver", "approved the budget", "who approved"],
                "any": ["does not identify an approver", "does not list who approved"],
            }
        ],
        "forbidden_claims": [
            {"label": "invented approver", "type": "positive_fact", "any": ["Mara Chen approved", "Leela approved"]}
        ],
        "expected_abstention": True,
        "source_policy": "required_present",
    }


@pytest.mark.parametrize(
    "answer",
    [
        "It does not identify an approver for Box A17.",
        "The index does not list who approved the budget.",
        "No approver is provided in the supplied context.",
        "I cannot determine who approved it from this document.",
        "The context is silent on who approved it.",
    ],
)
def test_absence_forms_pass_without_stock_abstention_phrase(answer: str):
    result = evaluate_answer(absence_case(), answer, supplied_document_ids=["doc"], required_document_id="doc")

    assert result["passed"] is True
    absence = result["matches"][0]
    assert absence["absence_construction"]
    assert absence["target_concept"]
    assert absence["containing_clause"]


@pytest.mark.parametrize(
    "answer",
    [
        "Mara Chen approved the budget.",
        "The document does not identify an approver, but Leela approved it.",
    ],
)
def test_affirmative_and_mixed_absence_claims_fail(answer: str):
    result = evaluate_answer(absence_case(), answer, supplied_document_ids=["doc"], required_document_id="doc")

    assert result["passed"] is False
    assert result["forbidden_passed"] is False or result["expected_passed"] is False


def relation_case() -> dict:
    return {
        "expected_facts": [
            {
                "label": "Arun duration",
                "type": "relation",
                "subject_aliases": ["Arun", "her brother Arun"],
                "predicate_aliases": ["lived", "stayed", "lasted"],
                "object_aliases": ["Kolkata"],
                "value_aliases": ["six months"],
            }
        ],
        "forbidden_claims": [
            {
                "label": "wrong Arun duration",
                "type": "relation",
                "subject_aliases": ["Arun", "her brother Arun"],
                "predicate_aliases": ["lived", "stayed", "lasted"],
                "object_aliases": ["Kolkata"],
                "value_aliases": ["two hours"],
            }
        ],
        "source_policy": "required_present",
    }


def test_relation_binding_does_not_cross_contaminate_unrelated_sentences():
    passing = evaluate_answer(
        relation_case(),
        "Arun lived in Kolkata for six months while studying music. Leela waited two hours at the station.",
        supplied_document_ids=["doc"],
        required_document_id="doc",
    )
    contrast = evaluate_answer(
        relation_case(),
        "The two-hour wait belonged to Leela, not Arun. Arun lived in Kolkata for six months.",
        supplied_document_ids=["doc"],
        required_document_id="doc",
    )
    failing = evaluate_answer(
        relation_case(),
        "Arun lived in Kolkata for two hours.",
        supplied_document_ids=["doc"],
        required_document_id="doc",
    )

    assert passing["passed"] is True
    assert contrast["passed"] is True
    assert failing["passed"] is False
    assert failing["forbidden_passed"] is False
    relation_match = passing["matches"][0]
    assert relation_match["relation_components"]["subject"] == "Arun"
    assert relation_match["relation_components"]["value"] == "six months"


@pytest.mark.parametrize(
    ("expected", "wrong", "fact_type"),
    [
        ("slot A", "slot B", "exact_identifier"),
        ("MAPLE-4", "MAPLE-7", "code"),
        ("HB-204", "HB-240", "code"),
        ("9:30", "10:00", "date_or_time"),
        ("18 filters", "16 filters", "quantity"),
        ("six months", "two hours", "quantity"),
    ],
)
def test_exact_values_remain_distinct(expected: str, wrong: str, fact_type: str):
    case = {
        "expected_facts": [{"label": expected, "type": fact_type, "any": [expected]}],
        "forbidden_claims": [{"label": wrong, "type": fact_type, "any": [wrong]}],
        "source_policy": "required_present",
    }

    result = evaluate_answer(case, f"The answer is {wrong}.", supplied_document_ids=["doc"], required_document_id="doc")

    assert result["expected_passed"] is False
    assert result["forbidden_passed"] is False


def test_grader_review_scoring_is_separate_from_confirmed_failures():
    cases = [
        {"passed": True, "status": "completed", "pipeline_diagnosis": "passed"},
        {"passed": False, "status": "completed", "pipeline_diagnosis": "generation_only"},
        {"passed": False, "status": "completed", "pipeline_diagnosis": "grader_review", "grader_review_required": True},
        {"passed": False, "status": "timeout", "pipeline_diagnosis": "timeout"},
        {"passed": False, "status": "error", "pipeline_diagnosis": "runtime_error"},
    ]

    score = aggregate_score(cases)
    taxonomy = pipeline_taxonomy_counts(cases)

    assert score["attempted"] == 5
    assert score["passed"] == 1
    assert score["failed"] == 1
    assert score["grader_review_count"] == 1
    assert score["adjudicated_total"] == 2
    assert score["adjudicated_pass_rate"] == 0.5
    assert score["coverage"] == 0.4
    assert sum(taxonomy.values()) == 5


def test_low_coverage_prevents_recommendation_eligibility():
    run = comparison_run_with_review("review-heavy", passed=8, failed=0, review=4)
    comparison = benchmark_comparison([run], current_suite_version="v0.1.12")

    assert comparison["groups"][0]["latest_run_coverage"] < 0.9
    assert comparison["groups"][0]["recommendation_eligible"] is False
    assert report_recommendations(comparison)["balanced"] is None


def test_passed_rank2_required_present_source_is_diagnosed_as_passed():
    case = {
        "id": "rank-two-source",
        "required_source_document": "archive_budget",
        "expected_facts": [{"label": "box", "type": "exact_identifier", "any": ["Box A17"]}],
        "forbidden_claims": [],
        "source_policy": "required_present",
    }
    grading = evaluate_answer(
        case,
        "The archive identifies Box A17.",
        supplied_document_ids=["distractor_doc", "archive_budget"],
        required_document_id="archive_budget",
    )

    diagnosis = diagnose_end_to_end({"hit_at1": False, "required_source_rank": 2}, grading, {})

    assert grading["passed"] is True
    assert grading["source_passed"] is True
    assert diagnosis == "passed"


def test_passed_case_never_enters_failure_diagnosis_bucket():
    case = {"passed": True, "status": "completed", "pipeline_diagnosis": "retrieval_only"}

    taxonomy = pipeline_taxonomy_counts([case])

    assert primary_case_bucket(case) == "passed"
    assert taxonomy["passed"] == 1
    assert taxonomy["retrieval_only"] == 0
    assert taxonomy["generation_only"] == 0
    assert taxonomy["both"] == 0


def test_legacy_stored_passed_retrieval_only_is_normalized_in_report_data(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        run_id = insert_report_run(
            db,
            "legacy-pass-run",
            "llama3.2:latest",
            [report_case("abstention_archive_budget", passed=True, pipeline_diagnosis="retrieval_only")],
        )
        campaign = create_completed_campaign(db, tmp_path)
        attach_runs_to_campaign(db, campaign["id"], [run_id])

        data = ReportService(db).build_report_data(campaign["id"], include_detailed_audit=True)
        case = data["benchmark_runs"][0]["cases"][0]

        assert case["passed"] is True
        assert case["pipeline_diagnosis"] == "passed"
        assert data["pipeline_diagnoses"]["passed"] == 1
        assert data["timeouts_errors"]["benchmark_case_failures"] == []
    finally:
        db.close()


def test_completed_case_passed_invariant_matches_canonical_diagnosis():
    cases = [
        {"passed": True, "status": "completed", "pipeline_diagnosis": "retrieval_only"},
        {"passed": False, "status": "completed", "pipeline_diagnosis": "retrieval_only"},
        {"passed": False, "status": "completed", "pipeline_diagnosis": "generation_only"},
        {"passed": False, "status": "completed", "pipeline_diagnosis": "both"},
        {"passed": False, "status": "completed", "pipeline_diagnosis": "grader_review", "grader_review_required": True},
    ]

    for case in cases:
        diagnosis = primary_case_bucket(case)
        assert (case["passed"] is True) == (diagnosis == "passed")


def test_report_outputs_share_normalized_diagnosis_sources(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        run_id = insert_report_run(
            db,
            "legacy-output-run",
            "llama3.2:latest",
            [report_case("abstention_archive_budget", passed=True, pipeline_diagnosis="retrieval_only")],
        )
        campaign = create_completed_campaign(db, tmp_path)
        attach_runs_to_campaign(db, campaign["id"], [run_id])
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[],
            include_detailed_audit=True,
        )
        data = json.loads(Path(result["paths"]["json"]).read_text(encoding="utf-8"))
        html = Path(result["paths"]["html"]).read_text(encoding="utf-8")
        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(result["paths"]["pdf"]).pages)
        snapshot_lines = "\n".join(line for _, _, lines in report_module.fallback_snapshot_specs(data) for line in lines)
        detailed = render_case_details(data)

        case = data["benchmark_runs"][0]["cases"][0]
        assert case["passed"] is True
        assert case["pipeline_diagnosis"] == "passed"
        assert "abstention_archive_budget: completed, passed=True, diagnosis=passed" in html
        assert "abstention_archive_budget: completed, passed=True, diagnosis=passed" in detailed
        assert "Benchmark cases passed" in pdf_text
        assert "Retrieval-caused only: 0" in snapshot_lines
        assert data["timeouts_errors"]["benchmark_case_failures"] == []
    finally:
        db.close()


def test_fastest_guidance_uses_median_latency_within_compatible_scope():
    comparison = benchmark_comparison(
        [
            guidance_run("llama3.2:1b", latency=1765, chronology_passed=2),
            guidance_run("llama3.2:latest", latency=3465, chronology_passed=2),
        ],
        current_suite_version="v0.1.12",
    )
    labels = {group["model"]: group["guidance_labels"] for group in comparison["groups"]}

    assert "Fastest usable config" in labels["llama3.2:1b"]
    assert "Fastest usable config" not in labels["llama3.2:latest"]
    assert "Weak at chronology" not in labels["llama3.2:1b"]
    assert "Weak at chronology" not in labels["llama3.2:latest"]


def test_best_quality_or_balanced_winner_is_not_automatically_fastest():
    comparison = benchmark_comparison(
        [
            guidance_run("fast-lower-quality", latency=1000, passed=3, chronology_passed=2),
            guidance_run("slow-best-quality", latency=3000, passed=5, chronology_passed=2),
        ],
        current_suite_version="v0.1.12",
    )
    labels = {group["model"]: group["guidance_labels"] for group in comparison["groups"]}

    assert comparison["recommended"]["model"] == "slow-best-quality"
    assert "Fastest usable config" in labels["fast-lower-quality"]
    assert "Fastest usable config" not in labels["slow-best-quality"]


def test_chronology_guidance_requires_two_current_observations_and_low_pass_rate():
    strong = benchmark_comparison(
        [guidance_run("strong", latency=1000, chronology_passed=2)],
        current_suite_version="v0.1.12",
    )["groups"][0]
    weak = benchmark_comparison(
        [guidance_run("weak", latency=1000, chronology_passed=1)],
        current_suite_version="v0.1.12",
    )["groups"][0]
    one_observation = benchmark_comparison(
        [guidance_run("one-observation", latency=1000, chronology_attempted=1, chronology_passed=0, passed=4)],
        current_suite_version="v0.1.12",
    )["groups"][0]

    assert "Weak at chronology" not in strong["guidance_labels"]
    assert "Weak at chronology" in weak["guidance_labels"]
    assert "Weak at chronology" not in one_observation["guidance_labels"]


def test_legacy_suite_chronology_failures_do_not_affect_current_guidance():
    comparison = benchmark_comparison(
        [
            guidance_run("current", latency=1000, chronology_passed=2, suite_version="v0.1.12"),
            guidance_run("legacy", latency=900, chronology_passed=0, suite_version="v0.1.3"),
        ],
        current_suite_version="v0.1.12",
    )

    labels = comparison["groups"][0]["guidance_labels"]
    assert comparison["included_run_count"] == 1
    assert comparison["excluded_run_count"] == 1
    assert "Weak at chronology" not in labels


def test_guidance_labels_are_deterministic_and_deduplicated():
    runs = [
        guidance_run("llama3.2:1b", latency=1765, chronology_passed=1),
        guidance_run("llama3.2:latest", latency=3465, chronology_passed=2),
    ]

    first = benchmark_comparison(runs, current_suite_version="v0.1.12")["groups"]
    second = benchmark_comparison(runs, current_suite_version="v0.1.12")["groups"]

    assert [group["guidance_labels"] for group in first] == [group["guidance_labels"] for group in second]
    assert all(len(group["guidance_labels"]) == len(set(group["guidance_labels"])) for group in first)


def test_regenerated_report_uses_fresh_guidance_labels(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        run_ids = [
            insert_report_run(db, "fresh-fast", "llama3.2:1b", guidance_run("llama3.2:1b", latency=1765)["cases"], latency=1765),
            insert_report_run(db, "fresh-slow", "llama3.2:latest", guidance_run("llama3.2:latest", latency=3465)["cases"], latency=3465),
        ]
        campaign = create_completed_campaign(db, tmp_path)
        attach_runs_to_campaign(db, campaign["id"], run_ids)

        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[],
        )
        data = json.loads(Path(result["paths"]["json"]).read_text(encoding="utf-8"))
        labels = {group["model"]: group["guidance_labels"] for group in data["comparison"]["groups"]}

        assert "Fastest usable config" in labels["llama3.2:1b"]
        assert "Fastest usable config" not in labels["llama3.2:latest"]
        assert "Weak at chronology" not in labels["llama3.2:1b"]
        assert "Weak at chronology" not in labels["llama3.2:latest"]
    finally:
        db.close()


def test_pdf_table_wrapping_preserves_words_and_case_ids(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        run_id = insert_report_run(
            db,
            "pdf-wrap-run",
            "llama3.2:latest",
            [
                report_case("clean_retrieval_archive_walk", passed=True),
                report_case(
                    "contamination_shipping_code",
                    passed=False,
                    pipeline_diagnosis="generation_only",
                    reasons=["configurations observations should stay as whole words"],
                ),
            ],
        )
        campaign = create_completed_campaign(db, tmp_path)
        attach_runs_to_campaign(db, campaign["id"], [run_id])

        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[],
        )
        reader = PdfReader(result["paths"]["pdf"])
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        recoverable = re.sub(r"[\s\u200b]+", "", text)

        assert len(reader.pages) > 0
        assert "configurations" in text
        assert "observations" in text
        assert "config\nurations" not in text
        assert "observatio\nn" not in text
        assert "clean_retrieval_archive_walk" in recoverable
        assert "contamination_shipping_code" in recoverable
    finally:
        db.close()


def test_successful_dom_report_finalizes_json_and_db(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "01-executive-summary.png", "label": "Executive Summary", "data_url": PNG_DATA_URL}],
        )
        data = json.loads(Path(result["paths"]["json"]).read_text(encoding="utf-8"))
        refreshed = ReportService(db)._campaign(campaign["id"])

        assert result["status"] == "completed"
        assert result["screenshot_manifest"][0]["generated_by"] == "dom"
        assert data["report_generation"]["status"] == "completed"
        assert data["campaign"]["report_status"] == "completed"
        assert data["report_schema_version"]
        assert data["report_files"]
        assert refreshed["report_status"] == data["report_generation"]["status"]
    finally:
        db.close()


def test_explicit_fallback_report_is_warning_and_content_fitted(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[],
            capture_failure_reason="frontend was not available",
        )

        assert result["status"] == "completed_with_warnings"
        assert all(item["generated_by"] == "backend-fallback" for item in result["screenshot_manifest"])
        assert any("frontend was not available" in warning for warning in result["warnings"])
        for item in result["screenshot_manifest"]:
            image = Image.open(Path(result["paths"]["json"]).parent / item["relative_path"])
            assert image.size != (1200, 760)
    finally:
        db.close()


def test_pdf_failure_preserves_usable_outputs_without_false_success(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    monkeypatch.setattr(report_module, "render_pdf_report", fail_pdf)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "01-executive-summary.png", "data_url": PNG_DATA_URL}],
        )

        assert result["status"] == "completed_with_warnings"
        assert "html" in result["paths"]
        assert "json" in result["paths"]
        assert "pdf" not in result["paths"]
    finally:
        db.close()


def test_report_outputs_share_canonical_view_model_totals(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "01-executive-summary.png", "data_url": PNG_DATA_URL}],
        )
        data = json.loads(Path(result["paths"]["json"]).read_text(encoding="utf-8"))
        view = data["view_model"]
        html = Path(result["paths"]["html"]).read_text(encoding="utf-8")
        pdf_text = "\n".join(page.extract_text() or "" for page in PdfReader(result["paths"]["pdf"]).pages)

        assert f"{view['job_status']['completed']} / {view['job_status']['planned']}" in html
        assert "Benchmark cases passed" in pdf_text
        assert view["case_outcomes"]["passed"] + view["case_outcomes"]["failed"] + view["case_outcomes"]["grader_review"] + view["case_outcomes"]["timeout"] + view["case_outcomes"]["runtime_error"] == view["case_outcomes"]["attempted"]
    finally:
        db.close()


def test_awaiting_capture_survives_restart(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        CampaignService(db)._await_report_capture(campaign["id"])
    finally:
        db.close()

    reopened = Database(tmp_path / "profile")
    try:
        assert CampaignService(reopened).get(campaign["id"])["report_status"] == "awaiting_capture"
    finally:
        reopened.close()


def comparison_run_with_review(model: str, *, passed: int, failed: int, review: int) -> dict:
    cases = []
    for index in range(passed):
        cases.append({"case_id": f"pass-{index}", "passed": True, "status": "completed", "pipeline_diagnosis": "passed", "benchmark_mode": "end_to_end", "counts_toward_primary": True, "case_category": "direct"})
    for index in range(failed):
        cases.append({"case_id": f"fail-{index}", "passed": False, "status": "completed", "pipeline_diagnosis": "generation_only", "benchmark_mode": "end_to_end", "counts_toward_primary": True, "case_category": "direct"})
    for index in range(review):
        cases.append({"case_id": f"review-{index}", "passed": False, "status": "completed", "pipeline_diagnosis": "grader_review", "grader_review_required": True, "benchmark_mode": "end_to_end", "counts_toward_primary": True, "case_category": "direct"})
    score = aggregate_score(cases)
    return {
        "id": model,
        "model": model,
        "verify": False,
        "suite_name": "local-rag",
        "suite_version": "v0.1.12",
        "total_passed": passed,
        "total_failed": failed,
        "average_latency_ms": 1000,
        "total_runtime_ms": 1000 * len(cases),
        "embedding_backend": "semantic",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.0,
        "created_at": 1,
        "benchmark_mode": "end_to_end",
        "thinking_mode": "off",
        "prompt_version": "rag-benchmark-v0.1.12",
        "answer_style": "precise",
        "num_predict": 192,
        "status": "completed",
        "timeout_count": 0,
        "runtime_error_count": 0,
        "grader_review_count": review,
        "end_to_end_score": score,
        "practical_score": score,
        "adversarial_score": aggregate_score([]),
        "cases": cases,
    }


def guidance_run(
    model: str,
    *,
    latency: int,
    passed: int = 3,
    total: int = 7,
    chronology_attempted: int = 2,
    chronology_passed: int = 2,
    suite_version: str = "v0.1.12",
) -> dict:
    cases = []
    remaining_passes = max(passed - chronology_passed, 0)
    for index in range(total):
        is_chronology = index < chronology_attempted
        if is_chronology:
            case_passed = index < chronology_passed
            category = "chronology_comprehension"
            case_id = f"chronology-{index}"
        else:
            direct_index = index - chronology_attempted
            case_passed = direct_index < remaining_passes
            category = "direct_extraction"
            case_id = f"direct-{direct_index}"
        cases.append(
            {
                "case_id": case_id,
                "passed": case_passed,
                "expected_passed": case_passed,
                "forbidden_passed": True,
                "source_passed": True,
                "status": "completed",
                "pipeline_diagnosis": "passed" if case_passed else "generation_only",
                "case_category": category,
                "benchmark_mode": "end_to_end",
                "counts_toward_primary": True,
                "latency_ms": latency,
            }
        )
    score = aggregate_score(cases)
    return {
        "id": model,
        "model": model,
        "verify": False,
        "suite_name": "local-rag",
        "suite_version": suite_version,
        "total_passed": sum(1 for case in cases if case["passed"]),
        "total_failed": sum(1 for case in cases if not case["passed"]),
        "average_latency_ms": latency,
        "total_runtime_ms": latency * total,
        "embedding_backend": "semantic",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.0,
        "created_at": 1,
        "benchmark_mode": "end_to_end",
        "thinking_mode": "off",
        "prompt_version": "rag-benchmark-v0.1.12",
        "answer_style": "precise",
        "num_predict": 192,
        "status": "completed",
        "timeout_count": 0,
        "runtime_error_count": 0,
        "grader_review_count": 0,
        "end_to_end_score": score,
        "practical_score": score,
        "adversarial_score": aggregate_score([]),
        "cases": cases,
    }


def report_case(
    case_id: str,
    *,
    passed: bool,
    pipeline_diagnosis: str | None = None,
    reasons: list[str] | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "question": "What does the benchmark case establish?",
        "answer_style": "precise",
        "required_source_document": "doc",
        "passed": passed,
        "expected_passed": passed,
        "forbidden_passed": True,
        "source_passed": True,
        "latency_ms": 1000,
        "reasons": reasons or ([] if passed else ["missing expected fact"]),
        "retrieved_document_ids": ["doc"],
        "retrieved_chunk_ids": ["doc:1"],
        "embedding_backend": "semantic",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.0,
        "created_at": utc_now_for_tests(),
        "case_category": "direct_extraction",
        "case_difficulty": "standard",
        "benchmark_mode": "end_to_end",
        "thinking_mode": "off",
        "repeat_index": 1,
        "status": "completed",
        "stage": "",
        "pipeline_diagnosis": pipeline_diagnosis or ("passed" if passed else "generation_only"),
        "counts_toward_primary": True,
        "grader_review_required": False,
    }


def insert_report_run(
    db: Database,
    run_id: str,
    model: str,
    cases: list[dict],
    *,
    latency: int = 1000,
    suite_version: str = "v0.1.12",
) -> str:
    now = utc_now_for_tests()
    score = aggregate_score(cases)
    db.conn.execute(
        """
        INSERT INTO benchmark_runs(
            id, model, verify, suite_name, suite_version, total_passed, total_failed,
            average_latency_ms, total_runtime_ms, notes, embedding_backend, embedding_model,
            temperature, created_at, app_version, prompt_version, benchmark_mode,
            thinking_mode, answer_style, status, repeat_count, num_predict,
            timeout_count, runtime_error_count, grader_review_count, retrieval_score_json,
            oracle_score_json, end_to_end_score_json, practical_score_json,
            adversarial_score_json, completed_at
        )
        VALUES (?, ?, 0, 'local-rag', ?, ?, ?, ?, ?, '', 'semantic', 'nomic-embed-text',
            0.0, ?, '0.1.12', 'rag-benchmark-v0.1.12', 'end_to_end',
            'off', 'precise', 'completed', 1, 192, 0, 0, 0, '{}', '{}', ?, ?, ?, ?)
        """,
        (
            run_id,
            model,
            suite_version,
            sum(1 for case in cases if case.get("passed")),
            sum(1 for case in cases if not case.get("passed")),
            latency,
            latency * max(len(cases), 1),
            now,
            json.dumps(score),
            json.dumps(score),
            json.dumps(aggregate_score([])),
            now,
        ),
    )
    for index, case in enumerate(cases):
        db.conn.execute(
            """
            INSERT INTO benchmark_case_results(
                id, run_id, case_id, question, answer_style, required_source_document,
                passed, expected_passed, forbidden_passed, source_passed, latency_ms,
                reasons_json, retrieved_document_ids_json, retrieved_chunk_ids_json,
                embedding_backend, embedding_model, temperature, created_at,
                case_category, case_difficulty, benchmark_mode, thinking_mode,
                repeat_index, status, stage, pipeline_diagnosis, counts_toward_primary,
                grader_review_required, error_message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '')
            """,
            (
                f"{run_id}-case-{index}",
                run_id,
                case.get("case_id"),
                case.get("question") or "Question?",
                case.get("answer_style") or "precise",
                case.get("required_source_document") or "doc",
                1 if case.get("passed") else 0,
                1 if case.get("expected_passed") else 0,
                1 if case.get("forbidden_passed") else 0,
                1 if case.get("source_passed") else 0,
                int(case.get("latency_ms") or latency),
                json.dumps(case.get("reasons") or []),
                json.dumps(case.get("retrieved_document_ids") or ["doc"]),
                json.dumps(case.get("retrieved_chunk_ids") or ["doc:1"]),
                case.get("embedding_backend") or "semantic",
                case.get("embedding_model") or "nomic-embed-text",
                float(case.get("temperature") or 0.0),
                now + index,
                case.get("case_category") or "direct_extraction",
                case.get("case_difficulty") or "standard",
                case.get("benchmark_mode") or "end_to_end",
                case.get("thinking_mode") or "off",
                int(case.get("repeat_index") or 1),
                case.get("status") or "completed",
                case.get("stage") or "",
                case.get("pipeline_diagnosis") or ("passed" if case.get("passed") else "generation_only"),
                1 if case.get("counts_toward_primary", True) else 0,
                1 if case.get("grader_review_required") else 0,
            ),
        )
    db.conn.commit()
    return run_id


def attach_runs_to_campaign(db: Database, campaign_id: str, run_ids: list[str]) -> None:
    now = utc_now_for_tests()
    job = db.conn.execute(
        "SELECT id FROM benchmark_campaign_jobs WHERE campaign_id = ? ORDER BY sequence ASC LIMIT 1",
        (campaign_id,),
    ).fetchone()
    assert job is not None
    db.conn.execute(
        """
        UPDATE benchmark_campaign_jobs
        SET status='completed', benchmark_run_ids_json=?, started_at=?, completed_at=?
        WHERE id=?
        """,
        (json.dumps(run_ids), now - 1000, now, job["id"]),
    )
    db.conn.execute(
        """
        UPDATE benchmark_campaigns
        SET status='completed', completed_job_count=1, failed_job_count=0, timed_out_job_count=0,
            started_at=?, completed_at=?
        WHERE id=?
        """,
        (now - 1000, now, campaign_id),
    )
    db.conn.commit()


def utc_now_for_tests() -> int:
    from odysseus_desktop_backend.storage import utc_ms

    return utc_ms()
