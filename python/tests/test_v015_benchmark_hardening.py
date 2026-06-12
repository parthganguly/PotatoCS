from __future__ import annotations

from pathlib import Path
from typing import Any

from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.eval_service import EvalService, benchmark_comparison
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database
from rpc_server import SidecarApp


def test_benchmark_comparison_prefers_verifier_off_when_pass_equal_and_faster():
    comparison = benchmark_comparison(
        [
            run_fixture("llama3.2", verify=False, passed=4, latency=800),
            run_fixture("llama3.2", verify=True, passed=4, latency=2400),
        ]
    )

    recommended = comparison["recommended"]

    assert recommended["verify"] is False
    assert recommended["passed"] == 4
    verifier_group = next(group for group in comparison["groups"] if group["verify"])
    assert "Verifier not useful here" in verifier_group["guidance_labels"]


def test_benchmark_comparison_recommends_verifier_only_when_it_meaningfully_improves_passes():
    comparison = benchmark_comparison(
        [
            run_fixture("llama3.2", verify=False, passed=3, latency=900),
            run_fixture("llama3.2", verify=True, passed=5, latency=1600),
        ]
    )

    recommended = comparison["recommended"]

    assert recommended["verify"] is True
    assert recommended["passed"] == 5
    assert "Verifier helped grounding" in recommended["guidance_labels"]


def test_guidance_labels_are_deterministic_from_failure_categories():
    comparison = benchmark_comparison(
        [
            run_fixture(
                "tiny:latest",
                verify=False,
                passed=1,
                latency=300,
                expected_failures=2,
                forbidden_failures=1,
                source_failures=1,
            )
        ]
    )

    labels = comparison["groups"][0]["guidance_labels"]

    assert labels == [
        "Weak at chronology",
        "Source contamination risk",
        "Not recommended for evidence-sensitive answers",
    ]


def test_app_retrieval_and_benchmark_retrieval_can_differ_without_backend_confusion(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        app.db.set_setting("embedding_backend", "local")
        app.evals._store_run(
            model="llama3.2",
            verify=False,
            total_runtime_ms=1000,
            temperature=0.0,
            case_results=[
                case_result(
                    passed=True,
                    embedding_backend="semantic",
                    embedding_model="nomic-embed-text",
                )
            ],
        )

        diagnostics = app.dispatch("diagnostics.get", {})
        comparison = app.dispatch("evals.comparison", {})

        assert diagnostics["rag"]["embedding"]["backend"] == "lexical"
        assert diagnostics["rag"]["embedding"]["semantic"] is False
        assert comparison["groups"][0]["embedding_backend"] == "semantic"
        assert comparison["groups"][0]["embedding_model"] == "nomic-embed-text"
    finally:
        app.close()


def test_potato_mode_forces_conservative_rag_settings(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        models = CapturingModelService(db)
        rag = FakeRAG()
        chat = ChatService(
            SessionService(db),
            SettingsService(db),
            models,
            rag=rag,  # type: ignore[arg-type]
        )

        result = chat.send(
            "Summarize this weak-model case.",
            model="fake:latest",
            use_rag=True,
            verify_rag=True,
            answer_style="detailed",
            temperature=0.9,
            rag_preset="potato",
        )

        assert rag.limits == [2]
        assert models.options_seen == [{"temperature": 0.0}]
        assert result["answer_style"] == "evidence_only"
        assert result["rag_preset"] == "potato"
        assert result["grounding"]["verifier"]["enabled"] is False
        prompt = models.calls[0]["messages"][0]["content"]
        assert "Potato Mode preset" in prompt
        assert "quote-first evidence only" in prompt
        assert "Do not speculate or produce broad synthesis" in prompt
    finally:
        db.close()


def test_evidence_only_style_prompt_includes_required_sections(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        models = CapturingModelService(db)
        chat = ChatService(
            SessionService(db),
            SettingsService(db),
            models,
            rag=FakeRAG(),  # type: ignore[arg-type]
        )

        chat.send(
            "What is confirmed?",
            model="fake:latest",
            use_rag=True,
            answer_style="evidence_only",
        )

        prompt = models.calls[0]["messages"][0]["content"]
        assert "Evidence only style" in prompt
        assert "Answer:" in prompt
        assert "Evidence:" in prompt
        assert "Not found / cannot confirm:" in prompt
    finally:
        db.close()


def run_fixture(
    model: str,
    *,
    verify: bool,
    passed: int,
    latency: int,
    expected_failures: int = 0,
    forbidden_failures: int = 0,
    source_failures: int = 0,
) -> dict[str, Any]:
    total = 5
    cases = []
    for index in range(total):
        case_passed = index < passed
        cases.append(
            {
                "case_id": f"case-{index}",
                "passed": case_passed,
                "expected_passed": index >= expected_failures,
                "forbidden_passed": index >= forbidden_failures,
                "source_passed": index >= source_failures,
                "latency_ms": latency,
            }
        )
    return {
        "id": f"{model}-{verify}",
        "model": model,
        "verify": verify,
        "suite_name": "local-rag",
        "suite_version": "v0.1.5",
        "total_passed": passed,
        "total_failed": total - passed,
        "average_latency_ms": latency,
        "total_runtime_ms": latency * total,
        "embedding_backend": "semantic",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.0,
        "created_at": 1,
        "cases": cases,
    }


def case_result(
    *,
    passed: bool,
    embedding_backend: str,
    embedding_model: str,
) -> dict[str, Any]:
    return {
        "case_id": "shape",
        "question": "What is tested?",
        "answer_style": "precise",
        "required_source_document": "doc",
        "passed": passed,
        "expected_passed": passed,
        "forbidden_passed": True,
        "source_passed": True,
        "latency_ms": 100,
        "reasons": [],
        "retrieved_document_ids": ["doc"],
        "retrieved_chunk_ids": ["chunk"],
        "embedding_backend": embedding_backend,
        "embedding_model": embedding_model,
        "temperature": 0.0,
    }


class CapturingModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[dict[str, Any]] = []
        self.options_seen: list[dict[str, Any]] = []

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
    ) -> str:
        self.calls.append({"model": model, "messages": messages})
        self.options_seen.append(dict(options or {}))
        return "Answer:\nConfirmed.\n\nEvidence:\nS1.\n\nNot found / cannot confirm:\nNone."


class FakeRAG:
    def __init__(self):
        self.limits: list[int] = []

    def build_quote_context(
        self,
        query: str,
        *,
        limit: int,
        document_ids: list[str] | None = None,
    ):
        self.limits.append(limit)
        return (
            "[S1] Fixture, page 1, chunk chunk-1\n- \"The fixture confirms the answer.\"",
            [
                {
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "content": "The fixture confirms the answer.",
                    "score": 1.0,
                    "page_start": 1,
                    "page_end": 1,
                    "metadata": {"title": "Fixture"},
                }
            ],
            [
                {
                    "snippet_id": "S1",
                    "chunk_id": "chunk-1",
                    "document_id": "doc-1",
                    "source": "Fixture",
                    "text": "The fixture confirms the answer.",
                    "score": 1.0,
                    "page_start": 1,
                    "page_end": 1,
                    "metadata": {"title": "Fixture"},
                }
            ],
        )
