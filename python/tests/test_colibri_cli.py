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
    assert "safetensors" in outcome["detail"]


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
