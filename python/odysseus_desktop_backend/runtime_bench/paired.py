"""Paired-arm execution for hardware-relative uplift measurements.

The module reuses the fixed shapes, loopback HTTP transport, inventory,
sampler and artifact writer from the existing harness.  It applies no
runtime policy: callers choose the two per-request configurations, while
the harness enforces protected fields, balanced order and memory safety.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

from odysseus_desktop_backend.runtime_bench.artifacts import write_artifact
from odysseus_desktop_backend.runtime_bench.harness import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ERROR_CONNECTION,
    ERROR_HTTP,
    ERROR_MALFORMED,
    ERROR_TIMEOUT,
    OLLAMA_ENDPOINT,
    _post_stream,
    ollama_resident_models,
    ollama_unload,
)
from odysseus_desktop_backend.runtime_bench.paired_artifacts import (
    PAIRED_ARTIFACT_SCHEMA_VERSION,
)
from odysseus_desktop_backend.runtime_bench.sampler import (
    ResourceSampler,
    system_memory_status,
    vram_used_bytes,
)
from odysseus_desktop_backend.runtime_bench.shapes import BENCHMARK_SHAPES, quality_check
from odysseus_desktop_backend.services import runtime_inventory as ri

# Exact reviewed PR #35 values. Kept by value so this dev-only harness
# does not import planner policy or create a second safety definition.
RAM_SAFETY_FLOOR_BYTES = 1_536 * 1024 * 1024
RAM_SAFETY_FRACTION = 0.12
VRAM_SAFETY_MARGIN_BYTES = 256 * 1024 * 1024
RUNTIME_OVERHEAD_BYTES = 600 * 1024 * 1024
KV_DTYPE_BYTES = 2
UNKNOWN_KV_BYTES_PER_TOKEN = 512 * 1024

CANCEL_TIMEOUT_SECONDS = 30.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_loopback_endpoint(endpoint: str) -> None:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("paired benchmarks require a loopback HTTP endpoint")


def safety_floor_bytes(total_ram_bytes: int) -> int:
    return max(RAM_SAFETY_FLOOR_BYTES, int(total_ram_bytes * RAM_SAFETY_FRACTION))


def _parameter_count(parameter_size: str) -> int | None:
    match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*B\s*", parameter_size or "", re.I)
    return int(float(match.group(1)) * 1_000_000_000) if match else None


def estimate_required_memory_bytes(model: dict[str, Any], context_limit: int) -> int:
    """Mirror the reviewed weights + architecture KV + overhead estimate."""
    weights = max(0, int(model.get("disk_bytes") or 0))
    geometry = model.get("kv_geometry") or {}
    layers = int(geometry.get("layers") or 0)
    kv_heads = int(geometry.get("kv_heads") or 0)
    key_length = int(geometry.get("key_length") or 0)
    value_length = int(geometry.get("value_length") or 0)
    if all(value > 0 for value in (layers, kv_heads, key_length, value_length)):
        kv = (key_length + value_length) * kv_heads * layers * KV_DTYPE_BYTES * context_limit
    else:
        kv = UNKNOWN_KV_BYTES_PER_TOKEN * context_limit
    return weights + kv + RUNTIME_OVERHEAD_BYTES


def arm_fits_preflight(
    *,
    status: dict[str, int],
    estimated_required_bytes: int,
    resident: dict[str, Any] | None,
    pre_gpu: dict[str, Any],
    options: dict[str, Any],
) -> bool:
    """Apply the reviewed RAM/VRAM budgets to the current arm state."""
    floor = safety_floor_bytes(int(status["total_ram_bytes"]))
    available = int(status["available_ram_bytes"])
    if available < floor:
        return False
    resident_size = int((resident or {}).get("size") or 0)
    resident_vram = int((resident or {}).get("size_vram") or 0)
    gpu_allowed = int(options.get("num_gpu", 1)) != 0
    if gpu_allowed and pre_gpu["state"] == "available":
        total_vram = int(pre_gpu.get("total_bytes") or 0)
        used_vram = int(pre_gpu.get("used_bytes") or 0)
        effective_free_vram = max(0, total_vram - used_vram + resident_vram)
        if estimated_required_bytes <= max(0, effective_free_vram - VRAM_SAFETY_MARGIN_BYTES):
            return True
    effective_available_ram = available + max(0, resident_size - resident_vram)
    return estimated_required_bytes <= max(0, effective_available_ram - floor)


def _model_inventory_entry(tag: str) -> dict[str, Any]:
    inventory = ri.model_inventory(include_details=True)
    for entry in inventory.get("models") or []:
        if entry.get("tag") == tag:
            return dict(entry)
    raise ValueError("requested model is not installed")


def _ollama_show_identity(model: str, endpoint: str, timeout: float = 10.0) -> dict[str, str]:
    try:
        with _post_stream(f"{endpoint}/api/show", {"model": model, "verbose": True}, timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return {"tokenizer": "unavailable", "template": "unavailable"}
    info = data.get("model_info") if isinstance(data.get("model_info"), dict) else {}
    tokenizer = str(info.get("tokenizer.ggml.model") or "unavailable")
    template = data.get("template")
    template_identity = (
        hashlib.sha256(str(template).encode("utf-8")).hexdigest()
        if isinstance(template, str) and template
        else "unavailable"
    )
    return {"tokenizer": tokenizer, "template": template_identity}


def model_descriptor_v2(tag: str, *, endpoint: str = OLLAMA_ENDPOINT) -> tuple[dict[str, Any], dict[str, Any]]:
    entry = _model_inventory_entry(tag)
    identities = _ollama_show_identity(tag, endpoint)
    family = str(entry.get("family") or "").lower()
    architecture = "moe" if "moe" in family else "dense" if family else "unknown"
    total_parameters = _parameter_count(str(entry.get("parameter_size") or ""))
    active_parameters = total_parameters if architecture == "dense" else None
    digest = str(entry.get("digest") or "unavailable").lower()
    descriptor = {
        "tag": tag,
        "digest": digest,
        "file_identity": digest,
        "format": str(entry.get("format") or "unavailable"),
        "quantization": str(entry.get("quantization") or "unavailable"),
        "architecture": architecture,
        "total_parameters": total_parameters,
        "active_parameters": active_parameters,
        "disk_bytes": int(entry.get("disk_bytes") or 0),
        "tokenizer_identity": identities["tokenizer"],
        "chat_template_identity": identities["template"],
    }
    return descriptor, entry


def _gpu_snapshot(smi_path: str | None, total_bytes: int | None) -> dict[str, Any]:
    if not smi_path:
        return {"state": "unavailable", "used_bytes": None, "total_bytes": None}
    used = vram_used_bytes(smi_path)
    if used is None:
        return {"state": "unavailable", "used_bytes": None, "total_bytes": total_bytes}
    return {"state": "available", "used_bytes": used, "total_bytes": total_bytes}


def _placement_from_residency(residency: dict[str, Any] | None) -> dict[str, str]:
    if not residency:
        return {"state": "unavailable", "cpu": "unknown", "gpu": "unknown"}
    size = int(residency.get("size") or 0)
    size_vram = int(residency.get("size_vram") or 0)
    if size <= 0:
        return {"state": "unavailable", "cpu": "unknown", "gpu": "unknown"}
    if size_vram <= 0:
        return {"state": "recorded", "cpu": "used", "gpu": "none"}
    if size_vram >= size:
        return {"state": "recorded", "cpu": "unused", "gpu": "full"}
    return {"state": "recorded", "cpu": "used", "gpu": "partial"}


def _resident_model(model: str, endpoint: str) -> dict[str, Any] | None:
    for item in ollama_resident_models(endpoint=endpoint):
        if str(item.get("name") or item.get("model") or "") == model:
            return item
    return None


def _ollama_ps_probe(endpoint: str) -> list[dict[str, Any]] | None:
    try:
        request = urllib.request.Request(f"{endpoint}/api/ps", method="GET")
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310 - loopback only
            decoded = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return None
    models = decoded.get("models")
    return [item for item in models if isinstance(item, dict)] if isinstance(models, list) else None


def _empty_cancellation() -> dict[str, Any]:
    return {
        "tested": False,
        "request_to_cancel_ms": None,
        "cancel_acknowledgement_ms": None,
        "cancel_latency_ms": None,
        "process_completion_ms": None,
        "final_state": "not_tested",
        "resources_released": None,
        "runtime_responsive": None,
    }


def _preflight_abort_run(
    *,
    run_index: int,
    repetition_index: int,
    execution_order: int,
    cold: bool,
    elapsed_ms: float | None,
    options: dict[str, Any],
    status: dict[str, int],
    floor: int,
    gpu_snapshot: dict[str, Any],
) -> dict[str, Any]:
    available = int(status["available_ram_bytes"])
    result = {
        "run_index": run_index,
        "repetition_index": repetition_index,
        "execution_order": execution_order,
        "captured_at": _now_iso(),
        "cold": cold,
        "elapsed_since_previous_arm_ms": round(elapsed_ms, 1) if elapsed_ms is not None else None,
        "pre_arm": {
            "available_ram_bytes": available,
            "gpu_snapshot": gpu_snapshot,
            "interference": {
                "state": "available",
                "system_cpu_percent": None,
                "memory_load_percent": int(status["memory_load_percent"]),
                "detected": False,
            },
        },
        "options": dict(options),
        "timings_ms": {"total": None, "load": None, "prompt_eval": None, "generation": None, "first_token": None},
        "tokens": {"prompt": 0, "generated": 0, "prompt_tps": None, "generation_tps": None},
        "memory": {
            "total_ram_bytes": int(status["total_ram_bytes"]),
            "available_ram_before_bytes": available,
            "min_available_ram_bytes": available,
            "process_peak_rss_bytes": 0,
            "pagefile_used_peak_bytes": max(0, int(status["pagefile_total_bytes"]) - int(status["pagefile_available_bytes"])),
            "sampler_interval_ms": 250,
            "sample_count": 0,
            "sampling_state": "available",
            "sampling_failure_category": "",
            "system_memory_samples": [],
            "safety_floor_bytes": floor,
            "safety_floor_crossed": False,
        },
        "gpu": {
            "pre": gpu_snapshot,
            "during_peak_used_bytes": None,
            "post": gpu_snapshot,
            "sampling_state": gpu_snapshot["state"],
            "sampling_failure_category": "" if gpu_snapshot["state"] == "available" else "gpu_probe_unavailable",
        },
        "disk": {"state": "unavailable", "read_bytes": None},
        "cache_state": "cold" if cold else "warm",
        "placement_state": "unavailable",
        "quality": {
            "state": "not_applicable", "assertion_count": 0, "score": 0,
            "deterministic_answer_sha256": None, "unsupported_category": "",
        },
        "cancellation": _empty_cancellation(),
        "error_category": "preflight_safety_abort",
        "truncation_state": "incomplete_evidence",
        "evidence_state": "incomplete",
    }
    return result


def execute_ollama_arm(
    *,
    model: str,
    shape: str,
    options: dict[str, Any],
    endpoint: str,
    timeout: float,
    run_index: int,
    repetition_index: int,
    execution_order: int,
    cold: bool,
    elapsed_since_previous_arm_ms: float | None,
    safety_floor: int,
    total_ram_bytes: int,
    cancel_probe: bool,
    cancel_after_ms: int,
    smi_path: str | None,
    gpu_total_bytes: int | None,
) -> dict[str, Any]:
    """Run one arm, closing the stream on safety breach or cancel probe."""
    require_loopback_endpoint(endpoint)
    spec = BENCHMARK_SHAPES[shape]
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": spec["prompt"]}],
        "stream": True,
        "options": dict(options),
    }
    status = system_memory_status()
    if status is None:
        raise RuntimeError("system memory probe unavailable")
    pre_gpu = _gpu_snapshot(smi_path, gpu_total_bytes)
    response_holder: dict[str, Any] = {"response": None}
    cancel_issued = threading.Event()
    cancel_issued_ms: list[float] = []
    safety_cancel = threading.Event()
    started = time.perf_counter()

    def close_response(*, safety: bool = False) -> None:
        if safety:
            safety_cancel.set()
        if not cancel_issued.is_set():
            cancel_issued_ms.append((time.perf_counter() - started) * 1000)
            cancel_issued.set()
        response = response_holder.get("response")
        if response is not None:
            try:
                response.close()
            except (OSError, ValueError):
                pass

    timer: threading.Timer | None = None
    if cancel_probe:
        timer = threading.Timer(cancel_after_ms / 1000, close_response)
        timer.daemon = True
        timer.start()

    content_parts: list[str] = []
    final_stats: dict[str, Any] = {}
    first_token_ms: float | None = None
    error = ""
    acknowledgement_ms: float | None = None
    with ResourceSampler(
        exe_substring="llama",
        smi_path=smi_path,
        safety_floor_bytes=safety_floor,
        on_safety_floor=lambda: close_response(safety=True),
    ) as sampler:
        try:
            with _post_stream(f"{endpoint}/api/chat", payload, timeout) as response:
                response_holder["response"] = response
                if cancel_issued.is_set():
                    response.close()
                else:
                    for raw_line in response:
                        line = raw_line.decode("utf-8", errors="replace").strip()
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                        except json.JSONDecodeError:
                            error = ERROR_MALFORMED
                            break
                        piece = str((chunk.get("message") or {}).get("content") or "")
                        if piece and first_token_ms is None:
                            first_token_ms = (time.perf_counter() - started) * 1000
                        if piece:
                            content_parts.append(piece)
                        if chunk.get("done"):
                            final_stats = chunk
                            break
        except TimeoutError:
            error = ERROR_TIMEOUT
        except urllib.error.HTTPError:
            error = ERROR_HTTP
        except (urllib.error.URLError, OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None)
            if not cancel_issued.is_set():
                error = ERROR_TIMEOUT if isinstance(reason, TimeoutError) else ERROR_CONNECTION
        finally:
            if cancel_issued.is_set():
                acknowledgement_ms = (time.perf_counter() - started) * 1000
            response_holder["response"] = None
            if timer is not None:
                timer.cancel()
    total_ms = (time.perf_counter() - started) * 1000

    cancellation = _empty_cancellation()
    if cancel_issued.is_set():
        before_unload = _ollama_ps_probe(endpoint)
        runtime_responsive = before_unload is not None
        resources_released = False
        if cancel_probe or safety_cancel.is_set():
            unload_ok = ollama_unload(model, endpoint=endpoint, timeout=min(CANCEL_TIMEOUT_SECONDS, timeout))
            after_unload = _ollama_ps_probe(endpoint)
            resources_released = bool(
                unload_ok
                and after_unload is not None
                and not any(str(item.get("name") or item.get("model") or "") == model for item in after_unload)
            )
        process_completion_ms = (time.perf_counter() - started) * 1000
        acknowledgement = acknowledgement_ms or process_completion_ms
        cancellation = {
            "tested": True,
            "request_to_cancel_ms": round(cancel_issued_ms[0], 1),
            "cancel_acknowledgement_ms": round(acknowledgement, 1),
            "cancel_latency_ms": round(max(0, acknowledgement - cancel_issued_ms[0]), 1),
            "process_completion_ms": round(process_completion_ms, 1),
            "final_state": "cancelled" if resources_released else "timeout",
            "resources_released": resources_released,
            "runtime_responsive": runtime_responsive,
        }
        error = "safety_abort" if safety_cancel.is_set() else "cancelled"

    memory_data = sampler.to_v2_dict()
    samples = memory_data["system_memory_samples"]
    pagefile_peak = memory_data["pagefile_used_peak_bytes"]
    post_gpu = _gpu_snapshot(smi_path, gpu_total_bytes)
    prompt_tokens = int(final_stats.get("prompt_eval_count") or 0)
    generated = int(final_stats.get("eval_count") or 0)
    prompt_ns = int(final_stats.get("prompt_eval_duration") or 0)
    generation_ns = int(final_stats.get("eval_duration") or 0)
    output = "".join(content_parts)
    quality_state = quality_check(shape, output) if not error else "not_applicable"
    context_limit = int(options["num_ctx"])
    truncation = "truncated_prompt" if prompt_tokens and prompt_tokens >= max(1, context_limit - 8) else "complete"
    if not final_stats and error:
        truncation = "incomplete_evidence"
    residency = _resident_model(model, endpoint)
    placement = _placement_from_residency(residency)
    disk_read = memory_data["disk_read_bytes"]
    result = {
        "run_index": run_index,
        "repetition_index": repetition_index,
        "execution_order": execution_order,
        "captured_at": _now_iso(),
        "cold": cold,
        "elapsed_since_previous_arm_ms": round(elapsed_since_previous_arm_ms, 1) if elapsed_since_previous_arm_ms is not None else None,
        "pre_arm": {
            "available_ram_bytes": int(status["available_ram_bytes"]),
            "gpu_snapshot": pre_gpu,
            "interference": {
                "state": "available",
                "system_cpu_percent": None,
                "memory_load_percent": int(status["memory_load_percent"]),
                "detected": False,
            },
        },
        "options": dict(options),
        "timings_ms": {
            "total": round(total_ms, 1),
            "load": round(int(final_stats.get("load_duration") or 0) / 1e6, 1) if final_stats else None,
            "prompt_eval": round(prompt_ns / 1e6, 1) if prompt_ns else None,
            "generation": round(generation_ns / 1e6, 1) if generation_ns else None,
            "first_token": round(first_token_ms, 1) if first_token_ms is not None else None,
        },
        "tokens": {
            "prompt": prompt_tokens,
            "generated": generated,
            "prompt_tps": round(prompt_tokens / (prompt_ns / 1e9), 2) if prompt_tokens and prompt_ns else None,
            "generation_tps": round(generated / (generation_ns / 1e9), 2) if generated and generation_ns else None,
        },
        "memory": {
            "total_ram_bytes": total_ram_bytes,
            "available_ram_before_bytes": int(status["available_ram_bytes"]),
            "min_available_ram_bytes": int(memory_data["system_min_available_bytes"]),
            "process_peak_rss_bytes": int(memory_data["runtime_peak_rss_bytes"]),
            "pagefile_used_peak_bytes": pagefile_peak,
            "sampler_interval_ms": int(memory_data["sampler_interval_ms"]),
            "sample_count": int(memory_data["samples"]),
            "sampling_state": "available" if memory_data["sampling_available"] else "unavailable",
            "sampling_failure_category": str(memory_data["sampling_failure_category"]),
            "system_memory_samples": samples,
            "safety_floor_bytes": safety_floor,
            "safety_floor_crossed": bool(memory_data["safety_floor_crossed"]),
        },
        "gpu": {
            "pre": pre_gpu,
            "during_peak_used_bytes": memory_data["vram_peak_used_bytes"],
            "post": post_gpu,
            "sampling_state": "available" if memory_data["vram_peak_used_bytes"] is not None else "unavailable",
            "sampling_failure_category": "" if memory_data["vram_peak_used_bytes"] is not None else "gpu_probe_unavailable",
        },
        "disk": {"state": "available" if disk_read is not None else "unavailable", "read_bytes": disk_read},
        "cache_state": "cold" if cold else "warm",
        "placement_state": placement["state"],
        "quality": {
            "state": quality_state,
            "assertion_count": 1 if quality_state in {"passed", "failed"} else 0,
            "score": 1 if quality_state == "passed" else 0,
            "deterministic_answer_sha256": hashlib.sha256(output.encode("utf-8")).hexdigest() if output and not error else None,
            "unsupported_category": "",
        },
        "cancellation": cancellation,
        "error_category": error,
        "truncation_state": truncation,
        "evidence_state": "complete" if not error and truncation == "complete" else "incomplete",
    }
    result["_placement"] = placement
    return result


def balanced_execution_plan(repeats: int, include_cold: bool = True) -> list[tuple[str, int, bool]]:
    """Return arm role, repetition index and cold state in balanced order."""
    if repeats < 3:
        raise ValueError("paired benchmark requires at least three warm repetitions")
    plan: list[tuple[str, int, bool]] = []
    if include_cold:
        plan.extend([("baseline", 0, True), ("candidate", 0, True)])
    for repetition in range(repeats):
        roles = ("baseline", "candidate") if repetition % 2 == 0 else ("candidate", "baseline")
        plan.extend((role, repetition, False) for role in roles)
    return plan


def run_paired_ollama_batch(
    *,
    model: str,
    shape: str,
    experiment_id: str,
    pair_id: str,
    batch_id: str,
    artifact_dir: str,
    baseline_options: dict[str, Any] | None = None,
    candidate_options: dict[str, Any] | None = None,
    context_limit: int = 4096,
    repeats: int = 3,
    include_cold: bool = True,
    cancel_probe: bool = False,
    cancel_after_ms: int = 500,
    endpoint: str = OLLAMA_ENDPOINT,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    execute_arm: Callable[..., dict[str, Any]] = execute_ollama_arm,
) -> dict[str, Any]:
    """Execute and persist two arm artifacts; return evidence and abort state."""
    require_loopback_endpoint(endpoint)
    spec = BENCHMARK_SHAPES[shape]
    protected = {
        "temperature": 0,
        "seed": 42,
        "top_p": 1,
        "top_k": 0,
        "num_predict": int(spec["num_predict"]),
        "num_ctx": int(context_limit),
    }
    arms = {}
    for role, supplied in (("baseline", baseline_options), ("candidate", candidate_options)):
        supplied = dict(supplied or {})
        for key, value in protected.items():
            if key in supplied and supplied[key] != value:
                raise ValueError(f"{key} is protected and must be identical across arms")
        arms[role] = {**protected, **supplied}
    if arms["baseline"].get("think") != arms["candidate"].get("think"):
        raise ValueError("think is protected and must be identical across arms")

    descriptor, inventory_entry = model_descriptor_v2(model, endpoint=endpoint)
    hardware = ri.hardware_inventory()
    hardware.setdefault("errors", {})
    smi_path = ri._nvidia_smi_path()
    if smi_path and not hardware.get("gpus"):
        raise ValueError("v2 hardware snapshot missing GPU while GPU probe is available")
    gpu_total = int((hardware.get("gpus") or [{}])[0].get("vram_total_bytes") or 0) or None
    status = system_memory_status()
    if status is None:
        raise RuntimeError("system memory probe unavailable")
    total_ram = int(status["total_ram_bytes"])
    floor = safety_floor_bytes(total_ram)
    estimated_required = estimate_required_memory_bytes(inventory_entry, context_limit)
    version = ri.detect_ollama_runtime()["version"]
    fixture_sha = hashlib.sha256(str(spec["prompt"]).encode("utf-8")).hexdigest()
    plan = balanced_execution_plan(repeats, include_cold)
    if cancel_probe:
        # Probes are additional cold-known runs after all performance
        # repetitions, so cancellation/unload cannot contaminate warm data.
        plan.extend([("baseline", repeats, True), ("candidate", repeats, True)])
    runs: dict[str, list[dict[str, Any]]] = {"baseline": [], "candidate": []}
    observed_placements: dict[str, list[dict[str, str]]] = {"baseline": [], "candidate": []}
    cancelled_role: set[str] = set()
    aborted = False
    last_completed = None

    for execution_order, (role, repetition, cold) in enumerate(plan):
        if aborted:
            break
        pre = system_memory_status()
        if pre is None:
            raise RuntimeError("system memory probe unavailable")
        elapsed_ms = (time.perf_counter() - last_completed) * 1000 if last_completed is not None else None
        run_index = len(runs[role])
        gpu_pre = _gpu_snapshot(smi_path, gpu_total)
        resident = _resident_model(model, endpoint)
        if not arm_fits_preflight(
            status=pre,
            estimated_required_bytes=estimated_required,
            resident=resident,
            pre_gpu=gpu_pre,
            options=arms[role],
        ):
            runs[role].append(
                _preflight_abort_run(
                    run_index=run_index,
                    repetition_index=repetition,
                    execution_order=execution_order,
                    cold=cold,
                    elapsed_ms=elapsed_ms,
                    options=arms[role],
                    status=pre,
                    floor=floor,
                    gpu_snapshot=gpu_pre,
                )
            )
            aborted = True
            break
        if cold:
            ollama_unload(model, endpoint=endpoint, timeout=min(30.0, timeout))
            pre = system_memory_status()
            if pre is None:
                raise RuntimeError("system memory probe unavailable")
            post_unload_gpu = _gpu_snapshot(smi_path, gpu_total)
            if not arm_fits_preflight(
                status=pre,
                estimated_required_bytes=estimated_required,
                resident=None,
                pre_gpu=post_unload_gpu,
                options=arms[role],
            ):
                runs[role].append(
                    _preflight_abort_run(
                        run_index=run_index,
                        repetition_index=repetition,
                        execution_order=execution_order,
                        cold=cold,
                        elapsed_ms=elapsed_ms,
                        options=arms[role],
                        status=pre,
                        floor=floor,
                        gpu_snapshot=post_unload_gpu,
                    )
                )
                aborted = True
                break
        do_cancel = cancel_probe and repetition == repeats and role not in cancelled_role
        run = execute_arm(
            model=model,
            shape=shape,
            options=arms[role],
            endpoint=endpoint,
            timeout=timeout,
            run_index=run_index,
            repetition_index=repetition,
            execution_order=execution_order,
            cold=cold,
            elapsed_since_previous_arm_ms=elapsed_ms,
            safety_floor=floor,
            total_ram_bytes=total_ram,
            cancel_probe=do_cancel,
            cancel_after_ms=cancel_after_ms,
            smi_path=smi_path,
            gpu_total_bytes=gpu_total,
        )
        observed = run.pop("_placement", None)
        if isinstance(observed, dict):
            observed_placements[role].append(observed)
        if do_cancel:
            cancelled_role.add(role)
        runs[role].append(run)
        last_completed = time.perf_counter()
        if run["error_category"] == "safety_abort":
            aborted = True

    artifacts: list[dict[str, Any]] = []
    for role in ("baseline", "candidate"):
        role_runs = runs[role]
        if not role_runs:
            continue
        placements = {run["placement_state"] for run in role_runs}
        placement = {"state": "recorded", "cpu": "unknown", "gpu": "unknown"}
        role_observations = observed_placements[role]
        if role_observations and all(item == role_observations[0] for item in role_observations):
            placement = dict(role_observations[0])
        elif placements != {"recorded"}:
            placement["state"] = "unavailable"
        artifact = {
            "schema_version": PAIRED_ARTIFACT_SCHEMA_VERSION,
            "batch_id": f"{batch_id}-{role}",
            "captured_at": _now_iso(),
            "experiment_id": experiment_id,
            "pair_id": pair_id,
            "arm_id": f"{pair_id}-{role}",
            "arm_role": role,
            "hardware": hardware,
            "runtime": {"name": "ollama", "version": version, "server_env": {}, "backend_options": arms[role]},
            "model": descriptor,
            "fixture": {
                "identity": f"runtime-bench-{shape}-v1",
                "sha256": fixture_sha,
                "task_requirement_id": f"{shape}-task-v1",
                "quality_criteria_id": f"{shape}-quality-v1",
            },
            "requirements": {
                "context_limit": context_limit,
                "output_token_limit": int(spec["num_predict"]),
                "sampling": {"temperature": 0, "seed": 42, "top_p": 1, "top_k": 0},
            },
            "placement": placement,
            "shape": shape,
            "mode": "interactive",
            "engine_kind": "real",
            "file_cache_state": "unknown_warmish",
            "runs": role_runs,
        }
        write_artifact(artifact, artifact_dir)
        artifacts.append(artifact)
    return {"artifacts": artifacts, "aborted": aborted}
