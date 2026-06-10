from __future__ import annotations

import json
from pathlib import Path

from odysseus_desktop_backend.services.eval_service import EvalService, format_benchmark_summary
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.storage import Database
from rpc_server import SidecarApp


def test_diagnostics_response_shape(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        app.models.detect_ollama = lambda: {
            "name": "ollama",
            "installed": False,
            "reachable": False,
            "endpoint": "http://127.0.0.1:11434",
            "version": "",
            "models": [],
            "error": "not running",
            "updated_at": 1,
        }
        app.ocr.status = lambda: {
            "available": False,
            "engine_name": "tesseract",
            "renderer": "",
            "message": "OCR unavailable",
            "dependencies": {},
        }

        diagnostics = app.dispatch("diagnostics.get", {})

        assert diagnostics["app_version"]
        assert diagnostics["backend_ready"] is True
        assert diagnostics["profile_dir"] == str(tmp_path / "profile")
        assert diagnostics["db_path"].endswith("app.db")
        assert diagnostics["backend_log_path"].endswith("backend.log")
        assert diagnostics["ollama"]["reachable"] is False
        assert diagnostics["ocr"]["available"] is False
        assert diagnostics["rag"]["ok"] is True
    finally:
        app.close()


def test_models_list_reports_ollama_missing_shape(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        app.models.detect_ollama = lambda: {
            "name": "ollama",
            "installed": False,
            "reachable": False,
            "endpoint": "http://127.0.0.1:11434",
            "version": "",
            "models": [],
            "error": "",
            "updated_at": 1,
        }

        status = app.dispatch("models.list", {})

        assert status["name"] == "ollama"
        assert status["models"] == []
        assert status["reachable"] is False
    finally:
        app.close()


def test_evals_json_rpc_methods_delegate_to_service(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        fake = FakeEvalService()
        app.evals = fake

        listed = app.dispatch("evals.list", {})
        run = app.dispatch("evals.run", {"model": "fake:latest", "verify": True})
        history = app.dispatch("evals.history", {"limit": 5})
        cleared = app.dispatch("evals.clear_history", {})

        assert listed["case_count"] == 1
        assert run["model"] == "fake:latest"
        assert fake.run_kwargs["verify"] is True
        assert history[0]["id"] == "run-1"
        assert cleared["cleared"] is True
    finally:
        app.close()


def test_eval_runner_persists_history_and_summary(tmp_path: Path):
    cases_dir = write_eval_fixture(tmp_path)
    db = Database(tmp_path / "profile")
    service = EvalService(
        db,
        cases_dir=cases_dir,
        model_service_factory=lambda temp_db, temperature: PassingEvalModelService(temp_db),
    )

    listed = service.list_cases()
    assert listed["case_count"] == 1
    assert listed["cases"][0]["answer_style"] == "layman"

    run = service.run(model="fake:latest", verify=False)

    assert run["model"] == "fake:latest"
    assert run["verify"] is False
    assert run["total_passed"] == 1
    assert run["total_failed"] == 0
    assert run["cases"][0]["passed"] is True
    assert run["cases"][0]["expected_passed"] is True
    assert run["cases"][0]["forbidden_passed"] is True
    assert run["cases"][0]["source_passed"] is True
    assert "| fake:latest | off | 1 | 0 |" in run["summary_markdown"]

    history = service.history()
    assert len(history) == 1
    assert history[0]["id"] == run["id"]
    assert history[0]["cases"][0]["case_id"] == "benchmark_shape"

    db.close()

    reopened = Database(tmp_path / "profile")
    try:
        persisted = EvalService(reopened, cases_dir=cases_dir).history()
        assert persisted[0]["id"] == run["id"]
    finally:
        reopened.close()


def test_eval_runner_verifier_on_result_shape(tmp_path: Path):
    cases_dir = write_eval_fixture(tmp_path)
    db = Database(tmp_path / "profile")
    try:
        service = EvalService(
            db,
            cases_dir=cases_dir,
            model_service_factory=lambda temp_db, temperature: PassingEvalModelService(temp_db),
        )

        run = service.run(model="fake:latest", verify=True)

        assert run["verify"] is True
        assert run["total_passed"] == 1
        assert run["cases"][0]["passed"] is True
    finally:
        db.close()


def test_clear_history_and_summary_format(tmp_path: Path):
    cases_dir = write_eval_fixture(tmp_path)
    db = Database(tmp_path / "profile")
    try:
        service = EvalService(
            db,
            cases_dir=cases_dir,
            model_service_factory=lambda temp_db, temperature: PassingEvalModelService(temp_db),
        )
        run = service.run(model="fake:latest", verify=False)
        summary = format_benchmark_summary([run])

        assert "Model | Verify | Passed | Failed | Avg latency | Notes" in summary
        assert "fake:latest" in summary
        assert service.clear_history()["cleared"] is True
        assert service.history() == []
    finally:
        db.close()


def write_eval_fixture(tmp_path: Path) -> Path:
    fixture_dir = tmp_path / "fixtures" / "documents"
    cases_dir = tmp_path / "rag_cases"
    fixture_dir.mkdir(parents=True)
    cases_dir.mkdir()
    document_path = fixture_dir / "benchmark_doc.md"
    document_path.write_text(
        "The benchmark report records pass and fail counts, average latency, and the model name.",
        encoding="utf-8",
    )
    case = {
        "id": "benchmark_shape",
        "documents": [
            {
                "id": "benchmark_doc",
                "path": "../fixtures/documents/benchmark_doc.md",
            }
        ],
        "question": "What does the benchmark report record?",
        "answer_style": "layman",
        "required_source_document": "benchmark_doc",
        "expected_facts": [
            {"label": "pass fail counts", "any": ["pass and fail counts"]},
            {"label": "latency", "any": ["average latency"]},
            {"label": "model name", "any": ["model name"]},
        ],
        "forbidden_claims": [
            {"label": "cloud", "any": ["cloud service", "downloads a model"]},
        ],
    }
    (cases_dir / "benchmark_shape.json").write_text(json.dumps(case), encoding="utf-8")
    return cases_dir


class PassingEvalModelService(ModelService):
    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        system = messages[0]["content"] if messages else ""
        if "Check the answer only against the retrieved evidence snippets" in system:
            return json.dumps(
                {
                    "claims": [],
                    "unsupported_claims": [],
                    "contradicted_claims": [],
                }
            )
        return "The benchmark report records pass and fail counts, average latency, and the model name."


class FakeEvalService:
    def list_cases(self):
        return {"case_count": 1, "cases": []}

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return {
            "id": "run-1",
            "model": kwargs["model"],
            "verify": kwargs["verify"],
            "cases": [],
        }

    def history(self, *, limit: int):
        self.limit = limit
        return [{"id": "run-1"}]

    def clear_history(self):
        return {"cleared": True}
