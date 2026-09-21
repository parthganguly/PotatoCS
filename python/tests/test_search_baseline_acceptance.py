"""Acceptance gaps at the preserved baseline; failures must remain visible."""
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from odysseus_desktop_backend.services.search_service import (
    SearchBudget, SearchBudgetError, SearchMetrics, SearchService,
    build_evidence_spans, evidence_selection_prompt, estimate_prompt_tokens,
    pack_evidence_window, resolve_model_context_tokens, verify_evidence_span_selection,
)
from odysseus_desktop_backend.services.model_service import ModelServiceError
from odysseus_desktop_backend.services.job_service import DocumentJobExecutor, JobFailure, JobRecord
from odysseus_desktop_backend.services.search_provider import FixtureSearchProvider
from odysseus_desktop_backend.storage import Database
from test_search_context_budget import StubModelService, passage, select
from test_search_v0 import FixtureFetcher, build_search_service


def test_small_reported_context_is_never_raised():
    limit, _ = resolve_model_context_tokens(StubModelService("{}", loaded_context=128), "fixture-model")
    assert limit <= 128


def test_impossible_window_does_not_force_first_span():
    spans = build_evidence_spans([passage("A retained sentence with several useful words.", passage_id="p")])
    window = pack_evidence_window("A question", spans, [], input_budget_tokens=1)
    assert not window.spans, "no evidence fits; caller must handle the fixed-overhead failure"


def test_impossible_complete_request_never_reaches_model():
    service = SearchService.__new__(SearchService)
    service.models = StubModelService('{"span_ids":[],"needs_more_search":false}', loaded_context=2048)
    try:
        service._select_evidence(
            "Q" * 4000, [passage("A retained sentence with useful context.", passage_id="p")],
            "fixture-model", SearchBudget(), SearchMetrics(), [], time.monotonic() + 30, warnings=[],
        )
    except SearchBudgetError:
        pass
    assert service.models.prompts == [], "fixed request overhead already exceeds the budget"


def test_configured_budget_agrees_with_requested_runtime_context():
    result = select([passage("A retained sentence with useful context.", passage_id="p")],
                    '{"span_ids":[],"needs_more_search":false}', configured_context=8192)
    assert result["models"].last_options.get("num_ctx") == result["metrics"].evidence_context_limit_tokens


def test_synthesis_complete_request_and_generation_fit_context():
    question = "Q" * 2500
    dossier = [passage("Long factual context " * 24 + ".", passage_id=f"p{i}") for i in range(8)]
    result = select(dossier, json.dumps({"span_ids": [f"P{i}:S1" for i in range(1, 9)],
                                        "needs_more_search": False}), question=question)
    assert not result["metrics"].evidence_window_truncated
    evidence, _ = verify_evidence_span_selection(result["selected"], result["spans"], dossier, SearchMetrics(), [])
    assert len(evidence) == 8
    service = SearchService.__new__(SearchService)
    service.models = StubModelService("An answer [E1].", loaded_context=4096)
    try:
        service._synthesize(question, evidence, "fixture-model", SearchBudget(),
                            SearchMetrics(), [], time.monotonic() + 30)
    except SearchBudgetError:
        pass
    # A bounded request or a typed refusal are both acceptable.
    if service.models.prompts:
        assert estimate_prompt_tokens(service.models.prompts[0]) + 1200 + 256 <= 4096


def test_malformed_selection_does_not_manufacture_relevant_support():
    result = select([passage("The unrelated Taro system has a bright blue cover.", passage_id="p")],
                    "broken JSON", question="What is Vela's maintenance threshold?")
    # Malformation is already observable; arbitrary position is not evidence relevance.
    assert result["diagnostics"][0].rejection_code == "json_decode_failed"
    assert result["selected"] == [], "fallback needs a separately justified support policy"


@pytest.mark.parametrize("reply_kind", ["valid", "malformed", "unknown_pointer", "exception"])
def test_private_content_stays_out_of_durable_diagnostics(tmp_path, caplog, reply_kind):
    secret = "SYNTHETIC_PRIVATE_SENTINEL_9381"
    private_text = f"The calibration note contains {secret} and remains confidential."
    seen = []

    class Model:
        def chat_detailed(self, model, messages, **kwargs):
            prompt = messages[-1]["content"]
            if "Generate concise public-web search formulations" in prompt:
                content = '{"queries":[]}'
            elif "Select only identifiers for spans" in prompt:
                assert private_text in prompt and "[P1] " + secret in prompt
                seen.append("selection")
                if reply_kind == "exception":
                    raise ModelServiceError(private_text)
                content = {"valid": '{"span_ids":["P1:S1"],"needs_more_search":false}',
                           "malformed": secret + " {",
                           "unknown_pointer": json.dumps({"span_ids": [secret], "needs_more_search": False})}[reply_kind]
            else:
                assert private_text in prompt
                seen.append("synthesis")
                content = private_text + " [E1]"
            return {"model": model, "content": content, "done_reason": "stop"}

    db, sessions, service = build_search_service(tmp_path / "profile", FixtureSearchProvider({}), FixtureFetcher({}), Model())
    try:
        path = tmp_path / (secret + ".txt")
        path.write_text(private_text, encoding="utf-8")
        doc = service.documents.import_document(str(path))
        service.rag.index_document(doc["id"])
        session = sessions.create(model="fixture-model")
        try:
            service.run(question="calibration note", session_id=session["id"], model="fixture-model", second_round_enabled=False)
        except Exception as exc:
            from odysseus_desktop_backend.services.search_service import SearchNoEvidenceError
            assert isinstance(exc, (ModelServiceError, SearchNoEvidenceError))
        rows = {table: [dict(r) for r in db.conn.execute(f"SELECT * FROM {table}")]
                for table in ("search_runs", "search_evidence_diagnostics")}
        assert "selection" in seen
        messages = sessions.messages(session["id"])
        if reply_kind == "valid":
            assert "synthesis" in seen
            assert private_text in messages[-1]["content"]
        if reply_kind == "malformed":
            # Malformed selection no longer synthesizes; the private passage reaches the
            # owner as retrieval output instead, which is user content, not a diagnostic.
            assert "synthesis" not in seen
            results = messages[-1]["metadata"]["search_results"]
            assert results["outcome"] == "results_only"
            assert any(private_text in item["text"] for item in results["passages"])
        traces = [m["metadata"].get("operation_trace", {}) for m in messages]
        assert secret not in json.dumps({"rows": rows, "traces": traces})
        assert secret not in caplog.text
        # Stored source/evidence, retrieval results and conversation are user content,
        # not diagnostics.
    finally:
        db.close()


def test_missing_external_configuration_is_not_zero_matches(tmp_path, monkeypatch):
    import odysseus_desktop_backend.services.search_provider as providers
    original = providers.configured_search_provider
    monkeypatch.setattr(providers, "configured_search_provider", lambda: original({}))
    db = Database(tmp_path)
    try:
        executor = DocumentJobExecutor.__new__(DocumentJobExecutor)
        executor.db = db
        executor.services = SimpleNamespace()
        job = JobRecord(id="missing", kind="search", session_id="s", query="q", model="fixture")
        with pytest.raises(JobFailure) as error:
            executor._run_search(job, lambda: None)
        assert error.value.code == "search_provider_unconfigured"
    finally:
        db.close()


def test_replay_schedule_coverage_and_decorative_citation_are_not_semantic_grades():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import search_replay as replay
    manifest = json.loads(replay.MANIFEST.read_text(encoding="utf-8"))
    order = replay.schedule(manifest)
    assert len(order) == 36
    for case in manifest["cases"]:
        for arm in ("A", "B"):
            assert sum(t["case_id"] == case["id"] and t["arm"] == arm for t in order) == 3
        dossier = replay.dossier_for(case)
        spans = build_evidence_spans(dossier)
        window = pack_evidence_window(case["question"], spans, [], input_budget_tokens=3046)
        assert window.spans == spans
        assert all(p.text in replay.compact_prompt(case["question"], dossier) for p in dossier)
        structure = replay.answer_structure("No answer is established. [E1]", replay.citation_mapping(dossier))
        assert structure["citation_membership"]
        assert "correct" not in structure and "supported" not in structure
        assert case["expected"] not in replay.compact_prompt(case["question"], dossier)
        assert case["expected"] not in evidence_selection_prompt(case["question"], spans, [])
