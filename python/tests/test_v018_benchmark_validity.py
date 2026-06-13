from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.services import eval_service as eval_module
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.eval_service import EvalService, benchmark_comparison, evaluate_answer
from odysseus_desktop_backend.services.model_service import ModelService, ModelTimeoutError
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database


def test_ollama_thinking_payload_is_top_level_and_structured(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        models = CapturingOllamaService(db)

        off = models.chat_detailed("qwen3", [{"role": "user", "content": "hi"}], options={"temperature": 0, "think": True}, thinking="off")
        assert models.payloads[-1]["think"] is False
        assert "think" not in models.payloads[-1]["options"]
        assert off["content"] == "final"
        assert off["thinking"] == "internal"
        assert off["eval_count"] == 10
        assert off["generation_tokens_per_second"] == 5.0

        models.chat_detailed("qwen3", [{"role": "user", "content": "hi"}], thinking="on")
        assert models.payloads[-1]["think"] is True

        models.chat_detailed("qwen3", [{"role": "user", "content": "hi"}], thinking="auto")
        assert "think" not in models.payloads[-1]

        assert models.chat("qwen3", [{"role": "user", "content": "hi"}]) == "final"
    finally:
        db.close()


def test_ollama_ps_offload_data_is_parsed_safely(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        models = CapturingOllamaService(db)
        status = models.ps()
        loaded = status["models"][0]

        assert loaded["parameter_size"] == "8B"
        assert loaded["quantization_level"] == "Q4_K_M"
        assert loaded["size_vram"] == 25
        assert loaded["estimated_gpu_loaded_fraction"] == 0.25
        assert loaded["partially_cpu_offloaded"] is True
    finally:
        db.close()


def test_verifier_timeout_preserves_original_answer(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        models = TimeoutVerifierModel(db)
        chat = ChatService(SessionService(db), SettingsService(db), models, rag=SimpleRAG())

        result = chat.send("What is confirmed?", model="fake", use_rag=True, verify_rag=True)

        assert result["assistant_message"]["content"] == "The source confirms alpha."
        assert result["grounding"]["verifier"]["status"] == "timeout"
    finally:
        db.close()


def test_correction_timeout_preserves_draft_answer(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        models = TimeoutCorrectionModel(db)
        chat = ChatService(SessionService(db), SettingsService(db), models, rag=SimpleRAG())

        result = chat.send("What is confirmed?", model="fake", use_rag=True, verify_rag=True)

        assert result["assistant_message"]["content"] == "The source says beta."
        assert result["grounding"]["correction_status"] == "timeout"
    finally:
        db.close()


def test_answer_timeout_stores_case_and_continues(tmp_path: Path):
    cases_dir = write_two_case_fixture(tmp_path)
    db = Database(tmp_path / "profile")
    try:
        service = EvalService(
            db,
            cases_dir=cases_dir,
            model_service_factory=lambda temp_db, temperature: OneTimeoutModel(temp_db),
        )

        run = service.run(model="fake", benchmark_mode="end_to_end", thinking_mode="off")

        assert run["status"] == "completed"
        assert run["total_passed"] == 1
        assert run["timeout_count"] == 1
        assert [case["status"] for case in run["cases"]] == ["completed", "timeout"]
        assert run["cases"][1]["retrieval_metrics"]["ranked_retrieved_document_ids"]
    finally:
        db.close()


def test_retrieval_only_mode_never_calls_chat_model(tmp_path: Path):
    cases_dir = write_two_case_fixture(tmp_path)
    db = Database(tmp_path / "profile")
    try:
        service = EvalService(
            db,
            cases_dir=cases_dir,
            model_service_factory=lambda temp_db, temperature: ExplodingModel(temp_db),
        )

        run = service.run(model="fake", benchmark_mode="retrieval_only")

        assert run["benchmark_mode"] == "retrieval_only"
        assert len(run["cases"]) == 2
    finally:
        db.close()


def test_indexing_failure_is_stored_as_case_error(tmp_path: Path):
    cases = tmp_path / "rag_cases"
    cases.mkdir()
    payload = {
        "id": "missing_fixture",
        "category": "direct_extraction",
        "difficulty": "easy",
        "benchmark_modes": ["retrieval_only"],
        "documents": [{"id": "missing_doc", "path": "../fixtures/documents/missing.md"}],
        "required_source_document": "missing_doc",
        "question": "What is missing?",
        "expected_facts": [{"label": "missing", "any": ["missing"]}],
        "forbidden_claims": [],
    }
    (cases / "missing_fixture.json").write_text(json.dumps(payload), encoding="utf-8")
    db = Database(tmp_path / "profile")
    try:
        service = EvalService(db, cases_dir=cases)

        run = service.run(model="fake", benchmark_mode="retrieval_only")

        assert run["status"] == "completed"
        assert run["runtime_error_count"] == 1
        assert len(run["cases"]) == 1
        assert run["cases"][0]["status"] == "error"
        assert run["cases"][0]["stage"] == "indexing"
    finally:
        db.close()


def test_setup_failure_finalizes_run_with_error_cases(tmp_path: Path, monkeypatch):
    cases_dir = write_two_case_fixture(tmp_path)
    db = Database(tmp_path / "profile")

    def fail_copy_settings(_source_db, _target_db):
        raise RuntimeError("settings copy failed")

    monkeypatch.setattr(eval_module, "copy_embedding_settings", fail_copy_settings)
    try:
        service = EvalService(
            db,
            cases_dir=cases_dir,
            model_service_factory=lambda temp_db, temperature: ExplodingModel(temp_db),
        )

        run = service.run(model="fake", benchmark_mode="retrieval_only")

        assert run["status"] == "error"
        assert "settings copy failed" in run["notes"]
        assert run["runtime_error_count"] == 2
        assert [case["stage"] for case in run["cases"]] == ["setup", "setup"]
    finally:
        db.close()


def test_running_benchmark_runs_are_marked_interrupted(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        service = EvalService(db, cases_dir=write_two_case_fixture(tmp_path))
        run = service._start_run(
            model="fake",
            verify=False,
            temperature=0.0,
            benchmark_mode="end_to_end",
            thinking_mode="off",
            answer_style="",
            repeat_count=1,
            num_predict=0,
        )

        recovered = service.recover_interrupted_runs(reason="sidecar restarted")
        history = service.history(limit=1)

        assert recovered == 1
        assert history[0]["id"] == run["id"]
        assert history[0]["status"] == "interrupted"
        assert "sidecar restarted" in history[0]["notes"]
        assert history[0]["runtime_error_count"] == 1
        assert history[0]["completed_at"] is not None
    finally:
        db.close()


def test_oracle_generation_uses_gold_evidence(tmp_path: Path):
    cases_dir = write_two_case_fixture(tmp_path)
    db = Database(tmp_path / "profile")
    model = RecordingOracleModel(db)
    try:
        service = EvalService(
            db,
            cases_dir=cases_dir,
            model_service_factory=lambda _temp_db, _temperature: model,
        )

        run = service.run(model="fake", benchmark_mode="oracle_generation")

        assert run["total_passed"] == 2
        assert "Alpha evidence" in model.messages_seen[0][1]["content"]
    finally:
        db.close()


def test_negation_aware_forbidden_matching():
    case = {
        "id": "negation",
        "required_source_document": "doc",
        "question": "Is there an emergency?",
        "expected_facts": [{"label": "no emergency", "any": ["no emergency"]}],
        "forbidden_claims": [{"label": "emergency", "any": ["There is an emergency"]}],
    }

    affirmative = evaluate_answer(case, "There is an emergency.", supplied_document_ids=["doc"], required_document_id="doc")
    negated = evaluate_answer(case, "There is no emergency.", supplied_document_ids=["doc"], required_document_id="doc")
    review = evaluate_answer(case, "Not only is there an emergency drill, there is an emergency.", supplied_document_ids=["doc"], required_document_id="doc")

    assert affirmative["forbidden_passed"] is False
    assert negated["forbidden_passed"] is True
    assert review["grader_review_required"] is True


def test_comparison_separates_mode_thinking_prompt_and_incomplete_runs():
    current = [
        comparison_run("a", mode="end_to_end", thinking="off", prompt="p1", passed=2),
        comparison_run("b", mode="end_to_end", thinking="on", prompt="p1", passed=3),
        comparison_run("c", mode="retrieval_only", thinking="off", prompt="p1", passed=3),
        comparison_run("d", mode="end_to_end", thinking="off", prompt="p2", passed=3),
        comparison_run("partial", mode="end_to_end", thinking="off", prompt="p1", passed=3, status="running"),
    ]

    comparison = benchmark_comparison(current)
    keys = {group["key"] for group in comparison["groups"]}

    assert len(keys) == 4
    assert comparison["incomplete_run_count"] == 1
    assert all(group["benchmark_mode"] != "retrieval_only" or not group["recommended"] for group in comparison["groups"])


def test_adversarial_only_score_does_not_control_recommendation():
    practical_winner = comparison_run(
        "practical",
        mode="end_to_end",
        thinking="off",
        prompt="p1",
        passed=1,
        cases=[case_result("core", True, "direct_extraction", True), case_result("adv", False, "negation_adversarial", False)],
    )
    adversarial_only = comparison_run(
        "adversarial",
        mode="end_to_end",
        thinking="off",
        prompt="p1",
        passed=1,
        cases=[case_result("core", False, "direct_extraction", True), case_result("adv", True, "negation_adversarial", False)],
    )

    comparison = benchmark_comparison([adversarial_only, practical_winner])

    assert comparison["recommended"]["model"] == "practical"


class CapturingOllamaService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.payloads: list[dict[str, Any]] = []

    def _tcp_reachable(self, host: str, port: int) -> bool:
        return True

    def _post_json(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.payloads.append(payload)
        return {
            "model": payload["model"],
            "message": {"content": "final", "thinking": "internal"},
            "done_reason": "stop",
            "total_duration": 3_000_000_000,
            "load_duration": 100,
            "prompt_eval_count": 20,
            "prompt_eval_duration": 1_000_000_000,
            "eval_count": 10,
            "eval_duration": 2_000_000_000,
        }

    def _get_json(self, url: str, timeout: float) -> dict[str, Any]:
        return {
            "models": [
                {
                    "name": "qwen3:8b",
                    "model": "qwen3:8b",
                    "size": 100,
                    "size_vram": 25,
                    "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M"},
                }
            ]
        }


class SimpleRAG:
    def build_quote_context(self, query: str, *, limit: int, document_ids: list[str] | None = None):
        snippet = {
            "snippet_id": "S1",
            "chunk_id": "chunk-alpha",
            "document_id": "doc",
            "source": "Fixture",
            "text": "Alpha evidence confirms alpha.",
            "score": 1,
            "page_start": 1,
            "page_end": 1,
            "metadata": {"title": "Fixture"},
        }
        return "[S1] Fixture\n- \"Alpha evidence confirms alpha.\"", [snippet], [snippet]


class TimeoutVerifierModel(ModelService):
    def chat_detailed(self, model: str, messages: list[dict[str, str]], **kwargs):
        if "Check the answer only against the retrieved evidence snippets" in messages[0]["content"]:
            raise ModelTimeoutError("timeout: verifier")
        return {"content": "The source confirms alpha.", "thinking": "", "elapsed_ms": 10}


class TimeoutCorrectionModel(ModelService):
    def chat_detailed(self, model: str, messages: list[dict[str, str]], **kwargs):
        if messages[-1]["content"].startswith("Revise the answer once"):
            raise ModelTimeoutError("timeout: correction")
        if "Check the answer only against the retrieved evidence snippets" in messages[0]["content"]:
            return {
                "content": json.dumps({"claims": [], "unsupported_claims": [], "contradicted_claims": ["beta"]}),
                "thinking": "",
                "elapsed_ms": 10,
            }
        return {"content": "The source says beta.", "thinking": "", "elapsed_ms": 10}


class OneTimeoutModel(ModelService):
    def chat(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any] | None = None) -> str:
        if "Beta question" in messages[-1]["content"]:
            raise ModelTimeoutError("timeout: answer")
        return "Alpha evidence confirms alpha."


class ExplodingModel(ModelService):
    def chat(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any] | None = None) -> str:
        raise AssertionError("retrieval-only mode must not call chat")


class RecordingOracleModel(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.messages_seen: list[list[dict[str, str]]] = []

    def chat(self, model: str, messages: list[dict[str, str]], *, options: dict[str, Any] | None = None) -> str:
        self.messages_seen.append(messages)
        if "Beta evidence" in messages[-1]["content"]:
            return "Beta evidence confirms beta."
        return "Alpha evidence confirms alpha."


def write_two_case_fixture(tmp_path: Path) -> Path:
    docs = tmp_path / "fixtures" / "documents"
    cases = tmp_path / "rag_cases"
    docs.mkdir(parents=True)
    cases.mkdir()
    (docs / "alpha.md").write_text("Alpha evidence confirms alpha.", encoding="utf-8")
    (docs / "beta.md").write_text("Beta evidence confirms beta.", encoding="utf-8")
    write_case(cases, "alpha", "Alpha question", "alpha_doc", "alpha.md", "Alpha evidence", "alpha")
    write_case(cases, "beta", "Beta question", "beta_doc", "beta.md", "Beta evidence", "beta")
    return cases


def write_case(cases: Path, case_id: str, question: str, doc_id: str, path: str, gold: str, fact: str) -> None:
    payload = {
        "id": case_id,
        "category": "direct_extraction",
        "difficulty": "easy",
        "benchmark_modes": ["retrieval_only", "oracle_generation", "end_to_end"],
        "counts_toward_primary_recommendation": True,
        "documents": [{"id": doc_id, "path": f"../fixtures/documents/{path}"}],
        "required_source_document": doc_id,
        "gold_chunk_text": gold,
        "question": question,
        "answer_style": "precise",
        "expected_facts": [{"label": fact, "any": [fact]}],
        "forbidden_claims": [],
        "source_policy": "all_retrieved",
        "retrieval_scope": "all",
    }
    (cases / f"{case_id}.json").write_text(json.dumps(payload), encoding="utf-8")


def comparison_run(
    model: str,
    *,
    mode: str,
    thinking: str,
    prompt: str,
    passed: int,
    status: str = "completed",
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    case_rows = cases or [case_result(f"case-{index}", index < passed, "direct_extraction", True) for index in range(3)]
    return {
        "id": model,
        "model": model,
        "verify": False,
        "suite_name": "local-rag",
        "suite_version": "v0.1.8",
        "total_passed": passed,
        "total_failed": len(case_rows) - passed,
        "average_latency_ms": 1000,
        "total_runtime_ms": 3000,
        "embedding_backend": "semantic",
        "embedding_model": "nomic-embed-text",
        "temperature": 0.0,
        "created_at": 1,
        "benchmark_mode": mode,
        "thinking_mode": thinking,
        "prompt_version": prompt,
        "answer_style": "precise",
        "num_predict": 0,
        "status": status,
        "timeout_count": 0,
        "cases": case_rows,
    }


def case_result(case_id: str, passed: bool, category: str, counts: bool) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "passed": passed,
        "expected_passed": passed,
        "forbidden_passed": True,
        "source_passed": passed,
        "latency_ms": 100,
        "case_category": category,
        "counts_toward_primary": counts,
        "benchmark_mode": "end_to_end",
        "retrieval_metrics": {"hit_at1": passed, "reciprocal_rank": 1.0 if passed else 0.0},
    }
