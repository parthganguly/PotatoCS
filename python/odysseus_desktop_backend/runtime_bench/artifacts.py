"""Benchmark artifact schema, validation, redaction sentinel, and writer.

Artifacts are the only durable output of the harness. They must be
machine-readable, self-describing (hardware + runtime + model + shape),
and free of usernames, absolute paths (any drive, UNC, or Unix),
prompts, and model outputs. `write_artifact` refuses anything that
fails validation or the redaction sentinel, and proves the resolved
target stays inside the requested artifact directory.
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

# batch_id becomes the artifact filename: safe slug only, no separators,
# no traversal, bounded length.
_SAFE_BATCH_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")

MAX_NOTES_CHARS = 200

# Only these server env keys may be recorded in artifacts; values are
# validated too (tuning flags are short tokens, never paths or secrets).
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
_SAFE_ENV_VALUE = re.compile(r"^[A-Za-z0-9._-]{0,32}$")

# Nested-field allowlists: unknown keys are rejected rather than carried.
MODEL_KEY_ALLOWLIST = {"tag", "digest", "format", "quantization", "parameter_size", "disk_bytes"}
RUNTIME_KEY_ALLOWLIST = {"name", "version", "server_env"}
HARDWARE_KEY_ALLOWLIST = {
    "schema_version",
    "os",
    "cpu",
    "ram",
    "gpus",
    "npu",
    "storage",
    "errors",
    "captured_at_ms",
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

# Path-like content is forbidden in every string an artifact carries:
# Windows drive paths on ANY drive, UNC paths, and Unix absolute paths
# under user/system roots.
_PATHLIKE_PATTERNS = (
    re.compile(r"[A-Za-z]:[\\/]"),
    re.compile(r"\\\\[A-Za-z0-9._$-]"),
    re.compile(r"(?:^|[\s\"'=:,(])/(?:home|Users|root|mnt|tmp|var|etc|usr)(?:/|$)"),
)


def contains_pathlike(text: str) -> bool:
    return any(pattern.search(text) for pattern in _PATHLIKE_PATTERNS)


def _string_violations(value: Any, where: str, problems: list[str]) -> None:
    """Recursively reject path-like content in every string field."""
    if isinstance(value, str):
        if contains_pathlike(value):
            problems.append(f"path-like content in {where}")
    elif isinstance(value, dict):
        for key, item in value.items():
            _string_violations(item, f"{where}.{key}", problems)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _string_violations(item, f"{where}[{index}]", problems)


def validate_artifact(artifact: dict[str, Any]) -> list[str]:
    """Return a list of schema problems; empty list means valid."""
    problems: list[str] = []
    missing = _REQUIRED_TOP_LEVEL - set(artifact)
    if missing:
        problems.append(f"missing top-level keys: {sorted(missing)}")
        return problems
    if artifact["schema_version"] != ARTIFACT_SCHEMA_VERSION:
        problems.append(f"unsupported schema_version: {artifact['schema_version']}")
    batch_id = artifact.get("batch_id")
    if not isinstance(batch_id, str) or not _SAFE_BATCH_ID.match(batch_id) or ".." in batch_id:
        problems.append("batch_id must be a safe slug (lowercase alnum, dot, dash, underscore)")
    if artifact["shape"] not in VALID_SHAPES:
        problems.append(f"invalid shape: {artifact['shape']}")
    if artifact["mode"] not in VALID_MODES:
        problems.append(f"invalid mode: {artifact['mode']}")
    if artifact["engine_kind"] not in VALID_ENGINE_KINDS:
        problems.append(f"invalid engine_kind: {artifact['engine_kind']}")

    runtime = artifact.get("runtime") or {}
    if not isinstance(runtime, dict) or not runtime.get("name") or not runtime.get("version"):
        problems.append("runtime must include name and version")
    if isinstance(runtime, dict):
        illegal_runtime = set(runtime) - RUNTIME_KEY_ALLOWLIST
        if illegal_runtime:
            problems.append(f"runtime keys not allow-listed: {sorted(illegal_runtime)}")
        env = runtime.get("server_env")
        if isinstance(env, dict):
            illegal = set(env) - SERVER_ENV_ALLOWLIST
            if illegal:
                problems.append(f"server_env keys not allow-listed: {sorted(illegal)}")
            for key, value in env.items():
                if not isinstance(value, str) or not _SAFE_ENV_VALUE.match(value):
                    problems.append(f"server_env value not allow-listed for {key}")

    model = artifact.get("model") or {}
    if not isinstance(model, dict) or not model.get("tag"):
        problems.append("model must include tag")
    if isinstance(model, dict):
        illegal_model = set(model) - MODEL_KEY_ALLOWLIST
        if illegal_model:
            problems.append(f"model keys not allow-listed: {sorted(illegal_model)}")

    hardware = artifact.get("hardware") or {}
    if not isinstance(hardware, dict) or "cpu" not in hardware or "ram" not in hardware:
        problems.append("hardware snapshot must include cpu and ram")
    if isinstance(hardware, dict):
        illegal_hw = set(hardware) - HARDWARE_KEY_ALLOWLIST
        if illegal_hw:
            problems.append(f"hardware keys not allow-listed: {sorted(illegal_hw)}")

    notes = artifact.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_CHARS:
        problems.append(f"notes must be a string of at most {MAX_NOTES_CHARS} characters")

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

    _string_violations(artifact, "artifact", problems)
    return problems


def redaction_violations(payload: str) -> list[str]:
    """Byte-level sentinel over the serialized artifact: local
    identifiers and path-like content that must never be stored."""
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
    # JSON escapes backslashes, so a drive path serializes as `D:\\` and
    # a UNC prefix as `\\\\`; match both serialized and plain forms.
    if re.search(r"[A-Za-z]:(\\\\|\\|/)", payload):
        violations.append("windows_absolute_path")
    if re.search(r"(\\\\){2}[A-Za-z0-9._$-]|\\\\[A-Za-z0-9._$-]+\\", payload):
        violations.append("unc_path")
    if re.search(r"(?:[\s\"'=:,(])/(?:home|Users|root|mnt|tmp|var|etc|usr)(?:/|\")", payload):
        violations.append("unix_absolute_path")
    return violations


def write_artifact(artifact: dict[str, Any], directory: str | Path) -> Path:
    """Validate, redaction-check, containment-check, and write."""
    problems = validate_artifact(artifact)
    if problems:
        raise ValueError(f"artifact schema invalid: {problems}")
    payload = json.dumps(artifact, indent=1, sort_keys=True)
    violations = redaction_violations(payload)
    if violations:
        raise ValueError(f"artifact redaction violation: {violations}")
    target_dir = Path(directory).resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / f"{artifact['batch_id']}.json").resolve()
    if target.parent != target_dir:
        raise ValueError("artifact path escapes the artifact directory")
    target.write_text(payload + "\n", encoding="utf-8")
    return target
