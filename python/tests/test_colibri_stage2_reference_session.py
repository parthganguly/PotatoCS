"""Private reference-session ownership: parent selection and cleanup.

Regression cover for the defect the first real human-approved invocation
exposed. That attempt failed pre-launch with ``reference_write_failed`` and
created no native process, but it *did* leave an empty
``odysseus-colibri-stage2-ref-*`` directory behind: the session was created
and validated outside the cleanup-owned block, so the validation failure
escaped past any teardown.

The validation itself was correct. ``TEMP``/``TMP`` pointed at a temp root
spelled with a Windows 8.3 short-name alias that canonicalizes to a
different long-form path -- and
``require_ordinary_directory`` compares the original lexical path against its
resolution precisely to catch a path that is not what it says it is. What was
wrong was creating the directory before that check could fail, and then
leaking it.

No test here touches ``D:\\Colibri``, the real engine, model files, or the
network. Every directory used is under ``tmp_path``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services import colibri_stage2_manifest as manifest_mod
from odysseus_desktop_backend.services import colibri_stage2_reference as ref_mod
from odysseus_desktop_backend.services import colibri_stage2_runner as runner

from tests.test_colibri_stage2_runner import (  # reuse the reviewed fixtures
    GOOD_OUTPUT,
    FakeApi,
    _build_fixture,
    _Fixture,
)

SESSION_PREFIX = "odysseus-colibri-stage2-ref-"


def _sessions_under(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return [entry for entry in directory.iterdir() if entry.name.startswith(SESSION_PREFIX)]


@pytest.fixture()
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Fixture:
    fixture = _build_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: fixture.manifest}),
    )
    return fixture


def _attempt(fixture: _Fixture, api: FakeApi, **overrides: Any) -> runner.OneTokenRunResult:
    kwargs: dict[str, Any] = dict(
        olmoe_exe=fixture.exe,
        converted_model_dir=fixture.model_dir,
        api=api,
        approved=True,
        interactive_check=lambda: True,
        job_member_probe=lambda job: 0,
        sleep=lambda seconds: None,
    )
    kwargs.update(overrides)
    return runner.attempt_one_token_proof(**kwargs)


# ---------------------------------------------------------------------------
# Deterministic parent selection
# ---------------------------------------------------------------------------


def test_default_parent_is_the_runtime_temp_sibling(registered: _Fixture) -> None:
    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()
    resolved = runner.default_reference_session_parent(registered.model_dir.resolve())
    assert resolved == runtime_temp.resolve()
    assert resolved.name == "runtime-temp"


def test_default_parent_requires_the_sibling_to_already_exist(registered: _Fixture) -> None:
    # Never provisioned silently: a missing parent is a failure.
    assert not (registered.model_dir.parent / "runtime-temp").exists()
    with pytest.raises(runner.ColibriStage2Failure, match="reference_write_failed"):
        runner.default_reference_session_parent(registered.model_dir.resolve())


def test_default_parent_rejects_a_reparse_point(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()
    real_lstat = os.lstat

    class _FakeStat:
        def __init__(self, real: Any) -> None:
            self.st_mode = real.st_mode
            self.st_file_attributes = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT

    def fake_lstat(path: Any, *args: object, **kwargs: object):
        result = real_lstat(path, *args, **kwargs)
        if Path(path) == runtime_temp:
            return _FakeStat(result)
        return result

    monkeypatch.setattr(runner.os, "lstat", fake_lstat)
    with pytest.raises(runner.ColibriStage2Failure, match="reference_write_failed"):
        runner.default_reference_session_parent(registered.model_dir.resolve())


def test_default_parent_is_used_when_no_explicit_parent_is_given(registered: _Fixture) -> None:
    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()
    result = _attempt(registered, FakeApi(stdout=GOOD_OUTPUT))
    assert result.ok is True
    assert result.session_created is True
    assert result.reference_session_removed is True
    # Nothing left behind in the deterministic parent.
    assert _sessions_under(runtime_temp) == []


def test_child_temp_points_only_at_the_created_session(registered: _Fixture) -> None:
    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()
    api = FakeApi(stdout=GOOD_OUTPUT)
    _attempt(registered, api)
    _, _, environment = api.create_suspended_calls[0]
    session_path = Path(environment["TEMP"])
    assert environment["TEMP"] == environment["TMP"]
    assert session_path.name.startswith(SESSION_PREFIX)
    assert session_path.parent == runtime_temp
    # The session itself, never its parent and never the caller's temp root.
    assert environment["TEMP"] != str(runtime_temp)


def test_explicit_synthetic_parent_still_works(registered: _Fixture, tmp_path: Path) -> None:
    explicit = tmp_path / "explicit-sessions"
    explicit.mkdir()
    result = _attempt(registered, FakeApi(stdout=GOOD_OUTPUT), reference_session_parent=explicit)
    assert result.ok is True
    assert _sessions_under(explicit) == []


def test_no_caller_facing_option_selects_the_parent() -> None:
    import inspect

    from odysseus_desktop_backend.services import colibri_stage2_token_cli as cli

    parser = cli._build_parser()
    option_strings = {opt for action in parser._actions for opt in action.option_strings}
    assert option_strings == {"--engine", "--converted-model-dir", "--approve"}
    # And the CLI never forwards a session parent to the attempt.
    assert "reference_session_parent" not in inspect.signature(cli.main).parameters


def test_environment_is_never_consulted_for_the_parent(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()
    decoy = registered.root / "decoy-temp"
    decoy.mkdir()
    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(decoy))

    api = FakeApi(stdout=GOOD_OUTPUT)
    _attempt(registered, api)
    _, _, environment = api.create_suspended_calls[0]
    assert Path(environment["TEMP"]).parent == runtime_temp
    assert _sessions_under(decoy) == []


# ---------------------------------------------------------------------------
# Windows 8.3 short-name alias regression
# ---------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="8.3 aliases are Windows-only")


def _short_path_name(path: Path) -> Path | None:
    """The volume's real 8.3 alias for ``path``, or None if unavailable."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetShortPathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    kernel32.GetShortPathNameW.restype = wintypes.DWORD
    buffer = ctypes.create_unicode_buffer(1024)
    written = kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    if not written or written >= len(buffer):
        return None
    short = Path(buffer.value)
    return None if str(short) == str(path) else short


@windows_only
def test_short_name_alias_temp_root_is_no_longer_selected(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temp root spelled with a real 8.3 alias must not be chosen, or leaked.

    The alias is not fabricated: a directory whose long name exceeds the 8.3
    form is created and Windows generates its own short name for it (the
    ``NAME~1`` shape). ``TEMP``/``TMP`` are pointed at that alias spelling,
    reproducing the first real invocation exactly.
    """

    long_named = registered.root / "Long Aliased Temp Root"
    long_named.mkdir()
    alias = _short_path_name(long_named)
    if alias is None:
        pytest.skip("8.3 short-name generation is disabled on this volume")

    # It really is an alias: a different spelling that canonicalizes to the
    # long form. This is exactly the mismatch require_ordinary_directory
    # exists to catch.
    assert str(alias) != str(long_named)
    assert alias.resolve() == long_named.resolve()
    assert os.path.normcase(str(alias.resolve())) != os.path.normcase(str(alias))

    # And it is genuinely rejected as an approved directory -- which is why
    # the old default, seeded from TEMP, failed after creating a session.
    with pytest.raises(runner.ColibriStage2Failure):
        runner.require_ordinary_directory(
            alias, missing_category="reference_write_failed", reparse_category="reference_write_failed"
        )

    for name in ("TEMP", "TMP", "TMPDIR"):
        monkeypatch.setenv(name, str(alias))

    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()

    api = FakeApi(stdout=GOOD_OUTPUT)
    result = _attempt(registered, api)

    # The real default ignores the environment entirely and succeeds.
    assert result.ok is True
    assert result.session_created is True
    assert result.reference_session_removed is True
    # Nothing was left under either spelling of the alias temp root.
    assert _sessions_under(alias) == []
    assert _sessions_under(long_named) == []
    assert _sessions_under(runtime_temp) == []
    _, _, environment = api.create_suspended_calls[0]
    assert Path(environment["TEMP"]).parent == runtime_temp
    assert str(alias) not in environment["TEMP"]


@windows_only
def test_alias_style_validation_failure_leaks_nothing_and_starts_no_process(
    registered: _Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact shape of the observed failure, with nothing left behind.

    Post-creation validation of the session directory is forced to fail the
    way the 8.3 alias made it fail. The run must reject pre-launch, create no
    process, and still remove the directory it created.
    """

    runtime_temp = registered.model_dir.parent / "runtime-temp"
    runtime_temp.mkdir()

    real_require = runner.require_ordinary_directory

    def failing_require(directory: Path, **kwargs: Any) -> Path:
        if directory.name.startswith(SESSION_PREFIX):
            raise runner.ColibriStage2Failure("reference_write_failed")
        return real_require(directory, **kwargs)

    monkeypatch.setattr(runner, "require_ordinary_directory", failing_require)

    api = FakeApi(stdout=GOOD_OUTPUT)
    result = _attempt(registered, api)

    assert result.ok is False
    assert result.category == "reference_write_failed"
    # No native process was created.
    assert api.calls == []
    assert api.create_suspended_calls == []
    assert result.exit_category == "not_observed"
    assert result.exit_code is None
    # The session was created -- and removed. Nothing leaked.
    assert result.session_created is True
    assert result.reference_session_removed is True
    assert result.cleanup_complete is True
    assert _sessions_under(runtime_temp) == []
    # No reference file was ever written, so none was removed.
    assert result.reference_removed is False
    # And no job/process claim is made.
    assert result.job_empty_proven is False
    assert result.job_member_count is None
    assert result.root_exit_confirmed is False
    assert result.descendant_count is None
    assert result.orphan_free is False


# ---------------------------------------------------------------------------
# Cleanup is owned from the first side effect
# ---------------------------------------------------------------------------


def test_reference_write_failure_removes_the_session(
    registered: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()

    def failing_write(session_dir: Path) -> Any:
        raise runner.ColibriStage2Failure("reference_write_failed")

    monkeypatch.setattr(runner, "write_private_reference", failing_write)
    result = _attempt(registered, FakeApi(), reference_session_parent=parent)

    assert result.ok is False
    assert result.category == "reference_write_failed"
    assert result.session_created is True
    assert result.reference_session_removed is True
    assert result.reference_removed is False
    assert result.cleanup_complete is True
    assert _sessions_under(parent) == []


def test_environment_construction_failure_removes_the_session(
    registered: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()

    def failing_environment(**kwargs: Any) -> dict[str, str]:
        raise runner.ColibriStage2Failure("platform_unsupported")

    monkeypatch.setattr(runner, "build_runner_environment", failing_environment)
    result = _attempt(registered, FakeApi(), reference_session_parent=parent)

    assert result.ok is False
    assert result.category == "platform_unsupported"
    assert result.session_created is True
    assert result.reference_session_removed is True
    assert _sessions_under(parent) == []


def test_session_removal_failure_is_reported_as_incomplete_cleanup(
    registered: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()

    def failing_teardown(session_dir: Path) -> None:
        raise runner.ColibriStage2Failure("reference_cleanup_failed")

    monkeypatch.setattr(runner, "teardown_private_reference_session", failing_teardown)
    result = _attempt(registered, FakeApi(stdout=GOOD_OUTPUT), reference_session_parent=parent)

    assert result.ok is False
    assert result.category == "cleanup_failed"
    assert result.session_created is True
    # Never claimed as removed when removal failed.
    assert result.reference_session_removed is False
    assert result.cleanup_complete is False


def test_unexpected_exception_after_session_creation_still_removes_it(
    registered: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()

    def exploding(session_dir: Path) -> Any:
        raise RuntimeError(r"internal defect referencing C:\Users\someone\secret")

    monkeypatch.setattr(runner, "write_private_reference", exploding)
    result = _attempt(registered, FakeApi(), reference_session_parent=parent)

    # Reported, not raised -- an escape would have leaked the directory and
    # let the caller call it a side-effect-free pre-launch rejection.
    assert result.ok is False
    assert result.category == "unexpected_internal_failure"
    assert result.session_created is True
    assert result.reference_session_removed is True
    assert _sessions_under(parent) == []
    assert "secret" not in repr(result)
    assert "someone" not in repr(result)


def test_native_execution_failure_still_removes_the_session(
    registered: _Fixture, tmp_path: Path
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()
    result = _attempt(
        registered, FakeApi(stdout=b"nothing useful here\n"), reference_session_parent=parent
    )
    assert result.ok is False
    assert result.session_created is True
    assert result.reference_session_removed is True
    assert result.reference_removed is True
    assert _sessions_under(parent) == []


def test_precondition_failure_creates_no_session_at_all(
    registered: _Fixture, tmp_path: Path
) -> None:
    # A failure before the cleanup-owned block is genuinely side-effect free,
    # so it still raises and no directory is ever created.
    parent = tmp_path / "sessions"
    parent.mkdir()
    (registered.model_dir / common.EXPECTED_SHARD_BASENAMES[0]).unlink()
    with pytest.raises(runner.ColibriStage2Failure, match="missing_converted_shard"):
        _attempt(registered, FakeApi(), reference_session_parent=parent)
    assert _sessions_under(parent) == []


def test_session_fields_default_to_false_when_nothing_was_created(
    registered: _Fixture, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()

    def failing_create(parent_dir: Path | None) -> Path:
        raise runner.ColibriStage2Failure("reference_write_failed")

    monkeypatch.setattr(runner, "create_private_reference_session", failing_create)
    result = _attempt(registered, FakeApi(), reference_session_parent=parent)

    assert result.ok is False
    assert result.category == "reference_write_failed"
    assert result.session_created is False
    assert result.reference_session_removed is False
    # Creation never happened, so nothing was owed and cleanup is complete.
    assert result.cleanup_complete is True
    assert _sessions_under(parent) == []


def test_successful_run_reports_both_removals(registered: _Fixture, tmp_path: Path) -> None:
    parent = tmp_path / "sessions"
    parent.mkdir()
    result = _attempt(registered, FakeApi(stdout=GOOD_OUTPUT), reference_session_parent=parent)
    assert result.ok is True
    assert result.session_created is True
    assert result.reference_session_removed is True
    assert result.reference_removed is True
    assert result.cleanup_complete is True
    assert _sessions_under(parent) == []


def test_session_lifecycle_fields_are_what_v3_identifies(registered: _Fixture, tmp_path: Path) -> None:
    """The schema identifier and the record shape must move together.

    ``session_created`` / ``reference_session_removed`` are the fields v3 was
    bumped for. A record carrying them may not claim to be a v2 capture, and
    the historical v2 attempt -- emitted before these fields existed -- is
    never relabelled.
    """

    import dataclasses

    parent = tmp_path / "sessions"
    parent.mkdir()
    result = _attempt(registered, FakeApi(stdout=GOOD_OUTPUT), reference_session_parent=parent)

    assert result.evidence_schema_version == "colibri-stage2-olmoe-token-evidence-v3"
    assert result.evidence_schema_version == common.TOKEN_RUN_EVIDENCE_SCHEMA_VERSION
    field_names = {field.name for field in dataclasses.fields(result)}
    assert {"session_created", "reference_session_removed"} <= field_names


def test_reference_module_prefix_matches_what_the_tests_scan_for() -> None:
    # Guards the leak assertions above against a silent prefix rename.
    assert ref_mod._SESSION_PREFIX == SESSION_PREFIX
