"""End-to-end proof against REAL upstream Colibri code (Phase 3).

Runs the actual upstream `coli serve` (openai_server.py HTTP surface, queue,
auth, headers, error objects) with a stub engine subprocess that speaks the
engine stdio protocol (fixtures/colibri/stub_engine.py) and a tiny GLM-shaped
model directory built in pure Python (fixtures/colibri/make_fixture_model.py).
No MinGW build, no model weights, no network beyond loopback.

Gated on ODYSSEUS_COLIBRI_UPSTREAM pointing at an upstream checkout (the repo
is never vendored). Locally:

    git clone https://github.com/JustVugg/colibri <dir>
    git -C <dir> checkout 54cfe5632446ad333ca81c44c6a6c71ffec8a01d
    ODYSSEUS_COLIBRI_UPSTREAM=<dir> python -m pytest python/tests/test_deep_local_e2e_upstream.py

Proves, against real upstream code: server detection, model listing, real
plan --json and doctor --json, one real text completion normalized end to
end, persisted-job completion with timing capture, and in-flight cancellation
where upstream's own disconnect->CANCEL->engine path runs.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.services import colibri_cli
from odysseus_desktop_backend.services.deep_local_jobs import (
    TERMINAL_STATES,
    DeepLocalJobService,
)
from odysseus_desktop_backend.services.deep_local_service import DeepLocalService
from odysseus_desktop_backend.services.providers.colibri import ColibriProvider
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database

UPSTREAM = os.environ.get("ODYSSEUS_COLIBRI_UPSTREAM", "")
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "colibri"

pytestmark = pytest.mark.skipif(
    not UPSTREAM or not (Path(UPSTREAM) / "c" / "coli").is_file(),
    reason="ODYSSEUS_COLIBRI_UPSTREAM not set to an upstream Colibri checkout",
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def fixture_model(tmp_path_factory: pytest.TempPathFactory) -> Path:
    sys.path.insert(0, str(FIXTURES))
    try:
        from make_fixture_model import build_fixture_model
    finally:
        sys.path.remove(str(FIXTURES))
    return build_fixture_model(tmp_path_factory.mktemp("glm-fixture") / "model")


@pytest.fixture(scope="module")
def coli_path() -> Path:
    return Path(UPSTREAM) / "c" / "coli"


@pytest.fixture(scope="module")
def real_serve(fixture_model: Path, coli_path: Path, tmp_path_factory: pytest.TempPathFactory):
    """Real upstream `coli serve` with the stub engine, on a free loopback port."""
    stub_bat = tmp_path_factory.mktemp("stub") / "stub_engine.bat"
    stub_bat.write_text(
        f'@echo off\r\n"{sys.executable}" "{FIXTURES / "stub_engine.py"}" %*\r\n',
        encoding="ascii",
    )
    port = _free_port()
    env = dict(os.environ)
    env["COLI_ENGINE"] = str(stub_bat)
    env.pop("COLI_API_KEY", None)  # auth off: covered by fake-server tests
    process = subprocess.Popen(
        [
            sys.executable,
            str(coli_path),
            "serve",
            "--model",
            str(fixture_model),
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        cwd=str(coli_path.parent),
    )
    endpoint = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    last_error = ""
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                stderr = (process.stderr.read() or b"").decode("utf-8", "replace")
                raise AssertionError(f"coli serve exited early: {stderr[-2000:]}")
            try:
                with urllib.request.urlopen(f"{endpoint}/health", timeout=1) as response:
                    if json.loads(response.read()).get("status") == "ok":
                        break
            except OSError as exc:
                last_error = str(exc)
            time.sleep(0.2)
        else:
            raise AssertionError(f"coli serve never became healthy: {last_error}")
        yield endpoint
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture()
def db(tmp_path: Path):
    database = Database(tmp_path)
    yield database
    database.close()


def _enable(db: Database, endpoint: str, cli_path: Path, model_path: Path) -> None:
    db.set_setting("deep_local_enabled", True)
    db.set_setting("deep_local_endpoint", endpoint)
    db.set_setting("deep_local_timeout_seconds", 60)
    db.set_setting("deep_local_cli_path", str(cli_path))
    db.set_setting("deep_local_model_path", str(model_path))


def _wait_terminal(service: DeepLocalJobService, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get(job_id)
        if snapshot["state"] in TERMINAL_STATES:
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"job did not reach a terminal state: {service.get(job_id)}")


# -- detection and listing against the real server ---------------------------


def test_real_server_detection_and_model_listing(real_serve: str) -> None:
    provider = ColibriProvider(real_serve, timeout=30)
    status = provider.health()
    assert status.reachable is True
    assert status.healthy is True
    assert status.detail.get("queue", {}).get("max_queue") == 8
    models = provider.list_models()
    assert [item.model_id for item in models] == ["glm-5.2-colibri"]


def test_deep_local_status_rpc_shape_against_real_server(
    real_serve: str, db: Database, coli_path: Path, fixture_model: Path
) -> None:
    _enable(db, real_serve, coli_path, fixture_model)
    status = DeepLocalService(SettingsService(db)).status()
    assert status["ok"] is True
    assert status["reachable"] is True and status["healthy"] is True
    assert status["models"] == ["glm-5.2-colibri"]


# -- real plan/doctor ---------------------------------------------------------


def test_real_coli_plan_json(coli_path: Path, fixture_model: Path) -> None:
    outcome = colibri_cli.run_plan(str(coli_path), str(fixture_model))
    assert outcome["ok"] is True, outcome
    assert outcome["plan"]["version"] == 2
    summary = outcome["summary"]
    assert summary["storage_required"] != "unknown"
    assert isinstance(summary["warnings"], list)


def test_real_coli_doctor_json(coli_path: Path, fixture_model: Path) -> None:
    outcome = colibri_cli.run_doctor(str(coli_path), str(fixture_model))
    assert outcome["ok"] is True, outcome
    checks = {item["id"]: item["status"] for item in outcome["checks"]}
    assert checks.get("model.path") == "pass"
    assert checks.get("model.config") == "pass"
    assert checks.get("model.tokenizer") == "pass"
    # No MinGW-built engine on this machine: doctor must fail that check and
    # the wrapper must map the report to a non-runnable overall state.
    assert checks.get("engine.binary") == "fail"
    assert outcome["overall"] in {"incompatible", "unsafe"}


# -- one real completion end to end -------------------------------------------


def test_real_completion_through_upstream_server(real_serve: str) -> None:
    provider = ColibriProvider(real_serve, timeout=60)
    result = provider.chat_once(
        "glm-5.2-colibri",
        [{"role": "user", "content": "What is the notice period?"}],
        temperature=0.0,
        max_output_tokens=64,
    )
    assert "30 days." in result.content
    assert result.provider == "colibri"
    assert result.model_id == "glm-5.2-colibri"
    assert result.completion_tokens > 0
    assert result.elapsed_ms >= 0
    assert result.finish_reason == "stop"


def test_persisted_job_completes_with_timing_capture(
    real_serve: str, db: Database, tmp_path: Path, coli_path: Path, fixture_model: Path
) -> None:
    _enable(db, real_serve, coli_path, fixture_model)
    service = DeepLocalJobService(tmp_path, db)
    try:
        outcome = service.submit(
            question="What is the notice period?",
            evidence=[{"source_id": "doc-1", "snippet": "The notice period is 30 days."}],
            max_output_tokens=64,
        )
        assert outcome["ok"] is True
        snapshot = _wait_terminal(service, outcome["job"]["job_id"])
        assert snapshot["state"] == "completed"
        assert "30 days." in snapshot["result_text"]
        usage = snapshot["usage"]
        assert usage["completion_tokens"] > 0
        assert usage["prompt_tokens"] > 0
        assert usage["elapsed_ms"] >= 0
        states = [item["state"] for item in snapshot["state_history"]]
        assert states[0] == "queued" and states[-1] == "completed"
    finally:
        service.shutdown()


def test_in_flight_cancel_reaches_upstream_engine_cancel_path(
    real_serve: str, db: Database, tmp_path: Path, coli_path: Path, fixture_model: Path
) -> None:
    """Cancel mid-generation: our side terminates as interrupted (honest),
    and upstream's disconnect-poll -> CANCEL -> engine path frees the slot,
    which the next job proves by completing."""
    _enable(db, real_serve, coli_path, fixture_model)
    service = DeepLocalJobService(tmp_path, db)
    try:
        slow = service.submit(question="STUB_SLOW please elaborate at great length")
        job_id = slow["job"]["job_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if service.get(job_id)["state"] == "running":
                break
            time.sleep(0.05)
        time.sleep(1.0)  # ensure the request is mid-generation on the server
        service.cancel(job_id)
        snapshot = _wait_terminal(service, job_id)
        assert snapshot["state"] == "interrupted"
        assert snapshot["message_code"] == "stopped_waiting"

        follow_up = service.submit(question="What is the notice period?", max_output_tokens=64)
        final = _wait_terminal(service, follow_up["job"]["job_id"], timeout=120)
        assert final["state"] == "completed"
        assert "30 days." in final["result_text"]
    finally:
        service.shutdown()
