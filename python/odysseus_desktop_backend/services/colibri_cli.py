from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger

logger = get_logger("colibri_cli")

SUPPORTED_DOCTOR_SCHEMA_VERSION = 1
SUPPORTED_PLAN_VERSION = 2
CLI_TIMEOUT_SECONDS = 30.0
MAX_CLI_OUTPUT_BYTES = 4 * 1024 * 1024
GB = 1_000_000_000
# Upstream (550ddcba): "cold expert misses may reach disk; normal decode
# speed depends on hit rate" — the honest "runnable but extremely slow" flag.
COLD_EXPERT_WARNING_FRAGMENT = "cold expert misses"
_EXECUTABLE_SUFFIXES = {".exe", ".bat", ".cmd", ".com"}
# Never hand secrets to the readiness CLI; it does not need them.
_STRIPPED_ENV_KEYS = ("COLI_API_KEY", "ODYSSEUS_COLIBRI_API_KEY")


def _error(category: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error_category": category, "error": message, **extra}


def _resolve_cli_path(cli_path: str) -> Path | None:
    if not (cli_path or "").strip():
        return None
    path = Path(cli_path).expanduser()
    try:
        path = path.resolve(strict=True)
    except OSError:
        return None
    return path if path.is_file() else None


def _build_argv(cli: Path, subcommand: str, model_path: str) -> list[str]:
    if cli.suffix.lower() in _EXECUTABLE_SUFFIXES:
        argv = [str(cli)]
    else:
        # Upstream ships `coli` as an extensionless Python script; the
        # documented Windows invocation is `python coli ...`.
        argv = [sys.executable, str(cli)]
    argv += [subcommand, "--json"]
    if model_path:
        argv += ["--model", model_path]
    return argv


def _run(
    cli_path: str,
    subcommand: str,
    *,
    model_path: str = "",
    timeout: float = CLI_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    cli = _resolve_cli_path(cli_path)
    if cli is None:
        return _error(
            "unavailable",
            "No Colibri command-line tool is configured. Set deep_local_cli_path "
            "to your coli script to enable readiness checks.",
        )
    clean_model = ""
    if (model_path or "").strip():
        clean_model = str(Path(model_path).expanduser().resolve())
    argv = _build_argv(cli, subcommand, clean_model)
    env = {key: value for key, value in os.environ.items() if key not in _STRIPPED_ENV_KEYS}
    try:
        completed = subprocess.run(  # noqa: S603 - argv array, shell never used
            argv,
            capture_output=True,
            timeout=timeout,
            env=env,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return _error(
            "timeout",
            f"The Colibri readiness check did not finish within {timeout:g} seconds.",
        )
    except OSError as exc:
        # Never log the exception text: OSError messages embed the argv,
        # which contains the CLI (and possibly model) path.
        logger.warning(
            "colibri cli launch failed subcommand=%s error_type=%s",
            subcommand,
            type(exc).__name__,
        )
        return _error("unavailable", "The configured Colibri command-line tool could not be started.")
    stdout = completed.stdout[:MAX_CLI_OUTPUT_BYTES].decode("utf-8", errors="replace")
    stderr = completed.stderr[:MAX_CLI_OUTPUT_BYTES].decode("utf-8", errors="replace")
    if stderr.strip():
        # stderr routinely contains CLI/model directories and other machine
        # detail. It is never logged and never leaves this module; only its
        # presence and size are observable.
        logger.info(
            "colibri %s wrote stderr bytes=%s exit_code=%s",
            subcommand,
            len(completed.stderr),
            completed.returncode,
        )
    return {
        "ok": True,
        "exit_code": completed.returncode,
        "stdout": stdout,
        "stderr_present": bool(stderr.strip()),
    }


def _parse_json(stdout: str) -> dict[str, Any] | None:
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _format_gb(value: Any) -> str:
    try:
        return f"{float(value) / GB:.1f} GB"
    except (TypeError, ValueError):
        return "unknown"


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Translate the raw plan into plain language. Warnings never become
    green claims: a plan with cold-expert warnings is always 'may be
    extremely slow'."""
    model = plan.get("model") if isinstance(plan.get("model"), dict) else {}
    tiers = plan.get("tiers") if isinstance(plan.get("tiers"), dict) else {}
    disk = tiers.get("disk") if isinstance(tiers.get("disk"), dict) else {}
    ram = tiers.get("ram") if isinstance(tiers.get("ram"), dict) else {}
    vram = tiers.get("vram") if isinstance(tiers.get("vram"), dict) else {}
    warnings = [str(item) for item in plan.get("warnings") or []]
    return {
        "storage_required": _format_gb(model.get("model_bytes")),
        "storage_available": _format_gb(disk.get("available_bytes")),
        "estimated_ram_peak": _format_gb(ram.get("budget_bytes")),
        "ram_available": _format_gb(ram.get("available_bytes")),
        "expert_cache_slots_per_layer": int(ram.get("cache_slots_per_layer") or 0),
        "vram_tier": _format_gb(vram.get("budget_bytes")),
        "vram_devices": len(vram.get("devices") or []),
        "expected_bottleneck": str(plan.get("expected_bottleneck") or ""),
        "may_be_extremely_slow": any(COLD_EXPERT_WARNING_FRAGMENT in item for item in warnings),
        "warnings": warnings,
    }


def run_plan(cli_path: str, model_path: str = "", *, timeout: float = CLI_TIMEOUT_SECONDS) -> dict[str, Any]:
    outcome = _run(cli_path, "plan", model_path=model_path, timeout=timeout)
    if not outcome.get("ok"):
        return outcome
    if outcome["exit_code"] != 0:
        # Upstream cmd_plan sys.exit()s with a plain-text message on failure;
        # there is no JSON on this path, and the message routinely embeds the
        # model directory — so it is never returned or logged. Fixed code and
        # plain-language copy only.
        return _error(
            "plan_failed",
            "Colibri could not build a resource plan for this model folder. "
            "Check that the folder exists and contains the model's config.json "
            "and safetensors files.",
        )
    plan = _parse_json(outcome["stdout"])
    if plan is None:
        return _error("malformed_response", "The Colibri plan output was not readable JSON.")
    version = plan.get("version")
    if version != SUPPORTED_PLAN_VERSION:
        return _error(
            "incompatible_server",
            f"This Colibri version reports plan format {version!r}; PotatoCs supports "
            f"format {SUPPORTED_PLAN_VERSION}. Update PotatoCs or use a matching Colibri release.",
        )
    return {"ok": True, "plan": plan, "summary": _plan_summary(plan)}


_CHECK_LANGUAGE = {
    "model.path": "Model folder",
    "model.config": "Model configuration",
    "model.tokenizer": "Model tokenizer",
    "model.shards": "Model files",
    "storage.persistence": "Model folder write access",
    "storage.disk": "Free disk space",
    "memory.ram": "Memory (RAM)",
    "engine.binary": "Colibri engine",
    "accelerator.cuda": "Graphics card (optional)",
    "placement.plan": "Placement plan",
    "config.arguments": "Check settings",
}


def _doctor_overall(report: dict[str, Any], exit_code: int) -> str:
    if exit_code == 2:
        return "invalid_arguments"
    checks = {str(item.get("id")): str(item.get("status")) for item in report.get("checks") or [] if isinstance(item, dict)}
    status = str(report.get("status") or "")
    if status == "error":
        if checks.get("memory.ram") == "fail":
            return "unsafe"
        return "incompatible"
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else {}
    warnings = [str(item) for item in plan.get("warnings") or []]
    slow = any(COLD_EXPERT_WARNING_FRAGMENT in item for item in warnings) or any(
        COLD_EXPERT_WARNING_FRAGMENT in str(item.get("summary") or "")
        for item in report.get("checks") or []
        if isinstance(item, dict)
    )
    if slow:
        return "runnable_slow"
    return "runnable_slow" if status == "warning" else "runnable"


def run_doctor(cli_path: str, model_path: str = "", *, timeout: float = CLI_TIMEOUT_SECONDS) -> dict[str, Any]:
    outcome = _run(cli_path, "doctor", model_path=model_path, timeout=timeout)
    if not outcome.get("ok"):
        return outcome
    report = _parse_json(outcome["stdout"])
    if report is None:
        return _error("malformed_response", "The Colibri doctor output was not readable JSON.")
    version = report.get("schema_version")
    if version != SUPPORTED_DOCTOR_SCHEMA_VERSION:
        return _error(
            "incompatible_server",
            f"This Colibri version reports doctor format {version!r}; PotatoCs supports "
            f"format {SUPPORTED_DOCTOR_SCHEMA_VERSION}. Update PotatoCs or use a matching Colibri release.",
        )
    checks = []
    for item in report.get("checks") or []:
        if not isinstance(item, dict):
            continue
        check_id = str(item.get("id") or "")
        checks.append(
            {
                "id": check_id,
                "label": _CHECK_LANGUAGE.get(check_id, check_id),
                "status": str(item.get("status") or ""),
                "summary": str(item.get("summary") or ""),
            }
        )
    plan = report.get("plan") if isinstance(report.get("plan"), dict) else None
    result: dict[str, Any] = {
        "ok": True,
        "status": str(report.get("status") or ""),
        "overall": _doctor_overall(report, outcome["exit_code"]),
        "exit_code": outcome["exit_code"],
        "checks": checks,
    }
    if plan is not None:
        result["plan_summary"] = _plan_summary(plan)
    return result
