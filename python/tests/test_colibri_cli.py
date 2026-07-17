"""Tests for the safe `coli plan/doctor --json` subprocess wrapper (Phase 3).

Driven entirely by fixtures and the fake_coli.py script — no Colibri
installation, engine build, or model download is required.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from odysseus_desktop_backend.services import colibri_cli
from odysseus_desktop_backend.services.deep_local_service import DeepLocalService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "colibri"
FAKE_COLI = str(FIXTURES / "fake_coli.py")


def set_mode(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    monkeypatch.setenv("FAKE_COLI_MODE", mode)


# -- invocation safety -----------------------------------------------------


def test_argv_array_never_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "echo_argv")
    outcome = colibri_cli._run(FAKE_COLI, "doctor", model_path=str(FIXTURES))
    assert outcome["ok"] is True
    argv = json.loads(outcome["stdout"])["argv"]
    assert argv[0] == "doctor"
    assert argv[1] == "--json"
    assert argv[2] == "--model"
    # model path is canonicalized before being passed
    assert Path(argv[3]) == FIXTURES.resolve()


def test_secrets_stripped_from_child_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "env_probe")
    monkeypatch.setenv("COLI_API_KEY", "SENTINEL-CLI-KEY")
    monkeypatch.setenv("ODYSSEUS_COLIBRI_API_KEY", "SENTINEL-CLI-KEY")
    outcome = colibri_cli._run(FAKE_COLI, "doctor")
    probe = json.loads(outcome["stdout"])
    assert probe["coli_api_key_present"] is False
    assert probe["odysseus_key_present"] is False


def test_missing_cli_path_is_unavailable() -> None:
    outcome = colibri_cli.run_doctor("", "")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "unavailable"
    outcome = colibri_cli.run_doctor("C:/does/not/exist/coli", "")
    assert outcome["error_category"] == "unavailable"


def test_subprocess_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "sleep")
    outcome = colibri_cli.run_doctor(FAKE_COLI, "", timeout=1.0)
    assert outcome["ok"] is False
    assert outcome["error_category"] == "timeout"


def test_non_json_output_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "garbage")
    outcome = colibri_cli.run_doctor(FAKE_COLI, "")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "malformed_response"


# -- plan ------------------------------------------------------------------


def test_plan_parses_and_translates(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "plan_ok")
    outcome = colibri_cli.run_plan(FAKE_COLI, "")
    assert outcome["ok"] is True
    assert outcome["plan"]["version"] == 2
    summary = outcome["summary"]
    assert summary["storage_required"] == "370.0 GB"
    assert summary["storage_available"] == "512.0 GB"
    assert summary["estimated_ram_peak"] == "21.1 GB"
    assert summary["expert_cache_slots_per_layer"] == 3
    assert summary["vram_devices"] == 0
    assert summary["expected_bottleneck"] == "disk expert misses"
    assert summary["may_be_extremely_slow"] is True


def test_plan_failure_exit_code_is_not_json(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "plan_error")
    outcome = colibri_cli.run_plan(FAKE_COLI, "")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "plan_failed"
    # Fixed plain-language copy only: upstream's failure text (which embeds
    # the model directory) must never surface as RPC-visible detail.
    assert "detail" not in outcome
    assert "model folder" in outcome["error"]


def test_plan_unknown_version_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "plan_unknown_version")
    outcome = colibri_cli.run_plan(FAKE_COLI, "")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "incompatible_server"
    assert "99" in outcome["error"]
    assert "format 2" in outcome["error"]


# -- doctor ----------------------------------------------------------------


def test_doctor_warning_with_cold_experts_is_runnable_slow(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "doctor_ok")
    outcome = colibri_cli.run_doctor(FAKE_COLI, "")
    assert outcome["ok"] is True
    assert outcome["status"] == "warning"
    assert outcome["overall"] == "runnable_slow"
    assert outcome["exit_code"] == 0
    labels = {check["id"]: check["label"] for check in outcome["checks"]}
    assert labels["memory.ram"] == "Memory (RAM)"
    assert labels["engine.binary"] == "Colibri engine"
    assert outcome["plan_summary"]["may_be_extremely_slow"] is True


def test_doctor_ram_fail_is_unsafe(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "doctor_ram_fail")
    outcome = colibri_cli.run_doctor(FAKE_COLI, "")
    assert outcome["ok"] is True
    assert outcome["status"] == "error"
    assert outcome["overall"] == "unsafe"
    assert outcome["exit_code"] == 1
    assert "plan_summary" not in outcome


def test_doctor_unknown_schema_version_fails_safely(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "doctor_unknown_version")
    outcome = colibri_cli.run_doctor(FAKE_COLI, "")
    assert outcome["ok"] is False
    assert outcome["error_category"] == "incompatible_server"
    assert "format 1" in outcome["error"]


def test_doctor_invalid_arguments_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "doctor_bad_args")
    outcome = colibri_cli.run_doctor(FAKE_COLI, "")
    assert outcome["ok"] is True
    assert outcome["overall"] == "invalid_arguments"
    assert outcome["exit_code"] == 2
    assert outcome["checks"][0]["id"] == "config.arguments"


# -- hostile-output privacy sentinels ----------------------------------------

# Values the wrapper must never surface, whether the CLI echoes them itself
# or they ride along in launch errors / environment dumps.
SENTINEL_MODEL_PATH = "C:/SENTINEL-MODELS/glm52-secret-project"
SENTINEL_USERNAME = "SENTINEL-USER-carlos"
SENTINEL_API_KEY = "SENTINEL-HOSTILE-KEY-77aa"


def _assert_clean(payload: str, log_text: str, *, cli_path: str) -> None:
    for sentinel in (SENTINEL_MODEL_PATH, SENTINEL_USERNAME, SENTINEL_API_KEY, cli_path):
        assert sentinel not in payload, f"sentinel leaked into RPC-visible result: {sentinel}"
        assert sentinel not in log_text, f"sentinel leaked into logs: {sentinel}"


@pytest.fixture()
def hostile_env(monkeypatch: pytest.MonkeyPatch) -> str:
    """Hostile CLI mode: stderr/stdout stuffed with the model path, a fake
    username, and a full environment dump (which would include the API key
    were it not stripped from the child environment)."""
    set_mode(monkeypatch, "hostile")
    hostile_text = f"{SENTINEL_MODEL_PATH} {SENTINEL_USERNAME}"
    monkeypatch.setenv("FAKE_COLI_HOSTILE_TEXT", hostile_text)
    monkeypatch.setenv("COLI_API_KEY", SENTINEL_API_KEY)
    monkeypatch.setenv("ODYSSEUS_COLIBRI_API_KEY", SENTINEL_API_KEY)
    return hostile_text


def test_plan_failure_leaks_nothing_hostile(
    hostile_env: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        outcome = colibri_cli.run_plan(FAKE_COLI, SENTINEL_MODEL_PATH)
    assert outcome["ok"] is False
    assert outcome["error_category"] == "plan_failed"
    _assert_clean(json.dumps(outcome), caplog.text, cli_path=FAKE_COLI)


def test_doctor_hostile_output_leaks_nothing(
    hostile_env: str, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        outcome = colibri_cli.run_doctor(FAKE_COLI, SENTINEL_MODEL_PATH)
    assert outcome["ok"] is False
    # Hostile stdout is not JSON, so the doctor path fails as malformed.
    assert outcome["error_category"] == "malformed_response"
    _assert_clean(json.dumps(outcome), caplog.text, cli_path=FAKE_COLI)


def test_launch_failure_never_logs_cli_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A resolvable file that cannot execute raises OSError whose message
    embeds the argv; only the error type may be logged."""
    import logging

    monkeypatch.setenv("ODYSSEUS_COLIBRI_API_KEY", SENTINEL_API_KEY)
    broken = tmp_path / "SENTINEL-USER-carlos-coli.exe"
    broken.write_bytes(b"MZ not a real executable")
    with caplog.at_level(logging.DEBUG):
        outcome = colibri_cli.run_plan(str(broken), SENTINEL_MODEL_PATH)
    assert outcome["ok"] is False
    assert outcome["error_category"] == "unavailable"
    _assert_clean(json.dumps(outcome), caplog.text, cli_path=str(broken))


# -- DeepLocalService wiring -------------------------------------------------


@pytest.fixture()
def settings(tmp_path):
    db = Database(tmp_path)
    try:
        yield SettingsService(db)
    finally:
        db.close()


def test_deep_local_plan_doctor_disabled_by_default(settings: SettingsService) -> None:
    service = DeepLocalService(settings)
    assert service.plan()["error_category"] == "disabled"
    assert service.doctor()["error_category"] == "disabled"


def test_deep_local_doctor_uses_configured_cli(settings: SettingsService, monkeypatch: pytest.MonkeyPatch) -> None:
    set_mode(monkeypatch, "doctor_ok")
    settings.set({"deep_local_enabled": True, "deep_local_cli_path": FAKE_COLI})
    outcome = DeepLocalService(settings).doctor()
    assert outcome["ok"] is True
    assert outcome["overall"] == "runnable_slow"


def test_deep_local_plan_reports_unconfigured_cli(settings: SettingsService) -> None:
    settings.set({"deep_local_enabled": True})
    outcome = DeepLocalService(settings).plan()
    assert outcome["ok"] is False
    assert outcome["error_category"] == "unavailable"
