from __future__ import annotations

import json
import base64
from io import BytesIO
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from odysseus_desktop_backend.services import campaign_service as campaign_module
from odysseus_desktop_backend.services import report_service as report_module
from odysseus_desktop_backend.services.campaign_service import CampaignService
from odysseus_desktop_backend.services.report_service import ReportService
from odysseus_desktop_backend.storage import Database, utc_ms


def make_png_data_url() -> str:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (360, 180), "#ffffff")
    draw = ImageDraw.Draw(image)
    for x in range(0, 360, 12):
        color = "#edf3f0" if (x // 12) % 2 == 0 else "#faf9f3"
        draw.rectangle((x, 0, min(x + 11, 359), 179), fill=color)
    draw.rectangle((18, 18, 342, 162), outline="#52615b", width=3)
    draw.text((34, 42), "Odysseus benchmark report", fill="#18231f")
    draw.text((34, 78), "DOM screenshot fixture", fill="#18231f")
    draw.text((34, 114), "content fitted", fill="#18231f")
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


PNG_DATA_URL = make_png_data_url()


def test_quick_preset_creates_only_quick_jobs(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        plan = CampaignService(db).plan({"preset": "quick", "models": ["llama3.2:1b", "nomic-embed-text:latest"]})

        assert [job["model"] for job in plan["planned_jobs"]] == ["llama3.2:1b"]
        assert {job["benchmark_mode"] for job in plan["planned_jobs"]} == {"end_to_end"}
        assert {job["thinking_mode"] for job in plan["planned_jobs"]} == {"off"}
        assert {job["verify"] for job in plan["planned_jobs"]} == {False}
        assert {job["repeat_count"] for job in plan["planned_jobs"]} == {1}
        assert all(job["num_predict"] > 0 for job in plan["planned_jobs"])
    finally:
        db.close()


def test_standard_preset_deduplicates_retrieval_only_jobs(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        plan = CampaignService(db).plan({"preset": "standard", "models": ["llama3.2:1b", "llama3.2:latest"]})
        retrieval_jobs = [job for job in plan["planned_jobs"] if job["benchmark_mode"] == "retrieval_only"]

        assert len(retrieval_jobs) == 1
        assert len(plan["planned_jobs"]) == 5
        assert [job["sequence"] for job in plan["planned_jobs"]] == [1, 2, 3, 4, 5]
    finally:
        db.close()


def test_thorough_preset_uses_explicit_combinations_and_skips_invalid_verifier(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        plan = CampaignService(db).plan(
            {
                "preset": "thorough",
                "models": ["llama3.2:1b"],
                "modes": ["oracle_generation", "end_to_end"],
                "thinking_modes": ["off", "on"],
                "verifier_settings": [False, True],
                "repeats": 3,
            }
        )

        assert len(plan["planned_jobs"]) == 6
        assert all(not job["verify"] for job in plan["planned_jobs"] if job["benchmark_mode"] == "oracle_generation")
        assert {job["repeat_count"] for job in plan["planned_jobs"]} == {3}
    finally:
        db.close()


def test_embedding_only_models_are_not_auto_chat_selected(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        models = CampaignService(db).installed_models()
        embedding = next(model for model in models if model["name"] == "nomic-embed-text:latest")
        chat = next(model for model in models if model["name"] == "llama3.2:1b")

        assert embedding["capability"] == "embedding"
        assert embedding["auto_select_chat"] is False
        assert chat["capability"] == "chat"
        assert chat["auto_select_chat"] is True
    finally:
        db.close()


def test_jobs_run_sequentially_and_failed_job_does_not_abort_later_jobs(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(campaign_module, "EvalService", fake_eval_service(calls, fail_models={"broken:latest"}))
    db = Database(tmp_path / "profile")
    try:
        service = CampaignService(db)
        plan = service.plan({"preset": "quick", "models": ["llama3.2:1b", "broken:latest", "llama3.2:latest"], "auto_generate_report": False})
        campaign = service.create({"plan": plan})

        service._run_campaign(campaign["id"])
        result = service.get(campaign["id"])

        assert calls == ["llama3.2:1b", "broken:latest", "llama3.2:latest"]
        assert result["status"] == "completed_with_errors"
        assert [job["status"] for job in result["jobs"]] == ["completed", "failed", "completed"]
    finally:
        db.close()


def test_timeout_job_does_not_abort_campaign(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(campaign_module, "EvalService", fake_eval_service(calls, timeout_models={"slow:latest"}))
    db = Database(tmp_path / "profile")
    try:
        service = CampaignService(db)
        plan = service.plan({"preset": "quick", "models": ["slow:latest", "llama3.2:latest"], "auto_generate_report": False})
        campaign = service.create({"plan": plan})

        service._run_campaign(campaign["id"])
        result = service.get(campaign["id"])

        assert calls == ["slow:latest", "llama3.2:latest"]
        assert result["status"] == "completed_with_errors"
        assert [job["status"] for job in result["jobs"]] == ["timed_out", "completed"]
    finally:
        db.close()


def test_pause_prevents_next_job_from_starting(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(campaign_module, "EvalService", fake_eval_service(calls))
    db = Database(tmp_path / "profile")
    try:
        service = CampaignService(db)
        plan = service.plan({"preset": "quick", "models": ["llama3.2:1b"], "auto_generate_report": False})
        campaign = service.create({"plan": plan})
        service.pause(campaign["id"])

        service._run_campaign(campaign["id"])
        result = service.get(campaign["id"])

        assert calls == []
        assert result["status"] == "paused"
        assert result["jobs"][0]["status"] == "queued"
    finally:
        db.close()


def test_cancel_preserves_completed_jobs_and_cancels_remaining(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(campaign_module, "EvalService", fake_eval_service(calls, cancel_after_first=True))
    db = Database(tmp_path / "profile")
    try:
        service = CampaignService(db)
        plan = service.plan({"preset": "quick", "models": ["llama3.2:1b", "llama3.2:latest"], "auto_generate_report": False})
        campaign = service.create({"plan": plan})

        service._run_campaign(campaign["id"])
        result = service.get(campaign["id"])

        assert calls == ["llama3.2:1b"]
        assert result["status"] == "cancelled"
        assert [job["status"] for job in result["jobs"]] == ["completed", "cancelled"]
        assert result["jobs"][0]["benchmark_run_ids"]
    finally:
        db.close()


def test_interrupted_campaign_is_recoverable_and_resume_skips_completed(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(campaign_module, "EvalService", fake_eval_service(calls))
    db = Database(tmp_path / "profile")
    try:
        service = CampaignService(db)
        plan = service.plan({"preset": "quick", "models": ["llama3.2:1b", "llama3.2:latest"], "auto_generate_report": False})
        campaign = service.create({"plan": plan})
        first, second = campaign["jobs"]
        db.conn.execute("UPDATE benchmark_campaigns SET status='running' WHERE id=?", (campaign["id"],))
        db.conn.execute("UPDATE benchmark_campaign_jobs SET status='completed', benchmark_run_ids_json=? WHERE id=?", (json.dumps(["done-run"]), first["id"]))
        db.conn.execute("UPDATE benchmark_campaign_jobs SET status='running' WHERE id=?", (second["id"],))
        db.conn.commit()

        assert service.recover_interrupted_campaigns() == 1
        service.resume(campaign["id"])
        service._run_campaign(campaign["id"])
        result = service.get(campaign["id"])

        assert calls == ["llama3.2:latest"]
        assert result["jobs"][0]["benchmark_run_ids"] == ["done-run"]
        assert result["jobs"][1]["status"] == "completed"
    finally:
        db.close()


def test_runtime_estimation_uses_history_and_threshold_warnings(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        insert_benchmark_run(db, "llama3.2:1b", "end_to_end", "off", False, total_runtime_ms=12_345, num_predict=192)
        plan = CampaignService(db).plan({"preset": "quick", "models": ["llama3.2:1b"]})

        assert plan["planned_jobs"][0]["estimate_source"] == "historical"
        assert plan["planned_jobs"][0]["estimated_runtime_ms"] == 12_345

        long_plan = CampaignService(db).plan(
            {
                "preset": "thorough",
                "models": ["qwen3:8b"],
                "modes": ["end_to_end"],
                "thinking_modes": ["on"],
                "verifier_settings": [True],
                "repeats": 3,
            }
        )
        assert long_plan["warnings"]
    finally:
        db.close()


def test_report_json_redacts_private_paths_and_contains_reproducibility_metadata(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        data = ReportService(db).build_report_data(campaign["id"])
        serialized = json.dumps(data)

        assert data["report_schema_version"] == "1"
        assert data["campaign"]["id"] == campaign["id"]
        assert data["eval_suite"]["version"] == "v0.1.12"
        assert str(tmp_path).lower() not in serialized.lower()
        assert "prompt_text" not in serialized
        assert "thinking_text" not in serialized
    finally:
        db.close()


def test_html_is_self_contained_and_pdf_valid_with_embedded_image(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "01-executive-summary.png", "label": "Executive Summary", "data_url": PNG_DATA_URL}],
        )

        html = Path(result["paths"]["html"]).read_text(encoding="utf-8")
        assert "http://" not in html and "https://" not in html
        assert "data:image/png;base64" in html
        pdf = Path(result["paths"]["pdf"])
        assert pdf.read_bytes()[:5] == b"%PDF-"
        reader = PdfReader(str(pdf))
        assert len(reader.pages) >= 1
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        assert "Odysseus Desktop Benchmark Report" in text
        assert "Benchmark Report" in text
    finally:
        db.close()


def test_missing_dom_screenshots_generates_local_fallback_visuals(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[],
        )

        assert result["status"] == "completed_with_warnings"
        assert result["screenshot_manifest"]
        assert all(item["relative_path"].startswith("screenshots/") for item in result["screenshot_manifest"])
        reader = PdfReader(str(Path(result["paths"]["pdf"])))
        assert sum(len(page.images) for page in reader.pages) >= 1
    finally:
        db.close()


def test_regenerated_report_data_does_not_expose_report_paths(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "01-executive-summary.png", "label": "Executive Summary", "data_url": PNG_DATA_URL}],
        )
        data = ReportService(db).build_report_data(campaign["id"])
        serialized = json.dumps(data)

        assert "report_paths" not in data["campaign"]
        assert data["campaign"]["report_files"]["pdf"] == Path(result["paths"]["pdf"]).name
        assert str(tmp_path).lower() not in serialized.lower()
    finally:
        db.close()


def test_screenshot_failure_does_not_fail_report(tmp_path: Path, monkeypatch):
    monkeypatch_models(monkeypatch)
    db = Database(tmp_path / "profile")
    try:
        campaign = create_completed_campaign(db, tmp_path)
        result = ReportService(db).generate_campaign_report(
            campaign["id"],
            output_folder=str(tmp_path / "reports"),
            screenshots=[{"name": "bad.png", "data_url": "not-image"}],
        )

        assert result["status"] == "completed_with_warnings"
        assert result["screenshot_manifest"]
        assert all(item["generated_by"] == "backend-fallback" for item in result["screenshot_manifest"])
        assert result["warnings"]
    finally:
        db.close()


def test_pdf_failure_preserves_html_and_json(tmp_path: Path, monkeypatch):
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


def monkeypatch_models(monkeypatch) -> None:
    monkeypatch.setattr(campaign_module, "ModelService", FakeModelService)


class FakeModelService:
    def __init__(self, db: Database):
        self.db = db

    def detect_ollama(self) -> dict[str, Any]:
        return {
            "models": ["llama3.2:1b", "llama3.2:latest", "qwen3:8b", "nomic-embed-text:latest", "broken:latest", "slow:latest"],
            "model_details": [
                {"name": "llama3.2:1b", "parameter_size": "1B", "quantization_level": "Q4", "size": 1_000},
                {"name": "llama3.2:latest", "parameter_size": "3B", "quantization_level": "Q4", "size": 3_000},
                {"name": "qwen3:8b", "parameter_size": "8B", "quantization_level": "Q4", "size": 8_000},
                {"name": "nomic-embed-text:latest", "family": "bert", "parameter_size": "", "size": 500},
                {"name": "broken:latest", "parameter_size": "1B", "size": 1000},
                {"name": "slow:latest", "parameter_size": "1B", "size": 1000},
            ],
        }

    def ps(self) -> dict[str, Any]:
        return {
            "models": [
                {"name": "qwen3:8b", "size": 8_000, "size_vram": 2_000, "parameter_size": "8B", "quantization_level": "Q4", "partially_cpu_offloaded": True}
            ]
        }


def fake_eval_service(
    calls: list[str],
    *,
    fail_models: set[str] | None = None,
    timeout_models: set[str] | None = None,
    cancel_after_first: bool = False,
):
    fail_models = fail_models or set()
    timeout_models = timeout_models or set()

    class FakeEvalService:
        def __init__(self, db: Database):
            self.db = db

        def run(self, **kwargs):
            model = kwargs["model"]
            calls.append(model)
            if cancel_after_first and len(calls) == 1:
                self.db.conn.execute("UPDATE benchmark_campaigns SET requested_action='cancel'")
                self.db.conn.commit()
            if model in fail_models:
                raise RuntimeError("simulated model failure")
            run_id = str(len(calls))
            return {
                "id": run_id,
                "model": model,
                "status": "completed",
                "total_passed": 1,
                "total_failed": 0,
                "timeout_count": 1 if model in timeout_models else 0,
                "runtime_error_count": 0,
            }

    return FakeEvalService


def insert_benchmark_run(
    db: Database,
    model: str,
    mode: str,
    thinking: str,
    verify: bool,
    *,
    total_runtime_ms: int,
    num_predict: int = 0,
) -> str:
    run_id = f"run-{model}-{mode}-{thinking}-{verify}"
    now = utc_ms()
    db.conn.execute(
        """
        INSERT INTO benchmark_runs(
            id, model, verify, suite_name, suite_version, total_passed, total_failed,
            average_latency_ms, total_runtime_ms, notes, created_at, app_version,
            prompt_version, benchmark_mode, thinking_mode, num_predict, status, completed_at
        )
        VALUES (?, ?, ?, 'local-rag', 'v0.1.12', 1, 0, ?, ?, '', ?, '0.1.12',
            'rag-benchmark-v0.1.12', ?, ?, ?, 'completed', ?)
        """,
        (run_id, model, 1 if verify else 0, total_runtime_ms, total_runtime_ms, now, mode, thinking, num_predict, now),
    )
    db.conn.commit()
    return run_id


def create_completed_campaign(db: Database, tmp_path: Path) -> dict[str, Any]:
    service = CampaignService(db)
    plan = service.plan({"preset": "quick", "models": ["llama3.2:1b"], "auto_generate_report": False})
    campaign = service.create({"plan": plan})
    run_id = insert_benchmark_run(db, "llama3.2:1b", "end_to_end", "off", False, total_runtime_ms=5000)
    job = campaign["jobs"][0]
    now = utc_ms()
    db.conn.execute(
        """
        UPDATE benchmark_campaigns
        SET status='completed',
            completed_job_count=1,
            started_at=?,
            completed_at=?,
            output_folder=?
        WHERE id=?
        """,
        (now - 5000, now, str(tmp_path / "reports"), campaign["id"]),
    )
    db.conn.execute(
        """
        UPDATE benchmark_campaign_jobs
        SET status='completed',
            benchmark_run_ids_json=?,
            started_at=?,
            completed_at=?
        WHERE id=?
        """,
        (json.dumps([run_id]), now - 5000, now, job["id"]),
    )
    db.conn.commit()
    return service.get(campaign["id"])


def fail_pdf(*_args, **_kwargs):
    raise RuntimeError("pdf failed")
