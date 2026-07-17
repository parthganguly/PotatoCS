"""Benchmark artifact schema, validation, redaction sentinel, and writer.

Artifacts are the only durable output of the harness. They must be
machine-readable, self-describing (hardware + runtime + model + shape),
and free of usernames, absolute paths, prompts, and model outputs.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

ARTIFACT_SCHEMA_VERSION = 1

VALID_SHAPES = {"tiny", "medium", "long_context", "repeat", "grounded", "overcommit"}
VALID_MODES = {"interactive", "persisted_job_sim"}
VALID_ENGINE_KINDS = {"real", "stub"}
VALID_QUALITY = {"passed", "failed", "not_applicable"}

# Only these server env keys may be recorded in artifacts; values are
# recorded verbatim (they are tuning flags, never secrets or paths).
SERVER_ENV_ALLOWLIST = {
    "OLLAMA_FLASH_ATTENTION",
    "OLLAMA_KV_CACHE_TYPE",
    "OLLAMA_KEEP_ALIVE",
    "OLLAMA_CONTEXT_LENGTH",
    "OLLAMA_NUM_PARALLEL",
    "OLLAMA_MAX_LOADED_MODELS",
    "OLLAMA_NO_CLOUD",
    "LLAMA_ARG_FIT",
    "LLAMA_ARG_FIT_TARGET",
}

_REQUIRED_TOP_LEVEL = {
    "schema_version",
    "batch_id",
    "captured_at",
    "hardware",
    "runtime",
    "model",
    "shape",
    "mode",
    "engine_kind",
    "runs",
}

_REQUIRED_RUN_KEYS = {
    "run_index",
    "cold",
    "options",
    "timings_ms",
    "tokens",
    "memory",
    "quality_check",
    "error_category",
}

_REQUIRED_TIMING_KEYS = {"total", "load", "prompt_eval", "generation", "first_token"}
_REQUIRED_TOKEN_KEYS = {"prompt", "generated", "prompt_tps", "generation_tps"}


def validate_artifact(artifact: dict[str, Any]) -> list[str]:
    """Return a list of schema problems; empty list means valid."""
    problems: list[str] = []
    missing = _REQUIRED_TOP_LEVEL - set(artifact)
    if missing:
        problems.append(f"missing top-level keys: {sorted(missing)}")
        return problems
    if artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        problems.append(f"unsupported schema_version: {artifact['schema_version']}")
    if artifact["shape"] not in VALID_SHAPES:
        problems.append(f"invalid shape: {artifact['shape']}")
    if artifact["mode"] not in VALID_MODES:
        problems.append(f"invalid mode: {artifact['mode']}")
    if artifact["engine_kind"] not in VALID_ENGINE_KINDS:
        problems.append(f"invalid engine_kind: {artifact['engine_kind']}")

    runtime = artifact.get("runtime") or {}
    if not isinstance(runtime, dict) or not runtime.get("name") or not runtime.get("version"):
        problems.append("runtime must include name and version")
    env = runtime.get("server_env") if isinstance(runtime, dict) else {}
    if isinstance(env, dict):
        illegal = set(env) - SERVER_ENV_ALLOWLIST
        if illegal:
            problems.append(f"server_env keys not allow-listed: {sorted(illegal)}")

    model = artifact.get("model") or {}
    if not isinstance(model, dict) or not model.get("tag"):
        problems.append("model must include tag")

    hardware = artifact.get("hardware") or {}
    if not isinstance(hardware, dict) or "cpu" not in hardware or "ram" not in hardware:
        problems.append("hardware snapshot must include cpu and ram")

    runs = artifact.get("runs")
    if not isinstance(runs, list) or not runs:
        problems.append("runs must be a non-empty list")
        return problems
    for index, run in enumerate(runs):
        if not isinstance(run, dict):
            problems.append(f"run {index} is not an object")
            continue
        missing_run = _REQUIRED_RUN_KEYS - set(run)
        if missing_run:
            problems.append(f"run {index} missing keys: {sorted(missing_run)}")
            continue
        if not isinstance(run["cold"], bool):
            problems.append(f"run {index} cold must be boolean")
        timings = run["timings_ms"]
        if not isinstance(timings, dict) or _REQUIRED_TIMING_KEYS - set(timings):
            problems.append(f"run {index} timings_ms incomplete")
        tokens = run["tokens"]
        if not isinstance(tokens, dict) or _REQUIRED_TOKEN_KEYS - set(tokens):
            problems.append(f"run {index} tokens incomplete")
        if run["quality_check"] not in VALID_QUALITY:
            problems.append(f"run {index} invalid quality_check: {run['quality_check']}")
        if run["error_category"] and run["quality_check"] == "passed":
            problems.append(f"run {index} cannot both pass quality and carry an error")
        for forbidden in ("prompt", "output", "content", "messages"):
            if forbidden in run:
                problems.append(f"run {index} must not embed {forbidden}")
    return problems


def redaction_violations(payload: str) -> list[str]:
    """Byte-level sentinel: local identifiers that must never be stored."""
    violations: list[str] = []
    home = os.path.expanduser("~")
    candidates = {home, home.replace("\\", "/"), home.replace("\\", "\\\\")}
    for env_key in ("USERNAME", "USER"):
        username = os.environ.get(env_key) or ""
        if len(username) >= 2:
            candidates.add(username)
    for candidate in candidates:
        if candidate and candidate not in ("~", "/") and candidate in payload:
            violations.append("local_identifier")
            break
    # Windows drive-letter absolute paths are never legitimate artifact data.
    if re.search(r"[A-Za-z]:\\\\?Users", payload):
        violations.append("windows_user_path")
    return violations


def write_artifact(artifact: dict[str, Any], directory: str | Path) -> Path:
    """Validate, redaction-check, and write one artifact JSON file."""
    problems = validate_artifact(artifact)
    if problems:
        raise ValueError(f"artifact schema invalid: {problems}")
    payload = json.dumps(artifact, indent=1, sort_keys=True)
    violations = redaction_violations(payload)
    if violations:
        raise ValueError(f"artifact redaction violation: {violations}")
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    name = f"{artifact['batch_id']}.json"
    path = target_dir / name
    path.write_text(payload + "\n", encoding="utf-8")
    return path
