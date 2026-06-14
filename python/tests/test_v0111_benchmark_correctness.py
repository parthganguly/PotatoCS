from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from odysseus_desktop_backend.services.eval_service import benchmark_comparison, diagnose_end_to_end, evaluate_answer
from odysseus_desktop_backend.services.report_service import (
    ReportService,
    pipeline_diagnoses,
    render_html_report,
    report_recommendations,
    score_rows,
)
from odysseus_desktop_backend.storage import Database

from test_v0110_campaign_reports import PNG_DATA_URL, create_completed_campaign, monkeypatch_models


def test_expected_no_emergency_passes_when_answer_is_negative():
    case = {
        "id": "negation_no_emergency",
        "required_source_document": "doc",
        "question": "Does the notice confirm an emergency?",
        "expected_facts": [{"label": "no emergency", "type": "negative", "any": ["no emergency"]}],
        "forbidden_claims": [{"label": "emergency", "type": "positive", "any": ["There is an emergency"]}],
    }

    result = evaluate_answer(case, "There is no emergency.", supplied_document_ids=["doc"], required_document_id="doc")

    assert result["passed"] is True
    expected = next(match for match in result["matches"] if match["label"] == "no emergency")
    forbidden = next(match for match in result["matches"] if match["label"] == "emergency")
    assert expected["fact_type"] == "negative_fact"
    assert expected["matched"] is True
    assert forbidden["matched"] is False


def test_expected_no_approver_passes_on_clear_abstention():
    case = {
        "id": "abstention_archive_budget",
        "required_source_document": "doc",
        "question": "Who approved the budget?",
        "expected_facts": [
            {"label": "no approver", "type": "abstention", "any": ["cannot confirm", "does not identify an approver"]}
        ],
        "forbidden_claims": [{"label": "invented approver", "any": ["Mara Chen approved"]}],
        "expected_abstention": True,
    }

    result = evaluate_answer(
        case,
        "The document does not say who approved the budget, so I cannot confirm an approver.",
        supplied_document_ids=["doc"],
        required_document_id="doc",
    )

    assert result["passed"] is True
    assert result["expected_passed"] is True


def test_unrelated_negation_does_not_negate_filter_or_pallet_facts():
    case = {
        "id": "direct_extraction_shipping",
        "required_source_document": "doc",
        "question": "How many filters and pallets?",
        "expected_facts": [
            {"label": "filter count", "type": "quantity", "any": ["18 blue ceramic filters", "18 filters"]},
            {"label": "pallet count", "type": "quantity", "any": ["three pallets", "3 pallets"]},
        ],
        "forbidden_claims": [{"label": "wrong count", "type": "quantity", "any": ["16 filters"]}],
    }

    answer = "Shipment HB-204 has 18 blue ceramic filters packed on three pallets. This memo does not report damaged cargo."
    result = evaluate_answer(case, answer, supplied_document_ids=["doc"], required_document_id="doc")

    assert result["passed"] is True
    assert all(match["matched"] for match in result["matches"] if match["kind"] == "expected")


def test_short_identifier_and_code_matching_requires_exact_values():
    slot_case = {
        "id": "slot",
        "required_source_document": "doc",
        "question": "Which slot?",
        "expected_facts": [{"label": "slot B", "type": "exact_identifier", "any": ["slot B"]}],
        "forbidden_claims": [{"label": "slot A", "type": "exact_identifier", "any": ["slot A"]}],
    }
    code_case = {
        "id": "code",
        "required_source_document": "doc",
        "question": "Which code?",
        "expected_facts": [{"label": "MAPLE-7", "type": "code", "any": ["MAPLE-7"]}],
        "forbidden_claims": [{"label": "MAPLE-4", "type": "code", "any": ["MAPLE-4"]}],
    }

    slot_result = evaluate_answer(slot_case, "The pack goes in slot A.", supplied_document_ids=["doc"], required_document_id="doc")
    code_result = evaluate_answer(code_case, "Use MAPLE-4.", supplied_document_ids=["doc"], required_document_id="doc")

    assert slot_result["expected_passed"] is False
    assert slot_result["forbidden_passed"] is False
    assert code_result["expected_passed"] is False
    assert code_result["forbidden_passed"] is False


def test_required_rank1_source_with_extra_candidates_is_generation_only_when_answer_missing():
    case = {
        "id": "shipping",
        "required_source_document": "doc",
        "question": "How many filters?",
        "expected_facts": [{"label": "filters", "type": "quantity", "any": ["18 filters"]}],
        "forbidden_claims": [],
        "source_policy": "required_present",
    }
    grading = evaluate_answer(case, "The answer is not provided.", supplied_document_ids=["doc"], required_document_id="doc")
    diagnosis = diagnose_end_to_end({"hit_at1": True}, grading, {})

    assert grading["source_passed"] is True
    assert grading["expected_passed"] is False
    assert diagnosis == "generation_only"


def test_pipeline_counts_use_canonical_both_taxonomy():
    runs = [
        {
            "cases": [
                {"pipeline_diagnosis": "both"},
                {"pipeline_diagnosis": "generation_only"},
                {"pipeline_diagnosis": "retrieval_only"},
                {"pipeline_diagnosis": "passed"},
            ]
        }
    ]

    counts = pipeline_diagnoses(runs)

    assert counts["both"] == 1
    assert "combined" not in counts
    rows = score_rows({"benchmark_runs": runs, "pipeline_diagnoses": counts})
    assert any(row[0] == "both retrieval and generation" and row[1] == "1" for row in rows)


def test_not_run_modes_render_na_not_zero():
    data = {
        "benchmark_runs": [
            {"cases": [{"benchmark_mode": "end_to_end", "passed": True, "counts_toward_primary": True}]}
        ],
        "pipeline_diagnoses": {},
    }

    rows = score_rows(data)

    retrieval = next(row for row in rows if row[0] == "retrieval")
    oracle = next(row for row in rows if row[0] == "oracle_generation")
    assert retrieval[1:] == ["N/A", "Not run"]
    assert oracle[1:] == ["N/A", "Not run"]


def test_equal_quality_produces_quality_tie_but_speed_can_choose():
    comparison = benchmark_comparison(
        [
            comparison_run("fast", passed=3, latency=1000),
            comparison_run("slow", passed=3, latency=5000),
        ],
        current_suite_version="v0.1.12",
    )

    recommendations = report_recommendations(comparison)

    assert recommendations["best_quality"]["tie"] is True
    assert recommendations["best_speed"]["model"] == "fast"
    assert recommendations["balanced"]["model"] == "fast"
    assert recommendations["deployment_readiness"] == "no"


def test_report_status_finalizes_with_warning_when_backend_fallback_snapshots_used(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(campaign["id"], output_folder=str(tmp_path / "reports"), screenshots=[])
        refreshed = ReportService(db)._campaign(campaign["id"])

        assert result["status"] == "completed_with_warnings"
        assert refreshed["report_status"] == "completed_with_warnings"
        assert result["screenshot_manifest"]
    finally:
        db.close()


def test_pdf_avoids_split_header_and_near_empty_pages(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "01-executive-summary.png", "label": "Executive Summary", "data_url": PNG_DATA_URL}],
        )

        reader = PdfReader(result["paths"]["pdf"])
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n".join(pages)
        assert "Timeo\nut" not in text
        assert "Eligibl\ne" not in text
        assert all(len(page.strip()) > 80 for page in pages)
    finally:
        db.close()


def test_html_uses_canonical_pipeline_labels(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        data = ReportService(db).build_report_data(campaign["id"])
        data["pipeline_diagnoses"] = {"retrieval_only": 1, "generation_only": 2, "both": 3, "grader_review": 0, "timeout": 0, "runtime_error": 0, "passed": 4}
        output_dir = tmp_path / "report"
        output_dir.mkdir()
        html = render_html_report(data, output_dir)

        assert "retrieval-caused only" in html
        assert "generation-caused only" in html
        assert "both retrieval and generation" in html
        assert "combined" not in html.lower()
        assert "mixed" not in html.lower()
    finally:
        db.close()


def comparison_run(model: str, *, passed: int, latency: int) -> dict[str, Any]:
    cases = [
        {
            "case_id": f"case-{index}",
            "passed": index < passed,
            "expected_passed": index < passed,
            "forbidden_passed": True,
            "source_passed": index < passed,
            "latency_ms": latency,
            "case_category": "direct_extraction",
            "counts_toward_primary": True,
            "benchmark_mode": "end_to_end",
            "retrieval_metrics": {"hit_at1": True, "reciprocal_rank": 1.0},
        }
        for index in range(3)
    ]
    return {
        "id": model,
        "model": model,
        "verify": False,
        "suite_name": "local-rag",
        "suite_version": "v0.1.12",
        "total_passed": passed,
        "total_failed": len(cases) - passed,
        "average_latency_ms": latency,
        "total_runtime_ms": latency * len(cases),
        "embedding_backend": "semantic",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.0,
        "created_at": 1,
        "benchmark_mode": "end_to_end",
        "thinking_mode": "off",
        "prompt_version": "rag-benchmark-v0.1.12",
        "answer_style": "precise",
        "num_predict": 0,
        "status": "completed",
        "timeout_count": 0,
        "cases": cases,
    }
