"""Cold-model chat lifecycle over the real production request path.

jobs.submit_chat -> JobService worker -> ChatService.send -> ModelService HTTP,
against a deterministic fake Ollama on loopback whose load and generation
delays are scripted. Only the timeout values are scaled down (one seam on
ModelService._post_json); every request still crosses a real socket.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.services import embedding_service, model_service
from offline_network import ALLOWED_PORTS_ENV
from odysseus_desktop_backend.services.job_service import TERMINAL_JOB_STATES
from rpc_server import RpcError, SidecarApp


PYTHON_DIR = Path(__file__).resolve().parents[1]
MODEL = "fixture-model:latest"
DOCUMENT_TEXT = (
    "PotatoCS v0.3.1 release notes.\n\n"
    "When the backend becomes unavailable a degraded banner appears with a Retry backend action; "
    "the banner clears after a successful recovery.\n\n"
    "The tracked installer checksum moved out of the generated dist folder so frontend builds no longer delete it.\n"
)
ANSWER = "Use Retry backend; the banner clears after recovery [1]."


class FakeOllama:
    """Loopback Ollama double: empty-messages chat = load, otherwise generation."""

    def __init__(self) -> None:
        self.prepare_delay = 0.0
        self.generate_delay = 0.0
        self.later_generate_delay = 0.0  # verifier/correction calls after the answer
        self.requests: list[str] = []
        self.bodies: list[dict[str, Any]] = []
        self.disconnects: list[str] = []
        self.arrived = {"prepare": threading.Event(), "generate": threading.Event()}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 - http.server API
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                kind = "prepare" if body.get("messages") == [] else "generate"
                fake.requests.append(kind)
                fake.bodies.append(body)
                fake.arrived[kind].set()
                if kind == "prepare":
                    delay = fake.prepare_delay
                else:
                    delay = fake.generate_delay if fake.count("generate") == 1 else fake.later_generate_delay
                if not self._hold(delay):
                    fake.disconnects.append(kind)
                    return
                if kind == "prepare":
                    payload = {"model": body["model"], "done": True, "done_reason": "load", "load_duration": int(delay * 1e9)}
                else:
                    payload = {
                        "model": body["model"],
                        "message": {"role": "assistant", "content": ANSWER},
                        "done": True,
                        "done_reason": "stop",
                        "prompt_eval_count": 40,
                        "eval_count": 12,
                    }
                raw = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except OSError:
                    fake.disconnects.append(kind)

            def _hold(self, seconds: float) -> bool:
                """Wait like a slow runner; return False as soon as the client hangs up."""
                deadline = time.monotonic() + seconds
                self.connection.settimeout(0.02)
                try:
                    while time.monotonic() < deadline:
                        try:
                            if self.connection.recv(1, socket.MSG_PEEK) == b"":
                                return False
                        except (socket.timeout, BlockingIOError):
                            continue
                        except OSError:
                            return False
                finally:
                    self.connection.settimeout(None)
                return True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def count(self, kind: str) -> int:
        return self.requests.count(kind)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def ollama(monkeypatch: pytest.MonkeyPatch):
    fake = FakeOllama()
    monkeypatch.setattr(model_service, "OLLAMA_ENDPOINT", fake.url)
    # Embeddings use the local fallback so nothing can reach a real Ollama.
    monkeypatch.setattr(embedding_service.OllamaEmbeddingProvider, "installed", lambda self: False)
    yield fake
    fake.close()


def scale_timeouts(monkeypatch: pytest.MonkeyPatch, seconds: float) -> None:
    """Shrink the real 120s preparation/generation bounds; the HTTP path is unchanged."""
    real = model_service.ModelService._post_json

    def bounded(self: Any, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        return real(self, url, payload, min(timeout, seconds))

    monkeypatch.setattr(model_service.ModelService, "_post_json", bounded)


@pytest.fixture()
def app(tmp_path: Path, ollama: FakeOllama):
    application = SidecarApp(tmp_path / "profile")
    source = tmp_path / "release-notes.txt"
    source.write_text(DOCUMENT_TEXT, encoding="utf-8")
    imported = application.dispatch("jobs.submit_import", {"paths": [str(source)]})["jobs"][0]
    final = wait_terminal(application, imported["job_id"])
    assert final["state"] == "completed", final
    application.test_document_id = final["document_id"]  # type: ignore[attr-defined]
    yield application
    application.close()


def wait_terminal(app: SidecarApp, job_id: str, timeout: float = 10.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = app.dispatch("jobs.get", {"job_id": job_id})
        if snapshot["state"] in TERMINAL_JOB_STATES:
            return snapshot
        time.sleep(0.02)
    raise AssertionError("job did not reach a terminal state")


def submit(app: SidecarApp, **extra: Any) -> dict[str, Any]:
    return app.dispatch(
        "jobs.submit_chat",
        {
            "message": "What should I do if the backend becomes unavailable?",
            "model": MODEL,
            "use_rag": True,
            "document_ids": [app.test_document_id],  # type: ignore[attr-defined]
            **extra,
        },
    )


def roles(app: SidecarApp, session_id: str) -> list[str]:
    return [message["role"] for message in app.dispatch("sessions.messages", {"session_id": session_id})]


def progress_stages(capfd: pytest.CaptureFixture[str]) -> list[str]:
    stages = []
    for line in capfd.readouterr().err.splitlines():
        if line.startswith("{") and "__odysseus_progress__" in line:
            event = json.loads(line)
            if str(event.get("operation_id", "")).startswith("operation-"):
                stages.append(event["stage"])
    return stages


def test_warm_request_completes_with_evidence_citations_and_model_identity(
    app: SidecarApp, ollama: FakeOllama, capfd: pytest.CaptureFixture[str]
) -> None:
    capfd.readouterr()
    submitted = submit(app)
    job = submitted["job"]
    assert job["kind"] == "chat" and job["session_id"] == submitted["session"]["id"]
    final = wait_terminal(app, job["job_id"])

    assert final["state"] == "completed" and final["message_code"] == ""
    result = app.dispatch("jobs.chat_result", {"job_id": job["job_id"]})
    assert result["assistant_message"]["id"] == final["message_id"]
    assert result["assistant_message"]["content"] == ANSWER
    assert result["retrieved_snippets"], "retrieved evidence must reach the frontend"
    assert "Retry backend" in json.dumps(result["retrieved_snippets"])
    trace = result["assistant_message"]["metadata"]["operation_trace"]
    assert MODEL in json.dumps(trace)
    assert ollama.requests == ["prepare", "generate"]
    stages = progress_stages(capfd)
    assert stages.index("model_prepare") < stages.index("answer_generation") < stages.index("done")
    assert roles(app, job["session_id"]) == ["user", "assistant"]
    # Job snapshots stay free of question text and document content.
    assert "backend becomes unavailable" not in json.dumps(final)
    assert "Retry backend" not in json.dumps(final)


def test_slow_preparation_is_bounded_separately_and_never_shown_as_generation(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    # Each stage fits its own 1s bound; together they exceed one shared 1s budget.
    scale_timeouts(monkeypatch, 1.0)
    ollama.prepare_delay = 0.7
    ollama.generate_delay = 0.7
    capfd.readouterr()
    job = submit(app)["job"]
    assert ollama.arrived["prepare"].wait(5)
    time.sleep(0.2)
    during = app.dispatch("jobs.get", {"job_id": job["job_id"]})
    assert during["state"] == "running" and during["message_id"] == ""
    assert progress_stages(capfd)[-1] == "model_prepare"
    assert roles(app, job["session_id"]) == ["user"]
    with pytest.raises(ValueError):
        app.dispatch("jobs.chat_result", {"job_id": job["job_id"]})

    final = wait_terminal(app, job["job_id"])
    assert final["state"] == "completed"
    assert ollama.requests == ["prepare", "generate"]
    assert roles(app, job["session_id"]) == ["user", "assistant"]


def test_preparation_timeout_fails_without_generation_or_answer(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    scale_timeouts(monkeypatch, 0.5)
    ollama.prepare_delay = 3.0
    job = submit(app)["job"]
    final = wait_terminal(app, job["job_id"])
    assert (final["state"], final["message_code"]) == ("failed", "model_prepare_timeout")
    assert ollama.count("generate") == 0
    assert roles(app, job["session_id"]) == ["user"]  # request kept, nothing fabricated
    with pytest.raises(ValueError):
        app.dispatch("jobs.chat_result", {"job_id": job["job_id"]})


def test_generation_timeout_is_reported_as_generation_not_preparation(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    scale_timeouts(monkeypatch, 0.5)
    ollama.generate_delay = 3.0
    job = submit(app)["job"]
    final = wait_terminal(app, job["job_id"])
    assert (final["state"], final["message_code"]) == ("failed", "model_generation_timeout")
    assert ollama.requests == ["prepare", "generate"]
    assert roles(app, job["session_id"]) == ["user"]


def test_unreachable_runtime_is_a_preparation_failure(app: SidecarApp, ollama: FakeOllama) -> None:
    ollama.close()
    job = submit(app)["job"]
    final = wait_terminal(app, job["job_id"])
    assert (final["state"], final["message_code"]) == ("failed", "model_prepare_failed")
    assert roles(app, job["session_id"]) == ["user"]


@pytest.mark.parametrize("stage", ["prepare", "generate"])
def test_cancel_aborts_the_active_model_request(
    app: SidecarApp, ollama: FakeOllama, capfd: pytest.CaptureFixture[str], stage: str
) -> None:
    ollama.prepare_delay = 30.0 if stage == "prepare" else 0.0
    ollama.generate_delay = 30.0
    capfd.readouterr()
    job = submit(app)["job"]
    assert ollama.arrived[stage].wait(5)
    started = time.monotonic()
    app.dispatch("jobs.cancel", {"job_id": job["job_id"]})
    final = wait_terminal(app, job["job_id"], timeout=5)

    assert time.monotonic() - started < 3, "cancel must not wait for the model bound"
    assert (final["state"], final["message_code"]) == ("cancelled", "cancelled_by_user")
    deadline = time.monotonic() + 3
    while stage not in ollama.disconnects and time.monotonic() < deadline:
        time.sleep(0.02)
    assert stage in ollama.disconnects, "Ollama must see the client hang up"
    assert ollama.count("generate") == (1 if stage == "generate" else 0)
    assert roles(app, job["session_id"]) == ["user"]
    stages = progress_stages(capfd)
    assert stages[-1] == "operation_cancelled" and "done" not in stages


def test_cancel_while_connecting_stays_pending_until_nothing_can_be_sent(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The connection completes only after cancel; the job must not be terminal before that."""
    port = int(ollama.url.rsplit(":", 1)[1])
    connecting, release = threading.Event(), threading.Event()
    real_connect_ex = socket.socket.connect_ex

    def held(sock: socket.socket, address: Any) -> int:
        status = real_connect_ex(sock, address)
        if address[1] == port:
            connecting.set()
            assert release.wait(10)
        return status

    monkeypatch.setattr(socket.socket, "connect_ex", held)
    job = submit(app)["job"]
    assert connecting.wait(5)
    app.dispatch("jobs.cancel", {"job_id": job["job_id"]})
    time.sleep(0.3)
    assert app.dispatch("jobs.get", {"job_id": job["job_id"]})["state"] == "cancel_requested"
    release.set()
    final = wait_terminal(app, job["job_id"], timeout=5)
    assert (final["state"], final["message_code"]) == ("cancelled", "cancelled_by_user")
    time.sleep(0.5)  # a leaked request would deliver its POST now
    assert ollama.requests == []
    assert roles(app, job["session_id"]) == ["user"]


def test_cancel_during_optional_verification_is_not_swallowed(app: SidecarApp, ollama: FakeOllama) -> None:
    ollama.later_generate_delay = 30.0
    job = submit(app, verify_rag=True)["job"]
    deadline = time.monotonic() + 5
    while ollama.count("generate") < 2:
        assert time.monotonic() < deadline, "verifier call never started"
        time.sleep(0.02)
    app.dispatch("jobs.cancel", {"job_id": job["job_id"]})
    final = wait_terminal(app, job["job_id"], timeout=5)
    assert (final["state"], final["message_code"]) == ("cancelled", "cancelled_by_user")
    assert roles(app, job["session_id"]) == ["user"]


def test_submit_chat_refuses_image_turns_and_empty_messages(app: SidecarApp) -> None:
    with pytest.raises(RpcError):
        app.dispatch("jobs.submit_chat", {"message": "describe", "model": MODEL, "artifact_ids": ["a"]})
    with pytest.raises(RpcError):
        app.dispatch("jobs.submit_chat", {"message": "  ", "model": MODEL})


def test_chat_job_methods_are_never_replayed_by_the_host() -> None:
    lib_rs = (PYTHON_DIR.parent / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    start = lib_rs.index("fn can_restart_and_retry")
    allowlist = lib_rs[start : lib_rs.index("}", lib_rs.index("matches!", start))]
    assert "jobs.submit_chat" not in allowlist
    assert "jobs.retry_chat" not in allowlist
    assert "jobs.chat_result" not in allowlist
    assert "jobs.chat_status" in allowlist  # reads the durable outcome; never starts work
    assert "jobs.get" in allowlist  # status polling is the safe recovery probe


def test_backend_loss_mid_preparation_leaves_request_unanswered_and_unreplayed(
    tmp_path: Path, ollama: FakeOllama
) -> None:
    """Kill the real sidecar process mid-load, start a new one on the same profile."""
    ollama.prepare_delay = 30.0
    profile = tmp_path / "profile"
    source = tmp_path / "release-notes.txt"
    source.write_text(DOCUMENT_TEXT, encoding="utf-8")
    bootstrap = (
        "import sys; sys.path.insert(0, {python!r});"
        "from odysseus_desktop_backend.services import model_service, embedding_service;"
        "model_service.OLLAMA_ENDPOINT = {url!r};"
        "embedding_service.OllamaEmbeddingProvider.installed = lambda self: False;"
        "import rpc_server; raise SystemExit(rpc_server.main())"
    ).format(python=str(PYTHON_DIR), url=ollama.url)

    def spawn() -> subprocess.Popen[str]:
        port = ollama.url.rsplit(":", 1)[1]  # the child may reach only this test's fake
        env = {**os.environ, "ODYSSEUS_PROFILE_DIR": str(profile), ALLOWED_PORTS_ENV: port}
        return subprocess.Popen(
            [sys.executable, "-c", bootstrap],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )

    def call(proc: subprocess.Popen[str], method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    first = spawn()
    try:
        imported = call(first, "jobs.submit_import", {"paths": [str(source)]})["result"]["jobs"][0]
        deadline = time.monotonic() + 15
        while call(first, "jobs.get", {"job_id": imported["job_id"]})["result"]["state"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        submitted = call(first, "jobs.submit_chat", {"message": "What if the backend fails?", "model": MODEL, "use_rag": True})
        job_id = submitted["result"]["job"]["job_id"]
        session_id = submitted["result"]["session"]["id"]
        assert ollama.arrived["prepare"].wait(10)
    finally:
        first.kill()
        first.wait(10)

    deadline = time.monotonic() + 5
    while "prepare" not in ollama.disconnects and time.monotonic() < deadline:
        time.sleep(0.02)
    assert "prepare" in ollama.disconnects  # the dead process's load request is abandoned

    second = spawn()
    try:
        stale = call(second, "jobs.get", {"job_id": job_id})
        assert "error" in stale, "a restarted backend must not resurrect the lost job"
        messages = call(second, "sessions.messages", {"session_id": session_id})["result"]
        assert [message["role"] for message in messages] == ["user"]
        assert call(second, "health.ping", {})["result"]["ok"] is True
    finally:
        second.stdin.close()  # type: ignore[union-attr]
        second.wait(10)
    assert ollama.requests == ["prepare"], "nothing was retried or replayed"
