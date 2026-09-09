"""Contract tests for the Phase 1 provider seam.

These prove that extracting the Ollama transport behind
`services.providers.ollama.OllamaProvider` changed nothing observable:
error classes keep their identity and import path, facade results keep
their exact shape, and error categories map exactly as before.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from odysseus_desktop_backend.services import model_service as model_module
from odysseus_desktop_backend.services.model_service import (
    ModelConnectionError,
    ModelEmptyResponseError,
    ModelInvalidModelError,
    ModelMalformedResponseError,
    ModelService,
    ModelServiceError,
    ModelTimeoutError,
)
from odysseus_desktop_backend.services.providers import base as providers_base
from odysseus_desktop_backend.storage import Database


TAGS_RESPONSE = {
    "models": [
        {
            "name": "llama3.2:latest",
            "modified_at": "2026-01-01T00:00:00Z",
            "size": 2_000_000_000,
            "digest": "sha256:abc",
            "details": {
                "format": "gguf",
                "family": "llama",
                "parameter_size": "3B",
                "quantization_level": "Q4_K_M",
            },
        }
    ]
}

CHAT_RESPONSE = {
    "model": "llama3.2:latest",
    "message": {"role": "assistant", "content": "Hello there.", "thinking": ""},
    "done_reason": "stop",
    "total_duration": 1_500_000_000,
    "load_duration": 100_000_000,
    "prompt_eval_count": 12,
    "prompt_eval_duration": 200_000_000,
    "eval_count": 6,
    "eval_duration": 300_000_000,
}


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    behavior: dict[str, Any] = {}

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/api/tags":
            self._send(200, json.dumps(TAGS_RESPONSE).encode())
        elif self.path == "/api/version":
            self._send(200, json.dumps({"version": "0.9.9"}).encode())
        else:
            self._send(404, b"{}")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        mode = self.behavior.get("chat_mode", "ok")
        if self.path != "/api/chat":
            self._send(404, b"{}")
            return
        if mode == "ok":
            self._send(200, json.dumps(CHAT_RESPONSE).encode())
        elif mode == "invalid_model":
            self._send(404, json.dumps({"error": "model 'nope' not found"}).encode())
        elif mode == "http_error":
            self._send(500, json.dumps({"error": "boom"}).encode())
        elif mode == "malformed":
            self._send(200, b"this is not json")
        elif mode == "empty_body":
            self._send(200, b"")
        elif mode == "empty_content":
            body = dict(CHAT_RESPONSE)
            body["message"] = {"role": "assistant", "content": "", "thinking": ""}
            self._send(200, json.dumps(body).encode())
        elif mode == "slow":
            time.sleep(1.5)
            self._send(200, json.dumps(CHAT_RESPONSE).encode())
        else:  # pragma: no cover - guard against typos in tests
            raise AssertionError(f"unknown chat_mode {mode}")


@pytest.fixture()
def fake_ollama(monkeypatch: pytest.MonkeyPatch):
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    _FakeOllamaHandler.behavior = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setattr(model_module, "OLLAMA_ENDPOINT", endpoint)
    try:
        yield endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path)
    try:
        yield database
    finally:
        database.close()


def make_service(db: Database) -> ModelService:
    # Construct after OLLAMA_ENDPOINT is patched so provider and facade agree.
    return ModelService(db)


def test_error_classes_keep_identity_across_modules() -> None:
    assert ModelServiceError is providers_base.ModelServiceError
    assert ModelTimeoutError is providers_base.ModelTimeoutError
    assert ModelConnectionError is providers_base.ModelConnectionError
    assert ModelInvalidModelError is providers_base.ModelInvalidModelError
    assert ModelMalformedResponseError is providers_base.ModelMalformedResponseError
    assert ModelEmptyResponseError is providers_base.ModelEmptyResponseError
    assert ModelTimeoutError.category == "timeout"
    assert ModelConnectionError.category == "connection_failure"
    assert issubclass(ModelServiceError, RuntimeError)


def test_detect_ollama_contract_shape(fake_ollama: str, db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    service = make_service(db)
    monkeypatch.setattr(model_module.shutil, "which", lambda _name: "ollama")
    monkeypatch.setattr(service, "_tcp_reachable", lambda _host, _port: True)

    status = service.detect_ollama()

    assert set(status.keys()) == {
        "name",
        "installed",
        "reachable",
        "endpoint",
        "version",
        "models",
        "model_details",
        "conversation_models",
        "error",
        "updated_at",
    }
    assert status["name"] == "ollama"
    assert status["installed"] is True
    assert status["reachable"] is True
    assert status["endpoint"] == fake_ollama
    assert status["version"] == "0.9.9"
    assert status["models"] == ["llama3.2:latest"]
    assert status["model_details"][0]["family"] == "llama"
    assert status["error"] == ""
    row = db.conn.execute("SELECT * FROM runtime_status WHERE name = 'ollama'").fetchone()
    assert row is not None
    assert row["endpoint"] == fake_ollama


def test_chat_detailed_contract_shape(fake_ollama: str, db: Database) -> None:
    service = make_service(db)
    result = service.chat_detailed("llama3.2:latest", [{"role": "user", "content": "hi"}])

    assert result["model"] == "llama3.2:latest"
    assert result["content"] == "Hello there."
    assert result["thinking"] == ""
    assert result["done_reason"] == "stop"
    assert result["prompt_eval_count"] == 12
    assert result["eval_count"] == 6
    assert result["total_duration_ns"] == 1_500_000_000
    assert result["load_duration_ns"] == 100_000_000
    assert result["generation_tokens_per_second"] == pytest.approx(6 / 0.3)
    assert result["prompt_tokens_per_second"] == pytest.approx(12 / 0.2)
    assert isinstance(result["elapsed_ms"], int)
    assert result["raw"]["done_reason"] == "stop"


def test_chat_plain_wrapper_returns_content(fake_ollama: str, db: Database) -> None:
    service = make_service(db)
    assert service.chat("llama3.2:latest", [{"role": "user", "content": "hi"}]) == "Hello there."


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("invalid_model", ModelInvalidModelError),
        ("http_error", ModelServiceError),
        ("malformed", ModelMalformedResponseError),
        ("empty_body", ModelEmptyResponseError),
        ("empty_content", ModelEmptyResponseError),
    ],
)
def test_chat_error_categories(fake_ollama: str, db: Database, mode: str, expected: type[Exception]) -> None:
    _FakeOllamaHandler.behavior["chat_mode"] = mode
    service = make_service(db)
    with pytest.raises(expected):
        service.chat_detailed("llama3.2:latest", [{"role": "user", "content": "hi"}])


def test_chat_timeout_category(fake_ollama: str, db: Database) -> None:
    _FakeOllamaHandler.behavior["chat_mode"] = "slow"
    service = make_service(db)
    with pytest.raises(ModelTimeoutError) as excinfo:
        service.chat_detailed("llama3.2:latest", [{"role": "user", "content": "hi"}], timeout=0.2)
    assert "timeout" in str(excinfo.value)


def test_connection_refused_category(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    monkeypatch.setattr(model_module, "OLLAMA_ENDPOINT", f"http://127.0.0.1:{closed_port}")
    service = make_service(db)
    with pytest.raises(ModelConnectionError) as excinfo:
        service.chat_detailed("llama3.2:latest", [{"role": "user", "content": "hi"}])
    assert "not reachable" in str(excinfo.value)


def test_instance_transport_monkeypatch_still_supported(db: Database, monkeypatch: pytest.MonkeyPatch) -> None:
    """test_mvp_hardening_smoke patches `_get_json`/`_tcp_reachable` on the
    instance; the seam must keep those hooks effective."""

    service = make_service(db)
    monkeypatch.setattr(model_module.shutil, "which", lambda _name: "ollama")
    monkeypatch.setattr(service, "_tcp_reachable", lambda _host, _port: True)
    monkeypatch.setattr(
        service,
        "_get_json",
        lambda url, timeout: {"models": [{"name": "stub:latest"}]} if "tags" in url else {"version": "x"},
    )
    status = service.detect_ollama()
    assert status["models"] == ["stub:latest"]
