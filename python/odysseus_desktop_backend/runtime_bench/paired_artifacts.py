"""Closed schema-v2 validation for paired benchmark arm artifacts.

Version 2 is deliberately additive: legacy version-1 artifacts retain
their original validator and planner meaning.  A v2 file represents one
arm of one pair; balanced execution order is captured on each run.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

PAIRED_ARTIFACT_SCHEMA_VERSION = 2

SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

TOP_KEYS = {
    "schema_version", "batch_id", "captured_at", "experiment_id", "pair_id",
    "arm_id", "arm_role", "hardware", "runtime", "model", "fixture",
    "requirements", "placement", "shape", "mode", "engine_kind",
    "file_cache_state", "runs",
}
RUNTIME_KEYS = {"name", "version", "server_env", "backend_options"}
SERVER_ENV_KEYS = {
    "OLLAMA_FLASH_ATTENTION", "OLLAMA_KV_CACHE_TYPE", "OLLAMA_KEEP_ALIVE",
    "OLLAMA_CONTEXT_LENGTH", "OLLAMA_NUM_PARALLEL", "OLLAMA_MAX_LOADED_MODELS",
    "OLLAMA_NO_CLOUD", "LLAMA_ARG_FIT", "LLAMA_ARG_FIT_TARGET",
}
MODEL_KEYS = {
    "tag", "digest", "file_identity", "format", "quantization", "architecture",
    "total_parameters", "active_parameters", "disk_bytes", "tokenizer_identity",
    "chat_template_identity",
}
FIXTURE_KEYS = {"identity", "sha256", "task_requirement_id", "quality_criteria_id"}
REQUIREMENT_KEYS = {"context_limit", "output_token_limit", "sampling"}
SAMPLING_KEYS = {"temperature", "seed", "top_p", "top_k"}
PLACEMENT_KEYS = {"state", "cpu", "gpu"}
RUN_KEYS = {
    "run_index", "repetition_index", "execution_order", "captured_at", "cold",
    "elapsed_since_previous_arm_ms", "pre_arm", "options", "timings_ms", "tokens",
    "memory", "gpu", "disk", "cache_state", "placement_state", "quality",
    "cancellation", "preflight_rejection_category", "error_category",
    "truncation_state", "evidence_state",
}
PRE_ARM_KEYS = {"available_ram_bytes", "gpu_snapshot", "interference"}
INTERFERENCE_KEYS = {"state", "system_cpu_percent", "memory_load_percent"}
GPU_SNAPSHOT_KEYS = {"state", "used_bytes", "free_bytes", "total_bytes"}
OPTION_KEYS = {
    "temperature", "seed", "num_predict", "num_ctx", "num_thread", "num_gpu",
    "num_batch", "n_predict", "think", "top_p", "top_k",
}
TIMING_KEYS = {"total", "load", "prompt_eval", "generation", "first_token"}
TOKEN_KEYS = {"prompt", "generated", "prompt_tps", "generation_tps"}
MEMORY_KEYS = {
    "total_ram_bytes", "available_ram_before_bytes", "min_available_ram_bytes",
    "process_peak_rss_bytes", "pagefile_used_peak_bytes", "sampler_interval_ms",
    "sample_count", "sampling_state", "sampling_failure_category",
    "system_memory_samples", "safety_floor_bytes", "safety_floor_crossed",
    "system_cpu_peak_percent", "system_cpu_mean_percent", "cpu_sampling_state",
    "cpu_sampling_failure_category",
}
MEMORY_SAMPLE_KEYS = {
    "elapsed_ms", "available_ram_bytes", "memory_load_percent",
    "pagefile_used_bytes", "system_cpu_percent",
}
GPU_KEYS = {
    "pre", "during_peak_used_bytes", "during_min_free_bytes", "post",
    "sampling_state", "sampling_failure_category",
}
DISK_KEYS = {"state", "read_bytes"}
QUALITY_KEYS = {"state", "assertion_count", "score", "deterministic_answer_sha256", "unsupported_category"}
CANCELLATION_KEYS = {
    "tested", "request_to_cancel_ms", "client_stream_closed_ms",
    "runtime_idle_ms", "process_completion_ms", "final_state",
    "resources_released", "runtime_responsive",
}

PREFLIGHT_REJECTION_CATEGORIES = {
    "", "unknown_weight_size", "unknown_kv_geometry", "malformed_model_metadata",
    "insufficient_memory_budget", "memory_probe_unavailable",
}

HARDWARE_KEYS = {"schema_version", "os", "cpu", "ram", "gpus", "npu", "storage", "errors", "captured_at_ms"}
HARDWARE_NESTED = {
    "os": {"name", "version", "arch"},
    "cpu": {"logical_threads", "physical_cores", "physical_cores_source", "isa"},
    "ram": {"total_bytes", "available_bytes"},
    "storage": {"profile_disk_free_bytes", "model_store_disk_free_bytes", "kind"},
}
ISA_KEYS = {"ssse3", "sse4_1", "sse4_2", "avx", "avx2", "avx512f"}
HARDWARE_GPU_KEYS = {"vendor", "name", "vram_total_bytes", "vram_free_bytes", "driver_version", "source"}
HARDWARE_ERROR_KEYS = {
    "cpu_physical_cores", "cpu_isa", "ram", "gpu",
    "storage_profile_disk_free_bytes", "storage_model_store_disk_free_bytes",
}

FIXED_FAILURES = {
    "", "timeout", "connection_failure", "http_error", "malformed_response",
    "preflight_safety_abort", "safety_abort", "cancelled", "incomplete_run",
}


def _closed(value: Any, keys: set[str], where: str, problems: list[str]) -> bool:
    if not isinstance(value, dict):
        problems.append(f"{where} must be an object")
        return False
    extra = set(value) - keys
    missing = keys - set(value)
    if extra:
        problems.append(f"{where} keys not allow-listed: {sorted(extra)}")
    if missing:
        problems.append(f"{where} missing keys: {sorted(missing)}")
    return not extra and not missing


def _finite(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _number(
    value: Any,
    where: str,
    problems: list[str],
    *,
    integer: bool = False,
    nullable: bool = False,
    minimum: float = 0,
    maximum: float | None = None,
) -> None:
    if value is None and nullable:
        return
    if not _finite(value):
        problems.append(f"{where} must be a finite number")
        return
    if integer and not isinstance(value, int):
        problems.append(f"{where} must be an integer")
        return
    if value < minimum:
        problems.append(f"{where} must be >= {minimum}")
    if maximum is not None and value > maximum:
        problems.append(f"{where} must be <= {maximum}")


def _timestamp(value: Any, where: str, problems: list[str]) -> None:
    if not isinstance(value, str) or not ISO_UTC.fullmatch(value):
        problems.append(f"{where} must be a UTC timestamp")
        return
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        problems.append(f"{where} must be a valid UTC timestamp")


def _enum(value: Any, allowed: set[str], where: str, problems: list[str]) -> None:
    if value not in allowed:
        problems.append(f"{where} must be one of {sorted(allowed)}")


def _bool(value: Any, where: str, problems: list[str], *, nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if not isinstance(value, bool):
        problems.append(f"{where} must be boolean")


def _validate_hardware(hardware: Any, problems: list[str]) -> None:
    if not _closed(hardware, HARDWARE_KEYS, "hardware", problems):
        return
    for key, keys in HARDWARE_NESTED.items():
        if not _closed(hardware[key], keys, f"hardware.{key}", problems):
            continue
    cpu = hardware["cpu"]
    if isinstance(cpu, dict):
        _number(cpu.get("logical_threads"), "hardware.cpu.logical_threads", problems, integer=True)
        _number(cpu.get("physical_cores"), "hardware.cpu.physical_cores", problems, integer=True)
        if not _closed(cpu.get("isa"), ISA_KEYS, "hardware.cpu.isa", problems):
            pass
        elif any(not isinstance(value, bool) for value in cpu["isa"].values()):
            problems.append("hardware.cpu.isa values must be boolean")
    ram = hardware["ram"]
    if isinstance(ram, dict):
        _number(ram.get("total_bytes"), "hardware.ram.total_bytes", problems, integer=True)
        _number(ram.get("available_bytes"), "hardware.ram.available_bytes", problems, integer=True)
        if _finite(ram.get("total_bytes")) and _finite(ram.get("available_bytes")) and ram["available_bytes"] > ram["total_bytes"]:
            problems.append("hardware.ram.available_bytes must not exceed total_bytes")
    if not isinstance(hardware["gpus"], list):
        problems.append("hardware.gpus must be a list")
    else:
        for index, gpu in enumerate(hardware["gpus"]):
            if _closed(gpu, HARDWARE_GPU_KEYS, f"hardware.gpus[{index}]", problems):
                _number(gpu["vram_total_bytes"], f"hardware.gpus[{index}].vram_total_bytes", problems, integer=True)
                _number(gpu["vram_free_bytes"], f"hardware.gpus[{index}].vram_free_bytes", problems, integer=True)
                if _finite(gpu["vram_total_bytes"]) and _finite(gpu["vram_free_bytes"]) and gpu["vram_free_bytes"] > gpu["vram_total_bytes"]:
                    problems.append(f"hardware.gpus[{index}].vram_free_bytes must not exceed total")
    if not isinstance(hardware["errors"], dict):
        problems.append("hardware.errors must be an object")
    else:
        extra_errors = set(hardware["errors"]) - HARDWARE_ERROR_KEYS
        if extra_errors:
            problems.append(f"hardware.errors keys not allow-listed: {sorted(extra_errors)}")
        for key, value in hardware["errors"].items():
            _enum(value, {"probe_timeout", "probe_unavailable", "probe_failed"}, f"hardware.errors.{key}", problems)
    storage = hardware["storage"]
    if isinstance(storage, dict):
        _number(storage.get("profile_disk_free_bytes"), "hardware.storage.profile_disk_free_bytes", problems, integer=True, nullable=True)
        _number(storage.get("model_store_disk_free_bytes"), "hardware.storage.model_store_disk_free_bytes", problems, integer=True, nullable=True)
    _number(hardware["captured_at_ms"], "hardware.captured_at_ms", problems, integer=True)


def validate_paired_artifact(artifact: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not _closed(artifact, TOP_KEYS, "top-level", problems):
        return problems
    if artifact["schema_version"] != PAIRED_ARTIFACT_SCHEMA_VERSION:
        problems.append("unsupported schema_version")
    for key in ("batch_id", "experiment_id", "pair_id", "arm_id"):
        value = artifact[key]
        if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or ".." in value:
            problems.append(f"{key} must be a safe slug")
    _timestamp(artifact["captured_at"], "captured_at", problems)
    _enum(artifact["arm_role"], {"baseline", "candidate"}, "arm_role", problems)
    _enum(artifact["shape"], {"tiny", "medium", "long_context", "repeat", "grounded", "overcommit"}, "shape", problems)
    _enum(artifact["mode"], {"interactive", "persisted_job_sim"}, "mode", problems)
    _enum(artifact["engine_kind"], {"real", "stub"}, "engine_kind", problems)
    _enum(artifact["file_cache_state"], {"unknown_warmish", "cold_verified", "warm_verified"}, "file_cache_state", problems)
    _validate_hardware(artifact["hardware"], problems)

    runtime = artifact["runtime"]
    if _closed(runtime, RUNTIME_KEYS, "runtime", problems):
        if not runtime["name"] or not runtime["version"]:
            problems.append("runtime identity and version are required")
        env = runtime["server_env"]
        if not isinstance(env, dict):
            problems.append("runtime.server_env must be an object")
        else:
            extra = set(env) - SERVER_ENV_KEYS
            if extra:
                problems.append(f"runtime.server_env keys not allow-listed: {sorted(extra)}")
            for key, value in env.items():
                if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{0,32}", value):
                    problems.append(f"runtime.server_env.{key} has invalid value")
        if not isinstance(runtime["backend_options"], dict) or set(runtime["backend_options"]) - OPTION_KEYS:
            problems.append("runtime.backend_options keys not allow-listed")
        else:
            _validate_options(runtime["backend_options"], "runtime.backend_options", problems)

    model = artifact["model"]
    if _closed(model, MODEL_KEYS, "model", problems):
        for key in ("tag", "digest", "file_identity", "format", "quantization", "tokenizer_identity", "chat_template_identity"):
            if not isinstance(model[key], str) or not model[key]:
                problems.append(f"model.{key} is required")
        _enum(model["architecture"], {"dense", "moe", "unknown"}, "model.architecture", problems)
        _number(model["total_parameters"], "model.total_parameters", problems, integer=True, nullable=True)
        _number(model["active_parameters"], "model.active_parameters", problems, integer=True, nullable=True)
        _number(model["disk_bytes"], "model.disk_bytes", problems, integer=True)
        if _finite(model["total_parameters"]) and _finite(model["active_parameters"]) and model["active_parameters"] > model["total_parameters"]:
            problems.append("model.active_parameters must not exceed total_parameters")
        digest = model["digest"]
        if digest != "unavailable" and (not isinstance(digest, str) or not SHA256.fullmatch(digest)):
            problems.append("model.digest must be sha256 or unavailable")
        file_identity = model["file_identity"]
        if file_identity != "unavailable" and (not isinstance(file_identity, str) or not SHA256.fullmatch(file_identity)):
            problems.append("model.file_identity must be sha256 or unavailable")

    fixture = artifact["fixture"]
    if _closed(fixture, FIXTURE_KEYS, "fixture", problems):
        if not isinstance(fixture["sha256"], str) or not SHA256.fullmatch(fixture["sha256"]):
            problems.append("fixture.sha256 must be sha256")
        for key in ("identity", "task_requirement_id", "quality_criteria_id"):
            if not isinstance(fixture[key], str) or not fixture[key]:
                problems.append(f"fixture.{key} is required")

    requirements = artifact["requirements"]
    if _closed(requirements, REQUIREMENT_KEYS, "requirements", problems):
        _number(requirements["context_limit"], "requirements.context_limit", problems, integer=True, minimum=1)
        _number(requirements["output_token_limit"], "requirements.output_token_limit", problems, integer=True, minimum=1)
        sampling = requirements["sampling"]
        if _closed(sampling, SAMPLING_KEYS, "requirements.sampling", problems):
            _number(sampling["temperature"], "requirements.sampling.temperature", problems, maximum=2)
            _number(sampling["seed"], "requirements.sampling.seed", problems, integer=True)
            _number(sampling["top_p"], "requirements.sampling.top_p", problems, maximum=1)
            _number(sampling["top_k"], "requirements.sampling.top_k", problems, integer=True)

    placement = artifact["placement"]
    if _closed(placement, PLACEMENT_KEYS, "placement", problems):
        _enum(placement["state"], {"recorded", "unavailable"}, "placement.state", problems)
        _enum(placement["cpu"], {"used", "unused", "unknown"}, "placement.cpu", problems)
        _enum(placement["gpu"], {"full", "partial", "none", "unknown"}, "placement.gpu", problems)
        if placement["state"] == "recorded" and "unknown" in {placement["cpu"], placement["gpu"]}:
            problems.append("recorded placement must identify CPU and GPU use")

    runs = artifact["runs"]
    if not isinstance(runs, list) or not runs:
        problems.append("runs must be a non-empty list")
        return problems
    for index, run in enumerate(runs):
        _validate_run(run, index, problems)
    return problems


def _validate_run(run: Any, index: int, problems: list[str]) -> None:
    where = f"run {index}"
    if not _closed(run, RUN_KEYS, where, problems):
        return
    for key in ("run_index", "repetition_index", "execution_order"):
        _number(run[key], f"{where}.{key}", problems, integer=True)
    _timestamp(run["captured_at"], f"{where}.captured_at", problems)
    _bool(run["cold"], f"{where}.cold", problems)
    _number(run["elapsed_since_previous_arm_ms"], f"{where}.elapsed_since_previous_arm_ms", problems, nullable=True)
    _enum(run["cache_state"], {"cold", "warm", "unknown"}, f"{where}.cache_state", problems)
    _enum(run["placement_state"], {"recorded", "unavailable"}, f"{where}.placement_state", problems)
    _enum(run["error_category"], FIXED_FAILURES, f"{where}.error_category", problems)
    _enum(
        run["preflight_rejection_category"],
        PREFLIGHT_REJECTION_CATEGORIES,
        f"{where}.preflight_rejection_category",
        problems,
    )
    _enum(run["truncation_state"], {"complete", "truncated_prompt", "incomplete_evidence"}, f"{where}.truncation_state", problems)
    _enum(run["evidence_state"], {"complete", "incomplete", "unavailable"}, f"{where}.evidence_state", problems)

    pre = run["pre_arm"]
    if _closed(pre, PRE_ARM_KEYS, f"{where}.pre_arm", problems):
        _number(pre["available_ram_bytes"], f"{where}.pre_arm.available_ram_bytes", problems, integer=True)
        _validate_gpu_snapshot(pre["gpu_snapshot"], f"{where}.pre_arm.gpu_snapshot", problems)
        interference = pre["interference"]
        if _closed(interference, INTERFERENCE_KEYS, f"{where}.pre_arm.interference", problems):
            _enum(interference["state"], {"available", "unavailable"}, f"{where}.pre_arm.interference.state", problems)
            _number(interference["system_cpu_percent"], f"{where}.pre_arm.interference.system_cpu_percent", problems, nullable=True, maximum=100)
            _number(interference["memory_load_percent"], f"{where}.pre_arm.interference.memory_load_percent", problems, nullable=True, maximum=100)

    options = run["options"]
    if not isinstance(options, dict) or set(options) - OPTION_KEYS:
        problems.append(f"{where}.options keys not allow-listed")
    else:
        _validate_options(options, f"{where}.options", problems)

    timings = run["timings_ms"]
    if _closed(timings, TIMING_KEYS, f"{where}.timings_ms", problems):
        for key, value in timings.items():
            _number(value, f"{where}.timings_ms.{key}", problems, nullable=True)
    tokens = run["tokens"]
    if _closed(tokens, TOKEN_KEYS, f"{where}.tokens", problems):
        _number(tokens["prompt"], f"{where}.tokens.prompt", problems, integer=True)
        _number(tokens["generated"], f"{where}.tokens.generated", problems, integer=True)
        _number(tokens["prompt_tps"], f"{where}.tokens.prompt_tps", problems, nullable=True)
        _number(tokens["generation_tps"], f"{where}.tokens.generation_tps", problems, nullable=True)

    memory = run["memory"]
    if _closed(memory, MEMORY_KEYS, f"{where}.memory", problems):
        for key in ("total_ram_bytes", "available_ram_before_bytes", "min_available_ram_bytes", "process_peak_rss_bytes", "pagefile_used_peak_bytes", "sampler_interval_ms", "sample_count", "safety_floor_bytes"):
            _number(memory[key], f"{where}.memory.{key}", problems, integer=True, nullable=key == "pagefile_used_peak_bytes")
        _enum(memory["sampling_state"], {"available", "unavailable"}, f"{where}.memory.sampling_state", problems)
        _enum(memory["sampling_failure_category"], {"", "memory_probe_unavailable"}, f"{where}.memory.sampling_failure_category", problems)
        for key in ("system_cpu_peak_percent", "system_cpu_mean_percent"):
            _number(memory[key], f"{where}.memory.{key}", problems, nullable=True, maximum=100)
        _enum(memory["cpu_sampling_state"], {"available", "unavailable"}, f"{where}.memory.cpu_sampling_state", problems)
        _enum(memory["cpu_sampling_failure_category"], {"", "cpu_probe_unavailable"}, f"{where}.memory.cpu_sampling_failure_category", problems)
        _bool(memory["safety_floor_crossed"], f"{where}.memory.safety_floor_crossed", problems)
        samples = memory["system_memory_samples"]
        if not isinstance(samples, list):
            problems.append(f"{where}.memory.system_memory_samples must be a list")
        else:
            for sample_index, sample in enumerate(samples):
                sample_where = f"{where}.memory.system_memory_samples[{sample_index}]"
                if _closed(sample, MEMORY_SAMPLE_KEYS, sample_where, problems):
                    for key in ("elapsed_ms", "available_ram_bytes", "memory_load_percent", "pagefile_used_bytes"):
                        _number(sample[key], f"{sample_where}.{key}", problems, integer=True, maximum=100 if key == "memory_load_percent" else None)
                    _number(sample["system_cpu_percent"], f"{sample_where}.system_cpu_percent", problems, nullable=True, maximum=100)

    gpu = run["gpu"]
    if _closed(gpu, GPU_KEYS, f"{where}.gpu", problems):
        _validate_gpu_snapshot(gpu["pre"], f"{where}.gpu.pre", problems)
        _validate_gpu_snapshot(gpu["post"], f"{where}.gpu.post", problems)
        _number(gpu["during_peak_used_bytes"], f"{where}.gpu.during_peak_used_bytes", problems, integer=True, nullable=True)
        _number(gpu["during_min_free_bytes"], f"{where}.gpu.during_min_free_bytes", problems, integer=True, nullable=True)
        _enum(gpu["sampling_state"], {"available", "unavailable"}, f"{where}.gpu.sampling_state", problems)
        _enum(gpu["sampling_failure_category"], {"", "gpu_probe_unavailable", "gpu_probe_failed", "not_sampled"}, f"{where}.gpu.sampling_failure_category", problems)
        if gpu["sampling_state"] == "available" and (
            gpu["during_peak_used_bytes"] is None or gpu["during_min_free_bytes"] is None
        ):
            problems.append(f"{where}.gpu available sampling requires used and free bytes")
        if gpu["sampling_state"] == "unavailable" and (
            gpu["during_peak_used_bytes"] is not None or gpu["during_min_free_bytes"] is not None
        ):
            problems.append(f"{where}.gpu unavailable sampling must use null bytes")
    disk = run["disk"]
    if _closed(disk, DISK_KEYS, f"{where}.disk", problems):
        _enum(disk["state"], {"available", "unavailable"}, f"{where}.disk.state", problems)
        _number(disk["read_bytes"], f"{where}.disk.read_bytes", problems, integer=True, nullable=True)
        if disk["state"] == "unavailable" and disk["read_bytes"] is not None:
            problems.append(f"{where}.disk unavailable state must use null bytes")
    quality = run["quality"]
    if _closed(quality, QUALITY_KEYS, f"{where}.quality", problems):
        _enum(quality["state"], {"passed", "failed", "not_applicable", "unsupported"}, f"{where}.quality.state", problems)
        _number(quality["assertion_count"], f"{where}.quality.assertion_count", problems, integer=True)
        _number(quality["score"], f"{where}.quality.score", problems, maximum=1)
        answer_hash = quality["deterministic_answer_sha256"]
        if answer_hash is not None and (not isinstance(answer_hash, str) or not SHA256.fullmatch(answer_hash)):
            problems.append(f"{where}.quality.deterministic_answer_sha256 must be sha256 or null")
        _enum(quality["unsupported_category"], {"", "quality_not_supported"}, f"{where}.quality.unsupported_category", problems)
    cancellation = run["cancellation"]
    if _closed(cancellation, CANCELLATION_KEYS, f"{where}.cancellation", problems):
        _bool(cancellation["tested"], f"{where}.cancellation.tested", problems)
        for key in ("request_to_cancel_ms", "client_stream_closed_ms", "runtime_idle_ms", "process_completion_ms"):
            _number(cancellation[key], f"{where}.cancellation.{key}", problems, nullable=True)
        _enum(cancellation["final_state"], {"not_tested", "cancelled", "completed", "timeout", "failed"}, f"{where}.cancellation.final_state", problems)
        _bool(cancellation["resources_released"], f"{where}.cancellation.resources_released", problems, nullable=True)
        _bool(cancellation["runtime_responsive"], f"{where}.cancellation.runtime_responsive", problems, nullable=True)
        required_timing_keys = ("request_to_cancel_ms", "client_stream_closed_ms", "process_completion_ms")
        timing_keys = (*required_timing_keys, "runtime_idle_ms")
        if cancellation["tested"]:
            if cancellation["final_state"] == "not_tested" or any(cancellation[key] is None for key in required_timing_keys):
                problems.append(f"{where}.cancellation tested probe is incomplete")
            if cancellation["resources_released"] is None or cancellation["runtime_responsive"] is None:
                problems.append(f"{where}.cancellation tested probe must record resource and runtime state")
        elif cancellation["final_state"] != "not_tested" or any(cancellation[key] is not None for key in timing_keys):
            problems.append(f"{where}.cancellation untested probe must not carry timings or a final state")
    if run["error_category"] and isinstance(run["quality"], dict) and run["quality"].get("state") == "passed":
        problems.append(f"{where} cannot pass quality while carrying an error")
    if run["error_category"] == "preflight_safety_abort" and not run["preflight_rejection_category"]:
        problems.append(f"{where} preflight abort must identify its rejection category")
    if run["error_category"] != "preflight_safety_abort" and run["preflight_rejection_category"]:
        problems.append(f"{where} non-preflight run must not carry a preflight rejection category")
    if run["evidence_state"] == "complete" and run["truncation_state"] != "complete":
        problems.append(f"{where} complete evidence cannot be truncated")


def _validate_gpu_snapshot(snapshot: Any, where: str, problems: list[str]) -> None:
    if _closed(snapshot, GPU_SNAPSHOT_KEYS, where, problems):
        _enum(snapshot["state"], {"available", "unavailable"}, f"{where}.state", problems)
        _number(snapshot["used_bytes"], f"{where}.used_bytes", problems, integer=True, nullable=True)
        _number(snapshot["free_bytes"], f"{where}.free_bytes", problems, integer=True, nullable=True)
        _number(snapshot["total_bytes"], f"{where}.total_bytes", problems, integer=True, nullable=True)
        if snapshot["state"] == "available" and any(snapshot[key] is None for key in ("used_bytes", "free_bytes", "total_bytes")):
            problems.append(f"{where} available state requires measured bytes")
        if snapshot["state"] == "unavailable" and any(snapshot[key] is not None for key in ("used_bytes", "free_bytes")):
            problems.append(f"{where} unavailable state must not use a numeric measurement")
        if _finite(snapshot["used_bytes"]) and _finite(snapshot["total_bytes"]) and snapshot["used_bytes"] > snapshot["total_bytes"]:
            problems.append(f"{where}.used_bytes must not exceed total_bytes")
        if _finite(snapshot["free_bytes"]) and _finite(snapshot["total_bytes"]) and snapshot["free_bytes"] > snapshot["total_bytes"]:
            problems.append(f"{where}.free_bytes must not exceed total_bytes")


def _validate_options(options: dict[str, Any], where: str, problems: list[str]) -> None:
    for key, value in options.items():
        if key == "think":
            _bool(value, f"{where}.{key}", problems)
        elif key in {"temperature", "top_p"}:
            _number(value, f"{where}.{key}", problems, maximum=1 if key == "top_p" else 2)
        else:
            minimum = 1 if key in {"num_predict", "n_predict", "num_ctx", "num_thread", "num_batch"} else 0
            _number(value, f"{where}.{key}", problems, integer=True, minimum=minimum)
