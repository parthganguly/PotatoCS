from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database


class DetailedFakeModelService(ModelService):
    def chat_detailed(self, model: str, messages: list[dict[str, str]], **_kwargs: Any) -> dict[str, Any]:
        return model_response(model=model)


class RecoveryArtifactStub:
    def __init__(self, message_id: str):
        self.analysis = {
            "id": "analysis-1",
            "message_id": message_id,
            "timings": {"elapsed_ms": 25},
        }

    def analysis_by_request(self, _request_id: str) -> dict[str, Any]:
        return self.analysis

    def attachments_for_messages(self, _message_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        return {}

    def hydrate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return messages


def model_response(**updates: Any) -> dict[str, Any]:
    response: dict[str, Any] = {
        "model": "llama3.2:latest",
        "content": "A compact local answer.",
        "thinking": "",
        "done_reason": "stop",
        "total_duration_ns": 1_500_000_000,
        "load_duration_ns": 25_000_000,
        "prompt_eval_count": 42,
        "prompt_eval_duration_ns": 100_000_000,
        "eval_count": 12,
        "eval_duration_ns": 500_000_000,
        "prompt_tokens_per_second": 420.0,
        "generation_tokens_per_second": 24.0,
        "elapsed_ms": 640,
        "raw": {},
    }
    response.update(updates)
    return response


def build_chat(tmp_path: Path) -> tuple[Database, SessionService, ChatService]:
    db = Database(tmp_path / "profile")
    sessions = SessionService(db)
    chat = ChatService(sessions, SettingsService(db), DetailedFakeModelService(db))
    return db, sessions, chat


def build_trace(chat: ChatService, **updates: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "model_response": model_response(),
        "analysis": None,
        "grounding": {"verifier": {"enabled": False, "status": "not_run"}},
        "timings": {"answer_latency_ms": 640, "verifier_latency_ms": 0, "correction_latency_ms": 0},
        "rag_enabled": False,
        "verifier_enabled": False,
        "retrieved_snippets": [],
        "selected_model": "llama3.2:latest",
        "selected_rag_preset": "standard",
        "selected_thinking_mode": "auto",
        "answer_style": "precise",
        "embedding_model": "nomic-embed-text",
        "embedding_backend": "ollama",
    }
    values.update(updates)
    return chat.build_operation_trace(**values)


def test_message_with_metadata_stores_and_retrieves_correctly(tmp_path: Path):
    db = Database(tmp_path / "profile")
    sessions = SessionService(db)
    session = sessions.create()
    metadata = {"operation_trace": {"schema_version": 1}}

    created = sessions.add_message(session["id"], "assistant", "answer", metadata=metadata)
    stored = db.conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (created["id"],)).fetchone()

    assert created["metadata"] == metadata
    assert json.loads(stored["metadata_json"]) == metadata
    assert sessions.messages(session["id"])[0]["metadata"] == metadata
    db.close()


def test_message_without_metadata_and_existing_callers_still_work(tmp_path: Path):
    db = Database(tmp_path / "profile")
    sessions = SessionService(db)
    session = sessions.create()

    message = sessions.add_message(session["id"], "assistant", "benchmark or eval answer")

    assert message["metadata"] == {}
    assert sessions.messages(session["id"])[0]["metadata"] == {}
    assert db.conn.execute("SELECT metadata_json FROM messages WHERE id = ?", (message["id"],)).fetchone()[0] == "{}"
    db.close()


def test_old_messages_return_empty_metadata_after_additive_migration(tmp_path: Path):
    profile = tmp_path / "old-profile"
    profile.mkdir()
    conn = sqlite3.connect(profile / "app.db")
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            model TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            last_message_at INTEGER
        );
        CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        INSERT INTO sessions(id, title, model, created_at, updated_at, last_message_at)
        VALUES ('old-session', 'Old chat', 'old-model', 1, 1, 1);
        INSERT INTO messages(id, session_id, role, content, created_at)
        VALUES ('old-message', 'old-session', 'assistant', 'Old answer', 1);
        """
    )
    conn.commit()
    conn.close()

    db = Database(profile)
    try:
        message_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(messages)")}
        assert "metadata_json" in message_columns
        assert SessionService(db).messages("old-session")[0]["metadata"] == {}
    finally:
        db.close()


def test_text_only_send_persists_exactly_one_operation_trace(tmp_path: Path):
    db, sessions, chat = build_chat(tmp_path)
    build_calls = 0
    original_builder = chat.build_operation_trace

    def counted_builder(**kwargs: Any) -> dict[str, Any]:
        nonlocal build_calls
        build_calls += 1
        return original_builder(**kwargs)

    chat.build_operation_trace = counted_builder  # type: ignore[method-assign]
    result = chat.send("What happened?")
    trace = result["assistant_message"]["metadata"]["operation_trace"]

    assert build_calls == 1
    assert trace["schema_version"] == 1
    assert trace["models"]["final_answer_model"] == "llama3.2"
    assert trace["timing"]["answer_latency_ms"] == 640
    assert trace["pipeline"]["rag_enabled"] is False
    assert trace["pipeline"]["verifier_enabled"] is False
    assert "vision_backend" not in trace["models"]
    assert sessions.messages(result["session"]["id"])[1]["metadata"]["operation_trace"] == trace
    db.close()


def test_recovered_multimodal_replay_returns_assistant_trace_metadata(tmp_path: Path):
    db, sessions, chat = build_chat(tmp_path)
    session = sessions.create()
    user_message = sessions.add_message(session["id"], "user", "Describe the image")
    trace = {"schema_version": 1, "pipeline": {"executed_multimodal_mode": "combined"}}
    sessions.add_message(
        session["id"],
        "assistant",
        "Recovered multimodal answer",
        metadata={"operation_trace": trace},
    )
    chat.artifacts = RecoveryArtifactStub(user_message["id"])  # type: ignore[assignment]

    recovered = chat._recover_multimodal_turn(
        "request-1",
        "precise",
        "standard",
        "auto",
        0.0,
    )

    assert recovered is not None
    assert recovered["user_message"]["metadata"] == {}
    assert recovered["assistant_message"]["metadata"]["operation_trace"] == trace
    assert recovered["messages"][1]["metadata"]["operation_trace"] == trace
    assert sessions.messages(session["id"])[1]["metadata"]["operation_trace"] == trace
    db.close()


def test_rag_trace_includes_only_safe_source_ids(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    trace = build_trace(
        chat,
        rag_enabled=True,
        verifier_enabled=True,
        grounding={"verifier": {"enabled": True, "status": "passed"}},
        retrieved_snippets=[
            {
                "chunk_id": "ocr:11111111-1111-1111-1111-111111111111:2",
                "document_id": "22222222-2222-2222-2222-222222222222",
                "page_start": 2,
                "page_end": 3,
                "text": "private retrieved chunk text",
                "source": "C:\\Users\\Someone\\private.pdf",
            }
        ],
    )

    assert trace["models"]["embedding_model"] == "nomic-embed-text"
    assert trace["models"]["embedding_backend"] == "ollama"
    assert trace["pipeline"]["verifier_status"] == "passed"
    assert trace["sources"] == {
        "retrieved_chunk_ids": ["ocr:11111111-1111-1111-1111-111111111111:2"],
        "retrieved_document_ids": ["22222222-2222-2222-2222-222222222222"],
        "retrieved_page_numbers": [2, 3],
    }
    assert "private retrieved chunk text" not in json.dumps(trace)
    db.close()


def test_multimodal_florence_trace_includes_backend_model_and_timings(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    trace = build_trace(
        chat,
        analysis={
            "actual_vision_backend": "florence2",
            "actual_vision_model": "microsoft/Florence-2-base-ft",
            "ocr_engine": "tesseract",
            "timings": {"elapsed_ms": 1800, "preprocessing_elapsed_ms": 90, "synthesis_elapsed_ms": 700},
            "evidence": {"vision": {"elapsed_ms": 820}},
            "output": {"provenance": {"mode_requested": "automatic", "mode_executed": "combined"}},
        },
    )

    assert trace["models"]["vision_backend"] == "florence2"
    assert trace["models"]["vision_model"] == "microsoft/Florence-2-base-ft"
    assert trace["models"]["ocr_engine"] == "tesseract"
    assert trace["timing"]["preprocessing_elapsed_ms"] == 90
    assert trace["timing"]["vision_elapsed_ms"] == 820
    assert trace["timing"]["synthesis_elapsed_ms"] == 700
    assert trace["timing"]["total_vision_elapsed_ms"] == 1800
    db.close()


def test_multimodal_ollama_trace_includes_backend_model_and_tokens(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    trace = build_trace(
        chat,
        model_response=model_response(prompt_eval_count=81, eval_count=23, generation_tokens_per_second=18.5),
        analysis={
            "actual_vision_backend": "ollama",
            "actual_vision_model": "qwen2.5vl:7b",
            "timings": {"elapsed_ms": 1200, "synthesis_elapsed_ms": 640},
            "evidence": {"vision": {"elapsed_ms": 510}},
            "output": {"provenance": {"mode_requested": "vision_only", "mode_executed": "vision_only"}},
        },
    )

    assert trace["models"]["vision_backend"] == "ollama"
    assert trace["models"]["vision_model"] == "qwen2.5vl:7b"
    assert trace["tokens"]["prompt_tokens"] == 81
    assert trace["tokens"]["completion_tokens"] == 23
    assert trace["tokens"]["generation_tokens_per_second"] == 18.5
    db.close()


def test_reused_visual_evidence_trace_marks_reuse_without_rerun(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    trace = build_trace(
        chat,
        analysis={
            "output": {
                "provenance": {
                    "context_evidence_action": "reused",
                    "mode_requested": "automatic",
                    "mode_executed": "combined",
                }
            }
        },
    )

    assert trace["pipeline"]["visual_evidence_reused"] is True
    assert trace["pipeline"]["vision_rerun"] is False
    assert trace["pipeline"]["context_evidence_action"] == "reused"
    db.close()


def test_trace_never_persists_absolute_paths_or_base64_images(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    encoded_image = "data:image/png;base64," + ("A" * 256)
    trace = build_trace(
        chat,
        model_response=model_response(
            raw={"images": [encoded_image], "stored_path": "C:\\Users\\Parth\\secret.png"}
        ),
        analysis={
            "source_path": "C:\\Users\\Parth\\source.png",
            "stored_path": "C:\\Users\\Parth\\stored.png",
            "actual_vision_backend": "ollama",
            "actual_vision_model": "C:\\models\\private-model",
            "warnings": ["C:\\Users\\Parth\\vision failed", encoded_image, "Safe local vision fallback was used."],
        },
        retrieved_snippets=[
            {
                "chunk_id": "safe-chunk",
                "document_id": "safe-document",
                "source_path": "C:\\Users\\Parth\\private.pdf",
                "stored_path": "C:\\profile\\documents\\private.pdf",
                "image": encoded_image,
            }
        ],
    )
    serialized = json.dumps(trace)

    assert "C:\\" not in serialized
    assert "data:image" not in serialized
    assert "A" * 80 not in serialized
    assert trace["warnings"] == ["Safe local vision fallback was used."]
    assert "vision_model" not in trace["models"]
    db.close()


def test_trace_never_persists_raw_prompt_response_ocr_or_rag_text(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    secrets = {
        "prompt": "SYSTEM-PROMPT-SENTINEL",
        "response": "RAW-OLLAMA-RESPONSE-SENTINEL",
        "ocr": "FULL-OCR-TEXT-SENTINEL",
        "rag": "FULL-RAG-CONTEXT-SENTINEL",
        "chunk": "RETRIEVED-CHUNK-TEXT-SENTINEL",
    }
    trace = build_trace(
        chat,
        model_response=model_response(
            raw={"system_prompt": secrets["prompt"], "response": secrets["response"]}
        ),
        analysis={
            "output": {"ocr_text": secrets["ocr"], "full_rag_context": secrets["rag"]},
            "evidence": {"ocr": {"text": secrets["ocr"]}},
        },
        retrieved_snippets=[
            {"chunk_id": "chunk-1", "document_id": "document-1", "text": secrets["chunk"]}
        ],
    )
    serialized = json.dumps(trace)

    for secret in secrets.values():
        assert secret not in serialized
    db.close()


def test_thinking_text_is_not_persisted_by_default(tmp_path: Path):
    db, _sessions, chat = build_chat(tmp_path)
    thinking = "MODEL-TRACE-SECRET " * 250
    trace = build_trace(chat, model_response=model_response(thinking=thinking))
    serialized = json.dumps(trace)

    assert trace["model_trace"] == {
        "thinking_returned": True,
        "thinking_char_count": len(thinking),
        "thinking_truncated": False,
    }
    assert thinking not in serialized
    assert "MODEL-TRACE-SECRET" not in serialized
    assert "thinking_text" not in serialized
    db.close()
