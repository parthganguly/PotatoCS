"""Evidence packet, request spans, persisted answer evidence/review and source reading.

All offline: lexical embeddings and scripted model doubles; no inference.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.cancellation import TurnDeadline
from odysseus_desktop_backend.services.answer_evidence import (
    COMPLETENESS_NOT_VERIFIED,
    citation_ids,
    request_spans,
    review_answer,
)
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import (
    MAX_EVIDENCE_SNIPPET_CHARS,
    MAX_EVIDENCE_SNIPPETS,
    RAGService,
    complete_document_block,
)
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database
from rpc_server import RpcError, SidecarApp
from test_chat_cold_model_lifecycle import FakeOllama, app, ollama  # noqa: F401 - fixtures
from test_chat_turn_contracts import Clock, ClockModels, run_turn

# A different domain from the release-note diagnostic: nested actions under one
# change, an independent change, an outcome and a limitation.
MANUAL = """# Greenhouse controller 2.4

## Changes

- **Irrigation watchdog**: when a valve stalls, the controller now raises an alarm.
  - A **Flush line** action lets the grower re-open the stalled valve.
  - Successful flushing clears the alarm.
- **Sensor calibration**: humidity offsets moved out of the `cache/` folder so a
  cache reset can no longer erase them.

```
reset --cache   # keeps calibration now
```

## Limits

- No support for CO2 dosing. Version 2.4.1 is not covered.
"""
QUESTION = (
    "What are the two changes, what can a grower do when a valve stalls, "
    "and what happens after flushing? Also explain what this release does not support."
)


class CapturingModels(ModelService):
    def __init__(self, db: Database, reply: str):
        super().__init__(db)
        self.reply = reply
        self.calls: list[list[dict[str, Any]]] = []

    def chat(self, model: str, messages: list[dict[str, str]], **_: Any) -> str:
        self.calls.append(messages)
        return self.reply


def build(tmp_path: Path, reply: str = "### Q1: x [S1]"):
    db = Database(tmp_path / "profile")
    db.set_setting("embedding_backend", "lexical")
    documents = DocumentService(db)
    rag = RAGService(documents, EmbeddingService(db), SQLiteNumPyVectorStore(db))
    models = CapturingModels(db, reply)
    sessions = SessionService(db)
    chat = ChatService(sessions, SettingsService(db), models, rag=rag, documents=documents)
    return db, documents, rag, models, sessions, chat


def import_indexed(documents: DocumentService, rag: RAGService, path: Path, text: str) -> dict[str, Any]:
    path.write_text(text, encoding="utf-8")
    document = documents.import_document(str(path))
    rag.index_document(document["id"])
    return documents.get(document["id"])


# ------------------------------------------------------------ request spans


def test_request_spans_split_only_explicit_asks_and_keep_verbatim_text():
    assert request_spans(QUESTION) == [
        "What are the two changes",
        "what can a grower do when a valve stalls",
        "what happens after flushing?",
        "Also explain what this release does not support.",
    ]
    original = (
        "I'm deciding whether to install v0.3.1. What are the two practical fixes, what can I do if the "
        "backend becomes unavailable, and what should I verify before running the installer? Also explain "
        "what this patch does not add."
    )
    spans = request_spans(original)
    assert len(spans) == 4 and all(span in original for span in spans)
    assert spans[0] == "What are the two practical fixes"


@pytest.mark.parametrize(
    "question",
    [
        "Compare apples, oranges, and pears.",
        "Describe the API, state management, and routing.",
        "What changed in 2.1.4, 2.2 and 3.0?",
        'What does "stop, what now" mean?',
        "What does `retry, what next` print?",
        "Summarize the document.",
        "What is X? Keep it short.",
        "What happened and when?",
    ],
)
def test_request_spans_do_not_split_lists_versions_quotes_or_code(question: str):
    assert request_spans(question) == [question]


def test_request_spans_keep_code_and_versions_whole_inside_real_splits():
    spans = request_spans("What does `a, what b` do in v2.4.1, and how is it reset?")
    assert spans == ["What does `a, what b` do in v2.4.1", "how is it reset?"]


@pytest.mark.parametrize("quoted", [
    "'what changed and why it failed'",
    "‘what changed and why it failed’",
    "'what's changed and why it's different'",
    "‘what’s changed and why it’s different’",
])
def test_request_spans_protect_single_quoted_passages(quoted: str):
    question = f"Explain {quoted}. Also describe the limits."
    assert request_spans(question) == [f"Explain {quoted}.", "Also describe the limits."]
    joined = f"Explain {quoted} and list the limits."
    assert request_spans(joined) == [joined]


def test_apostrophes_do_not_hide_independent_asks():
    assert request_spans("What's the writer's plan, and how does it work?") == [
        "What's the writer's plan", "how does it work?",
    ]


def test_complete_page_keeps_boundary_newlines_exactly():
    text = "\n\n# Procedure\n\n- Prepare\n  - Check the valve\n\n\n"
    block = complete_document_block({"snippet_id": "S1", "source": "Procedure", "page_start": 1, "text": text})
    assert block == (
        "[S1] Procedure, page 1 (complete imported Markdown document)\n"
        f"----- begin S1 -----\n{text}\n----- end S1 -----"
    )


def test_prompt_allows_explicit_substep_counts(tmp_path: Path):
    db, documents, rag, models, _sessions, chat = build(tmp_path)
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)
    chat.send("How many substeps does the watchdog have? Also explain their outcomes.", use_rag=True, document_ids=[document["id"]])
    system = models.calls[0][0]["content"]
    user = models.calls[0][-1]["content"]
    assert "count the category the user asks for" in system
    assert "including substeps when requested" in user
    assert "do not count it as a separate item" not in system
    db.close()


# ------------------------------------------------------------ review


def test_review_never_passes_and_flags_absent_unknown_and_missing_sections():
    review = review_answer("No citations at all.", evidence_ids=["S1"], expected_sections=2, coverage="complete_markdown")
    assert review["completeness"] == "not_verified"
    assert review["completeness_label"] == COMPLETENESS_NOT_VERIFIED
    assert review["citations_absent"] is True
    assert review["missing_sections"] == ["Q1", "Q2"]

    unknown = review_answer("### Q1\nA [S1, S4]\n### Q2\nB [S9]", evidence_ids=["S1"], expected_sections=2, coverage="excerpts")
    assert unknown["valid_ids"] == ["S1"]
    assert unknown["unknown_ids"] == ["S4", "S9"]
    assert unknown["missing_sections"] == []
    assert any("not the complete source" in warning for warning in unknown["warnings"])

    # A valid, resolvable citation on an unsupported claim is still not a pass.
    valid = review_answer("### Q1: The moon is cheese [S1].", evidence_ids=["S1"], expected_sections=1, coverage="complete_markdown")
    assert valid["warnings"] == []
    assert valid["completeness"] == "not_verified"
    assert "passed" not in json.dumps(valid)


def test_citation_ids_parse_only_bracketed_tokens():
    assert citation_ids("See [S1] and [S2; S3], not S4 or [S]") == ["S1", "S2", "S3"]


# Shared with scripts/test-answer-evidence.mjs, which checks the UI against the same cases.
REFERENCE_CASES = json.loads((Path(__file__).parent / "fixtures" / "source_reference_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", REFERENCE_CASES["cases"], ids=lambda case: case["name"])
def test_shared_reference_cases_define_the_saved_review(case: dict[str, Any]):
    review = review_answer(
        case["text"], evidence_ids=REFERENCE_CASES["evidence_ids"], expected_sections=0, coverage="complete_markdown"
    )
    assert review["cited_ids"] == case["cited"]
    assert review["valid_ids"] == case["valid"]
    assert review["unknown_ids"] == case["unknown"]
    assert [item["text"] for item in review["malformed_references"]] == case["malformed"]
    assert review["malformed_reference_count"] == len(case["malformed"])
    for item in review["malformed_references"]:
        assert case["text"][item["start"] : item["end"]] == item["text"]
    assert review["warnings"] == case["warnings"]
    assert review["completeness"] == "not_verified"


def test_malformed_locations_are_recorded_and_the_list_is_bounded():
    review = review_answer("Line one [S1].\nLine two [S.2].", evidence_ids=["S1"], expected_sections=0, coverage="complete_markdown")
    assert review["malformed_references"] == [{"text": "[S.2]", "start": 24, "end": 29, "line": 2}]
    many = " ".join(f"[S.{number}]" for number in range(25))
    review = review_answer(many, evidence_ids=["S1"], expected_sections=0, coverage="complete_markdown")
    assert review["malformed_reference_count"] == 25
    assert len(review["malformed_references"]) == 20
    assert review["warnings"][-1].endswith("[S.19] and 5 more.")


def test_malformed_reference_is_saved_without_touching_the_answer(tmp_path: Path):
    reply = "Returns go to inspection [S1]. No pilot end date is set [S.1]."
    db, documents, rag, models, sessions, chat = build(tmp_path, reply)
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)
    result = chat.send("What happens to returns?", use_rag=True, document_ids=[document["id"]])
    assert len(models.calls) == 1  # no repair or second inference
    saved = sessions.messages(result["session"]["id"])[-1]
    assert saved["content"] == reply
    evidence = saved["metadata"]["answer_evidence"]
    assert evidence["schema_version"] == 2
    assert evidence["review"]["valid_ids"] == ["S1"]
    assert evidence["review"]["malformed_references"] == [{"text": "[S.1]", "start": 56, "end": 61, "line": 1}]
    assert "Malformed source reference, not linked to any source: [S.1]." in evidence["review"]["warnings"]
    db.close()


def test_reading_a_schema_1_answer_leaves_its_review_as_saved(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        session = app.sessions.create(model="llama3.2:latest")
        legacy = {
            "schema_version": 1,
            "coverage": "complete_markdown",
            "evidence": [],
            "review": {"cited_ids": [], "valid_ids": [], "unknown_ids": [], "citations_absent": True, "warnings": []},
        }
        app.sessions.add_message(session["id"], "assistant", "No end date [S.1].", {"answer_evidence": legacy})
        for _ in range(2):
            saved = app.dispatch("sessions.messages", {"session_id": session["id"]})[-1]
            assert saved["content"] == "No end date [S.1]."
            assert saved["metadata"]["answer_evidence"] == legacy  # not re-checked, not rewritten
    finally:
        app.close()


# ------------------------------------------------------------ packet


def test_small_markdown_is_supplied_verbatim_with_hierarchy(tmp_path: Path):
    db, documents, rag, *_ = build(tmp_path)
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)
    context, results, snippets = rag.build_quote_context(QUESTION, document_ids=[document["id"]], complete_markdown=True)
    assert len(snippets) == 1
    snippet = snippets[0]
    assert snippet["snippet_id"] == "S1"
    assert snippet["coverage"] == "complete_document"
    assert snippet["text"] == MANUAL
    assert snippet["chunk_id"] == ""
    assert snippet["retrieved_chunk_ids"] and set(snippet["retrieved_chunk_ids"]) <= {r["chunk_id"] for r in results}
    assert snippet["document_content_hash"] == document["content_hash"]
    # Nesting, indentation and the code fence survive in the prompt packet.
    assert "  - A **Flush line** action lets the grower re-open the stalled valve.\n  - Successful flushing clears the alarm." in context
    assert "```\nreset --cache   # keeps calibration now\n```" in context
    assert context.startswith("[S1] manual, page 1 (complete imported Markdown document)\n----- begin S1 -----\n# Greenhouse")
    assert len(context) <= MAX_EVIDENCE_SNIPPETS * MAX_EVIDENCE_SNIPPET_CHARS + 200
    db.close()


def test_large_mixed_and_non_markdown_fall_back_to_excerpts(tmp_path: Path):
    db, documents, rag, *_ = build(tmp_path)
    large = import_indexed(documents, rag, tmp_path / "large.md", MANUAL + ("- filler valve line.\n" * 400))
    txt = import_indexed(documents, rag, tmp_path / "manual.txt", MANUAL)
    small = import_indexed(documents, rag, tmp_path / "small.md", MANUAL)

    for ids in ([large["id"]], [txt["id"]], [small["id"], txt["id"]]):
        _context, _results, snippets = rag.build_quote_context(QUESTION, document_ids=ids, complete_markdown=True)
        assert snippets and all(snippet["coverage"] == "excerpt" for snippet in snippets), ids
        assert all(len(snippet["text"]) <= MAX_EVIDENCE_SNIPPET_CHARS + 3 for snippet in snippets)

    # Without the Standard opt-in, and with max_snippets=0, nothing is expanded.
    _c, _r, snippets = rag.build_quote_context(QUESTION, document_ids=[small["id"]])
    assert all(snippet["coverage"] == "excerpt" for snippet in snippets)
    context, _r, snippets = rag.build_quote_context(QUESTION, document_ids=[small["id"]], max_snippets=0, complete_markdown=True)
    assert (context, snippets) == ("", [])
    db.close()


# ------------------------------------------------------------ chat prompt + persistence


def test_standard_markdown_prompt_is_concise_sectioned_and_persisted(tmp_path: Path):
    reply = "### Q1: Two changes [S1]\n### Q3: Alarm clears [S7]\nNothing else."
    db, documents, rag, models, sessions, chat = build(tmp_path, reply)
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)
    result = chat.send(QUESTION, use_rag=True, document_ids=[document["id"]])

    system, user = models.calls[0][0]["content"], models.calls[0][-1]["content"]
    assert MANUAL.strip() in system
    for irrelevant in ("OCR", "Rebecca", "biography", "Warsaw", "chronolog"):
        assert irrelevant not in system
    assert QUESTION not in system  # the question appears once, in the user message
    assert user.startswith(QUESTION)
    assert user.count(QUESTION) == 1
    for number, span in enumerate(request_spans(QUESTION), start=1):
        assert f"### Q{number}: {span}" in user

    assistant = result["assistant_message"]
    assert assistant["content"] == reply  # never repaired or re-cited
    evidence = assistant["metadata"]["answer_evidence"]
    assert evidence["coverage"] == "complete_markdown"
    assert evidence["evidence"][0]["text"] == MANUAL
    assert evidence["evidence"][0]["id"] == "S1"
    review = evidence["review"]
    assert review["missing_sections"] == ["Q2", "Q4"]
    assert review["observed_sections"] == ["Q1", "Q3"]
    assert review["valid_ids"] == ["S1"] and review["unknown_ids"] == ["S7"]
    assert review["completeness"] == "not_verified"
    # Diagnostics stay content-free.
    trace = json.dumps(assistant["metadata"]["operation_trace"])
    assert "Flush line" not in trace and "Greenhouse" not in trace
    assert assistant["metadata"]["operation_trace"]["sources"]["retrieved_chunk_ids"]

    # Reopen: each answer keeps its own evidence, not the latest turn's.
    other = import_indexed(documents, rag, tmp_path / "other.md", "# Bakery\n\n- Ovens preheat to 220 C.\n")
    models.reply = "Ovens preheat to 220 C [S1]."
    chat.send("What temperature do ovens preheat to?", session_id=result["session"]["id"], use_rag=True, document_ids=[other["id"]])
    answers = [m for m in sessions.messages(result["session"]["id"]) if m["role"] == "assistant"]
    assert [a["metadata"]["answer_evidence"]["evidence"][0]["document_id"] for a in answers] == [document["id"], other["id"]]
    assert answers[0]["metadata"]["answer_evidence"] == evidence
    assert answers[1]["metadata"]["answer_evidence"]["sections_requested"] is False
    db.close()


def test_potato_mode_keeps_excerpts_and_three_section_contract(tmp_path: Path):
    db, documents, rag, models, _sessions, chat = build(tmp_path, "Answer:\nx\n\nEvidence:\nS1\n\nNot found / cannot confirm:\nNone")
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)
    result = chat.send(QUESTION, use_rag=True, document_ids=[document["id"]], rag_preset="potato")
    system, user = models.calls[0][0]["content"], models.calls[0][-1]["content"]
    assert "Potato Mode preset" in system
    assert "complete imported Markdown" not in system
    assert "### Q1" not in user
    evidence = result["assistant_message"]["metadata"]["answer_evidence"]
    assert evidence["coverage"] == "excerpts"
    assert evidence["sections_requested"] is False
    assert evidence["review"]["missing_sections"] == []
    db.close()


def test_verifier_findings_survive_in_saved_evidence(tmp_path: Path):
    class VerifyingModels(CapturingModels):
        def chat_detailed(self, model: str, messages: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
            if kwargs.get("response_format") == "json":
                content = json.dumps({"claims": [{"text": "Valves self-heal", "status": "unsupported"}]})
            else:
                content = "Valves self-heal [S1]."
            return {"model": model, "content": content, "thinking": "", "elapsed_ms": 1}

    db = Database(tmp_path / "profile")
    db.set_setting("embedding_backend", "lexical")
    documents = DocumentService(db)
    rag = RAGService(documents, EmbeddingService(db), SQLiteNumPyVectorStore(db))
    sessions = SessionService(db)
    chat = ChatService(sessions, SettingsService(db), VerifyingModels(db, ""), rag=rag, documents=documents)
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)
    result = chat.send("What happens when a valve stalls?", use_rag=True, document_ids=[document["id"]], verify_rag=True)
    saved = sessions.messages(result["session"]["id"])[-1]["metadata"]["answer_evidence"]
    assert saved["verifier"]["status"] == "failed"
    assert saved["verifier"]["unsupported_claims"] == ["Valves self-heal"]
    assert saved["review"]["valid_ids"] == ["S1"]
    assert saved["review"]["completeness"] == "not_verified"
    db.close()


# ------------------------------------------------------------ source reading


def test_read_source_reads_only_the_owned_copy_with_honest_states(tmp_path: Path):
    db, documents, rag, *_ = build(tmp_path)
    document = import_indexed(documents, rag, tmp_path / "manual.md", MANUAL)

    page = documents.read_imported_source(document["id"], max_chars=40)
    assert page["state"] == "available"
    assert page["text"] == MANUAL[:40] and page["next_offset"] == 40 and page["line_start"] == 1
    rest = documents.read_imported_source(document["id"], offset=40)
    assert page["text"] + rest["text"] == MANUAL and rest["next_offset"] is None
    assert documents.read_imported_source(document["id"], expected_content_hash="0" * 64)["state"] == "changed"

    # The external original is irrelevant once imported.
    (tmp_path / "manual.md").unlink()
    assert documents.read_imported_source(document["id"])["state"] == "available"

    txt = import_indexed(documents, rag, tmp_path / "plain.txt", "plain text body for reading")
    assert documents.read_imported_source(txt["id"])["state"] == "unsupported"
    assert documents.read_imported_source("no-such-id")["state"] == "not_found"

    stored = Path(document["stored_path"])
    stored.write_text(MANUAL + "tampered\n", encoding="utf-8")
    changed = documents.read_imported_source(document["id"])
    assert changed["state"] == "changed" and changed["text"] == ""

    # A stored_path pointing outside the profile is never followed.
    outside = tmp_path / "outside.md"
    outside.write_text(MANUAL, encoding="utf-8")
    db.conn.execute("UPDATE documents SET stored_path = ? WHERE id = ?", (str(outside), document["id"]))
    db.conn.commit()
    escaped = documents.read_imported_source(document["id"])
    assert escaped["state"] == "missing" and escaped["text"] == ""

    stored.unlink()
    db.conn.execute("UPDATE documents SET stored_path = ? WHERE id = ?", (str(stored), document["id"]))
    db.conn.commit()
    assert documents.read_imported_source(document["id"])["state"] == "missing"
    rag.delete_document(document["id"])
    assert documents.read_imported_source(document["id"])["state"] == "deleted"
    db.close()


def test_read_source_rpc_takes_a_document_id_not_a_path(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        source = tmp_path / "notes.md"
        source.write_text(MANUAL, encoding="utf-8")
        document = app.documents.import_document(str(source))
        result = app.dispatch("documents.read_source", {"document_id": document["id"], "path": str(source)})
        assert result["state"] == "available" and result["text"] == MANUAL
        # A path is not a document ID.
        assert app.dispatch("documents.read_source", {"document_id": str(source)})["state"] == "not_found"
        with pytest.raises(RpcError):
            app.dispatch("documents.read_source", {"document_id": document["id"], "offset": -1})
        with pytest.raises(RpcError):
            app.dispatch("documents.read_source", {})
    finally:
        app.close()


# ------------------------------------------------------------ tracked (job) turn


def test_tracked_turn_commits_its_evidence_atomically(app: SidecarApp) -> None:  # noqa: F811
    source = app.profile_dir.parent / "manual.md"
    source.write_text(MANUAL, encoding="utf-8")
    document = app.documents.import_document(str(source))
    app.rag.index_document(document["id"])
    session = app.sessions.create(model="llama3.2:latest")
    turn, _ = app.turns.accept(
        session_id=session["id"],
        content=QUESTION,
        attachment_document_ids=[],
        request={"model": "llama3.2:latest", "use_rag": True, "document_ids": [document["id"]]},
        operation_key=str(uuid.uuid4()),
    )
    clock = Clock()
    models = ClockModels(clock, prepare=[(1, None)], generate=[(1, "### Q1: changes [S1]")])
    run_turn(app, models, TurnDeadline(300, clock), turn=turn)
    answer = [m for m in app.dispatch("sessions.messages", {"session_id": session["id"]}) if m["role"] == "assistant"][-1]
    evidence = answer["metadata"]["answer_evidence"]
    assert evidence["coverage"] == "complete_markdown"
    assert evidence["review"]["missing_sections"] == ["Q2", "Q3", "Q4"]
    assert "### Q4: Also explain what this release does not support." in models.messages[0][-1]["content"]
