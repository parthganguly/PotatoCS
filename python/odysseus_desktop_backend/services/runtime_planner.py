"""Execution planner: hardware + models + evidence -> versioned plan.

Pure computation: no I/O, no settings mutation, no network. Consumes the
inventories from `runtime_inventory` plus benchmark-evidence summaries
(fingerprinted; see `runtime_plan_service.summarize_artifacts`) and
returns a deterministic, fail-safe execution plan.

Honesty contract (post-review):
- KV-cache memory is computed from real model geometry
  (2 tensors x layers x kv_heads x head_dim x dtype x context); unknown
  geometry uses a documented conservative upper bound and can never
  support a fit claim.
- `measured` confidence requires a full evidence-fingerprint match
  (runtime+version, model identity, CPU/GPU/RAM, context, options,
  server env, placement band, shape) — never model tag alone.
- Without compatible measured evidence the plan carries no numeric
  TTFT/TPS and is never classified `interactive`; derived scores are
  used for ranking only.
- This planner only ever plans the Ollama runtime; it never uses Deep
  Local terminology. Genuine Deep Local planning is the Colibri
  surface's job (`deep_local.plan`/`deep_local.doctor`).
"""

from __future__ import annotations

import re
from typing import Any

PLAN_VERSION = 2

OBJECTIVES = ("fast", "balanced", "deep")

# Safety margins — named constants, not magic numbers.
RAM_SAFETY_FLOOR_BYTES = 1_536 * 1024 * 1024  # leave at least 1.5 GB of RAM
RAM_SAFETY_FRACTION = 0.12                    # or 12% of total, whichever is larger
VRAM_SAFETY_MARGIN_BYTES = 256 * 1024 * 1024  # leave 256 MB of VRAM
RUNTIME_OVERHEAD_BYTES = 600 * 1024 * 1024    # graph/compute-buffer class observed on Ollama 0.31.1

# KV cache dtype: f16 unless a plan explicitly targets a quantized cache.
KV_DTYPE_BYTES = 2

# Conservative upper bound for models whose KV geometry is unknown:
# 512 KiB/token — 3.5x the largest measured GQA config in this repo
# (qwen3:8b at 144 KiB/token) and above llama-70B-class GQA
# (320 KiB/token). Unknown geometry additionally forbids fit claims.
UNKNOWN_KV_BYTES_PER_TOKEN = 512 * 1024

EVIDENCE_MAX_AGE_DAYS = 30

# The context Ollama 0.31.1 applied on the measured machine when no
# num_ctx was sent (verified via /api/ps residency in the artifacts).
DEFAULT_CONTEXT_TOKENS = 4096

# Interactive-class thresholds — provisional pending P1/P2 measurements;
# applied ONLY to measured evidence, never to derived scores.
INTERACTIVE_TTFT_MS = 10_000
INTERACTIVE_MIN_TPS = 5.0
SLOW_INTERACTIVE_TTFT_MS = 60_000
SLOW_INTERACTIVE_MIN_TPS = 1.0

CONTEXT_BY_OBJECTIVE = {"fast": 4096, "balanced": 8192, "deep": 8192}

WARN_NO_GPU_PROBE = "no_gpu_probe"
WARN_STALE_EVIDENCE = "stale_evidence"
WARN_NO_EVIDENCE = "no_benchmark_evidence"
WARN_LOW_RAM_HEADROOM = "low_ram_headroom"
WARN_SLOW_CLASS = "expect_slow_generation"
WARN_UNKNOWN_HARDWARE = "unknown_hardware_conservative"
WARN_SPEED_UNMEASURED = "speed_unmeasured"
WARN_MEMORY_RECLAIM = "requires_memory_reclaim"

REJECT_EXCEEDS_RAM = "exceeds_ram_margin"
REJECT_EXCEEDS_TOTAL_RAM = "exceeds_total_ram_envelope"
REJECT_NOT_TEXT_MODEL = "not_a_text_generation_model"
REJECT_RUNTIME_UNAVAILABLE = "runtime_unavailable"
REJECT_RANKED_BELOW_SELECTED = "ranked_below_selected"
REJECT_UNKNOWN_KV_GEOMETRY = "unknown_kv_geometry"

_FIT_PLACEMENT_BAND = {
    "fits_gpu_full": "full_gpu",
    "fits_gpu_partial": "partial_gpu",
    "fits_cpu_ram": "cpu",
}


def _parse_param_billions(parameter_size: str) -> float:
    match = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*B\s*$", str(parameter_size or ""), re.IGNORECASE)
    return float(match.group(1)) if match else 0.0


def kv_cache_bytes(geometry: dict[str, Any] | None, context_tokens: int, dtype_bytes: int = KV_DTYPE_BYTES) -> tuple[int, bool]:
    """Architecture-aware KV-cache size: K and V tensors per layer.

    bytes = (key_length + value_length) * kv_heads * layers * dtype * tokens

    Returns (bytes, geometry_known). Unknown/partial geometry returns the
    documented conservative upper bound and geometry_known=False.
    """
    if isinstance(geometry, dict):
        layers = int(geometry.get("layers") or 0)
        kv_heads = int(geometry.get("kv_heads") or 0)
        key_length = int(geometry.get("key_length") or 0)
        value_length = int(geometry.get("value_length") or 0)
        if layers > 0 and kv_heads > 0 and key_length > 0 and value_length > 0:
            per_token = (key_length + value_length) * kv_heads * layers * dtype_bytes
            return per_token * int(context_tokens), True
    return UNKNOWN_KV_BYTES_PER_TOKEN * int(context_tokens), False


def estimate_memory_bytes(model: dict[str, Any], context_tokens: int) -> dict[str, Any]:
    """Weights + architecture-aware KV cache + runtime overhead.

    Unknown sizes estimate high, never low; `kv_geometry_known: False`
    means no fit class may be claimed from this estimate.
    """
    weights = int(model.get("disk_bytes") or 0)
    params_b = _parse_param_billions(model.get("parameter_size") or "")
    if weights <= 0 and params_b > 0:
        weights = int(params_b * 1.05 * 1024**3)  # assume 8-bit-class weights when size unknown
    kv, geometry_known = kv_cache_bytes(model.get("kv_geometry"), context_tokens)
    total = weights + kv + RUNTIME_OVERHEAD_BYTES
    return {
        "weights": weights,
        "kv_cache": kv,
        "total": total,
        "kv_geometry_known": geometry_known,
    }


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


def classify_fit(
    model: dict[str, Any],
    hardware: dict[str, Any],
    context_tokens: int,
) -> tuple[str, dict[str, Any]]:
    """Fit classification. `unknown_kv_geometry` when geometry is missing
    (fit may then never be claimed); `reachable_after_memory_reclaim`
    when the model exceeds currently-available RAM but would fit total
    RAM with margins after other applications release memory — an
    Ollama-honest state, NOT a Deep Local claim."""
    estimate = estimate_memory_bytes(model, context_tokens)
    if not estimate["kv_geometry_known"]:
        return "unknown_kv_geometry", estimate
    vram_budget = _vram_budget(hardware)
    ram_budget = _ram_budget(hardware)
    total = estimate["total"]
    if vram_budget and total <= vram_budget:
        return "fits_gpu_full", estimate
    if ram_budget and total <= ram_budget:
        if vram_budget > 0:
            return "fits_gpu_partial", estimate
        return "fits_cpu_ram", estimate
    ram_total = int((hardware.get("ram") or {}).get("total_bytes") or 0)
    reclaim_budget = max(0, ram_total - max(RAM_SAFETY_FLOOR_BYTES, int(ram_total * RAM_SAFETY_FRACTION)))
    if reclaim_budget and total <= reclaim_budget:
        return "reachable_after_memory_reclaim", estimate
    return "not_runnable", estimate


def classify_execution(ttft_ms: float | None, tps: float | None) -> str:
    """Execution class from MEASURED numbers only."""
    if ttft_ms is None or tps is None:
        return "persisted_job"
    if ttft_ms <= INTERACTIVE_TTFT_MS and tps >= INTERACTIVE_MIN_TPS:
        return "interactive"
    if ttft_ms <= SLOW_INTERACTIVE_TTFT_MS and tps >= SLOW_INTERACTIVE_MIN_TPS:
        return "slow_interactive"
    return "persisted_job"


def _conservative_execution_class(fit: str) -> str:
    """Class for configurations with no compatible measurement: never
    `interactive`."""
    if fit in ("fits_gpu_full", "fits_gpu_partial", "fits_cpu_ram"):
        return "slow_interactive"
    return "persisted_job"


# -- evidence fingerprint compatibility ----------------------------------


def hardware_fingerprint_fields(hardware: dict[str, Any]) -> dict[str, Any]:
    """The hardware identity fields an evidence fingerprint must match."""
    cpu = hardware.get("cpu") or {}
    isa = cpu.get("isa") or {}
    gpus = hardware.get("gpus") or []
    return {
        "cpu_arch": str((hardware.get("os") or {}).get("arch") or ""),
        "physical_cores": int(cpu.get("physical_cores") or 0),
        "logical_threads": int(cpu.get("logical_threads") or 0),
        "avx2": bool(isa.get("avx2")),
        "avx512f": bool(isa.get("avx512f")),
        "gpus": sorted(
            f"{gpu.get('vendor', '')}/{gpu.get('name', '')}/{int(gpu.get('vram_total_bytes') or 0)}"
            for gpu in gpus
        ),
        "ram_total_bytes": int((hardware.get("ram") or {}).get("total_bytes") or 0),
    }


def _model_identity_matches(candidate: dict[str, Any], fingerprint: dict[str, Any]) -> bool:
    if str(fingerprint.get("model_tag") or "") != str(candidate.get("tag") or ""):
        return False
    if str(fingerprint.get("quantization") or "") != str(candidate.get("quantization") or ""):
        return False
    evidence_digest = str(fingerprint.get("model_digest") or "")
    candidate_digest = str(candidate.get("digest") or "")
    if evidence_digest and candidate_digest:
        return evidence_digest == candidate_digest
    # Digest missing on one side (older artifacts): fall back to exact
    # on-disk byte size as the weights-identity proxy.
    return int(fingerprint.get("model_disk_bytes") or 0) == int(candidate.get("disk_bytes") or 0)


def evidence_compatible(
    summary: dict[str, Any],
    *,
    candidate: dict[str, Any],
    hardware: dict[str, Any],
    runtime: dict[str, Any],
    plan_context: int,
    plan_options: dict[str, Any],
    fit: str,
) -> bool:
    """Full compatibility rule for `measured` confidence (documented):

    exact match on: runtime name+version; model tag + quantization +
    (digest, or byte-size when a digest is missing); CPU arch, physical
    and logical cores, AVX2/AVX512F; GPU identity set (vendor/model/
    total VRAM — 'no GPU' only matches 'no GPU'); total RAM; benchmark
    shape 'medium' (the representative interactive workload); context
    (evidence 'default' equals DEFAULT_CONTEXT_TOKENS); tuning options
    (num_thread/num_gpu/num_batch, absent == absent); server env
    mapping. Free RAM/VRAM are compared through the placement band:
    the evidence's measured placement (full_gpu/partial_gpu/cpu from
    residency) must equal the band the plan's fit class predicts —
    memory-pressure differences that change placement change the band
    and disqualify the evidence.
    """
    fingerprint = summary.get("fingerprint") or {}
    if str(fingerprint.get("runtime") or "") != str(runtime.get("name") or ""):
        return False
    if str(fingerprint.get("runtime_version") or "") != str(runtime.get("version") or ""):
        return False
    if not _model_identity_matches(candidate, fingerprint):
        return False
    if str(fingerprint.get("shape") or "") != "medium":
        return False

    hw = hardware_fingerprint_fields(hardware)
    for key in ("cpu_arch", "physical_cores", "logical_threads", "avx2", "avx512f", "gpus", "ram_total_bytes"):
        if fingerprint.get(key) != hw[key]:
            return False

    evidence_context = fingerprint.get("context")
    if evidence_context == "default":
        evidence_context = DEFAULT_CONTEXT_TOKENS
    if int(evidence_context or 0) != int(plan_context):
        return False

    evidence_options = fingerprint.get("tuning_options") or {}
    plan_tuning = {
        key: value for key, value in plan_options.items() if key in ("num_thread", "num_gpu", "num_batch")
    }
    if evidence_options != plan_tuning:
        return False
    if (fingerprint.get("server_env") or {}) != {}:
        # The planner emits no server env; evidence measured under a
        # modified server env is a different configuration.
        return False
    expected_band = _FIT_PLACEMENT_BAND.get(fit)
    if expected_band is None:
        return False
    return str(fingerprint.get("placement_band") or "") == expected_band


def _find_evidence(
    candidates_evidence: list[dict[str, Any]] | None,
    *,
    candidate: dict[str, Any],
    hardware: dict[str, Any],
    runtime: dict[str, Any],
    plan_context: int,
    plan_options: dict[str, Any],
    fit: str,
    now_ms: int,
) -> tuple[dict[str, Any] | None, bool]:
    """Return (compatible summary, stale). Incompatible evidence is
    ignored entirely; compatible-but-old evidence is returned stale."""
    for summary in candidates_evidence or []:
        if not evidence_compatible(
            summary,
            candidate=candidate,
            hardware=hardware,
            runtime=runtime,
            plan_context=plan_context,
            plan_options=plan_options,
            fit=fit,
        ):
            continue
        stale = False
        captured = int(summary.get("captured_at_ms") or 0)
        if captured and now_ms and now_ms - captured > EVIDENCE_MAX_AGE_DAYS * 86_400_000:
            stale = True
        return summary, stale
    return None, False


def _ranking_score(candidate: dict[str, Any]) -> float:
    """Derived score for ORDERING candidates only. Never surfaces as a
    speed claim, never feeds execution classification."""
    if candidate.get("measured_tps") is not None:
        return float(candidate["measured_tps"])
    params_b = candidate["params_b"]
    if params_b <= 0:
        return 0.0
    score = 120.0 / max(params_b, 0.5)
    if candidate["fit"] == "fits_cpu_ram":
        score /= 4
    elif candidate["fit"] == "fits_gpu_partial":
        score /= 2
    elif candidate["fit"] == "reachable_after_memory_reclaim":
        score /= 8
    return score


def _empty_plan(
    objective: str,
    runtime: dict[str, Any] | None,
    warnings: list[str],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "plan_version": PLAN_VERSION,
        "objective": objective,
        "runtime": runtime,
        "model": None,
        "fit_class": None,
        "execution_class": None,
        "options": {},
        "server_env": {},
        "estimates": {},
        "confidence": "conservative_default",
        "warnings": sorted(set(warnings)),
        "rejected_alternatives": sorted(rejected, key=lambda item: item["model"]),
        "evidence": {"benchmark_batch_ids": [], "sample_count": 0, "stale": False},
        "thresholds": _threshold_record(),
    }


def build_plan(
    *,
    objective: str,
    hardware: dict[str, Any],
    models: list[dict[str, Any]],
    runtimes: list[dict[str, Any]],
    evidence: list[dict[str, Any]] | None = None,
    now_ms: int = 0,
) -> dict[str, Any]:
    """Produce a versioned execution plan. Deterministic, side-effect free."""
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {OBJECTIVES}")

    warnings: list[str] = []
    rejected: list[dict[str, Any]] = []

    ollama = next((runtime for runtime in runtimes if runtime.get("name") == "ollama"), None)
    if not (ollama and (ollama.get("healthy") or ollama.get("reachable"))):
        return _empty_plan(
            objective,
            None,
            ["runtime_unavailable"],
            [
                {"model": str(model.get("tag") or ""), "reason_code": REJECT_RUNTIME_UNAVAILABLE, "detail_numbers": {}}
                for model in models
            ],
        )
    runtime_identity = {"name": "ollama", "version": str(ollama.get("version") or "")}

    hw_known = bool((hardware.get("ram") or {}).get("total_bytes"))
    if not hw_known:
        warnings.append(WARN_UNKNOWN_HARDWARE)
    if not hardware.get("gpus"):
        warnings.append(WARN_NO_GPU_PROBE)

    context_tokens = CONTEXT_BY_OBJECTIVE[objective]
    plan_options: dict[str, Any] = {"num_ctx": context_tokens}

    candidates: list[dict[str, Any]] = []
    for model in models:
        tag = str(model.get("tag") or "")
        if not _is_text_model(model):
            rejected.append({"model": tag, "reason_code": REJECT_NOT_TEXT_MODEL, "detail_numbers": {}})
            continue
        fit, estimate = classify_fit(model, hardware, context_tokens)
        if fit == "unknown_kv_geometry":
            rejected.append(
                {
                    "model": tag,
                    "reason_code": REJECT_UNKNOWN_KV_GEOMETRY,
                    "detail_numbers": {"conservative_kv_bytes": estimate["kv_cache"]},
                }
            )
            continue
        if fit == "not_runnable":
            rejected.append(
                {
                    "model": tag,
                    "reason_code": REJECT_EXCEEDS_TOTAL_RAM,
                    "detail_numbers": {
                        "estimated_total_bytes": estimate["total"],
                        "ram_total_bytes": int((hardware.get("ram") or {}).get("total_bytes") or 0),
                    },
                }
            )
            continue
        if fit == "reachable_after_memory_reclaim" and objective != "deep":
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
        summary, stale = _find_evidence(
            evidence,
            candidate=model,
            hardware=hardware,
            runtime=runtime_identity,
            plan_context=context_tokens,
            plan_options=plan_options,
            fit=fit,
            now_ms=now_ms,
        )
        measured_ttft = measured_tps = None
        if summary is not None and not stale:
            measured_ttft = summary.get("warm_ttft_ms")
            measured_tps = summary.get("generation_tps")
        candidates.append(
            {
                "model": model,
                "tag": tag,
                "fit": fit,
                "estimate": estimate,
                "measured_ttft_ms": measured_ttft,
                "measured_tps": measured_tps,
                "evidence_summary": summary,
                "evidence_stale": stale,
                "params_b": _parse_param_billions(model.get("parameter_size") or ""),
            }
        )

    if not candidates:
        return _empty_plan(objective, runtime_identity, warnings + ["no_eligible_model"], rejected)

    for candidate in candidates:
        candidate["score"] = _ranking_score(candidate)
        candidate["measured_class"] = (
            classify_execution(candidate["measured_ttft_ms"], candidate["measured_tps"])
            if candidate["measured_tps"] is not None
            else None
        )

    def fast_key(candidate: dict[str, Any]) -> tuple:
        return (-candidate["score"], candidate["params_b"], candidate["tag"])

    def capability_key(candidate: dict[str, Any]) -> tuple:
        return (-candidate["params_b"], -candidate["score"], candidate["tag"])

    if objective == "fast":
        # Only measured-interactive configurations may be preferred as
        # "interactive"; without any, fall back to score ranking with an
        # honest (non-interactive) classification.
        measured_interactive = [c for c in candidates if c["measured_class"] == "interactive"]
        pool = sorted(measured_interactive or candidates, key=fast_key)
    elif objective == "balanced":
        usable = [c for c in candidates if c["measured_class"] in ("interactive", "slow_interactive")]
        pool = sorted(usable or candidates, key=capability_key)
    else:
        pool = sorted(candidates, key=capability_key)

    selected = pool[0]
    for candidate in candidates:
        if candidate is selected:
            continue
        rejected.append(
            {
                "model": candidate["tag"],
                "reason_code": REJECT_RANKED_BELOW_SELECTED,
                "detail_numbers": {
                    "ranking_score": round(candidate["score"], 2),
                    "parameter_billions": candidate["params_b"],
                },
            }
        )

    if selected["measured_tps"] is not None:
        execution_class = selected["measured_class"]
        estimates_basis = "measured"
    else:
        execution_class = _conservative_execution_class(selected["fit"])
        estimates_basis = "unmeasured"
        warnings.append(WARN_SPEED_UNMEASURED)
    if execution_class != "interactive":
        warnings.append(WARN_SLOW_CLASS)
    if selected["fit"] == "reachable_after_memory_reclaim":
        warnings.append(WARN_MEMORY_RECLAIM)

    vram_budget = _vram_budget(hardware)
    estimate = selected["estimate"]
    if selected["fit"] == "fits_gpu_full":
        vram_estimate = min(estimate["total"], vram_budget)
    elif selected["fit"] == "fits_gpu_partial" and vram_budget > 0:
        vram_estimate = vram_budget
    else:
        vram_estimate = 0

    ram_headroom = _ram_budget(hardware) - (estimate["total"] - vram_estimate)
    if 0 <= ram_headroom < 1024**3:
        warnings.append(WARN_LOW_RAM_HEADROOM)

    if selected["evidence_stale"]:
        warnings.append(WARN_STALE_EVIDENCE)
    if selected["evidence_summary"] is None:
        warnings.append(WARN_NO_EVIDENCE)

    if selected["measured_tps"] is not None:
        confidence = "measured"
    elif hw_known:
        confidence = "derived"
    else:
        confidence = "conservative_default"

    summary = selected["evidence_summary"]
    batch_ids = sorted(str(b) for b in (summary or {}).get("batch_ids") or [])

    return {
        "plan_version": PLAN_VERSION,
        "objective": objective,
        "runtime": runtime_identity,
        "model": {
            "tag": selected["tag"],
            "quantization": str(selected["model"].get("quantization") or ""),
            "parameter_size": str(selected["model"].get("parameter_size") or ""),
            "disk_bytes": int(selected["model"].get("disk_bytes") or 0),
        },
        "fit_class": selected["fit"],
        "execution_class": execution_class,
        "options": plan_options,
        "server_env": {},
        "estimates": {
            "basis": estimates_basis,
            "ram_bytes": max(0, estimate["total"] - vram_estimate),
            "vram_bytes": vram_estimate,
            "kv_cache_bytes": estimate["kv_cache"],
            "disk_working_set_bytes": estimate["weights"],
            "ttft_ms": selected["measured_ttft_ms"],
            "generation_tps": selected["measured_tps"],
        },
        "confidence": confidence,
        "warnings": sorted(set(warnings)),
        "rejected_alternatives": sorted(rejected, key=lambda item: item["model"]),
        "evidence": {
            "benchmark_batch_ids": batch_ids,
            "sample_count": int((summary or {}).get("sample_count") or 0),
            "stale": selected["evidence_stale"],
        },
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
        "unknown_kv_bytes_per_token": UNKNOWN_KV_BYTES_PER_TOKEN,
        "provisional": True,
    }
