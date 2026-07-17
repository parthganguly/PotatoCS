"""Execution planner: hardware + models + evidence -> versioned plan.

Pure computation: no I/O, no settings mutation, no network. Consumes the
inventories from `runtime_inventory` plus optional benchmark evidence
summaries and returns a deterministic, fail-safe execution plan (RFC
section 6). The planner recommends; it never applies.
"""

from __future__ import annotations

import re
from typing import Any

PLAN_VERSION = 1

OBJECTIVES = ("fast", "balanced", "deep")

# Safety margins (RFC section 6) — named constants, not magic numbers.
RAM_SAFETY_FLOOR_BYTES = 1_536 * 1024 * 1024  # leave at least 1.5 GB of RAM
RAM_SAFETY_FRACTION = 0.12                    # or 12% of total, whichever is larger
VRAM_SAFETY_MARGIN_BYTES = 256 * 1024 * 1024  # leave 256 MB of VRAM
KV_CACHE_BYTES_PER_TOKEN_PER_B_PARAMS = 24    # f16 KV heuristic, conservative
RUNTIME_OVERHEAD_BYTES = 600 * 1024 * 1024    # measured Ollama server overhead class
EVIDENCE_MAX_AGE_DAYS = 30

# Interactive-class thresholds (RFC section 9) — provisional pending
# P1/P2-tier measurements; recorded in every plan.
INTERACTIVE_TTFT_MS = 10_000
INTERACTIVE_MIN_TPS = 5.0
SLOW_INTERACTIVE_TTFT_MS = 60_000
SLOW_INTERACTIVE_MIN_TPS = 1.0

# Objective-specific context defaults: bounded context is a reach lever.
CONTEXT_BY_OBJECTIVE = {"fast": 4096, "balanced": 8192, "deep": 8192}

WARN_NO_GPU_PROBE = "no_gpu_probe"
WARN_STALE_EVIDENCE = "stale_evidence"
WARN_NO_EVIDENCE = "no_benchmark_evidence"
WARN_LOW_RAM_HEADROOM = "low_ram_headroom"
WARN_SLOW_CLASS = "expect_slow_generation"
WARN_UNKNOWN_HARDWARE = "unknown_hardware_conservative"

REJECT_EXCEEDS_RAM = "exceeds_ram_margin"
REJECT_EXCEEDS_DEEP = "exceeds_deep_envelope"
REJECT_NOT_TEXT_MODEL = "not_a_text_generation_model"
REJECT_RUNTIME_UNAVAILABLE = "runtime_unavailable"
REJECT_SLOWER_ALTERNATIVE = "slower_than_selected"
REJECT_LESS_CAPABLE = "less_capable_than_selected"


def _parse_param_billions(parameter_size: str) -> float:
    """'8.2B' -> 8.2, '1.2B' -> 1.2; 0.0 when unknown."""
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*B\s*$", str(parameter_size or ""), re.IGNORECASE)
    return float(match.group(1)) if match else 0.0


def estimate_memory_bytes(model: dict[str, Any], context_tokens: int) -> dict[str, int]:
    """Conservative estimate: weights (disk bytes ~ quantized weights) +
    KV cache + runtime overhead. Unknown sizes estimate high, never low."""
    weights = int(model.get("disk_bytes") or 0)
    params_b = _parse_param_billions(model.get("parameter_size") or "")
    if weights <= 0 and params_b > 0:
        # No disk size: assume 8-bit quantization ~ 1.05 GB per B params.
        weights = int(params_b * 1.05 * 1024**3)
    if params_b <= 0 and weights > 0:
        # No parameter count: infer from weights assuming 4-bit (~0.6 GB/B),
        # which overestimates params and therefore overestimates KV.
        params_b = weights / (0.6 * 1024**3)
    kv_cache = int(context_tokens * params_b * KV_CACHE_BYTES_PER_TOKEN_PER_B_PARAMS)
    total = weights + kv_cache + RUNTIME_OVERHEAD_BYTES
    return {"weights": weights, "kv_cache": kv_cache, "total": total}


def _ram_budget(hardware: dict[str, Any]) -> int:
    ram = hardware.get("ram") or {}
    total = int(ram.get("total_bytes") or 0)
    available = int(ram.get("available_bytes") or 0)
    if total <= 0 or available <= 0:
        return 0
    margin = max(RAM_SAFETY_FLOOR_BYTES, int(total * RAM_SAFETY_FRACTION))
    return max(0, available - margin)


def _vram_budget(hardware: dict[str, Any]) -> int:
    gpus = hardware.get("gpus") or []
    if not gpus:
        return 0
    best = max(int(gpu.get("vram_free_bytes") or 0) for gpu in gpus)
    return max(0, best - VRAM_SAFETY_MARGIN_BYTES)


def _is_text_model(model: dict[str, Any]) -> bool:
    capabilities = [str(cap).lower() for cap in model.get("capabilities") or []]
    if capabilities:
        return "completion" in capabilities or "chat" in capabilities
    tag = str(model.get("tag") or "").lower()
    return "embed" not in tag


def _evidence_for(
    evidence: list[dict[str, Any]] | None,
    model_tag: str,
    hardware: dict[str, Any],
    now_ms: int,
) -> tuple[dict[str, Any] | None, bool]:
    """Return (matching evidence summary, stale flag)."""
    if not evidence:
        return None, False
    hw_cpu = (hardware.get("cpu") or {}).get("logical_threads")
    for item in evidence:
        if str(item.get("model_tag") or "") != model_tag:
            continue
        stale = False
        captured = int(item.get("captured_at_ms") or 0)
        if captured and now_ms and now_ms - captured > EVIDENCE_MAX_AGE_DAYS * 86_400_000:
            stale = True
        evidence_threads = (item.get("hardware_cpu") or {}).get("logical_threads")
        if evidence_threads is not None and hw_cpu is not None and evidence_threads != hw_cpu:
            stale = True
        return item, stale
    return None, False


def classify_execution(ttft_ms: float | None, tps: float | None) -> str:
    if ttft_ms is None or tps is None:
        return "persisted_job"
    if ttft_ms <= INTERACTIVE_TTFT_MS and tps >= INTERACTIVE_MIN_TPS:
        return "interactive"
    if ttft_ms <= SLOW_INTERACTIVE_TTFT_MS and tps >= SLOW_INTERACTIVE_MIN_TPS:
        return "slow_interactive"
    return "persisted_job"


def _estimate_speed(
    model: dict[str, Any],
    fit: str,
    evidence_summary: dict[str, Any] | None,
) -> tuple[float | None, float | None, str]:
    """Return (ttft_ms, generation_tps, confidence_source)."""
    if evidence_summary:
        ttft = evidence_summary.get("warm_ttft_ms")
        tps = evidence_summary.get("generation_tps")
        if ttft is not None and tps is not None:
            return float(ttft), float(tps), "measured"
    params_b = _parse_param_billions(model.get("parameter_size") or "")
    if params_b <= 0:
        return None, None, "conservative_default"
    # Derived heuristics from this machine's measured class (P3): scale
    # inversely with parameter count; CPU-only is ~4x slower than GPU-resident.
    base_tps = 120.0 / max(params_b, 0.5)
    if fit == "fits_cpu_ram":
        base_tps /= 4
    elif fit == "fits_gpu_partial":
        base_tps /= 2
    ttft = 500.0 + params_b * 250.0
    return ttft, round(base_tps, 1), "derived"


def classify_fit(
    model: dict[str, Any],
    hardware: dict[str, Any],
    context_tokens: int,
) -> tuple[str, dict[str, int]]:
    estimate = estimate_memory_bytes(model, context_tokens)
    vram_budget = _vram_budget(hardware)
    ram_budget = _ram_budget(hardware)
    total = estimate["total"]
    if vram_budget and total <= vram_budget:
        return "fits_gpu_full", estimate
    if ram_budget and total <= ram_budget:
        # Weights beyond VRAM spill to RAM: partial offload when a GPU
        # exists, CPU-only otherwise.
        if vram_budget > 0:
            return "fits_gpu_partial", estimate
        return "fits_cpu_ram", estimate
    # Deep envelope: total RAM (not just available) is the honest ceiling
    # for a persisted job on an otherwise-idle machine, still with margin.
    ram_total = int((hardware.get("ram") or {}).get("total_bytes") or 0)
    deep_budget = max(0, ram_total - max(RAM_SAFETY_FLOOR_BYTES, int(ram_total * RAM_SAFETY_FRACTION)))
    if deep_budget and total <= deep_budget:
        return "reachable_deep_local", estimate
    return "not_runnable", estimate


def build_plan(
    *,
    objective: str,
    hardware: dict[str, Any],
    models: list[dict[str, Any]],
    runtimes: list[dict[str, Any]],
    evidence: list[dict[str, Any]] | None = None,
    now_ms: int = 0,
) -> dict[str, Any]:
    """Produce a versioned execution plan. Deterministic and side-effect free."""
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {OBJECTIVES}")

    warnings: list[str] = []
    rejected: list[dict[str, Any]] = []

    ollama = next((runtime for runtime in runtimes if runtime.get("name") == "ollama"), None)
    runtime_available = bool(ollama and (ollama.get("healthy") or ollama.get("reachable")))
    if not runtime_available:
        return {
            "plan_version": PLAN_VERSION,
            "objective": objective,
            "runtime": None,
            "model": None,
            "execution_class": None,
            "options": {},
            "server_env": {},
            "estimates": {},
            "confidence": "conservative_default",
            "warnings": ["runtime_unavailable"],
            "rejected_alternatives": [
                {"model": str(model.get("tag") or ""), "reason_code": REJECT_RUNTIME_UNAVAILABLE, "detail_numbers": {}}
                for model in sorted(models, key=lambda item: str(item.get("tag") or ""))
            ],
            "evidence": {"benchmark_batch_ids": [], "stale": False},
            "thresholds": _threshold_record(),
        }

    hw_known = bool((hardware.get("ram") or {}).get("total_bytes"))
    if not hw_known:
        warnings.append(WARN_UNKNOWN_HARDWARE)
    if not hardware.get("gpus"):
        warnings.append(WARN_NO_GPU_PROBE)

    context_tokens = CONTEXT_BY_OBJECTIVE[objective]
    physical_cores = int((hardware.get("cpu") or {}).get("physical_cores") or 0)

    candidates: list[dict[str, Any]] = []
    for model in models:
        tag = str(model.get("tag") or "")
        if not _is_text_model(model):
            rejected.append({"model": tag, "reason_code": REJECT_NOT_TEXT_MODEL, "detail_numbers": {}})
            continue
        fit, estimate = classify_fit(model, hardware, context_tokens)
        if fit == "not_runnable":
            rejected.append(
                {
                    "model": tag,
                    "reason_code": REJECT_EXCEEDS_DEEP,
                    "detail_numbers": {
                        "estimated_total_bytes": estimate["total"],
                        "ram_total_bytes": int((hardware.get("ram") or {}).get("total_bytes") or 0),
                    },
                }
            )
            continue
        if fit == "reachable_deep_local" and objective != "deep":
            rejected.append(
                {
                    "model": tag,
                    "reason_code": REJECT_EXCEEDS_RAM,
                    "detail_numbers": {
                        "estimated_total_bytes": estimate["total"],
                        "ram_budget_bytes": _ram_budget(hardware),
                    },
                }
            )
            continue
        evidence_summary, stale = _evidence_for(evidence, tag, hardware, now_ms)
        ttft, tps, source = _estimate_speed(model, fit, evidence_summary if not stale else None)
        candidates.append(
            {
                "model": model,
                "tag": tag,
                "fit": fit,
                "estimate": estimate,
                "ttft_ms": ttft,
                "tps": tps,
                "confidence_source": source,
                "evidence_summary": evidence_summary,
                "evidence_stale": stale,
                "params_b": _parse_param_billions(model.get("parameter_size") or ""),
            }
        )

    if not candidates:
        return {
            "plan_version": PLAN_VERSION,
            "objective": objective,
            "runtime": {"name": "ollama", "version": str(ollama.get("version") or "")},
            "model": None,
            "execution_class": None,
            "options": {},
            "server_env": {},
            "estimates": {},
            "confidence": "conservative_default",
            "warnings": warnings + ["no_eligible_model"],
            "rejected_alternatives": sorted(rejected, key=lambda item: item["model"]),
            "evidence": {"benchmark_batch_ids": [], "stale": False},
            "thresholds": _threshold_record(),
        }

    # Deterministic objective-driven selection.
    def fast_key(candidate: dict[str, Any]) -> tuple:
        return (-(candidate["tps"] or 0.0), candidate["params_b"], candidate["tag"])

    def capability_key(candidate: dict[str, Any]) -> tuple:
        return (-candidate["params_b"], -(candidate["tps"] or 0.0), candidate["tag"])

    if objective == "fast":
        interactive = [candidate for candidate in candidates if classify_execution(candidate["ttft_ms"], candidate["tps"]) == "interactive"]
        pool = interactive or candidates
        pool.sort(key=fast_key)
    elif objective == "balanced":
        usable = [
            candidate
            for candidate in candidates
            if classify_execution(candidate["ttft_ms"], candidate["tps"]) in {"interactive", "slow_interactive"}
        ]
        pool = usable or candidates
        pool.sort(key=capability_key)
    else:  # deep
        pool = sorted(candidates, key=capability_key)

    selected = pool[0]
    for candidate in candidates:
        if candidate is selected:
            continue
        reason = REJECT_SLOWER_ALTERNATIVE if objective == "fast" else REJECT_LESS_CAPABLE
        rejected.append(
            {
                "model": candidate["tag"],
                "reason_code": reason,
                "detail_numbers": {
                    "estimated_tps": candidate["tps"],
                    "parameter_billions": candidate["params_b"],
                },
            }
        )

    execution_class = classify_execution(selected["ttft_ms"], selected["tps"])
    if execution_class != "interactive":
        warnings.append(WARN_SLOW_CLASS)

    vram_budget = _vram_budget(hardware)
    estimate = selected["estimate"]
    options: dict[str, Any] = {"num_ctx": context_tokens}
    if physical_cores >= 4:
        options["num_thread"] = physical_cores
    if selected["fit"] == "fits_gpu_full":
        vram_estimate = min(estimate["total"], vram_budget)
    elif selected["fit"] == "fits_gpu_partial" and vram_budget > 0:
        vram_estimate = vram_budget
    else:
        vram_estimate = 0
        if vram_budget == 0 and hardware.get("gpus"):
            pass  # GPU present but no free budget: leave offload to runtime default
    # num_gpu stays unset (runtime default) unless evidence-backed rules
    # from Phase 4 say otherwise; unsupported/unproven flags are omitted.

    ram_headroom = _ram_budget(hardware) - (estimate["total"] - vram_estimate)
    if 0 <= ram_headroom < 1024**3:
        warnings.append(WARN_LOW_RAM_HEADROOM)

    if selected["evidence_stale"]:
        warnings.append(WARN_STALE_EVIDENCE)
    if selected["evidence_summary"] is None:
        warnings.append(WARN_NO_EVIDENCE)

    confidence = selected["confidence_source"]
    if confidence == "measured" and selected["evidence_stale"]:
        confidence = "derived"
    if not hw_known:
        confidence = "conservative_default"

    batch_ids = []
    if selected["evidence_summary"]:
        batch_ids = [str(bid) for bid in selected["evidence_summary"].get("batch_ids") or []]

    return {
        "plan_version": PLAN_VERSION,
        "objective": objective,
        "runtime": {"name": "ollama", "version": str(ollama.get("version") or "")},
        "model": {
            "tag": selected["tag"],
            "quantization": str(selected["model"].get("quantization") or ""),
            "parameter_size": str(selected["model"].get("parameter_size") or ""),
            "disk_bytes": int(selected["model"].get("disk_bytes") or 0),
        },
        "fit_class": selected["fit"],
        "execution_class": execution_class,
        "options": options,
        "server_env": {},
        "estimates": {
            "ram_bytes": max(0, estimate["total"] - vram_estimate),
            "vram_bytes": vram_estimate,
            "disk_working_set_bytes": estimate["weights"],
            "ttft_ms": selected["ttft_ms"],
            "generation_tps": selected["tps"],
        },
        "confidence": confidence,
        "warnings": sorted(set(warnings)),
        "rejected_alternatives": sorted(rejected, key=lambda item: item["model"]),
        "evidence": {"benchmark_batch_ids": batch_ids, "stale": selected["evidence_stale"]},
        "thresholds": _threshold_record(),
    }


def _threshold_record() -> dict[str, Any]:
    return {
        "interactive_ttft_ms": INTERACTIVE_TTFT_MS,
        "interactive_min_tps": INTERACTIVE_MIN_TPS,
        "slow_interactive_ttft_ms": SLOW_INTERACTIVE_TTFT_MS,
        "slow_interactive_min_tps": SLOW_INTERACTIVE_MIN_TPS,
        "ram_safety_floor_bytes": RAM_SAFETY_FLOOR_BYTES,
        "ram_safety_fraction": RAM_SAFETY_FRACTION,
        "vram_safety_margin_bytes": VRAM_SAFETY_MARGIN_BYTES,
        "provisional": True,
    }
