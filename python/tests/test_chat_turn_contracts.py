"""Interactive turn deadlines and durable attempts/retry (owner-approved contracts).

Deadline arithmetic runs on a controlled clock against a fake runtime that
records the timeout each operation was given. Job-level behavior runs through
the real RPC -> JobService -> ChatService -> ModelService path against the
loopback fake Ollama from the cold-model lifecycle tests. Races use separate
database connections on separate threads, like the RPC thread and the worker.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import pytest

import rpc_server
from odysseus_desktop_backend.cancellation import (
    JobCancelledError,
    TurnDeadline,
    TurnDeadlineExceeded,
    deadline_scope,
)
from odysseus_desktop_backend.services import model_service
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.chat_turns import AttemptRetired, ChatTurnStore
from odysseus_desktop_backend.services.job_service import chat_failure_code
from odysseus_desktop_backend.services.model_service import ModelService, ModelTimeoutError
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.storage import Database
from rpc_server import RpcError, SidecarApp
from test_chat_cold_model_lifecycle import (  # noqa: F401 - fixtures
    ANSWER,
    MODEL,
    FakeOllama,
    app,
    ollama,
    roles,
    scale_timeouts,
    submit,
    wait_terminal,
)

QUESTION = "What should I do if the backend becomes unavailable?"
PASSED = json.dumps({"claims": [{"text": "Use Retry backend", "status": "supported", "source_ids": ["S1"]}]})
CONTRADICTED = json.dumps(
    {"claims": [{"text": "Wrong", "status": "contradicted", "source_ids": ["S1"]}], "contradicted_claims": ["Wrong"]}
)


# ---------------------------------------------------------------- helpers


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class ClockModels:
    """Fake runtime: records each call's timeout, advances the controlled clock, then answers."""

    def __init__(self, clock: Clock, **script: list[tuple[float, Any]]):
        self.clock = clock
        self.script = {kind: list(steps) for kind, steps in script.items()}
        self.calls: list[tuple[str, float]] = []
        self.messages: list[list[dict[str, Any]]] = []

    def _step(self, kind: str, timeout: float) -> Any:
        self.calls.append((kind, round(timeout, 6)))
        advance, outcome = self.script[kind].pop(0)
        self.clock.now += advance
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    def prepare(self, model: str, *, options: Any = None, timeout: float) -> dict[str, Any]:
        self._step("prepare", timeout)
        return {"model": model, "elapsed_ms": 0, "load_duration_ns": 0}

    def chat_detailed(self, model: str, messages: list[dict[str, Any]], *, timeout: float, response_format: Any = None, **_: Any) -> dict[str, Any]:
        if response_format == "json":
            kind = "verify"
        elif "Revise the answer once" in str(messages[-1].get("content")):
            kind = "correct"
        else:
            kind = "generate"
            self.messages.append(messages)
        content = self._step(kind, timeout)
        return {"model": model, "content": content, "thinking": "", "elapsed_ms": 0}

    def chat(self, *args: Any, **kwargs: Any) -> str:  # pragma: no cover - never used
        raise AssertionError("unexpected chat()")


def kinds(models: ClockModels) -> list[str]:
    return [kind for kind, _ in models.calls]


def new_turn(app: SidecarApp, question: str = QUESTION, attachments: list[str] | None = None) -> dict[str, Any]:
    session = app.sessions.create(model=MODEL)
    turn, created = app.turns.accept(
        session_id=session["id"],
        content=question,
        attachment_document_ids=attachments or [],
        request={"model": MODEL, "use_rag": True, "document_ids": [app.test_document_id]},  # type: ignore[attr-defined]
        operation_key=str(uuid.uuid4()),
    )
    assert created
    return turn


def run_turn(app: SidecarApp, models: ClockModels, deadline: TurnDeadline, *, verify: bool = False, turn: dict[str, Any] | None = None) -> dict[str, Any]:
    turn = turn or new_turn(app)
    app.turns.start(turn["current_attempt_id"])
    chat = ChatService(app.sessions, app.settings, models, rag=app.rag, documents=app.documents, sources=app.sources, turns=app.turns)
    session_id, content, attachments, request = app.turns.request(turn["message_id"])
    with deadline_scope(deadline):
        return chat.send(
            session_id=session_id,
            message=content,
            attachment_document_ids=attachments,
            attempt_id=turn["current_attempt_id"],
            user_message_id=turn["message_id"],
            verify_rag=verify,
            **request,
        )


def store_for(app: SidecarApp) -> ChatTurnStore:
    """A second connection to the same profile, as the worker thread has."""
    return ChatTurnStore(Database(app.profile_dir))


# ------------------------------------------------------- A. deadlines


def test_queue_time_consumes_the_overall_deadline(app: SidecarApp) -> None:
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 250  # queued/retrieval time before preparation
    models = ClockModels(clock, prepare=[(10, None)], generate=[(1, ANSWER)])
    run_turn(app, models, deadline)
    assert models.calls == [("prepare", 50.0), ("generate", 40.0)]


def test_preparation_and_generation_each_get_the_lesser_allowance(app: SidecarApp) -> None:
    clock = Clock()
    models = ClockModels(clock, prepare=[(100, None)], generate=[(1, ANSWER)])
    run_turn(app, models, TurnDeadline(300, clock))
    assert models.calls == [("prepare", 120.0), ("generate", 120.0)]

    clock2 = Clock()
    deadline = TurnDeadline(300, clock2)
    clock2.now += 100
    models2 = ClockModels(clock2, prepare=[(150, None)], generate=[(1, ANSWER)])
    run_turn(app, models2, deadline)
    assert models2.calls == [("prepare", 120.0), ("generate", 50.0)]


def test_retrieval_delay_past_the_deadline_admits_no_inference(app: SidecarApp, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = Clock()
    real = RAGService.build_quote_context

    def slow_retrieval(self: RAGService, *args: Any, **kwargs: Any) -> Any:
        clock.now += 301  # embeddings/retrieval outlived the whole turn
        return real(self, *args, **kwargs)

    monkeypatch.setattr(RAGService, "build_quote_context", slow_retrieval)
    models = ClockModels(clock)
    with pytest.raises(TurnDeadlineExceeded) as raised:
        run_turn(app, models, TurnDeadline(300, clock))
    assert models.calls == []
    assert chat_failure_code(raised.value, None) == "chat_turn_timeout"


def test_overall_expiry_during_generation_is_a_turn_timeout(app: SidecarApp) -> None:
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 200
    models = ClockModels(clock, prepare=[(0, None)], generate=[(100, ModelTimeoutError("timeout"))])
    with pytest.raises(ModelTimeoutError) as raised:
        run_turn(app, models, deadline)
    assert models.calls == [("prepare", 100.0), ("generate", 100.0)]
    assert chat_failure_code(raised.value, deadline) == "chat_turn_timeout"
    # A generation timeout with turn time left stays a generation timeout.
    assert chat_failure_code(ModelTimeoutError("t"), TurnDeadline(300, Clock())) == "model_generation_timeout"


def test_verification_and_correction_share_one_60s_budget(app: SidecarApp) -> None:
    clock = Clock()
    models = ClockModels(
        clock,
        prepare=[(0, None)],
        generate=[(5, "draft answer [S1]")],
        verify=[(25, CONTRADICTED), (5, PASSED)],
        correct=[(30, "corrected answer [S1]")],
    )
    result = run_turn(app, models, TurnDeadline(300, clock), verify=True)
    assert models.calls == [("prepare", 120.0), ("generate", 120.0), ("verify", 60.0), ("correct", 35.0), ("verify", 5.0)]
    assert result["assistant_message"]["content"] == "corrected answer [S1]"
    assert "verification_notice" not in result["assistant_message"]["metadata"]


def test_draft_is_kept_when_the_budget_expires_before_final_verification(app: SidecarApp) -> None:
    clock = Clock()
    models = ClockModels(
        clock,
        prepare=[(0, None)],
        generate=[(5, "draft answer [S1]")],
        verify=[(25, CONTRADICTED)],
        correct=[(35, "corrected answer [S1]")],  # uses the rest of the 60 s
    )
    result = run_turn(app, models, TurnDeadline(300, clock), verify=True)
    assert kinds(models) == ["prepare", "generate", "verify", "correct"]  # no final verification started
    answer = result["assistant_message"]
    assert answer["content"].startswith("draft answer")
    assert answer["metadata"]["verification_notice"] == {
        "label": "Verification incomplete",
        "reason": "Verification ran out of time.",
    }
    saved = [m for m in app.sessions.messages(answer["session_id"]) if m["role"] == "assistant"]
    assert [m["content"] for m in saved] == [answer["content"]]


def test_draft_is_kept_when_the_correction_fails_final_verification(app: SidecarApp) -> None:
    clock = Clock()
    models = ClockModels(
        clock,
        prepare=[(0, None)],
        generate=[(1, "draft answer [S1]")],
        verify=[(1, CONTRADICTED), (1, CONTRADICTED)],
        correct=[(1, "corrected answer [S1]")],
    )
    result = run_turn(app, models, TurnDeadline(300, clock), verify=True)
    assert result["assistant_message"]["content"].startswith("draft answer")
    assert "did not pass verification" in result["assistant_message"]["metadata"]["verification_notice"]["reason"]


def test_verifier_timeout_keeps_the_draft_with_a_visible_reason(app: SidecarApp) -> None:
    clock = Clock()
    models = ClockModels(
        clock, prepare=[(0, None)], generate=[(1, "draft answer [S1]")], verify=[(60, ModelTimeoutError("t"))]
    )
    result = run_turn(app, models, TurnDeadline(300, clock), verify=True)
    assert result["assistant_message"]["content"].startswith("draft answer")
    assert result["assistant_message"]["metadata"]["verification_notice"] == {
        "label": "Verification incomplete",
        "reason": "Verification ran out of time.",
    }


def test_overall_expiry_during_verification_keeps_the_complete_draft(app: SidecarApp) -> None:
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 270
    models = ClockModels(clock, prepare=[(0, None)], generate=[(10, "draft answer [S1]")], verify=[(20, ModelTimeoutError("t"))])
    result = run_turn(app, models, deadline, verify=True)
    assert models.calls[-1] == ("verify", 20.0)  # min(60, 20 remaining)
    assert result["assistant_message"]["content"].startswith("draft answer")
    assert app.turns.status(message_id=result["user_message"]["id"])["latest_state"] == "completed"


def test_draft_completed_after_expiry_is_committed_without_verification(app: SidecarApp) -> None:
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 250
    models = ClockModels(clock, prepare=[(0, None)], generate=[(60, "late but complete [S1]")])
    result = run_turn(app, models, deadline, verify=True)
    assert kinds(models) == ["prepare", "generate"]  # no verification started after expiry
    assert result["assistant_message"]["content"].startswith("late but complete")
    assert result["assistant_message"]["metadata"]["verification_notice"]["reason"] == "Verification ran out of time."


def test_transport_clips_to_the_turn_and_admits_nothing_after_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []
    monkeypatch.setattr(ModelService, "_request_json", lambda self, open_url, url, payload, timeout: seen.append(timeout) or {})
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 290
    with deadline_scope(deadline):
        ModelService(None)._post_json("http://127.0.0.1:9/api/chat", {}, timeout=120)  # type: ignore[arg-type]
        clock.now += 10
        with pytest.raises(ModelTimeoutError):
            ModelService(None)._post_json("http://127.0.0.1:9/api/chat", {}, timeout=120)  # type: ignore[arg-type]
    assert seen == [10.0]  # the expired call never reached the transport


def test_progress_and_stage_changes_never_move_the_deadline() -> None:
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    budget = deadline.budget(60)
    clock.now += 59
    assert deadline.remaining() == 241 and budget.remaining() == 1
    assert deadline.budget(60).remaining() == 60  # a new sub-budget, not an extension
    clock.now += 241
    assert deadline.expired() and budget.expired()
    with pytest.raises(TurnDeadlineExceeded):
        deadline.allow(120)


# --------------------------------------------------- B. job-level deadlines


def test_overall_deadline_during_preparation_times_the_turn_out(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rpc_server, "INTERACTIVE_TURN_SECONDS", 0.6)
    ollama.prepare_delay = 5.0
    submitted = submit(app, operation_key="k-prep")
    final = wait_terminal(app, submitted["job"]["job_id"])
    assert (final["state"], final["message_code"]) == ("failed", "chat_turn_timeout")
    assert ollama.count("generate") == 0
    turn = app.dispatch("jobs.chat_status", {"operation_key": "k-prep"})["turn"]
    assert (turn["latest_state"], turn["latest_outcome_code"]) == ("timed_out", "chat_turn_timeout")
    assert roles(app, final["session_id"]) == ["user"]


def test_queued_turn_that_outlives_its_deadline_never_reaches_the_model(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    ollama.prepare_delay = 1.0
    first = submit(app)
    assert ollama.arrived["prepare"].wait(5)
    monkeypatch.setattr(rpc_server, "INTERACTIVE_TURN_SECONDS", 0.3)
    second = app.dispatch("jobs.submit_chat", {"message": "A queued question", "model": MODEL, "operation_key": "k-queued"})
    final = wait_terminal(app, second["job"]["job_id"])
    assert (final["state"], final["message_code"]) == ("failed", "chat_turn_timeout")
    assert wait_terminal(app, first["job"]["job_id"])["state"] == "completed"
    assert ollama.requests == ["prepare", "generate"]  # only the first turn
    assert roles(app, second["session"]["id"]) == ["user"]


def test_blocking_retrieval_past_the_deadline_stays_unresolved_until_it_returns(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rpc_server, "INTERACTIVE_TURN_SECONDS", 0.3)
    release = threading.Event()
    real = RAGService.build_quote_context

    def blocking(self: RAGService, *args: Any, **kwargs: Any) -> Any:
        assert release.wait(10)  # an operation that cannot be interrupted
        return real(self, *args, **kwargs)

    monkeypatch.setattr(RAGService, "build_quote_context", blocking)
    job = submit(app, operation_key="k-block")["job"]
    time.sleep(0.6)
    during = app.dispatch("jobs.get", {"job_id": job["job_id"]})
    assert during["state"] == "running" and during["deadline_reached"] is True
    turn = app.dispatch("jobs.chat_status", {"operation_key": "k-block"})["turn"]
    assert turn["latest_state"] == "running" and not turn["can_retry"]  # not settled while work may continue
    released_at = time.time() * 1000
    release.set()
    final = wait_terminal(app, job["job_id"])
    assert (final["state"], final["message_code"], final["deadline_reached"]) == ("failed", "chat_turn_timeout", False)
    assert ollama.requests == []
    attempt = app.dispatch("jobs.chat_status", {"operation_key": "k-block"})["turn"]["attempts"][0]
    assert attempt["state"] == "timed_out" and attempt["finished_at"] >= released_at - 5  # settled after it returned


# ----------------------------------------------- C. attempts and commit safety


def test_accepting_persists_the_question_once_and_snapshots_stay_private(app: SidecarApp, ollama: FakeOllama) -> None:
    ollama.prepare_delay = 0.5
    submitted = submit(app, operation_key="k-once")
    turn = submitted["turn"]
    assert submitted["job"]["attempt_id"] == turn["current_attempt_id"]
    assert roles(app, submitted["session"]["id"]) == ["user"]  # persisted at acceptance, before any work
    final = wait_terminal(app, submitted["job"]["job_id"])
    assert final["state"] == "completed"
    assert QUESTION not in json.dumps(final) and QUESTION not in json.dumps(submitted["job"])
    status = app.dispatch("jobs.chat_status", {"message_id": turn["message_id"]})["turn"]
    assert (status["answered"], status["latest_state"], status["attempt_count"]) == (True, "completed", 1)
    messages = app.dispatch("sessions.messages", {"session_id": submitted["session"]["id"]})
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["turn"]["answer_message_id"] == messages[1]["id"]


def test_duplicate_operation_key_returns_the_existing_operation(app: SidecarApp, ollama: FakeOllama) -> None:
    first = submit(app, operation_key="k-dup")
    again = submit(app, operation_key="k-dup", message="a different payload")
    assert again["turn"]["message_id"] == first["turn"]["message_id"]
    assert again["job"]["job_id"] == first["job"]["job_id"]
    wait_terminal(app, first["job"]["job_id"])
    assert roles(app, first["session"]["id"]) == ["user", "assistant"]
    assert ollama.requests == ["prepare", "generate"]
    assert len([j for j in app.dispatch("jobs.list", {}) if j["kind"] == "chat"]) == 1


def test_one_active_attempt_per_conversation(app: SidecarApp, ollama: FakeOllama) -> None:
    ollama.prepare_delay = 1.0
    first = submit(app)
    with pytest.raises(ValueError, match="in progress"):
        submit(app, session_id=first["session"]["id"])
    wait_terminal(app, first["job"]["job_id"])
    assert roles(app, first["session"]["id"]) == ["user", "assistant"]


def test_cancel_racing_answer_commit_has_exactly_one_outcome(app: SidecarApp) -> None:
    outcomes = []
    for _ in range(25):
        turn = new_turn(app)
        attempt = turn["current_attempt_id"]
        app.turns.start(attempt)
        barrier = threading.Barrier(2)
        result: dict[str, Any] = {}

        def commit() -> None:
            store = store_for(app)
            barrier.wait()
            try:
                store.commit_answer(attempt, content="answer", metadata={}, title="t")
                result["commit"] = "saved"
            except JobCancelledError:
                result["commit"] = "cancelled"
            store.db.close()

        def cancel() -> None:
            store = store_for(app)
            barrier.wait()
            store.request_cancel(attempt)
            store.db.close()

        threads = [threading.Thread(target=commit), threading.Thread(target=cancel)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        status = app.turns.status(message_id=turn["message_id"])
        answers = [m for m in app.sessions.messages(status["session_id"]) if m["role"] == "assistant"]
        cancel_recorded = app.db.conn.execute(
            "SELECT cancel_requested FROM chat_attempts WHERE id = ?", (attempt,)
        ).fetchone()[0]
        if result["commit"] == "saved":
            # The commit won: the cancel found nothing left to stop.
            assert status["latest_state"] == "completed" and len(answers) == 1 and not cancel_recorded
        else:
            assert status["latest_state"] == "running" and answers == []
            app.turns.finish(attempt, "cancelled", "cancelled_by_user")  # what the worker does next
        outcomes.append(result["commit"])
    assert set(outcomes) <= {"saved", "cancelled"}


def test_concurrent_retries_cannot_both_claim_execution(app: SidecarApp) -> None:
    turn = new_turn(app)
    app.turns.start(turn["current_attempt_id"])
    app.turns.finish(turn["current_attempt_id"], "failed", "model_generation_failed")
    barrier = threading.Barrier(2)
    results: list[str] = []

    def retry(key: str) -> None:
        store = store_for(app)
        barrier.wait()
        try:
            store.retry(turn["message_id"], key)
            results.append("claimed")
        except ValueError:
            results.append("refused")
        store.db.close()

    threads = [threading.Thread(target=retry, args=(f"r-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert sorted(results) == ["claimed", "refused"]
    status = app.turns.status(message_id=turn["message_id"])
    assert [a["state"] for a in status["attempts"]] == ["failed", "pending"]


def test_retired_attempt_can_never_commit_after_a_retry(app: SidecarApp) -> None:
    turn = new_turn(app)
    old = turn["current_attempt_id"]
    app.turns.start(old)
    assert app.turns.reconcile_interrupted() == 1  # e.g. backend restarted
    retried, created = app.turns.retry(turn["message_id"], "k-new")
    assert created and retried["current_attempt_id"] != old
    with pytest.raises(AttemptRetired):
        store_for(app).commit_answer(old, content="stale", metadata={}, title="t")
    new = retried["current_attempt_id"]
    app.turns.start(new)
    app.turns.commit_answer(new, content="fresh", metadata={}, title="t")
    with pytest.raises(AttemptRetired):  # at most one committed answer
        app.turns.commit_answer(new, content="again", metadata={}, title="t")
    contents = [m["content"] for m in app.sessions.messages(turn["session_id"])]
    assert contents == [QUESTION, "fresh"]


def test_database_rejects_a_second_answer_for_one_message(app: SidecarApp) -> None:
    import sqlite3

    turn = new_turn(app)
    attempt = turn["current_attempt_id"]
    app.turns.start(attempt)
    answer = app.turns.commit_answer(attempt, content="a", metadata={}, title="t")
    conn = app.db.conn
    conn.execute(
        "INSERT INTO chat_attempts(id, message_id, session_id, attempt_number, operation_key, state, created_at) VALUES ('x', ?, ?, 2, 'kx', 'failed', 0)",
        (turn["message_id"], turn["session_id"]),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE chat_attempts SET state = 'completed', answer_message_id = ? WHERE id = 'x'", (answer["id"],))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE chat_attempts SET state = 'completed' WHERE id = 'x'")  # answer id required
    conn.rollback()


def test_cancel_while_queued_settles_the_attempt_without_any_request(app: SidecarApp, ollama: FakeOllama) -> None:
    ollama.prepare_delay = 1.0
    first = submit(app)
    assert ollama.arrived["prepare"].wait(5)
    second = app.dispatch("jobs.submit_chat", {"message": "Queued then cancelled", "model": MODEL, "operation_key": "k-qc"})
    app.dispatch("jobs.cancel", {"job_id": second["job"]["job_id"]})
    status = app.dispatch("jobs.chat_status", {"operation_key": "k-qc"})["turn"]
    assert status["latest_state"] == "cancelled" and status["can_retry"]  # settled immediately, durably
    assert wait_terminal(app, second["job"]["job_id"])["state"] == "cancelled"
    wait_terminal(app, first["job"]["job_id"])
    assert ollama.requests == ["prepare", "generate"]
    assert roles(app, second["session"]["id"]) == ["user"]


def test_saved_answer_survives_result_delivery_failure(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: ChatService, message: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("hydration failed after commit")

    monkeypatch.setattr(ChatService, "_hydrate_single_message", broken)
    submitted = submit(app, operation_key="k-deliver")
    final = wait_terminal(app, submitted["job"]["job_id"])
    assert final["state"] == "completed"  # the answer exists, so the job is not a failure
    with pytest.raises(ValueError):
        app.dispatch("jobs.chat_result", {"job_id": final["job_id"]})
    status = app.dispatch("jobs.chat_status", {"operation_key": "k-deliver"})["turn"]
    assert status["answered"] and status["latest_state"] == "completed"
    assert roles(app, submitted["session"]["id"]) == ["user", "assistant"]


# ------------------------------------------------------ D. retry and history


def generation_history(ollama: FakeOllama) -> list[list[tuple[str, str]]]:
    return [
        [(m["role"], m["content"].split("\n\n")[0]) for m in body["messages"] if m["role"] != "system"]
        for body in ollama.bodies
        if body.get("messages")
    ]


def test_retry_reuses_the_original_message_and_history_holds_each_question_once(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    scale_timeouts(monkeypatch, 0.4)
    ollama.generate_delay = 3.0
    failed = submit(app, operation_key="k-1")
    session_id = failed["session"]["id"]
    assert wait_terminal(app, failed["job"]["job_id"])["message_code"] == "model_generation_timeout"
    turn = app.dispatch("jobs.chat_status", {"operation_key": "k-1"})["turn"]
    assert turn["can_retry"] and turn["latest_state"] == "failed"

    ollama.generate_delay = 0.0
    ollama.later_generate_delay = 0.0
    retried = app.dispatch("jobs.retry_chat", {"message_id": turn["message_id"], "operation_key": "k-2"})
    assert retried["turn"]["message_id"] == turn["message_id"]
    again = app.dispatch("jobs.retry_chat", {"message_id": turn["message_id"], "operation_key": "k-2"})
    assert again["job"]["job_id"] == retried["job"]["job_id"]  # idempotent, nothing new started
    assert wait_terminal(app, retried["job"]["job_id"])["state"] == "completed"

    follow = submit(app, operation_key="k-3", session_id=session_id, message="And after recovery?")
    assert wait_terminal(app, follow["job"]["job_id"])["state"] == "completed"
    assert roles(app, session_id) == ["user", "assistant", "user", "assistant"]
    histories = generation_history(ollama)
    assert histories[-1] == [("user", QUESTION), ("assistant", ANSWER), ("user", "And after recovery?")]
    assert histories[-2] == [("user", QUESTION)]  # the retry: the question once, no failed copy
    final = app.dispatch("jobs.chat_status", {"message_id": turn["message_id"]})["turn"]
    assert [a["state"] for a in final["attempts"]] == ["failed", "completed"]
    with pytest.raises(ValueError, match="already has an answer"):
        app.dispatch("jobs.retry_chat", {"message_id": turn["message_id"], "operation_key": "k-4"})


def test_failed_questions_never_enter_later_model_history(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    scale_timeouts(monkeypatch, 0.4)
    ollama.prepare_delay = 3.0
    failed = submit(app, operation_key="k-f", message="The failed question")
    session_id = failed["session"]["id"]
    wait_terminal(app, failed["job"]["job_id"])
    ollama.prepare_delay = 0.0
    later = submit(app, operation_key="k-l", session_id=session_id)
    assert wait_terminal(app, later["job"]["job_id"])["state"] == "completed"
    assert generation_history(ollama)[-1] == [("user", QUESTION)]
    old = app.dispatch("jobs.chat_status", {"operation_key": "k-f"})["turn"]
    assert not old["can_retry"] and old["can_copy"]  # an older failed turn is copied, not retried
    with pytest.raises(ValueError, match="latest question"):
        app.dispatch("jobs.retry_chat", {"message_id": old["message_id"], "operation_key": "k-x"})
    messages = app.dispatch("sessions.messages", {"session_id": session_id})
    assert [m["content"] for m in messages if m["role"] == "user"] == ["The failed question", QUESTION]


def test_attachments_are_linked_once_and_a_missing_source_is_an_explicit_retry_error(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = app.test_document_id  # type: ignore[attr-defined]
    scale_timeouts(monkeypatch, 0.4)
    ollama.prepare_delay = 3.0
    submitted = app.dispatch(
        "jobs.submit_chat",
        {"message": QUESTION, "model": MODEL, "attachment_document_ids": [doc], "operation_key": "k-a"},
    )
    wait_terminal(app, submitted["job"]["job_id"])
    message_id = submitted["turn"]["message_id"]
    links = app.db.conn.execute("SELECT document_id FROM message_documents WHERE message_id = ?", (message_id,)).fetchall()
    assert [row[0] for row in links] == [doc]
    context = app.dispatch("sources.conversation_context", {"session_id": submitted["session"]["id"]})
    assert [s["id"] for s in context] == [doc]

    # Retry still uses the original attachment after it left the conversation.
    app.dispatch("sources.remove_from_conversation", {"session_id": submitted["session"]["id"], "backend_kind": "document", "source_id": doc})
    ollama.prepare_delay = 0.0
    retried = app.dispatch("jobs.retry_chat", {"message_id": message_id, "operation_key": "k-b"})
    assert wait_terminal(app, retried["job"]["job_id"])["state"] == "completed"
    assert "Retry backend" in json.dumps(ollama.bodies[-1])  # the original source's evidence
    assert len(app.db.conn.execute("SELECT 1 FROM message_documents WHERE message_id = ?", (message_id,)).fetchall()) == 1

    # A deleted original source is refused, never silently replaced.
    ollama.prepare_delay = 3.0
    failing = app.dispatch(
        "jobs.submit_chat",
        {"message": "Second question", "model": MODEL, "attachment_document_ids": [doc], "operation_key": "k-c"},
    )
    wait_terminal(app, failing["job"]["job_id"])
    app.documents.mark_deleted(doc)
    with pytest.raises(ValueError, match="no longer available"):
        app.dispatch("jobs.retry_chat", {"message_id": failing["turn"]["message_id"], "operation_key": "k-d"})
    assert app.dispatch("jobs.chat_status", {"operation_key": "k-c"})["turn"]["attempt_count"] == 1


def test_legacy_conversations_keep_their_messages_and_get_no_invented_outcomes(app: SidecarApp, ollama: FakeOllama) -> None:
    session = app.sessions.create(model=MODEL)
    app.sessions.add_message(session["id"], "user", "Legacy unanswered")
    app.sessions.add_message(session["id"], "user", "Legacy question")
    app.sessions.add_message(session["id"], "assistant", "Legacy answer")
    before = app.dispatch("sessions.messages", {"session_id": session["id"]})
    assert all("turn" not in message for message in before)
    job = submit(app, session_id=session["id"], operation_key="k-legacy")["job"]
    assert wait_terminal(app, job["job_id"])["state"] == "completed"
    assert generation_history(ollama)[-1] == [
        ("user", "Legacy unanswered"),
        ("user", "Legacy question"),
        ("assistant", "Legacy answer"),
        ("user", QUESTION),
    ]
    after = app.dispatch("sessions.messages", {"session_id": session["id"]})
    assert [m["content"] for m in after[:3]] == [m["content"] for m in before]
    assert all("turn" not in m for m in after[:3])
    assert app.db.conn.execute("SELECT COUNT(*) FROM chat_attempts").fetchone()[0] == 1


def test_status_checks_and_restarts_never_replay_inference(
    tmp_path: Path, app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch
) -> None:
    scale_timeouts(monkeypatch, 0.4)
    ollama.generate_delay = 3.0
    failed = submit(app, operation_key="k-nr")
    wait_terminal(app, failed["job"]["job_id"])
    requests = list(ollama.requests)
    for _ in range(3):
        assert app.dispatch("jobs.chat_status", {"operation_key": "k-nr"})["turn"]["latest_state"] == "failed"
    from odysseus_desktop_backend.profile_ownership import ProfileInUseError

    with pytest.raises(ProfileInUseError):  # a second live backend may not open the profile
        SidecarApp(app.profile_dir)
    app.close()  # a restart: the first backend is gone before the next one starts
    restarted = SidecarApp(app.profile_dir)
    try:
        assert restarted.dispatch("jobs.chat_status", {"operation_key": "k-nr"})["turn"]["latest_state"] == "failed"
        time.sleep(0.5)
        assert restarted.dispatch("jobs.chat_status", {"operation_key": "never-sent"}) == {"found": False, "turn": None, "job": None}
    finally:
        restarted.close()
    assert ollama.requests == requests


def test_retry_refusals(app: SidecarApp, ollama: FakeOllama) -> None:
    with pytest.raises(RpcError):
        app.dispatch("jobs.retry_chat", {"message_id": "not-a-turn", "operation_key": "k"})
    ollama.prepare_delay = 1.0
    running = submit(app, operation_key="k-run")
    with pytest.raises(ValueError, match="in progress"):
        app.dispatch("jobs.retry_chat", {"message_id": running["turn"]["message_id"], "operation_key": "k-r2"})
    wait_terminal(app, running["job"]["job_id"])
    with pytest.raises(ValueError, match="operation_key"):
        app.dispatch("jobs.submit_chat", {"message": "x", "model": MODEL, "operation_key": "k" * 129})


def test_backend_loss_settles_as_interrupted_and_only_an_explicit_retry_runs_again(
    tmp_path: Path, ollama: FakeOllama
) -> None:
    """Kill the real sidecar mid-load; the next process reconciles, never replays."""
    import os
    import subprocess
    import sys

    from offline_network import ALLOWED_PORTS_ENV

    ollama.prepare_delay = 30.0
    profile = tmp_path / "profile"
    bootstrap = (
        "import sys; sys.path.insert(0, {python!r});"
        "from odysseus_desktop_backend.services import model_service, embedding_service;"
        "model_service.OLLAMA_ENDPOINT = {url!r};"
        "embedding_service.OllamaEmbeddingProvider.installed = lambda self: False;"
        "import rpc_server; raise SystemExit(rpc_server.main())"
    ).format(python=str(Path(rpc_server.__file__).resolve().parent), url=ollama.url)
    env = {**os.environ, "ODYSSEUS_PROFILE_DIR": str(profile), ALLOWED_PORTS_ENV: ollama.url.rsplit(":", 1)[1]}

    def spawn() -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, "-c", bootstrap],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
        )

    def call(proc: subprocess.Popen[str], method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert proc.stdin and proc.stdout
        proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}) + "\n")
        proc.stdin.flush()
        return json.loads(proc.stdout.readline())

    first = spawn()
    try:
        submitted = call(first, "jobs.submit_chat", {"message": "What if the backend fails?", "model": MODEL, "operation_key": "k-loss"})
        message_id = submitted["result"]["turn"]["message_id"]
        session_id = submitted["result"]["session"]["id"]
        assert ollama.arrived["prepare"].wait(10)
    finally:
        first.kill()
        first.wait(10)

    second = spawn()
    try:
        status = call(second, "jobs.chat_status", {"operation_key": "k-loss"})["result"]
        turn = status["turn"]
        assert (turn["latest_state"], turn["latest_outcome_code"]) == ("interrupted", "backend_restarted")
        assert turn["can_retry"] and status["job"] is None
        assert ollama.requests == ["prepare"], "restart and status checks replay nothing"

        ollama.prepare_delay = 0.0
        retried = call(second, "jobs.retry_chat", {"message_id": message_id, "operation_key": "k-loss-2"})["result"]
        assert retried["turn"]["message_id"] == message_id
        deadline = time.monotonic() + 10
        while call(second, "jobs.get", {"job_id": retried["job"]["job_id"]})["result"]["state"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.05)
        messages = call(second, "sessions.messages", {"session_id": session_id})["result"]
        assert [m["role"] for m in messages] == ["user", "assistant"]
        assert [a["state"] for a in messages[0]["turn"]["attempts"]] == ["interrupted", "completed"]
    finally:
        second.stdin.close()  # type: ignore[union-attr]
        second.wait(10)
    assert ollama.requests == ["prepare", "prepare", "generate"]


def test_store_guards_hold_on_their_own(app: SidecarApp) -> None:
    """Each store guard, independent of the job worker's own checks."""
    queued = new_turn(app)
    app.turns.request_cancel(queued["current_attempt_id"])
    with pytest.raises(JobCancelledError):
        app.turns.start(queued["current_attempt_id"])  # a cancelled attempt never starts

    running = new_turn(app)
    attempt = running["current_attempt_id"]
    app.turns.start(attempt)
    # Not the turn's authorized attempt (e.g. a stale worker), even while "running".
    app.db.conn.execute("UPDATE chat_turns SET current_attempt_id = 'other' WHERE message_id = ?", (running["message_id"],))
    app.db.conn.commit()
    with pytest.raises(AttemptRetired):
        app.turns.commit_answer(attempt, content="x", metadata={}, title="t")
    assert [m["role"] for m in app.sessions.messages(running["session_id"])] == ["user"]


@pytest.mark.parametrize("cancel_first", [True, False])
def test_job_cancel_at_the_commit_boundary_settles_once(
    app: SidecarApp, ollama: FakeOllama, monkeypatch: pytest.MonkeyPatch, cancel_first: bool
) -> None:
    """Through RPC and the worker: cancel lands just before, or just after, the commit."""
    at_commit, release, committed = threading.Event(), threading.Event(), threading.Event()
    real = ChatTurnStore.commit_answer

    def gated(self: ChatTurnStore, *args: Any, **kwargs: Any) -> dict[str, Any]:
        if cancel_first:
            at_commit.set()
            assert release.wait(10)
            return real(self, *args, **kwargs)
        result = real(self, *args, **kwargs)
        committed.set()
        assert release.wait(10)
        return result

    monkeypatch.setattr(ChatTurnStore, "commit_answer", gated)
    submitted = submit(app, operation_key="k-edge")
    assert (at_commit if cancel_first else committed).wait(10)
    app.dispatch("jobs.cancel", {"job_id": submitted["job"]["job_id"]})
    release.set()
    final = wait_terminal(app, submitted["job"]["job_id"])
    turn = app.dispatch("jobs.chat_status", {"operation_key": "k-edge"})["turn"]
    if cancel_first:
        assert (final["state"], final["message_code"]) == ("cancelled", "cancelled_by_user")
        assert (turn["latest_state"], turn["answered"]) == ("cancelled", False)
        assert roles(app, final["session_id"]) == ["user"]
    else:
        assert final["state"] == "completed"  # cancel never un-commits an answer
        assert (turn["latest_state"], turn["answered"]) == ("completed", True)
        assert roles(app, final["session_id"]) == ["user", "assistant"]


def test_turn_embeddings_use_the_bounded_transport_and_never_swallow_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    from odysseus_desktop_backend.cancellation import cancellation_scope
    from odysseus_desktop_backend.services.embedding_service import EmbeddingService, OllamaEmbeddingProvider

    seen: list[float] = []
    monkeypatch.setattr(
        ModelService, "_request_json", lambda self, open_url, url, payload, timeout: seen.append(timeout) or {"embeddings": [[1.0, 0.0]]}
    )
    provider = OllamaEmbeddingProvider(endpoint="http://127.0.0.1:9")
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 293
    with deadline_scope(deadline):
        assert len(provider.embed(["q"])) == 1
        clock.now += 7
        with pytest.raises(RuntimeError):  # expired: refused before any connection
            provider.embed(["q"])
    assert seen == [7.0]  # min(60 s embedding limit, 7 s left in the turn)

    # Outside a turn (imports, indexing) the original transport is untouched.
    monkeypatch.setattr(OllamaEmbeddingProvider, "_post_json", lambda self, url, payload, timeout: seen.append(-timeout) or {"embeddings": [[1.0]]})
    provider.embed(["q"])
    assert seen[-1] == -60

    # A cancel during the query embedding propagates instead of falling back to lexical.
    class Raising:
        backend = "semantic"
        model_key = "ollama:x"

        def embed(self, texts: list[str]) -> list[Any]:
            raise JobCancelledError("cancelled")

    service = EmbeddingService.__new__(EmbeddingService)
    service.forced_provider = Raising()
    service._last_status = None
    service._set_status = lambda status: None  # type: ignore[method-assign]
    service._provider_status = lambda provider, dims: {}  # type: ignore[method-assign]
    event = threading.Event()
    event.set()
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        service.embed_query("q")


def test_in_turn_availability_probe_obeys_the_turn_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The embedding availability probe never starts after, nor runs past, the 300 s deadline."""
    import socket as socket_module

    from odysseus_desktop_backend.cancellation import cancellation_scope
    from odysseus_desktop_backend.services.embedding_service import OllamaEmbeddingProvider

    probes: list[tuple[str, float]] = []
    monkeypatch.setattr(OllamaEmbeddingProvider, "_tcp_reachable", lambda self, host, port, timeout=0.5: probes.append(("tcp", timeout)) or True)
    tags = {"models": [{"name": "nomic-embed-text:latest"}]}

    # Out of a turn: unchanged (0.5 s connect probe, plain 3 s GET).
    monkeypatch.setattr(OllamaEmbeddingProvider, "_get_json", lambda self, url, timeout: probes.append(("plain", timeout)) or tags)
    provider = OllamaEmbeddingProvider("nomic-embed-text", endpoint="http://127.0.0.1:9")
    assert provider.installed() and probes == [("tcp", 0.5), ("plain", 3)]

    # In a turn with 1.2 s left: both steps clipped; the GET uses the bounded transport.
    probes.clear()
    monkeypatch.setattr(ModelService, "_request_json", lambda self, open_url, url, payload, timeout: probes.append(("bounded", timeout)) or tags)
    clock = Clock()
    deadline = TurnDeadline(300, clock)
    clock.now += 298.8
    with deadline_scope(deadline):
        assert provider.installed()
    assert probes == [("tcp", 0.5), ("bounded", pytest.approx(1.2))]

    # Expired: no probe starts at all, and the turn ends instead of falling back.
    probes.clear()
    clock.now += 5
    with deadline_scope(deadline), pytest.raises(TurnDeadlineExceeded):
        provider.installed()
    assert probes == []

    # Cancelled: propagates, never read as "not installed".
    monkeypatch.undo()
    monkeypatch.setattr(OllamaEmbeddingProvider, "_tcp_reachable", lambda self, host, port, timeout=0.5: True)
    event = threading.Event()
    event.set()
    with deadline_scope(TurnDeadline(300)), cancellation_scope(event), pytest.raises(JobCancelledError):
        provider.installed()

    # A real trickling peer: the probe is cut off at the deadline and the peer sees the hang-up.
    server = socket_module.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    hung_up = threading.Event()

    def trickle() -> None:
        conn, _ = server.accept()
        conn.recv(4096)
        try:
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while True:  # one header byte at a time: an inactivity limit would never fire
                conn.sendall(b"X")
                time.sleep(0.05)
        except OSError:
            hung_up.set()
        finally:
            conn.close()

    threading.Thread(target=trickle, daemon=True).start()
    slow = OllamaEmbeddingProvider("nomic-embed-text", endpoint=f"http://127.0.0.1:{server.getsockname()[1]}")
    started = time.monotonic()
    with deadline_scope(TurnDeadline(0.4)), pytest.raises(TurnDeadlineExceeded):
        slow.installed()
    assert time.monotonic() - started < 1.5
    assert hung_up.wait(3)
    server.close()
