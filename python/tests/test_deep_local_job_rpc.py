"""deep_local.submit/get/list/cancel/retry JSON-RPC slice over SidecarApp.dispatch.

Proves the full RPC path against the fake loopback Colibri server, the
startup-repair wiring in SidecarApp.__init__, RPC-boundary validation, and
that RPC-visible list payloads never carry question/evidence/result content.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.services.deep_local_jobs import TERMINAL_STATES
from odysseus_desktop_backend.storage import Database
from rpc_server import RpcError, SidecarApp

MODEL_ID = "glm-5.2-colibri"
SENTINEL_QUESTION = "RPC-SENTINEL-QUESTION-77aa what is the renewal date?"
SENTINEL_SNIPPET = "RPC-SENTINEL-SNIPPET-19bb renewal happens each March"
SENTINEL_ANSWER = "RPC-SENTINEL-ANSWER-53cc in March"

LIST_SNAPSHOT_KEYS = {
    "job_id",
    "provider",
    "state",
    "message_code",
    "error_category",
    "endpoint",
    "model_id",
    "question_chars",
    "evidence_count",
    "result_chars",
    "usage",
    "warnings",
    "state_history",
    "attempt_count",
    "retry_of",
    "created_at",
    "started_at",
    "finished_at",
    "elapsed_ms",
    "queue_position",
}


class _Handler(BaseHTTPRequestHandler):
    release: threading.Event = threading.Event()
    request_started: threading.Event = threading.Event()
    hang: bool = False

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send(200, json.dumps({"status": "ok"}).encode())
            return
        if self.path == "/v1/models":
            self._send(200, json.dumps({"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 1, "owned_by": "colibri"}]}).encode())
            return
        self._send(404, json.dumps({"error": {"message": "nope", "code": "not_found"}}).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        if type(self).hang:
            type(self).request_started.set()
            self.release.wait(timeout=30)
        self._send(
            200,
            json.dumps(
                {
                    "id": "chatcmpl-test",
                    "object": "chat.completion",
                    "created": 1,
                    "model": MODEL_ID,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": SENTINEL_ANSWER, "refusal": None},
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
                }
            ).encode(),
            {"x-colibri-queue-wait-ms": "5"},
        )


@pytest.fixture()
def fake_colibri():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    _Handler.release = threading.Event()
    _Handler.request_started = threading.Event()
    _Handler.hang = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield endpoint
    finally:
        _Handler.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def app(tmp_path: Path):
    application = SidecarApp(tmp_path / "profile")
    yield application
    application.close()


def _enable(app: SidecarApp, endpoint: str) -> None:
    app.dispatch(
        "settings.set",
        {"values": {"deep_local_enabled": True, "deep_local_endpoint": endpoint, "deep_local_timeout_seconds": 30}},
    )


def _wait_terminal(app: SidecarApp, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = app.dispatch("deep_local.get", {"job_id": job_id})
        if snapshot["state"] in TERMINAL_STATES:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"job did not reach terminal state: {snapshot}")


def test_rpc_methods_registered(app: SidecarApp) -> None:
    for method in ["deep_local.submit", "deep_local.get", "deep_local.list", "deep_local.cancel", "deep_local.retry"]:
        assert method in app.methods


def test_rpc_submit_get_list_lifecycle(app: SidecarApp, fake_colibri: str) -> None:
    _enable(app, fake_colibri)
    outcome = app.dispatch(
        "deep_local.submit",
        {
            "question": SENTINEL_QUESTION,
            "evidence": [{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
        },
    )
    assert outcome["ok"] is True
    job_id = outcome["job"]["job_id"]
    final = _wait_terminal(app, job_id)
    assert final["state"] == "completed"
    assert final["result_text"] == SENTINEL_ANSWER

    listed = app.dispatch("deep_local.list", {})
    assert isinstance(listed, list) and listed
    snapshot = next(item for item in listed if item["job_id"] == job_id)
    assert set(snapshot.keys()) == LIST_SNAPSHOT_KEYS
    json.dumps(listed)  # must be JSON-serializable as-is
    payload = json.dumps(listed)
    assert SENTINEL_QUESTION not in payload
    assert SENTINEL_SNIPPET not in payload
    assert SENTINEL_ANSWER not in payload


def test_rpc_submit_disabled_is_structured_not_exception(app: SidecarApp) -> None:
    outcome = app.dispatch("deep_local.submit", {"question": "hello"})
    assert outcome["ok"] is False
    assert outcome["error_category"] == "disabled"


def test_rpc_validation_errors(app: SidecarApp, fake_colibri: str) -> None:
    _enable(app, fake_colibri)
    with pytest.raises(RpcError):
        app.dispatch("deep_local.submit", {})
    with pytest.raises(RpcError):
        app.dispatch("deep_local.submit", {"question": "q", "evidence": "not-a-list"})
    with pytest.raises(RpcError):
        app.dispatch("deep_local.submit", {"question": "q", "top_p": "high"})
    with pytest.raises(RpcError):
        app.dispatch("deep_local.get", {})
    with pytest.raises(KeyError):
        app.dispatch("deep_local.get", {"job_id": "missing"})
    with pytest.raises(KeyError):
        app.dispatch("deep_local.cancel", {"job_id": "missing"})
    with pytest.raises(KeyError):
        app.dispatch("deep_local.retry", {"job_id": "missing"})


def test_rpc_cancel_and_retry_flow(app: SidecarApp, fake_colibri: str) -> None:
    _enable(app, fake_colibri)
    _Handler.hang = True
    outcome = app.dispatch("deep_local.submit", {"question": SENTINEL_QUESTION})
    job_id = outcome["job"]["job_id"]
    assert _Handler.request_started.wait(timeout=10)
    cancelled = app.dispatch("deep_local.cancel", {"job_id": job_id})
    assert cancelled["state"] == "cancel_requested"
    final = _wait_terminal(app, job_id)
    assert final["state"] == "interrupted"
    assert final["message_code"] == "stopped_waiting"

    _Handler.hang = False
    _Handler.release.set()
    retried = app.dispatch("deep_local.retry", {"job_id": job_id})
    assert retried["ok"] is True
    assert retried["job"]["attempt_count"] == 2
    final_retry = _wait_terminal(app, retried["job"]["job_id"])
    assert final_retry["state"] == "completed"
    assert final_retry["question"] == SENTINEL_QUESTION


def test_startup_repair_wired_into_sidecar(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    db = Database(profile)
    db.conn.execute(
        "INSERT INTO deep_local_jobs (id, state, question, created_at, updated_at) "
        "VALUES ('j-stuck', 'running', 'q', 1000, 1000)"
    )
    db.conn.commit()
    db.close()

    application = SidecarApp(profile)
    try:
        snapshot = application.dispatch("deep_local.get", {"job_id": "j-stuck"})
        assert snapshot["state"] == "interrupted"
        assert snapshot["message_code"] == "interrupted_by_restart"
        assert snapshot["error_category"] == "interrupted"
        assert snapshot["state_history"][-1]["state"] == "interrupted"
    finally:
        application.close()


def test_diagnostics_and_health_exclude_deep_local_content(app: SidecarApp, fake_colibri: str) -> None:
    """diagnostics.get must not embed Deep Local job content or endpoints' payloads."""
    _enable(app, fake_colibri)
    outcome = app.dispatch(
        "deep_local.submit",
        {
            "question": SENTINEL_QUESTION,
            "evidence": [{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
        },
    )
    _wait_terminal(app, outcome["job"]["job_id"])
    diagnostics = app.dispatch("diagnostics.get", {})
    payload = json.dumps(diagnostics)
    assert SENTINEL_QUESTION not in payload
    assert SENTINEL_SNIPPET not in payload
    assert SENTINEL_ANSWER not in payload
