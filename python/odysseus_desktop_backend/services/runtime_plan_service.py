"""Read-only facade for runtime inventory, benchmark evidence, and planning.

Consumed by the `runtime.*` RPC methods. Strictly read-only: no settings
mutation, no downloads, no server control, no DB writes.

Dispatcher contract (single-threaded sidecar): **the RPC request path
never probes.** Requests answer immediately from the latest completed
snapshot (with explicit age), and probing happens in a single guarded
background refresher thread:

    no snapshot        -> fixed `refreshing` result + refresh started
    fresh snapshot     -> served as-is
    stale snapshot     -> served with its age + refresh started
    refresh in flight  -> observed, never duplicated

The refresher builds into private state, publishes immutable completed
snapshots atomically, and stamps each run with a generation so a
timeout-ignoring probe from an abandoned run can never publish or
mutate anything. Worst-case dispatcher blocking is therefore cache/
file-read time (milliseconds), not probe time; the background refresh
itself stays bounded by the per-probe budgets in `runtime_inventory`
(worst case ~14 s, declared as REFRESH_WORST_CASE_SECONDS).

Evidence contract: benchmark artifacts are grouped by a full per-run
configuration fingerprint (runtime+version, model identity,
CPU/GPU/RAM, context, tuning options, server env, placement band,
shape). Artifacts whose warm runs disagree on any of those fields are
rejected as heterogeneous. Only same-fingerprint artifacts aggregate;
each summary carries sample count, median, range, and its exact
contributing batch ids. Stub-engine artifacts never become evidence.
"""

from __future__ import annotations

import copy
import json
import statistics
import threading
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

# Worst-case BACKGROUND refresh duration (every probe at its cap, all
# bounds from runtime_inventory): hardware = nvidia-smi 3.0s; runtimes =
# tcp 0.5s + ollama version 2.5s + llama-server --version 3.0s; models =
# tags 2.5s + detail budget 2.0s (+0.5s join slack) = 14.0s. This bounds
# the refresher thread, NOT the dispatcher: RPC calls never wait for it.
REFRESH_WORST_CASE_SECONDS = 14.0

ERROR_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
ERROR_REFRESH_FAILED = "refresh_failed"

REFRESH_IDLE = "idle"
REFRESH_RUNNING = "refreshing"

# A refresher alive this long past its worst case is presumed hung; a
# new generation may start and the hung worker's eventual result is
# discarded by the generation check.
REFRESH_HUNG_SECONDS = REFRESH_WORST_CASE_SECONDS * 4

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


def _run_identity(run: dict[str, Any]) -> dict[str, Any]:
    """Per-run configuration identity: context + tuning options from the
    run's own request options, placement band from the run's own
    residency."""
    options = run.get("options") or {}
    context = options.get("num_ctx")
    residency = run.get("residency") if isinstance(run.get("residency"), dict) else {}
    return {
        "context": int(context) if context else "default",
        "tuning_options": {key: options[key] for key in _TUNING_OPTION_KEYS if key in options},
        "placement_band": placement_band(residency.get("gpu_fraction")),
    }


def evidence_fingerprint(artifact: dict[str, Any]) -> dict[str, Any] | None:
    """Complete configuration identity of one benchmark artifact.

    Built per-run and required to be homogeneous: if the warm
    quality-passing runs disagree on context, tuning options, or
    placement band, the artifact is heterogeneous and returns None —
    mixed measurements must never combine under one identity.
    """
    if artifact.get("schema_version") != 1:
        return None
    runtime = artifact.get("runtime") or {}
    model = artifact.get("model") or {}
    hardware = artifact.get("hardware") or {}
    warm_runs = _warm_passing_runs(artifact)
    if not warm_runs:
        return None

    identities = [_run_identity(run) for run in warm_runs]
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        return None  # heterogeneous: contributing runs differ

    server_env = {
        key: value
        for key, value in (runtime.get("server_env") or {}).items()
        if key not in _PERF_NEUTRAL_ENV_KEYS
    }
    return {
        "runtime": str(runtime.get("name") or ""),
        "runtime_version": str(runtime.get("version") or ""),
        "model_tag": str(model.get("tag") or ""),
        "model_digest": str(model.get("digest") or ""),
        "quantization": str(model.get("quantization") or ""),
        "model_disk_bytes": int(model.get("disk_bytes") or 0),
        "shape": str(artifact.get("shape") or ""),
        "context": first["context"],
        "tuning_options": first["tuning_options"],
        "server_env": server_env,
        "placement_band": first["placement_band"],
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
    """Refresh state machine (single guarded background worker):

        idle + no snapshot   --request--> start refresh, reply `refreshing`
        idle + fresh         --request--> reply from snapshot
        idle + stale         --request--> start refresh, reply stale+age
        refreshing           --request--> reply snapshot/`refreshing`; never
                                          start a second worker
        refresh done         --publish--> immutable deep-copied snapshot,
                                          generation-checked (late/hung
                                          workers from older generations
                                          are discarded)
        refresh hung         --request--> after REFRESH_HUNG_SECONDS a new
                                          generation may start; the hung
                                          worker's eventual result is
                                          discarded by the generation check
    """

    def __init__(self, evidence_dir: str | Path | None = None):
        # Research phase: benchmark evidence is a dev-machine artifact
        # directory; production profiles have none and the planner
        # degrades to derived/conservative confidence by design.
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self._lock = threading.Lock()
        self._published: dict[str, Any] | None = None
        self._published_at: float = 0.0
        self._last_success_at_ms: int = 0
        self._last_failure_category: str = ""
        self._refresh_thread: threading.Thread | None = None
        self._refresh_started_at: float = 0.0
        self._generation: int = 0

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

    # -- background snapshot refresher ----------------------------------

    def _collect_snapshot(self) -> dict[str, Any]:
        """Probe sequence; runs ONLY on the refresher thread."""
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

    def _refresh_worker(self, generation: int) -> None:
        try:
            snapshot = self._collect_snapshot()
            failure = ""
        except Exception:  # noqa: BLE001 - fixed category, no raw detail
            snapshot = None
            failure = ERROR_REFRESH_FAILED
        with self._lock:
            if generation != self._generation:
                return  # obsolete generation: discard entirely
            if snapshot is not None:
                # Publish an immutable private copy; the collector's own
                # references are dropped here and never touched again.
                self._published = copy.deepcopy(snapshot)
                self._published_at = time.monotonic()
                self._last_success_at_ms = int(snapshot["captured_at_ms"])
                self._last_failure_category = ""
            else:
                self._last_failure_category = failure

    def _start_refresh_locked(self) -> None:
        """Start one refresher; caller must hold the lock. A live worker
        blocks new starts until it is presumed hung."""
        alive = self._refresh_thread is not None and self._refresh_thread.is_alive()
        if alive and (time.monotonic() - self._refresh_started_at) <= REFRESH_HUNG_SECONDS:
            return
        self._generation += 1
        worker = threading.Thread(
            target=self._refresh_worker,
            args=(self._generation,),
            name="runtime-inventory-refresh",
            daemon=True,
        )
        self._refresh_thread = worker
        self._refresh_started_at = time.monotonic()
        worker.start()

    def _observe(self) -> dict[str, Any]:
        """Non-blocking snapshot observation; may start one refresh."""
        with self._lock:
            snapshot = self._published
            age_seconds = (time.monotonic() - self._published_at) if snapshot is not None else None
            if snapshot is None or age_seconds > INVENTORY_CACHE_TTL_SECONDS:
                self._start_refresh_locked()
            refreshing = self._refresh_thread is not None and self._refresh_thread.is_alive()
            return {
                "snapshot": copy.deepcopy(snapshot) if snapshot is not None else None,
                "cache_age_ms": int(age_seconds * 1000) if age_seconds is not None else None,
                "refresh_state": REFRESH_RUNNING if refreshing else REFRESH_IDLE,
                "last_success_at_ms": self._last_success_at_ms,
                "failure_category": self._last_failure_category,
            }

    def _unavailable(self, observed: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": False,
            "status": "refreshing",
            "error_category": observed["failure_category"] or ERROR_SNAPSHOT_UNAVAILABLE,
            "error": (
                "The runtime inventory snapshot is not ready yet; a background "
                "refresh is in progress. Retry shortly."
            ),
            "refresh_state": observed["refresh_state"],
            "last_success_at_ms": observed["last_success_at_ms"],
        }

    # -- RPC surface -----------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        observed = self._observe()
        snapshot = observed["snapshot"]
        if snapshot is None:
            return self._unavailable(observed)
        return {
            "ok": True,
            "hardware": snapshot["hardware"],
            "runtimes": snapshot["runtimes"],
            "models": snapshot["models"],
            "partial": not snapshot["details_complete"],
            "model_error_category": snapshot["model_error_category"],
            "cache_age_ms": observed["cache_age_ms"],
            "refresh_state": observed["refresh_state"],
            "last_success_at_ms": observed["last_success_at_ms"],
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
        observed = self._observe()
        snapshot = observed["snapshot"]
        if snapshot is None:
            return self._unavailable(observed)
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
            "inventory_cache_age_ms": observed["cache_age_ms"],
            "refresh_state": observed["refresh_state"],
        }

    def recommendations(self) -> dict[str, Any]:
        observed = self._observe()
        snapshot = observed["snapshot"]
        if snapshot is None:
            return self._unavailable(observed)
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
            "inventory_cache_age_ms": observed["cache_age_ms"],
            "refresh_state": observed["refresh_state"],
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
