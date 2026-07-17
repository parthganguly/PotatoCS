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


def runtime_capability_matrix() -> dict[str, dict[str, dict[str, str]]]:
    return {runtime: {key: dict(value) for key, value in caps.items()} for runtime, caps in _MATRIX.items()}


def capability(runtime: str, name: str) -> dict[str, str]:
    """Look up one capability; unknown runtime/name degrade to unknown."""
    entry = _MATRIX.get(runtime, {}).get(name)
    if entry is None:
        return {"state": UNKNOWN, "evidence": "unknown"}
    return dict(entry)
