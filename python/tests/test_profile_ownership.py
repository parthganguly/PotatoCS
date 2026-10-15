"""Exclusive profile ownership: at most one live backend owns a profile.

Real sidecar processes (rpc_server.main) share an isolated temporary profile;
the only model peer is the loopback fake Ollama. No real inference.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from odysseus_desktop_backend.profile_ownership import (
    LOCK_FILE_NAME,
    PROFILE_IN_USE_EXIT_CODE,
    PROFILE_IN_USE_MESSAGE,
    ProfileInUseError,
    ProfileOwnership,
)
from offline_network import ALLOWED_PORTS_ENV
from rpc_server import SidecarApp
from test_chat_cold_model_lifecycle import MODEL, FakeOllama, ollama  # noqa: F401 - fixture

PYTHON_DIR = Path(__file__).resolve().parents[1]


class Sidecar:
    """One real backend process on `profile`, reaching only the fake Ollama."""

    def __init__(self, profile: Path, fake: FakeOllama, capture_stderr: bool = False):
        bootstrap = (
            "import sys; sys.path.insert(0, {python!r});"
            "from odysseus_desktop_backend.services import model_service, embedding_service;"
            "model_service.OLLAMA_ENDPOINT = {url!r};"
            "embedding_service.OllamaEmbeddingProvider.installed = lambda self: False;"
            "import rpc_server; raise SystemExit(rpc_server.main())"
        ).format(python=str(PYTHON_DIR), url=fake.url)
        env = {**os.environ, "ODYSSEUS_PROFILE_DIR": str(profile), ALLOWED_PORTS_ENV: fake.url.rsplit(":", 1)[1]}
        self.proc = subprocess.Popen(
            [sys.executable, "-c", bootstrap],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL, text=True, env=env,
        )
        self._id = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        assert self.proc.stdin and self.proc.stdout
        self._id += 1
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "sidecar exited"
        return json.loads(line)

    def result(self, method: str, params: dict[str, Any] | None = None) -> Any:
        response = self.call(method, params)
        assert "result" in response, response
        return response["result"]

    def close(self) -> int:
        """Normal shutdown: stdin EOF, the sidecar closes itself."""
        assert self.proc.stdin
        self.proc.stdin.close()
        return self.proc.wait(15)

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(15)


def wait_state(sidecar: Sidecar, job_id: str, states: set[str], timeout: float = 15) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = sidecar.result("jobs.get", {"job_id": job_id})
        if job["state"] in states:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job never reached {states}")


def profile_fingerprint(profile: Path) -> dict[str, tuple[int, int]]:
    """Size and mtime of every file except the lock file itself."""
    return {
        str(path.relative_to(profile)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in profile.rglob("*")
        if path.is_file() and path.name != LOCK_FILE_NAME
    }


def refused(profile: Path, fake: FakeOllama) -> tuple[int, str]:
    second = Sidecar(profile, fake, capture_stderr=True)
    code = second.proc.wait(15)
    assert second.proc.stderr
    return code, second.proc.stderr.read()


def test_second_backend_is_refused_before_touching_the_profile_and_cannot_retire_a_live_attempt(
    tmp_path: Path, ollama: FakeOllama
) -> None:
    profile = tmp_path / "profile"
    ollama.prepare_delay = 2.0
    owner = Sidecar(profile, ollama)
    try:
        assert owner.result("health.ping")["ok"] is True
        submitted = owner.result("jobs.submit_chat", {"message": "Still answering?", "model": MODEL, "operation_key": "k-live"})
        assert ollama.arrived["prepare"].wait(10)  # attempt A is running, its request in flight
        before = profile_fingerprint(profile)

        code, stderr = refused(profile, ollama)
        assert code == PROFILE_IN_USE_EXIT_CODE
        assert PROFILE_IN_USE_MESSAGE in stderr
        assert "Traceback" not in stderr
        assert profile_fingerprint(profile) == before  # no migration, log, reconciliation or job

        status = owner.result("jobs.chat_status", {"operation_key": "k-live"})
        assert status["turn"]["latest_state"] in {"pending", "running"}  # not retired by B
        final = wait_state(owner, submitted["job"]["job_id"], {"completed", "failed", "cancelled"})
        assert final["state"] == "completed"
        turn = owner.result("jobs.chat_status", {"operation_key": "k-live"})["turn"]
        assert (turn["latest_state"], turn["answered"], turn["attempt_count"]) == ("completed", True, 1)
    finally:
        owner.close()
    assert ollama.requests == ["prepare", "generate"]  # B started no inference


def test_a_different_profile_opens_independently(tmp_path: Path, ollama: FakeOllama) -> None:
    first = Sidecar(tmp_path / "one", ollama)
    second = Sidecar(tmp_path / "two", ollama)
    try:
        assert first.result("health.ping")["ok"] is True
        assert second.result("health.ping")["ok"] is True
    finally:
        assert first.close() == 0
        assert second.close() == 0


def test_normal_shutdown_releases_ownership(tmp_path: Path, ollama: FakeOllama) -> None:
    profile = tmp_path / "profile"
    first = Sidecar(profile, ollama)
    assert first.result("health.ping")["ok"] is True
    assert first.close() == 0
    second = Sidecar(profile, ollama)
    try:
        assert second.result("health.ping")["ok"] is True
    finally:
        assert second.close() == 0


def test_forced_termination_releases_ownership_and_the_replacement_reconciles(
    tmp_path: Path, ollama: FakeOllama
) -> None:
    profile = tmp_path / "profile"
    ollama.prepare_delay = 30.0
    first = Sidecar(profile, ollama)
    submitted = first.result("jobs.submit_chat", {"message": "Interrupted question", "model": MODEL, "operation_key": "k-kill"})
    message_id = submitted["turn"]["message_id"]
    assert ollama.arrived["prepare"].wait(10)
    first.kill()  # confirmed terminated; the lock file is left behind on purpose
    assert (profile / LOCK_FILE_NAME).exists()

    replacement = Sidecar(profile, ollama)
    try:
        turn = replacement.result("jobs.chat_status", {"operation_key": "k-kill"})["turn"]
        assert (turn["latest_state"], turn["latest_outcome_code"], turn["can_retry"]) == ("interrupted", "backend_restarted", True)
        assert turn["message_id"] == message_id
    finally:
        replacement.close()
    assert ollama.requests == ["prepare"]  # reconciliation replays nothing


def test_in_process_backend_holds_ownership_until_closed(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    app = SidecarApp(profile)
    try:
        with pytest.raises(ProfileInUseError):
            SidecarApp(profile)
        with pytest.raises(ProfileInUseError):
            ProfileOwnership(profile)
    finally:
        app.close()
        app.close()  # idempotent
    reopened = SidecarApp(profile)
    reopened.close()


def test_host_and_sidecar_agree_on_the_profile_in_use_contract() -> None:
    lib_rs = (PYTHON_DIR.parent / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    code = re.search(r"const PROFILE_IN_USE_EXIT_CODE: i32 = (\d+);", lib_rs)
    message = re.search(r'const PROFILE_IN_USE_MESSAGE: &str =\s*"([^"]+)";', lib_rs)
    assert code and int(code.group(1)) == PROFILE_IN_USE_EXIT_CODE
    assert message and message.group(1) == PROFILE_IN_USE_MESSAGE
