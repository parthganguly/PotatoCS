"""Launch-scoped private Ollama runtime and the opt-in experimental candidate.

Offline: two in-process fake peers stand in for the private runtime and the ordinary
11434 runtime; the ordinary one must never be contacted while the override is set.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.services import model_service
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import (
    EXPERIMENTAL_MODEL,
    EXPERIMENTAL_MODEL_DIGEST,
    OLLAMA_ENDPOINT,
    PRIVATE_ENDPOINT_ENV,
    ModelConnectionError,
    ModelIdentityError,
    ModelService,
    canonical_model_tag,
    is_experimental_model,
    ollama_endpoint,
    private_endpoint,
)
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database
from rpc_server import RpcError, SidecarApp
from test_chat_cold_model_lifecycle import wait_terminal

EMBED = "nomic-embed-text:latest"
OTHER_DIGEST = "f" * 64
MEMO = """# Costume co-op memo

- Reservations are recorded against set IDs, not actor names.
- Every return goes to the inspection rail before circulation.
"""
QUESTION = "How are reservations recorded?"


class Peer:
    """Loopback Ollama double that records every request it receives."""

    def __init__(self, *, chat_digest: str | None = EXPERIMENTAL_MODEL_DIGEST, embed: bool = True) -> None:
        self.tags = [{"name": EMBED, "model": EMBED, "digest": "e" * 64}] if embed else []
        if chat_digest is not None:
            self.tags.append({"name": EXPERIMENTAL_MODEL, "model": EXPERIMENTAL_MODEL, "digest": chat_digest})
        self.loaded_digest = chat_digest
        self.answer_model: str | None = EXPERIMENTAL_MODEL  # None: the response has no "model" field
        self.loaded = False
        self.requests: list[tuple[str, str, dict[str, Any]]] = []
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                peer.requests.append(("GET", self.path, {}))
                if self.path == "/api/tags":
                    self._send({"models": peer.tags})
                elif self.path == "/api/version":
                    self._send({"version": "0.40.5"})
                elif self.path == "/api/ps":
                    loaded = [{"name": EXPERIMENTAL_MODEL, "model": EXPERIMENTAL_MODEL, "digest": peer.loaded_digest}]
                    self._send({"models": loaded if peer.loaded else []})
                else:
                    self._send({"error": "not found"}, 404)

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                peer.requests.append(("POST", self.path, body))
                installed = {canonical_model_tag(tag["name"]) for tag in peer.tags}  # untagged = :latest
                if canonical_model_tag(str(body.get("model") or "")) not in installed:
                    self._send({"error": f"model '{body.get('model')}' not found"}, 404)
                elif self.path == "/api/embed":
                    self._send({"embeddings": [[1.0, 0.5, 0.25] for _ in body["input"]]})
                elif self.path == "/api/show":
                    kind = "embedding" if canonical_model_tag(body["model"]) == EMBED else "completion"
                    self._send({"capabilities": [kind], "details": {"family": "x"}, "model_info": {}})
                elif self.path == "/api/chat" and not body.get("messages"):
                    peer.loaded = True
                    self._send({"model": body["model"], "done": True, "done_reason": "load"})
                elif self.path == "/api/chat":
                    content = "Reservations use set IDs [S1]. No end date [S.1]."
                    answer = {"message": {"role": "assistant", "content": content},
                              "done": True, "done_reason": "stop", "prompt_eval_count": 9, "eval_count": 9}
                    if peer.answer_model is not None:
                        answer["model"] = peer.answer_model
                    self._send(answer)
                else:
                    self._send({"error": "not found"}, 404)

            def _send(self, payload: dict[str, Any], status: int = 200) -> None:
                raw = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def chats(self) -> list[dict[str, Any]]:
        return [body for method, path, body in self.requests if path == "/api/chat"]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def ordinary(monkeypatch: pytest.MonkeyPatch):
    """Stands in for the ordinary 11434 runtime; it must stay untouched in private mode."""
    peer = Peer()
    monkeypatch.setattr(model_service, "OLLAMA_ENDPOINT", peer.url)
    yield peer
    peer.close()


def private_runtime(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> Peer:
    peer = Peer(**kwargs)
    monkeypatch.setenv(PRIVATE_ENDPOINT_ENV, peer.url)
    return peer


def chat_params(session_id: str | None, model: str, document_id: str = "", key: str = "k1") -> dict[str, Any]:
    params: dict[str, Any] = {"message": QUESTION, "model": model, "operation_key": key, "use_rag": bool(document_id)}
    if session_id:
        params["session_id"] = session_id
    if document_id:
        params["document_ids"] = [document_id]
    return params


# ------------------------------------------------------------ endpoint override


@pytest.mark.parametrize(
    "value",
    [
        "http://localhost:5000",
        "https://127.0.0.1:5000",
        "http://127.0.0.1",
        "http://127.0.0.1:11434",
        "http://10.0.0.5:5000",
        "http://[::1]:5000",
        "http://127.0.0.1:5000/api",
        "http://user@127.0.0.1:5000",
        "http://127.0.0.1:notaport",
    ],
)
def test_invalid_override_fails_every_request_without_reaching_any_runtime(
    value: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer
) -> None:
    monkeypatch.setenv(PRIVATE_ENDPOINT_ENV, value)
    db = Database(tmp_path / "profile")
    try:
        with pytest.raises(ModelConnectionError, match=PRIVATE_ENDPOINT_ENV):
            private_endpoint()
        with pytest.raises(ModelConnectionError):
            ModelService(db).detect_ollama()
        with pytest.raises(ModelConnectionError):
            ModelService(db).chat_detailed("llama3.2", [{"role": "user", "content": "hi"}])
        with pytest.raises(ModelConnectionError):
            EmbeddingService(db).embed_query("q")
    finally:
        db.close()
    assert ordinary.requests == []


def test_override_is_normalized_and_absent_override_keeps_the_ordinary_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    assert private_endpoint() == "" and ollama_endpoint() == OLLAMA_ENDPOINT
    assert not is_experimental_model(EXPERIMENTAL_MODEL)
    monkeypatch.setenv(PRIVATE_ENDPOINT_ENV, " http://127.0.0.1:54380/ ")
    assert private_endpoint() == ollama_endpoint() == "http://127.0.0.1:54380"
    assert is_experimental_model("QWEN3:4b-instruct-2507-q4_K_M")
    assert not is_experimental_model("llama3.2")


def test_unreachable_private_runtime_names_it_in_every_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer) -> None:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens there now
    monkeypatch.setenv(PRIVATE_ENDPOINT_ENV, f"http://127.0.0.1:{port}")
    db = Database(tmp_path / "profile")
    try:
        models = ModelService(db)
        status = models.detect_ollama()
        assert (status["reachable"], status["endpoint"]) == (False, f"http://127.0.0.1:{port}")
        assert status["private_runtime"]["state"] == "unreachable"
        assert models.ps() == {"models": [], "reachable": False, "error": f"Ollama is not reachable at http://127.0.0.1:{port}"}
        with pytest.raises(ModelConnectionError, match=f"not reachable at http://127.0.0.1:{port}"):
            models.chat_detailed("llama3.2", [{"role": "user", "content": "hi"}])
    finally:
        db.close()
    assert ordinary.requests == []


# ------------------------------------------------------------ routing through the product


def test_discovery_indexing_preparation_generation_and_inspection_use_only_the_private_endpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer
) -> None:
    private = private_runtime(monkeypatch)
    app = SidecarApp(tmp_path / "profile")
    try:
        status = app.dispatch("models.detect_ollama", {})
        assert status["endpoint"] == private.url
        assert status["private_runtime"] == {
            "endpoint": private.url,
            "experimental_model": EXPERIMENTAL_MODEL,
            "expected_digest": EXPERIMENTAL_MODEL_DIGEST,
            "installed_digest": EXPERIMENTAL_MODEL_DIGEST,
            "state": "ready",
        }
        app.dispatch("models.capabilities", {"refresh": True})
        catalog = app.dispatch("models.detect_ollama", {})["conversation_models"]
        assert [item["tag"] for item in catalog] == [EXPERIMENTAL_MODEL]  # the embedder is not a chat choice

        source = tmp_path / "memo.md"
        source.write_text(MEMO, encoding="utf-8")
        imported = wait_terminal(app, app.dispatch("jobs.submit_import", {"paths": [str(source)]})["jobs"][0]["job_id"])
        assert imported["state"] == "completed", imported
        health = app.dispatch("rag.health", {})
        assert health["embedding"]["semantic"] is True and health["embedding"]["model"] == "nomic-embed-text"

        session = app.dispatch("sessions.create", {})
        assert session["model"] == "llama3.2"  # the ordinary default, not the candidate
        app.dispatch("sessions.update", {"session_id": session["id"], "updates": {"model": EXPERIMENTAL_MODEL}})
        submitted = app.dispatch("jobs.submit_chat", chat_params(session["id"], EXPERIMENTAL_MODEL, imported["document_id"]))
        assert wait_terminal(app, submitted["job"]["job_id"])["state"] == "completed"

        answer = app.dispatch("sessions.messages", {"session_id": session["id"]})[-1]
        models = answer["metadata"]["operation_trace"]["models"]
        assert models["final_answer_model"] == EXPERIMENTAL_MODEL
        assert models["final_answer_model_digest"] == EXPERIMENTAL_MODEL_DIGEST
        assert models["runtime_endpoint"] == private.url
        review = answer["metadata"]["answer_evidence"]["review"]
        assert review["valid_ids"] == ["S1"] and [item["text"] for item in review["malformed_references"]] == ["[S.1]"]
        assert app.dispatch("models.detect_ollama", {})["private_runtime"]["state"] == "ready"
        assert app.models.ps()["models"][0]["digest"] == EXPERIMENTAL_MODEL_DIGEST
        assert app.dispatch("settings.get", {})["default_model"] == "llama3.2"  # never promoted
    finally:
        app.close()
        private.close()

    assert ordinary.requests == []
    # One preparation and exactly one answer generation, both for the selected tag.
    assert [bool(body["messages"]) for body in private.chats()] == [False, True]
    assert {body["model"] for body in private.chats()} == {EXPERIMENTAL_MODEL}
    paths = {path for _method, path, _body in private.requests}
    assert {"/api/tags", "/api/version", "/api/show", "/api/embed", "/api/chat", "/api/ps"} <= paths
    embeds = [body for _method, path, body in private.requests if path == "/api/embed"]
    assert len(embeds) >= 2  # indexing and the query embedding


def test_no_lexical_fallback_in_the_private_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer) -> None:
    private = private_runtime(monkeypatch, embed=False)
    db = Database(tmp_path / "profile")
    try:
        embeddings = EmbeddingService(db)
        status = embeddings.status()
        assert status["semantic"] is False
        assert f"not installed at the private runtime {private.url}" in str(status["message"])
        with pytest.raises(RuntimeError):
            embeddings.embed_texts(["chunk"])
        with pytest.raises(RuntimeError):
            embeddings.embed_query("query")
        assert [path for _m, path, _b in private.requests if path == "/api/embed"]  # it tried the private one

        # An ordinary launch keeps its lexical fallback.
        monkeypatch.delenv(PRIVATE_ENDPOINT_ENV)
        ordinary.tags = [tag for tag in ordinary.tags if tag["name"] != EMBED]
        assert EmbeddingService(db).embed_query("query").backend == "lexical"
    finally:
        db.close()
        private.close()


# ------------------------------------------------------------ explicit selection


def test_candidate_is_used_only_after_explicit_selection_in_the_conversation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer
) -> None:
    private = private_runtime(monkeypatch)
    app = SidecarApp(tmp_path / "profile")
    try:
        with pytest.raises(ValueError, match="never assigned when a conversation is created"):
            app.dispatch("sessions.create", {"model": EXPERIMENTAL_MODEL})
        with pytest.raises(ValueError, match="never saved as the default model"):
            app.dispatch("settings.set", {"values": {"default_model": EXPERIMENTAL_MODEL}})
        with pytest.raises(RpcError, match="explicitly"):  # a new conversation has no selection
            app.dispatch("jobs.submit_chat", chat_params(None, EXPERIMENTAL_MODEL))
        with pytest.raises(RpcError, match="explicitly"):
            app.dispatch("jobs.submit_search", {"query": QUESTION, "model": EXPERIMENTAL_MODEL})
        assert app.dispatch("sessions.list", {}) == []

        session = app.dispatch("sessions.create", {})
        with pytest.raises(RpcError, match="explicitly"):  # the conversation still uses llama3.2
            app.dispatch("jobs.submit_chat", chat_params(session["id"], EXPERIMENTAL_MODEL))
        with pytest.raises(ValueError, match="explicitly"):
            app.chat.send(QUESTION, session_id=session["id"], model=EXPERIMENTAL_MODEL)

        app.dispatch("sessions.update", {"session_id": session["id"], "updates": {"model": EXPERIMENTAL_MODEL}})
        with pytest.raises(RpcError, match="explicitly"):  # request tag must equal the selected tag
            app.dispatch("jobs.submit_chat", chat_params(session["id"], "llama3.2"))
        assert private.chats() == []
        assert app.dispatch("jobs.chat_status", {"operation_key": "k1"})["found"] is False

        submitted = app.dispatch("jobs.submit_chat", chat_params(session["id"], EXPERIMENTAL_MODEL))
        assert wait_terminal(app, submitted["job"]["job_id"])["state"] == "completed"
        assert app.dispatch("settings.get", {})["default_model"] == "llama3.2"
    finally:
        app.close()
        private.close()
    assert ordinary.requests == []


def test_ordinary_launch_keeps_defaults_and_selection_unchanged(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        assert SessionService(db).create(model=EXPERIMENTAL_MODEL)["model"] == EXPERIMENTAL_MODEL
        assert SettingsService(db).set({"default_model": EXPERIMENTAL_MODEL})["default_model"] == EXPERIMENTAL_MODEL
        assert ModelService(db).verify_experimental_identity(EXPERIMENTAL_MODEL, loaded=False) == ""
    finally:
        db.close()


# ------------------------------------------------------------ missing / mismatched candidate


@pytest.mark.parametrize(
    "chat_digest,expected",
    [(None, "is not installed at the private runtime"), (OTHER_DIGEST, f"has digest {OTHER_DIGEST}, expected {EXPERIMENTAL_MODEL_DIGEST}")],
)
def test_missing_or_mismatched_candidate_is_refused_before_acceptance(
    chat_digest: str | None, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer
) -> None:
    private = private_runtime(monkeypatch, chat_digest=chat_digest)
    app = SidecarApp(tmp_path / "profile")
    try:
        state = app.dispatch("models.detect_ollama", {})["private_runtime"]["state"]
        assert state == ("missing" if chat_digest is None else "digest_mismatch")
        session = app.dispatch("sessions.create", {})
        app.dispatch("sessions.update", {"session_id": session["id"], "updates": {"model": EXPERIMENTAL_MODEL}})
        with pytest.raises(RpcError, match=expected) as refused:
            app.dispatch("jobs.submit_chat", chat_params(session["id"], EXPERIMENTAL_MODEL))
        assert "no other model was used" in refused.value.message
        assert app.dispatch("jobs.chat_status", {"operation_key": "k1"})["found"] is False
        assert app.dispatch("sessions.messages", {"session_id": session["id"]}) == []
    finally:
        app.close()
        private.close()
    assert private.chats() == [] and ordinary.requests == []


@pytest.mark.parametrize(
    "phase,observed,generated",
    [
        ("loaded digest", OTHER_DIGEST, [False]),  # the runner loaded something else: preparation only
        ("response tag", "reported no model identity", [False, True]),  # the answer names no model
    ],
)
def test_identity_failure_after_acceptance_keeps_an_actionable_content_free_reason(
    phase: str, observed: str, generated: list[bool], monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer
) -> None:
    private = private_runtime(monkeypatch)
    if phase == "loaded digest":
        private.loaded_digest = OTHER_DIGEST
    else:
        private.answer_model = None
    app = SidecarApp(tmp_path / "profile")
    try:
        session = app.dispatch("sessions.create", {})
        app.dispatch("sessions.update", {"session_id": session["id"], "updates": {"model": EXPERIMENTAL_MODEL}})
        submitted = app.dispatch("jobs.submit_chat", chat_params(session["id"], EXPERIMENTAL_MODEL))
        final = wait_terminal(app, submitted["job"]["job_id"])
        assert (final["state"], final["message_code"]) == ("failed", "model_identity_mismatch")
        detail = final["message_detail"]
        for part in (private.url, EXPERIMENTAL_MODEL, observed, "re-verify the private model store"):
            assert part in detail
        assert QUESTION not in detail and "Reservations" not in detail and len(detail) <= 600
        turn = app.dispatch("jobs.chat_status", {"operation_key": "k1"})["turn"]
        assert (turn["latest_state"], turn["answered"], turn["can_retry"]) == ("failed", False, True)
        assert turn["latest_outcome_code"] == "model_identity_mismatch"
        assert turn["latest_outcome_detail"] == turn["attempts"][0]["outcome_detail"] == detail
        assert [message["role"] for message in app.dispatch("sessions.messages", {"session_id": session["id"]})] == ["user"]
    finally:
        app.close()
    reopened = SidecarApp(tmp_path / "profile")  # the reason is durable with the attempt
    try:
        assert reopened.dispatch("jobs.chat_status", {"operation_key": "k1"})["turn"]["latest_outcome_detail"] == detail
    finally:
        reopened.close()
        private.close()
    assert [bool(body["messages"]) for body in private.chats()] == generated
    assert ordinary.requests == []


@pytest.mark.parametrize(
    "answer_model,expected",
    [("llama3.2:latest", "answered as llama3.2:latest"), ("", "reported no model identity"), (None, "reported no model identity")],
)
def test_an_answer_not_reported_under_the_selected_tag_is_not_kept(
    answer_model: str | None, expected: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    private = private_runtime(monkeypatch)
    private.answer_model = answer_model
    db = Database(tmp_path / "profile")
    try:
        with pytest.raises(ModelIdentityError, match=expected) as refused:
            ModelService(db).chat_detailed(EXPERIMENTAL_MODEL, [{"role": "user", "content": "hi"}])
        assert private.url in refused.value.detail
    finally:
        db.close()
        private.close()


def test_ordinary_launch_still_names_the_requested_model_when_the_response_omits_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ordinary: Peer
) -> None:
    monkeypatch.delenv(PRIVATE_ENDPOINT_ENV, raising=False)
    ordinary.answer_model = None
    db = Database(tmp_path / "profile")
    try:
        result = ModelService(db).chat_detailed(EXPERIMENTAL_MODEL, [{"role": "user", "content": "hi"}])
        assert result["model"] == EXPERIMENTAL_MODEL and "model_digest" not in result
    finally:
        db.close()
