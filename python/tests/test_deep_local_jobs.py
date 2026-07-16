"""Persisted Deep Local job system tests (Phase 2 of the Deep Local build).

Covers: persist-before-inference, the nine-state machine, single-generation
FIFO, queue backoff via waiting_for_provider, honest cancellation semantics
(cancelled_before_start vs interrupted), startup repair, explicit
non-duplicating retry, evidence bounds, and privacy (no prompts, evidence,
results, paths, or keys in logs or list snapshots).

Uses the same fake loopback Colibri server as test_colibri_provider.py.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from odysseus_desktop_backend.services.deep_local_jobs import (
    MAX_EVIDENCE_ITEMS,
    MAX_QUEUED_JOBS,
    TERMINAL_STATES,
    DeepLocalJobService,
    _build_messages,
)
from odysseus_desktop_backend.storage import Database

MODEL_ID = "glm-5.2-colibri"
SENTINEL_QUESTION = "SENTINEL-QUESTION-b7d2 what does the contract say?"
SENTINEL_SNIPPET = "SENTINEL-SNIPPET-4e19 the notice period is 30 days"
SENTINEL_ANSWER = "SENTINEL-ANSWER-a3c8 thirty days"
SENTINEL_KEY = "SENTINEL-COLIBRI-KEY-9f31"


def _openai_error(message: str, code: str, error_type: str = "invalid_request_error") -> bytes:
    return json.dumps(
        {"error": {"message": message, "type": error_type, "param": None, "code": code}}
    ).encode()


class _FakeColibriHandler(BaseHTTPRequestHandler):
    behavior: dict[str, Any] = {}
    seen: list[dict[str, Any]] = []
    release: threading.Event = threading.Event()
    request_started: threading.Event = threading.Event()

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
            if self.behavior.get("health_mode", "ok") == "ok":
                self._send(200, json.dumps({"status": "ok", "scheduler": {"active": 0, "queued": 0, "capacity": 1, "max_queue": 8}}).encode())
            else:
                self._send(500, _openai_error("engine failed", "engine_error", "server_error"))
            return
        if self.path == "/v1/models":
            self._send(200, json.dumps({"object": "list", "data": [{"id": MODEL_ID, "object": "model", "created": 1, "owned_by": "colibri"}]}).encode())
            return
        self._send(404, _openai_error("Not found.", "not_found"))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append({"path": self.path, "body": body})
        if self.path != "/v1/chat/completions":
            self._send(404, _openai_error("Not found.", "not_found"))
            return
        mode = self.behavior.get("chat_mode", "ok")
        if mode == "queue_full_once":
            self.behavior["chat_mode"] = "ok"
            self._send(429, _openai_error("The inference queue is full.", "queue_full", "rate_limit_error"), {"Retry-After": "0.1"})
            return
        if mode == "queue_full_always":
            self._send(429, _openai_error("The inference queue is full.", "queue_full", "rate_limit_error"), {"Retry-After": "0.05"})
            return
        if mode == "server_error":
            self._send(500, _openai_error("engine failed", "engine_error", "server_error"))
            return
        if mode == "hang_until_released":
            type(self).request_started.set()
            self.release.wait(timeout=30)
        content = self.behavior.get("content", SENTINEL_ANSWER)
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
                            "message": {"role": "assistant", "content": content, "refusal": None},
                            "logprobs": None,
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
                }
            ).encode(),
            {"x-colibri-queue-wait-ms": "42"},
        )


@pytest.fixture()
def fake_colibri():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeColibriHandler)
    _FakeColibriHandler.behavior = {}
    _FakeColibriHandler.seen = []
    _FakeColibriHandler.release = threading.Event()
    _FakeColibriHandler.request_started = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield endpoint
    finally:
        _FakeColibriHandler.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def db(tmp_path):
    database = Database(tmp_path)
    yield database
    database.close()


@pytest.fixture()
def service(tmp_path, db):
    svc = DeepLocalJobService(tmp_path, db)
    yield svc
    svc.shutdown()


def _enable(db: Database, endpoint: str) -> None:
    db.set_setting("deep_local_enabled", True)
    db.set_setting("deep_local_endpoint", endpoint)
    db.set_setting("deep_local_timeout_seconds", 30)


def _wait_terminal(service: DeepLocalJobService, job_id: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get(job_id)
        if snapshot["state"] in TERMINAL_STATES:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"job did not reach a terminal state: {service.get(job_id)}")


def _wait_state(service: DeepLocalJobService, job_id: str, state: str, timeout: float = 15.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get(job_id)
        if snapshot["state"] == state:
            return snapshot
        time.sleep(0.02)
    raise AssertionError(f"job never reached {state}: {service.get(job_id)}")


# -- gating and validation --------------------------------------------------


def test_submit_disabled_returns_structured_error(service: DeepLocalJobService) -> None:
    outcome = service.submit(question="hello")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "disabled"


def test_submit_rejects_non_loopback_endpoint(service: DeepLocalJobService, db: Database) -> None:
    db.set_setting("deep_local_enabled", True)
    db.set_setting("deep_local_endpoint", "http://192.168.1.50:8000")
    outcome = service.submit(question="hello")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "disabled"


def test_submit_validates_question_and_evidence(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    with pytest.raises(ValueError):
        service.submit(question="   ")
    with pytest.raises(ValueError):
        service.submit(question="q" * 8_001)
    with pytest.raises(ValueError):
        service.submit(question="q", evidence=[{"source_id": "", "snippet": "s"}])
    with pytest.raises(ValueError):
        service.submit(question="q", evidence=[{"source_id": "a", "snippet": "s" * 4_001}])
    with pytest.raises(ValueError):
        service.submit(
            question="q",
            evidence=[{"source_id": f"s{i}", "snippet": "x"} for i in range(MAX_EVIDENCE_ITEMS + 1)],
        )
    with pytest.raises(ValueError):
        service.submit(question="q", max_output_tokens=0)
    with pytest.raises(ValueError):
        service.submit(question="q", max_output_tokens=100_000)
    with pytest.raises(ValueError):
        service.submit(question="q", thinking="hard")


def test_evidence_total_size_bounded(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    items = [{"source_id": f"s{i}", "snippet": "x" * 4_000} for i in range(17)]
    with pytest.raises(ValueError):
        service.submit(question="q", evidence=items)


# -- persist-before-inference and completion ---------------------------------


def test_job_persisted_before_inference(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    outcome = service.submit(question=SENTINEL_QUESTION)
    assert outcome["ok"] is True
    job_id = outcome["job"]["job_id"]
    row = db.conn.execute("SELECT * FROM deep_local_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row is not None
    assert row["question"] == SENTINEL_QUESTION
    _FakeColibriHandler.release.set()
    _wait_terminal(service, job_id)


def test_happy_path_completion_persists_result_and_usage(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    outcome = service.submit(
        question=SENTINEL_QUESTION,
        evidence=[{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
    )
    job_id = outcome["job"]["job_id"]
    snapshot = _wait_terminal(service, job_id)
    assert snapshot["state"] == "completed"
    assert snapshot["message_code"] == ""
    assert snapshot["model_id"] == MODEL_ID
    assert snapshot["result_text"] == SENTINEL_ANSWER
    assert snapshot["usage"]["completion_tokens"] == 7
    assert snapshot["usage"]["queue_wait_ms"] == 42
    states = [item["state"] for item in snapshot["state_history"]]
    assert states == ["queued", "checking_runtime", "running", "completed"]
    row = db.conn.execute("SELECT * FROM deep_local_jobs WHERE id = ?", (job_id,)).fetchone()
    assert row["state"] == "completed"
    assert row["result_text"] == SENTINEL_ANSWER
    sent = _FakeColibriHandler.seen[-1]["body"]
    assert sent["model"] == MODEL_ID
    assert SENTINEL_SNIPPET in sent["messages"][1]["content"]
    assert sent["stream"] is False


def test_prompt_includes_source_citations() -> None:
    messages = _build_messages("what?", [{"source_id": "abc", "snippet": "text"}])
    assert "[S1]" in messages[1]["content"]
    assert "abc" in messages[1]["content"]


def test_provider_failure_maps_to_failed_with_category(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "server_error"
    outcome = service.submit(question="q")
    snapshot = _wait_terminal(service, outcome["job"]["job_id"])
    assert snapshot["state"] == "failed"
    assert snapshot["message_code"] == "deep_local_failed"
    assert snapshot["error_category"] == "server_error"


def test_unreachable_server_fails_with_connection_category(
    service: DeepLocalJobService, db: Database
) -> None:
    _enable(db, "http://127.0.0.1:9")  # discard port: nothing listens
    outcome = service.submit(question="q")
    snapshot = _wait_terminal(service, outcome["job"]["job_id"])
    assert snapshot["state"] == "failed"
    assert snapshot["error_category"] in {"connection_failure", "server_error"}


# -- queue semantics ----------------------------------------------------------


def test_queue_full_once_backs_off_then_completes(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "queue_full_once"
    outcome = service.submit(question="q")
    snapshot = _wait_terminal(service, outcome["job"]["job_id"])
    assert snapshot["state"] == "completed"
    states = [item["state"] for item in snapshot["state_history"]]
    assert "waiting_for_provider" in states


def test_queue_full_past_deadline_fails_with_queue_category(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    db.set_setting("deep_local_queue_wait_seconds", 0)
    _FakeColibriHandler.behavior["chat_mode"] = "queue_full_always"
    outcome = service.submit(question="q")
    snapshot = _wait_terminal(service, outcome["job"]["job_id"])
    assert snapshot["state"] == "failed"
    assert snapshot["error_category"] == "queue_saturated"


def test_submit_queue_capacity_bounded(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    jobs = [service.submit(question=f"q{i}") for i in range(MAX_QUEUED_JOBS)]
    with pytest.raises(ValueError):
        service.submit(question="one too many")
    _FakeColibriHandler.release.set()
    for outcome in jobs:
        _wait_terminal(service, outcome["job"]["job_id"], timeout=30)


def test_jobs_run_fifo_one_generation_at_a_time(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    first = service.submit(question="first")
    second = service.submit(question="second")
    _wait_terminal(service, first["job"]["job_id"])
    _wait_terminal(service, second["job"]["job_id"])
    prompts = [item["body"]["messages"][1]["content"] for item in _FakeColibriHandler.seen]
    assert [("first" in p, "second" in p) for p in prompts] == [(True, False), (False, True)]
    listed = service.list()
    assert {item["state"] for item in listed} == {"completed"}


# -- cancellation honesty ------------------------------------------------------


def test_cancel_before_start_is_cancelled_before_start(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    blocker = service.submit(question="blocker")
    queued = service.submit(question="queued victim")
    cancelled = service.cancel(queued["job"]["job_id"])
    assert cancelled["state"] == "cancel_requested"
    _FakeColibriHandler.release.set()
    snapshot = _wait_terminal(service, queued["job"]["job_id"])
    assert snapshot["state"] == "cancelled_before_start"
    assert snapshot["message_code"] == "cancelled_before_start"
    _wait_terminal(service, blocker["job"]["job_id"])


def test_cancel_in_flight_is_interrupted_never_cancelled(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    outcome = service.submit(question="long job")
    job_id = outcome["job"]["job_id"]
    assert _FakeColibriHandler.request_started.wait(timeout=10)
    response = service.cancel(job_id)
    assert response["state"] == "cancel_requested"
    snapshot = _wait_terminal(service, job_id)
    assert snapshot["state"] == "interrupted"
    assert snapshot["message_code"] == "stopped_waiting"
    # The engine may still be generating; we only stopped waiting.
    _FakeColibriHandler.release.set()


def test_cancel_after_completion_stays_completed(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    outcome = service.submit(question="quick")
    job_id = outcome["job"]["job_id"]
    _wait_terminal(service, job_id)
    snapshot = service.cancel(job_id)
    assert snapshot["state"] == "completed"


def test_cancel_is_idempotent(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    outcome = service.submit(question="long job")
    job_id = outcome["job"]["job_id"]
    assert _FakeColibriHandler.request_started.wait(timeout=10)
    service.cancel(job_id)
    again = service.cancel(job_id)
    assert again["state"] in {"cancel_requested", "interrupted"}
    snapshot = _wait_terminal(service, job_id)
    assert snapshot["state"] == "interrupted"
    _FakeColibriHandler.release.set()


def test_unknown_job_cancel_raises_key_error(service: DeepLocalJobService) -> None:
    with pytest.raises(KeyError):
        service.cancel("nope")
    with pytest.raises(KeyError):
        service.get("nope")


# -- startup repair -------------------------------------------------------------


def test_startup_repair_marks_inflight_rows_interrupted(tmp_path) -> None:
    db = Database(tmp_path)
    try:
        now = 1_000
        for job_id, state in [
            ("j-queued", "queued"),
            ("j-checking", "checking_runtime"),
            ("j-waiting", "waiting_for_provider"),
            ("j-running", "running"),
            ("j-cancelreq", "cancel_requested"),
            ("j-done", "completed"),
            ("j-failed", "failed"),
        ]:
            db.conn.execute(
                "INSERT INTO deep_local_jobs (id, state, question, created_at, updated_at) "
                "VALUES (?, ?, 'q', ?, ?)",
                (job_id, state, now, now),
            )
        db.conn.commit()
        service = DeepLocalJobService(tmp_path, db)
        repaired = service.repair_startup_state()
        assert repaired == 5
        states = {
            row["id"]: (row["state"], row["message_code"])
            for row in db.conn.execute("SELECT * FROM deep_local_jobs").fetchall()
        }
        for job_id in ["j-queued", "j-checking", "j-waiting", "j-running", "j-cancelreq"]:
            assert states[job_id] == ("interrupted", "interrupted_by_restart")
        assert states["j-done"] == ("completed", "")
        assert states["j-failed"] == ("failed", "")
        service.shutdown()
    finally:
        db.close()


def test_repair_is_noop_when_clean(service: DeepLocalJobService) -> None:
    assert service.repair_startup_state() == 0


# -- retry ------------------------------------------------------------------------


def test_retry_clones_interrupted_job_and_completes(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    outcome = service.submit(
        question=SENTINEL_QUESTION,
        evidence=[{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
    )
    job_id = outcome["job"]["job_id"]
    assert _FakeColibriHandler.request_started.wait(timeout=10)
    service.cancel(job_id)
    _wait_terminal(service, job_id)
    _FakeColibriHandler.behavior["chat_mode"] = "ok"
    _FakeColibriHandler.release.set()
    retried = service.retry(job_id)
    assert retried["ok"] is True
    new_id = retried["job"]["job_id"]
    assert new_id != job_id
    assert retried["job"]["attempt_count"] == 2
    assert retried["job"]["retry_of"] == job_id
    snapshot = _wait_terminal(service, new_id)
    assert snapshot["state"] == "completed"
    assert snapshot["question"] == SENTINEL_QUESTION


def test_retry_rejects_active_and_completed_jobs(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    outcome = service.submit(question="busy")
    job_id = outcome["job"]["job_id"]
    assert _FakeColibriHandler.request_started.wait(timeout=10)
    with pytest.raises(ValueError):
        service.retry(job_id)
    _FakeColibriHandler.behavior["chat_mode"] = "ok"
    _FakeColibriHandler.release.set()
    # in-flight cancel -> interrupted; then complete a fresh job and try retrying it
    done = service.submit(question="quick")
    done_id = done["job"]["job_id"]
    _wait_terminal(service, job_id)
    _wait_terminal(service, done_id)
    with pytest.raises(ValueError):
        service.retry(done_id)


def test_retry_does_not_stack_duplicates(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    _FakeColibriHandler.behavior["chat_mode"] = "hang_until_released"
    outcome = service.submit(question="original")
    job_id = outcome["job"]["job_id"]
    assert _FakeColibriHandler.request_started.wait(timeout=10)
    service.cancel(job_id)
    _wait_terminal(service, job_id)
    first_retry = service.retry(job_id)
    second_retry = service.retry(job_id)
    assert second_retry["duplicate"] is True
    assert second_retry["job"]["job_id"] == first_retry["job"]["job_id"]
    _FakeColibriHandler.behavior["chat_mode"] = "ok"
    _FakeColibriHandler.release.set()
    _wait_terminal(service, first_retry["job"]["job_id"])


def test_submit_request_id_is_idempotent(
    service: DeepLocalJobService, db: Database, fake_colibri: str
) -> None:
    _enable(db, fake_colibri)
    first = service.submit(question="q", request_id="req-1")
    second = service.submit(question="q", request_id="req-1")
    assert second["duplicate"] is True
    assert second["job"]["job_id"] == first["job"]["job_id"]
    _wait_terminal(service, first["job"]["job_id"])


def test_retry_of_persisted_row_after_restart(tmp_path, fake_colibri: str) -> None:
    """Full restart cycle: submit, kill mid-flight (simulated), repair, retry."""
    db = Database(tmp_path)
    _enable(db, fake_colibri)
    db.conn.execute(
        "INSERT INTO deep_local_jobs (id, state, question, evidence_json, params_json, model_id, created_at, updated_at) "
        "VALUES ('j-dead', 'running', ?, ?, ?, ?, 1000, 1000)",
        (
            SENTINEL_QUESTION,
            json.dumps([{"source_id": "doc-9", "snippet": SENTINEL_SNIPPET}]),
            json.dumps({"max_output_tokens": 64, "temperature": 0.0, "top_p": None, "thinking": "off"}),
            MODEL_ID,
        ),
    )
    db.conn.commit()
    service = DeepLocalJobService(tmp_path, db)
    try:
        assert service.repair_startup_state() == 1
        repaired = service.get("j-dead")
        assert repaired["state"] == "interrupted"
        assert repaired["message_code"] == "interrupted_by_restart"
        retried = service.retry("j-dead")
        assert retried["ok"] is True
        snapshot = _wait_terminal(service, retried["job"]["job_id"])
        assert snapshot["state"] == "completed"
        assert snapshot["question"] == SENTINEL_QUESTION
        assert snapshot["attempt_count"] == 2
    finally:
        service.shutdown()
        db.close()


# -- privacy -----------------------------------------------------------------------


def test_list_snapshots_exclude_content(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    outcome = service.submit(
        question=SENTINEL_QUESTION,
        evidence=[{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
    )
    _wait_terminal(service, outcome["job"]["job_id"])
    listed = service.list()
    assert listed, "expected at least one listed job"
    payload = json.dumps(listed)
    assert SENTINEL_QUESTION not in payload
    assert SENTINEL_SNIPPET not in payload
    assert SENTINEL_ANSWER not in payload
    assert listed[0]["question_chars"] == len(SENTINEL_QUESTION)
    assert listed[0]["evidence_count"] == 1


def test_logs_never_contain_content_or_key(
    service: DeepLocalJobService,
    db: Database,
    fake_colibri: str,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ODYSSEUS_COLIBRI_API_KEY", SENTINEL_KEY)
    _enable(db, fake_colibri)
    with caplog.at_level(logging.DEBUG):
        good = service.submit(
            question=SENTINEL_QUESTION,
            evidence=[{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
        )
        _wait_terminal(service, good["job"]["job_id"])
        _FakeColibriHandler.behavior["chat_mode"] = "server_error"
        bad = service.submit(question=SENTINEL_QUESTION)
        _wait_terminal(service, bad["job"]["job_id"])
    log_text = caplog.text
    assert SENTINEL_QUESTION not in log_text
    assert SENTINEL_SNIPPET not in log_text
    assert SENTINEL_ANSWER not in log_text
    assert SENTINEL_KEY not in log_text


def test_get_returns_full_content_for_owner(service: DeepLocalJobService, db: Database, fake_colibri: str) -> None:
    _enable(db, fake_colibri)
    outcome = service.submit(
        question=SENTINEL_QUESTION,
        evidence=[{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}],
    )
    snapshot = _wait_terminal(service, outcome["job"]["job_id"])
    assert snapshot["question"] == SENTINEL_QUESTION
    assert snapshot["evidence"] == [{"source_id": "doc-1", "snippet": SENTINEL_SNIPPET}]
    assert snapshot["result_text"] == SENTINEL_ANSWER


def test_persisted_row_readable_after_service_restart_get(
    tmp_path, fake_colibri: str
) -> None:
    db = Database(tmp_path)
    _enable(db, fake_colibri)
    service = DeepLocalJobService(tmp_path, db)
    outcome = service.submit(question=SENTINEL_QUESTION)
    job_id = outcome["job"]["job_id"]
    _wait_terminal(service, job_id)
    service.shutdown()

    fresh = DeepLocalJobService(tmp_path, db)
    try:
        assert fresh.repair_startup_state() == 0
        snapshot = fresh.get(job_id)
        assert snapshot["state"] == "completed"
        assert snapshot["result_text"] == SENTINEL_ANSWER
        listed = fresh.list()
        assert any(item["job_id"] == job_id for item in listed)
    finally:
        fresh.shutdown()
        db.close()
