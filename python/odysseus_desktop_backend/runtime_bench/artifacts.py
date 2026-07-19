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
PAIRED_ARTIFACT_SCHEMA_VERSION = 2

VALID_SHAPES = {"tiny", "medium", "long_context", "repeat", "grounded", "overcommit"}
VALID_MODES = {"interactive", "persisted_job_sim"}
VALID_ENGINE_KINDS = {"real", "stub"}
VALID_QUALITY = {"passed", "failed", "not_applicable"}

# batch_id becomes the artifact filename: safe slug only, no separators,
# no traversal, bounded length.
_SAFE_BATCH_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")

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

# CLOSED schema: unknown keys are rejected at EVERY level. There is no
# free-form text field anywhere in an artifact ("notes" was removed —
# provenance lives in the batch_id slug).
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
_HW_NESTED_ALLOWLIST: dict[str, set[str]] = {
    "os": {"name", "version", "arch"},
    "cpu": {"logical_threads", "physical_cores", "physical_cores_source", "isa"},
    "ram": {"total_bytes", "available_bytes"},
    "storage": {"profile_disk_free_bytes", "model_store_disk_free_bytes", "kind"},
}
_HW_ISA_ALLOWLIST = {"ssse3", "sse4_1", "sse4_2", "avx", "avx2", "avx512f"}
_HW_GPU_ALLOWLIST = {"vendor", "name", "vram_total_bytes", "vram_free_bytes", "driver_version", "source"}

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
_OPTIONAL_TOP_LEVEL = {"file_cache_state"}
TOP_LEVEL_ALLOWLIST = _REQUIRED_TOP_LEVEL | _OPTIONAL_TOP_LEVEL

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
_OPTIONAL_RUN_KEYS = {"residency"}
RUN_KEY_ALLOWLIST = _REQUIRED_RUN_KEYS | _OPTIONAL_RUN_KEYS

_REQUIRED_TIMING_KEYS = {"total", "load", "prompt_eval", "generation", "first_token"}
_REQUIRED_TOKEN_KEYS = {"prompt", "generated", "prompt_tps", "generation_tps"}
_MEMORY_KEY_ALLOWLIST = {
    "runtime_peak_rss_bytes",
    "system_min_available_bytes",
    "vram_peak_used_bytes",
    "sampler_interval_ms",
    "samples",
}
_RESIDENCY_KEY_ALLOWLIST = {"size_bytes", "size_vram_bytes", "gpu_fraction", "context_length"}
_OPTIONS_KEY_ALLOWLIST = {
    "temperature",
    "seed",
    "num_predict",
    "num_ctx",
    "num_thread",
    "num_gpu",
    "num_batch",
    "n_predict",
    "think",
}

# No artifact string may contain a path separator AT ALL. This is the
# robust form of "reject all absolute paths": it needs no root
# allowlist and cannot be evaded by single-segment paths (`/secret`),
# spaces (`/opt/private model/x`), or Unicode segments — every known
# artifact string (tags, quantizations, versions, ISO timestamps,
# fixed enums) is separator-free by construction. Control characters
# and quotes are equally forbidden.
_FORBIDDEN_STRING_CHARS = ("/", "\\", "\n", "\r", "\t", '"')


def contains_pathlike(text: str) -> bool:
    return any(char in text for char in _FORBIDDEN_STRING_CHARS)


def _string_violations(value: Any, where: str, problems: list[str]) -> None:
    """Recursively reject separator/control characters in every string
    (keys included)."""
    if isinstance(value, str):
        if contains_pathlike(value):
            problems.append(f"forbidden characters in {where}")
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and contains_pathlike(key):
                problems.append(f"forbidden characters in key under {where}")
            _string_violations(item, f"{where}.{key}", problems)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _string_violations(item, f"{where}[{index}]", problems)


def _check_closed(obj: Any, allowlist: set[str], where: str, problems: list[str]) -> bool:
    if not isinstance(obj, dict):
        problems.append(f"{where} must be an object")
        return False
    illegal = set(obj) - allowlist
    if illegal:
        problems.append(f"{where} keys not allow-listed: {sorted(illegal)}")
        return False
    return True


def _is_finite_number(value: Any) -> bool:
    """True for int/float that are finite. Booleans are NOT numbers."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    return False


def _check_number(
    obj: dict[str, Any],
    key: str,
    where: str,
    problems: list[str],
    *,
    integer: bool = False,
    minimum: float | None = 0,
    maximum: float | None = None,
    nullable: bool = False,
) -> None:
    """Finite numeric/range validation for one field. Missing keys are
    the closed-schema checks' concern; this validates present values."""
    if key not in obj:
        return
    value = obj[key]
    if value is None:
        if not nullable:
            problems.append(f"{where}.{key} must not be null")
        return
    if not _is_finite_number(value):
        problems.append(f"{where}.{key} must be a finite number")
        return
    if integer and not isinstance(value, int):
        problems.append(f"{where}.{key} must be an integer")
        return
    if minimum is not None and value < minimum:
        problems.append(f"{where}.{key} must be >= {minimum}")
    if maximum is not None and value > maximum:
        problems.append(f"{where}.{key} must be <= {maximum}")


def validate_artifact(artifact: dict[str, Any]) -> list[str]:
    """Return a list of schema problems; empty list means valid.

    The schema is CLOSED at every level: any key outside the explicit
    allowlists — top level, runs, timings/tokens/memory/residency/
    options, model, runtime, hardware and its nested objects — is a
    validation failure, so arbitrary text (prompts, secrets, private
    metadata) cannot ride along anywhere.
    """
    problems: list[str] = []
    if not isinstance(artifact, dict):
        return ["artifact must be an object"]
    if artifact.get("schema_version") == PAIRED_ARTIFACT_SCHEMA_VERSION:
        from odysseus_desktop_backend.runtime_bench.paired_artifacts import (
            validate_paired_artifact,
        )

        problems = validate_paired_artifact(artifact)
        _string_violations(artifact, "artifact", problems)
        return problems
    missing = _REQUIRED_TOP_LEVEL - set(artifact)
    if missing:
        problems.append(f"missing top-level keys: {sorted(missing)}")
        return problems
    illegal_top = set(artifact) - TOP_LEVEL_ALLOWLIST
    if illegal_top:
        problems.append(f"top-level keys not allow-listed: {sorted(illegal_top)}")
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

    runtime = artifact.get("runtime")
    if _check_closed(runtime, RUNTIME_KEY_ALLOWLIST, "runtime", problems):
        if not runtime.get("name") or not runtime.get("version"):
            problems.append("runtime must include name and version")
        env = runtime.get("server_env")
        if env is not None and _check_closed(env, SERVER_ENV_ALLOWLIST, "server_env", problems):
            for key, value in env.items():
                if not isinstance(value, str) or not _SAFE_ENV_VALUE.match(value):
                    problems.append(f"server_env value not allow-listed for {key}")

    model = artifact.get("model")
    if _check_closed(model, MODEL_KEY_ALLOWLIST, "model", problems):
        if not model.get("tag"):
            problems.append("model must include tag")
        _check_number(model, "disk_bytes", "model", problems, integer=True)

    hardware = artifact.get("hardware")
    if _check_closed(hardware, HARDWARE_KEY_ALLOWLIST, "hardware", problems):
        if "cpu" not in hardware or "ram" not in hardware:
            problems.append("hardware snapshot must include cpu and ram")
        for section, allowed in _HW_NESTED_ALLOWLIST.items():
            if section in hardware and isinstance(hardware[section], dict):
                _check_closed(hardware[section], allowed, f"hardware.{section}", problems)
        isa = (hardware.get("cpu") or {}).get("isa") if isinstance(hardware.get("cpu"), dict) else None
        if isinstance(isa, dict):
            _check_closed(isa, _HW_ISA_ALLOWLIST, "hardware.cpu.isa", problems)
        gpus = hardware.get("gpus")
        if isinstance(gpus, list):
            for gpu_index, gpu in enumerate(gpus):
                _check_closed(gpu, _HW_GPU_ALLOWLIST, f"hardware.gpus[{gpu_index}]", problems)
        errors = hardware.get("errors")
        if isinstance(errors, dict):
            for key, value in errors.items():
                if value not in {"probe_timeout", "probe_unavailable", "probe_failed"}:
                    problems.append(f"hardware.errors.{key} is not a fixed category")
        cpu = hardware.get("cpu")
        if isinstance(cpu, dict):
            _check_number(cpu, "logical_threads", "hardware.cpu", problems, integer=True)
            _check_number(cpu, "physical_cores", "hardware.cpu", problems, integer=True)
        ram = hardware.get("ram")
        if isinstance(ram, dict):
            _check_number(ram, "total_bytes", "hardware.ram", problems, integer=True)
            _check_number(ram, "available_bytes", "hardware.ram", problems, integer=True)
        if isinstance(gpus, list):
            for gpu_index, gpu in enumerate(gpus):
                if isinstance(gpu, dict):
                    _check_number(gpu, "vram_total_bytes", f"hardware.gpus[{gpu_index}]", problems, integer=True)
                    _check_number(gpu, "vram_free_bytes", f"hardware.gpus[{gpu_index}]", problems, integer=True)
        storage = hardware.get("storage")
        if isinstance(storage, dict):
            _check_number(storage, "profile_disk_free_bytes", "hardware.storage", problems, integer=True, nullable=True)
            _check_number(storage, "model_store_disk_free_bytes", "hardware.storage", problems, integer=True, nullable=True)
        _check_number(hardware, "captured_at_ms", "hardware", problems, integer=True)

    file_cache_state = artifact.get("file_cache_state")
    if file_cache_state is not None and file_cache_state not in {"unknown_warmish", "cold_verified", "warm_verified"}:
        problems.append("file_cache_state must be a fixed enum value")

    runs = artifact.get("runs")
    if not isinstance(runs, list) or not runs:
        problems.append("runs must be a non-empty list")
        return problems
    for index, run in enumerate(runs):
        if not _check_closed(run, RUN_KEY_ALLOWLIST, f"run {index}", problems):
            continue
        missing_run = _REQUIRED_RUN_KEYS - set(run)
        if missing_run:
            problems.append(f"run {index} missing keys: {sorted(missing_run)}")
            continue
        if not isinstance(run["cold"], bool):
            problems.append(f"run {index} cold must be boolean")
        timings = run["timings_ms"]
        if not _check_closed(timings, _REQUIRED_TIMING_KEYS, f"run {index} timings_ms", problems) or (
            _REQUIRED_TIMING_KEYS - set(timings)
        ):
            problems.append(f"run {index} timings_ms incomplete")
        tokens = run["tokens"]
        if not _check_closed(tokens, _REQUIRED_TOKEN_KEYS, f"run {index} tokens", problems) or (
            _REQUIRED_TOKEN_KEYS - set(tokens)
        ):
            problems.append(f"run {index} tokens incomplete")
        _check_closed(run["memory"], _MEMORY_KEY_ALLOWLIST, f"run {index} memory", problems)
        _check_closed(run["options"], _OPTIONS_KEY_ALLOWLIST, f"run {index} options", problems)
        if "residency" in run:
            _check_closed(run["residency"], _RESIDENCY_KEY_ALLOWLIST, f"run {index} residency", problems)
        if run["quality_check"] not in VALID_QUALITY:
            problems.append(f"run {index} invalid quality_check: {run['quality_check']}")
        if run["error_category"] and run["quality_check"] == "passed":
            problems.append(f"run {index} cannot both pass quality and carry an error")

        # Numeric type/range validation (review round 5): finite
        # numbers only, no booleans, sensible non-negative ranges.
        _check_number(run, "run_index", f"run {index}", problems, integer=True)
        if isinstance(timings, dict):
            for key in _REQUIRED_TIMING_KEYS:
                _check_number(timings, key, f"run {index} timings_ms", problems, nullable=True)
        if isinstance(tokens, dict):
            _check_number(tokens, "prompt", f"run {index} tokens", problems, integer=True)
            _check_number(tokens, "generated", f"run {index} tokens", problems, integer=True)
            _check_number(tokens, "prompt_tps", f"run {index} tokens", problems, nullable=True)
            _check_number(tokens, "generation_tps", f"run {index} tokens", problems, nullable=True)
        memory = run["memory"]
        if isinstance(memory, dict):
            _check_number(memory, "runtime_peak_rss_bytes", f"run {index} memory", problems, integer=True)
            _check_number(memory, "system_min_available_bytes", f"run {index} memory", problems, integer=True)
            _check_number(memory, "vram_peak_used_bytes", f"run {index} memory", problems, integer=True, nullable=True)
            _check_number(memory, "sampler_interval_ms", f"run {index} memory", problems, integer=True)
            _check_number(memory, "samples", f"run {index} memory", problems, integer=True)
        options = run["options"]
        if isinstance(options, dict):
            for key in ("seed", "num_predict", "num_ctx", "num_thread", "num_gpu", "num_batch", "n_predict"):
                _check_number(options, key, f"run {index} options", problems, integer=True)
            _check_number(options, "temperature", f"run {index} options", problems)
        residency = run.get("residency")
        if isinstance(residency, dict):
            _check_number(residency, "size_bytes", f"run {index} residency", problems, integer=True)
            _check_number(residency, "size_vram_bytes", f"run {index} residency", problems, integer=True)
            _check_number(residency, "context_length", f"run {index} residency", problems, integer=True)
            _check_number(residency, "gpu_fraction", f"run {index} residency", problems, maximum=1, nullable=True)

    _string_violations(artifact, "artifact", problems)
    return problems


def redaction_violations(payload: str) -> list[str]:
    """Byte-level sentinel over the serialized artifact.

    Because no legitimate artifact string contains a path separator,
    the serialized JSON contains no `/` and no `\\` at all (JSON's own
    syntax uses neither outside strings). Any separator byte in the
    payload is therefore a violation — this subsumes Windows drive
    paths, UNC paths, and every POSIX absolute path including
    single-segment (`/secret`), spaced, and Unicode forms.
    """
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
    if "/" in payload or "\\" in payload:
        violations.append("path_separator")
    if re.search(r"[A-Za-z]:(\\|/)", payload):
        violations.append("windows_absolute_path")
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
