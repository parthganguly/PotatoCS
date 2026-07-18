"""Read-only facade for runtime inventory, benchmark evidence, and planning.

Consumed by the `runtime.*` RPC methods. Strictly read-only: no settings
mutation, no downloads, no server control, no DB writes.

Latency contract (single-threaded sidecar): every probe is individually
bounded and the sum of worst-case bounds is the declared ceiling
`RUNTIME_RPC_LATENCY_CEILING_SECONDS`. Model-detail probes run under a
hard total budget in a worker thread (a hanging model cannot multiply
across the list), and a completed snapshot is cached for
`INVENTORY_CACHE_TTL_SECONDS`, so only a cold call pays probe cost at
all. Cache age and partial state are explicit in every response.

Evidence contract: benchmark artifacts are grouped by a full
configuration fingerprint (runtime+version, model identity, CPU/GPU/RAM,
context, tuning options, server env, placement band, shape). Only
same-fingerprint artifacts aggregate into one summary; each summary
carries sample count, median, range, and its exact contributing batch
ids. Stub-engine artifacts never become planner evidence.
"""

from __future__ import annotations

import json
import statistics
import time
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services import runtime_inventory as ri
from odysseus_desktop_backend.services import runtime_planner as rp
from odysseus_desktop_backend.storage import utc_ms

logger = get_logger("runtime_plan")

MAX_EVIDENCE_FILES = 200
MAX_EVIDENCE_FILE_BYTES = 4 * 1024 * 1024

INVENTORY_CACHE_TTL_SECONDS = 60.0

# Worst-case cold arithmetic (every probe at its cap, all bounds from
# runtime_inventory): hardware = nvidia-smi 3.0s; runtimes = tcp 0.5s +
# ollama version 2.5s + llama-server --version 3.0s; models = tags 2.5s
# + detail budget 2.0s (+0.5s join slack). Total 14.0s; declared with
# margin:
RUNTIME_RPC_LATENCY_CEILING_SECONDS = 15.0

# Server-env keys that do not affect performance and are stripped from
# fingerprints (OLLAMA_NO_CLOUD only disables remote features the
# benchmarks never touch).
_PERF_NEUTRAL_ENV_KEYS = {"OLLAMA_NO_CLOUD"}

_TUNING_OPTION_KEYS = ("num_thread", "num_gpu", "num_batch")

FULL_GPU_FRACTION = 0.99
CPU_ONLY_FRACTION = 0.01


def placement_band(gpu_fraction: float | None) -> str:
    """Free-RAM/VRAM compatibility band: memory conditions are compared
    through their observable outcome — where the runtime actually placed
    the model — rather than raw free-byte equality."""
    if gpu_fraction is None:
        return "unknown"
    if gpu_fraction >= FULL_GPU_FRACTION:
        return "full_gpu"
    if gpu_fraction <= CPU_ONLY_FRACTION:
        return "cpu"
    return "partial_gpu"


def evidence_fingerprint(artifact: dict[str, Any]) -> dict[str, Any] | None:
    """Complete configuration identity of one benchmark artifact."""
    if artifact.get("schema_version") != 1:
        return None
    runtime = artifact.get("runtime") or {}
    model = artifact.get("model") or {}
    hardware = artifact.get("hardware") or {}
    runs = [run for run in artifact.get("runs") or [] if isinstance(run, dict)]
    if not runs:
        return None

    options = runs[0].get("options") or {}
    context = options.get("num_ctx")
    tuning = {key: options[key] for key in _TUNING_OPTION_KEYS if key in options}
    server_env = {
        key: value
        for key, value in (runtime.get("server_env") or {}).items()
        if key not in _PERF_NEUTRAL_ENV_KEYS
    }
    residency_fractions = [
        run["residency"].get("gpu_fraction")
        for run in runs
        if isinstance(run.get("residency"), dict) and run["residency"].get("gpu_fraction") is not None
    ]
    fraction = residency_fractions[-1] if residency_fractions else None

    return {
        "runtime": str(runtime.get("name") or ""),
        "runtime_version": str(runtime.get("version") or ""),
        "model_tag": str(model.get("tag") or ""),
        "model_digest": str(model.get("digest") or ""),
        "quantization": str(model.get("quantization") or ""),
        "model_disk_bytes": int(model.get("disk_bytes") or 0),
        "shape": str(artifact.get("shape") or ""),
        "context": int(context) if context else "default",
        "tuning_options": tuning,
        "server_env": server_env,
        "placement_band": placement_band(fraction),
        **rp.hardware_fingerprint_fields(hardware),
    }


def fingerprint_key(fingerprint: dict[str, Any]) -> str:
    return json.dumps(fingerprint, sort_keys=True)


def _warm_passing_runs(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        run
        for run in artifact.get("runs") or []
        if isinstance(run, dict)
        and not run.get("cold")
        and not run.get("error_category")
        and run.get("quality_check") in {"passed", "not_applicable"}
    ]


def summarize_artifacts(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Aggregate ONLY artifacts sharing an identical fingerprint.

    Baseline / CPU-only / flash-KV / offload / thread variants have
    different fingerprints by construction (options, server env, or
    placement differ) and therefore can never merge into one summary.
    """
    groups: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        if artifact.get("engine_kind") == "stub":
            continue
        fingerprint = evidence_fingerprint(artifact)
        if fingerprint is None or not fingerprint["model_tag"]:
            continue
        runs = _warm_passing_runs(artifact)
        ttfts = [
            run["timings_ms"]["first_token"]
            for run in runs
            if isinstance(run.get("timings_ms"), dict) and run["timings_ms"].get("first_token") is not None
        ]
        tpss = [
            run["tokens"]["generation_tps"]
            for run in runs
            if isinstance(run.get("tokens"), dict) and run["tokens"].get("generation_tps") is not None
        ]
        if not ttfts or not tpss:
            continue
        key = fingerprint_key(fingerprint)
        group = groups.setdefault(
            key,
            {
                "fingerprint": fingerprint,
                "ttfts": [],
                "tpss": [],
                "batch_ids": set(),
                "captured_at_ms": 0,
            },
        )
        group["ttfts"].extend(ttfts)
        group["tpss"].extend(tpss)
        group["batch_ids"].add(str(artifact.get("batch_id") or ""))
        captured = int((artifact.get("hardware") or {}).get("captured_at_ms") or 0)
        group["captured_at_ms"] = max(group["captured_at_ms"], captured)

    summaries = []
    for key in sorted(groups):
        group = groups[key]
        summaries.append(
            {
                "fingerprint": group["fingerprint"],
                "model_tag": group["fingerprint"]["model_tag"],
                "shape": group["fingerprint"]["shape"],
                "sample_count": len(group["tpss"]),
                "warm_ttft_ms": statistics.median(group["ttfts"]),
                "ttft_range_ms": [min(group["ttfts"]), max(group["ttfts"])],
                "generation_tps": statistics.median(group["tpss"]),
                "tps_range": [min(group["tpss"]), max(group["tpss"])],
                "batch_ids": sorted(group["batch_ids"]),
                "captured_at_ms": group["captured_at_ms"],
            }
        )
    return summaries


class RuntimePlanService:
    def __init__(self, evidence_dir: str | Path | None = None):
        # Research phase: benchmark evidence is a dev-machine artifact
        # directory; production profiles have none and the planner
        # degrades to derived/conservative confidence by design.
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self._snapshot: dict[str, Any] | None = None
        self._snapshot_at: float = 0.0

    # -- evidence --------------------------------------------------------

    def _load_artifacts(self) -> list[dict[str, Any]]:
        if self.evidence_dir is None or not self.evidence_dir.is_dir():
            return []
        artifacts: list[dict[str, Any]] = []
        for path in sorted(self.evidence_dir.glob("*.json"))[:MAX_EVIDENCE_FILES]:
            try:
                if path.stat().st_size > MAX_EVIDENCE_FILE_BYTES:
                    continue
                artifacts.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
        return artifacts

    def _evidence_summaries(self) -> list[dict[str, Any]]:
        return summarize_artifacts(self._load_artifacts())

    # -- bounded cached inventory ---------------------------------------

    def _collect_snapshot(self) -> dict[str, Any]:
        hardware = ri.hardware_inventory()
        runtimes = ri.runtime_inventory()
        models = ri.model_inventory()
        return {
            "hardware": hardware,
            "runtimes": runtimes["runtimes"],
            "models": models["models"],
            "details_complete": bool(models.get("details_complete")),
            "model_error_category": models["error_category"],
            "captured_at_ms": utc_ms(),
        }

    def _snapshot_fresh(self) -> tuple[dict[str, Any], int]:
        """Return (snapshot, cache_age_ms), refreshing when stale.

        Incomplete snapshots (partial detail probes) are not cached so a
        transiently hanging model does not pin partial data for the TTL.
        """
        now = time.monotonic()
        if self._snapshot is not None and now - self._snapshot_at <= INVENTORY_CACHE_TTL_SECONDS:
            return self._snapshot, int((now - self._snapshot_at) * 1000)
        snapshot = self._collect_snapshot()
        if snapshot["details_complete"]:
            self._snapshot = snapshot
            self._snapshot_at = time.monotonic()
        return snapshot, 0

    # -- RPC surface -----------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        snapshot, cache_age_ms = self._snapshot_fresh()
        return {
            "ok": True,
            "hardware": snapshot["hardware"],
            "runtimes": snapshot["runtimes"],
            "models": snapshot["models"],
            "partial": not snapshot["details_complete"],
            "model_error_category": snapshot["model_error_category"],
            "cache_age_ms": cache_age_ms,
            "latency_ceiling_seconds": RUNTIME_RPC_LATENCY_CEILING_SECONDS,
            "captured_at_ms": snapshot["captured_at_ms"],
        }

    def benchmarks(self) -> dict[str, Any]:
        artifacts = self._load_artifacts()
        listing = []
        for artifact in artifacts:
            runs = artifact.get("runs") or []
            listing.append(
                {
                    "batch_id": str(artifact.get("batch_id") or ""),
                    "runtime": str((artifact.get("runtime") or {}).get("name") or ""),
                    "runtime_version": str((artifact.get("runtime") or {}).get("version") or ""),
                    "model_tag": str((artifact.get("model") or {}).get("tag") or ""),
                    "shape": str(artifact.get("shape") or ""),
                    "engine_kind": str(artifact.get("engine_kind") or ""),
                    "run_count": len(runs),
                    "failed_runs": sum(1 for run in runs if isinstance(run, dict) and run.get("error_category")),
                    "quality_failures": sum(
                        1 for run in runs if isinstance(run, dict) and run.get("quality_check") == "failed"
                    ),
                }
            )
        return {
            "ok": True,
            "available": bool(listing),
            "artifacts": sorted(listing, key=lambda item: item["batch_id"]),
            "evidence_summaries": self._evidence_summaries(),
        }

    def plan(self, objective: str) -> dict[str, Any]:
        if objective not in rp.OBJECTIVES:
            return {
                "ok": False,
                "error_category": "invalid_objective",
                "error": "Objective must be one of: fast, balanced, deep.",
            }
        snapshot, cache_age_ms = self._snapshot_fresh()
        plan = rp.build_plan(
            objective=objective,
            hardware=snapshot["hardware"],
            models=snapshot["models"],
            runtimes=snapshot["runtimes"],
            evidence=self._evidence_summaries(),
            now_ms=utc_ms(),
        )
        logger.info(
            "runtime plan objective=%s model=%s class=%s confidence=%s warnings=%s",
            objective,
            (plan.get("model") or {}).get("tag"),
            plan.get("execution_class"),
            plan.get("confidence"),
            plan.get("warnings"),
        )
        return {
            "ok": True,
            "plan": plan,
            "inventory_partial": not snapshot["details_complete"],
            "inventory_cache_age_ms": cache_age_ms,
        }

    def recommendations(self) -> dict[str, Any]:
        snapshot, cache_age_ms = self._snapshot_fresh()
        evidence = self._evidence_summaries()
        now = utc_ms()
        plans = {
            objective: rp.build_plan(
                objective=objective,
                hardware=snapshot["hardware"],
                models=snapshot["models"],
                runtimes=snapshot["runtimes"],
                evidence=evidence,
                now_ms=now,
            )
            for objective in rp.OBJECTIVES
        }
        return {
            "ok": True,
            "objectives": list(rp.OBJECTIVES),
            "plans": plans,
            "capability_matrix": _capability_matrix(),
            "research_findings": _measured_findings(),
            "inventory_partial": not snapshot["details_complete"],
            "inventory_cache_age_ms": cache_age_ms,
            "note": (
                "Estimates, not guarantees. Plans are recommendations only; "
                "nothing is applied automatically. Findings marked "
                "measured_exploratory are research observations, not "
                "production recommendations."
            ),
        }


def _capability_matrix() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import runtime_capability_matrix

    return runtime_capability_matrix()


def _measured_findings() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import measured_findings

    return measured_findings()
