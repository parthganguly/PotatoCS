"""Runtime capability matrix (RFC section 7).

Every entry cites its evidence class. `measured` entries may only be
set by Phase 4+ evidence recorded in LOCAL_RUNTIME_BENCHMARKS.md;
everything else stays `binary_help`, `live_probe`, or `unknown`.
"""

from __future__ import annotations

SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
UNKNOWN = "unknown"

_MATRIX: dict[str, dict[str, dict[str, str]]] = {
    "ollama": {
        # Verified against the 0.31.1 binary help and live API probes
        # (2026-07-17 session); applies to the 0.31.x series.
        "per_request_context": {"state": SUPPORTED, "evidence": "live_probe"},
        "per_request_threads": {"state": SUPPORTED, "evidence": "live_probe"},
        "gpu_offload_layers": {"state": SUPPORTED, "evidence": "live_probe"},
        "batch_size": {"state": SUPPORTED, "evidence": "binary_help"},
        "flash_attention": {"state": SUPPORTED, "evidence": "binary_help"},  # server env
        "kv_cache_quantization": {"state": SUPPORTED, "evidence": "binary_help"},  # server env
        "keep_alive": {"state": SUPPORTED, "evidence": "live_probe"},
        "prompt_cache_reuse": {"state": UNKNOWN, "evidence": "unknown"},  # measured in Phase 4
        "speculative_decoding": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "streamed_moe_experts": {"state": UNSUPPORTED, "evidence": "binary_help"},
    },
    "llamacpp": {
        # Upstream release b10064 server flags; per-server (launch-time).
        "per_request_context": {"state": SUPPORTED, "evidence": "binary_help"},
        "per_request_threads": {"state": SUPPORTED, "evidence": "binary_help"},
        "gpu_offload_layers": {"state": SUPPORTED, "evidence": "binary_help"},
        "batch_size": {"state": SUPPORTED, "evidence": "binary_help"},
        "flash_attention": {"state": SUPPORTED, "evidence": "binary_help"},
        "kv_cache_quantization": {"state": SUPPORTED, "evidence": "binary_help"},
        "keep_alive": {"state": SUPPORTED, "evidence": "binary_help"},  # server lifetime
        "prompt_cache_reuse": {"state": SUPPORTED, "evidence": "binary_help"},
        "speculative_decoding": {"state": SUPPORTED, "evidence": "binary_help"},
        "streamed_moe_experts": {"state": UNSUPPORTED, "evidence": "binary_help"},
    },
    "colibri": {
        # Audited at upstream 54cfe563 (Deep Local track).
        "per_request_context": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "per_request_threads": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "gpu_offload_layers": {"state": UNKNOWN, "evidence": "unknown"},
        "batch_size": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "flash_attention": {"state": UNKNOWN, "evidence": "unknown"},
        "kv_cache_quantization": {"state": UNKNOWN, "evidence": "unknown"},
        "keep_alive": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "prompt_cache_reuse": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "speculative_decoding": {"state": UNSUPPORTED, "evidence": "binary_help"},
        "streamed_moe_experts": {"state": SUPPORTED, "evidence": "binary_help"},
    },
}

VALID_EVIDENCE = {"binary_help", "live_probe", "measured", "unknown"}

# Research findings from the 2026-07 benchmark session. Every entry is
# `measured_exploratory`: a real measurement on one P3 machine that has
# NOT met the safety acceptance criteria for a production
# recommendation (minimum system-RAM headroom in every run, stable GPU
# detection, comparable ambient VRAM, multiple alternating A/B rounds,
# no hidden reload in warm samples, quality pass, no failure increase).
# Nothing in this table is emitted as a plan recommendation.
MEASURED_FINDINGS: dict[str, dict[str, str]] = {
    "keep_alive_reload_cost": {
        "status": "measured_exploratory",
        "summary": "keep_alive=0 cost 6.2-8.7s per turn vs ~0.4s warm (llama3.2:3b, one machine)",
        "evidence": "ollama-0311-llama32-3b-tiny-keepalive0 vs tiny baseline",
    },
    "context_truncation_correctness": {
        "status": "measured_exploratory",
        "summary": "default context truncated a ~5k-token document (quality 0/12); num_ctx 8192 passed 4/4",
        "evidence": "ollama-0311-llama32-1b-longctx-baseline vs -ctx8192",
    },
    "flash_attention_q8_kv_under_vram_pressure": {
        "status": "measured_exploratory",
        "summary": "one paired A/B batch showed 10.1 -> 23.5 tok/s under ambient VRAM pressure; optimized arm ran at critically low system RAM and its snapshot missed the GPU — not a safe recommendation yet",
        "evidence": "ollama-0311-llama32-3b-medium-paired-default vs -paired-flashkv",
    },
    "prompt_cache_reuse": {
        "status": "measured_exploratory",
        "summary": "repeat-prompt eval 880ms -> 12ms (existing runtime behavior, pinned by measurement)",
        "evidence": "ollama-0311-llama32-1b-longctx-ctx8192",
    },
}


def measured_findings() -> dict[str, dict[str, str]]:
    return {name: dict(entry) for name, entry in MEASURED_FINDINGS.items()}


def runtime_capability_matrix() -> dict[str, dict[str, dict[str, str]]]:
    return {runtime: {key: dict(value) for key, value in caps.items()} for runtime, caps in _MATRIX.items()}


def capability(runtime: str, name: str) -> dict[str, str]:
    """Look up one capability; unknown runtime/name degrade to unknown."""
    entry = _MATRIX.get(runtime, {}).get(name)
    if entry is None:
        return {"state": UNKNOWN, "evidence": "unknown"}
    return dict(entry)
