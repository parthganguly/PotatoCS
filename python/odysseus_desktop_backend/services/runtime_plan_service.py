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
# Strict budgets for one evidence load pass (review round 3): total
# bytes parsed and wall-clock time are both capped; exceeding either
# stops the pass and marks the result truncated. Results are cached by
# a directory-stat key so repeat RPC calls do no parsing at all.
EVIDENCE_TOTAL_BYTE_BUDGET = 16 * 1024 * 1024
EVIDENCE_TIME_BUDGET_SECONDS = 1.0

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
    """Refresh state machine (ONE persistent background worker for the
    service lifetime — threads are never replaced or stacked):

        idle + no snapshot   --request--> signal refresh, reply `refreshing`
        idle + fresh         --request--> reply from snapshot
        idle + stale         --request--> signal refresh, reply stale+age
        refreshing           --request--> reply snapshot/`refreshing`;
                                          the signal is level-triggered,
                                          no second worker ever starts
        refresh done         --publish--> immutable deep-copied snapshot,
                                          generation-checked (a collect
                                          superseded by close() never
                                          publishes)
        worker hung          --request--> requests keep answering from
                                          the last snapshot; at most this
                                          one unkillable daemon thread
                                          exists for the process lifetime
        close()              ----------> worker loop exits at the next
                                          wakeup; no further publishes
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
        self._worker: threading.Thread | None = None
        self._refresh_requested = threading.Event()
        self._collecting = False
        self._closed = threading.Event()
        self._generation: int = 0
        self._evidence_cache_lock = threading.Lock()
        self._evidence_cache_key: tuple | None = None
        self._evidence_cache: dict[str, Any] | None = None

    # -- evidence (cached, strictly budgeted) ----------------------------

    def _evidence_dir_key(self) -> tuple | None:
        """Cheap directory identity: (name, size, mtime_ns) per file."""
        if self.evidence_dir is None or not self.evidence_dir.is_dir():
            return None
        entries = []
        try:
            for path in sorted(self.evidence_dir.glob("*.json"))[:MAX_EVIDENCE_FILES]:
                stat = path.stat()
                entries.append((path.name, stat.st_size, stat.st_mtime_ns))
        except OSError:
            return None
        return tuple(entries)

    def _evidence_state(self) -> dict[str, Any]:
        """Listing + summaries under strict byte/time budgets, cached by
        the directory-stat key. A cache hit costs one directory scan
        (milliseconds); a miss parses at most EVIDENCE_TOTAL_BYTE_BUDGET
        bytes or EVIDENCE_TIME_BUDGET_SECONDS, whichever ends first."""
        key = self._evidence_dir_key()
        with self._evidence_cache_lock:
            if self._evidence_cache_key == key and self._evidence_cache is not None:
                return self._evidence_cache
        artifacts: list[dict[str, Any]] = []
        truncated = False
        if key is not None:
            bytes_read = 0
            deadline = time.monotonic() + EVIDENCE_TIME_BUDGET_SECONDS
            for name, size, _mtime in key:
                if time.monotonic() > deadline or bytes_read + size > EVIDENCE_TOTAL_BYTE_BUDGET:
                    truncated = True
                    break
                if size > MAX_EVIDENCE_FILE_BYTES:
                    continue
                try:
                    artifacts.append(json.loads((self.evidence_dir / name).read_text(encoding="utf-8")))
                    bytes_read += size
                except (OSError, json.JSONDecodeError):
                    continue
        state = {
            "listing": _benchmark_listing(artifacts),
            "summaries": summarize_artifacts(artifacts),
            "truncated": truncated,
        }
        with self._evidence_cache_lock:
            self._evidence_cache_key = key
            self._evidence_cache = state
        return state

    def _evidence_summaries(self) -> list[dict[str, Any]]:
        return self._evidence_state()["summaries"]

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

    def _collect_and_publish(self, generation: int) -> None:
        try:
            snapshot = self._collect_snapshot()
            failure = ""
        except Exception:  # noqa: BLE001 - fixed category, no raw detail
            snapshot = None
            failure = ERROR_REFRESH_FAILED
        with self._lock:
            if generation != self._generation or self._closed.is_set():
                return  # superseded (close) — never publish
            if snapshot is not None:
                # Publish an immutable private copy; the collector's own
                # references are dropped here and never touched again.
                self._published = copy.deepcopy(snapshot)
                self._published_at = time.monotonic()
                self._last_success_at_ms = int(snapshot["captured_at_ms"])
                self._last_failure_category = ""
            else:
                self._last_failure_category = failure

    def _worker_loop(self) -> None:
        while not self._closed.is_set():
            self._refresh_requested.wait()
            if self._closed.is_set():
                return
            self._refresh_requested.clear()
            with self._lock:
                self._generation += 1
                generation = self._generation
                self._collecting = True
            try:
                self._collect_and_publish(generation)
            finally:
                with self._lock:
                    self._collecting = False

    def _request_refresh_locked(self) -> None:
        """Signal the persistent worker; caller must hold the lock. The
        signal is level-triggered — no second thread is ever created,
        even if the worker is hung inside a probe."""
        if self._closed.is_set():
            return
        if self._worker is None:
            self._worker = threading.Thread(
                target=self._worker_loop,
                name="runtime-inventory-refresh",
                daemon=True,
            )
            self._worker.start()
        self._refresh_requested.set()

    def close(self) -> None:
        """Stop the refresher with the sidecar. A worker hung inside a
        probe cannot be killed (daemon thread; dies with the process),
        but it can never publish after close."""
        with self._lock:
            self._closed.set()
            self._generation += 1  # invalidate any in-flight collect
            self._refresh_requested.set()  # wake the loop so it exits
        if self._worker is not None:
            self._worker.join(timeout=2)

    def _observe(self) -> dict[str, Any]:
        """Non-blocking snapshot observation; may signal one refresh."""
        with self._lock:
            snapshot = self._published
            age_seconds = (time.monotonic() - self._published_at) if snapshot is not None else None
            if snapshot is None or age_seconds > INVENTORY_CACHE_TTL_SECONDS:
                self._request_refresh_locked()
            refreshing = self._collecting or self._refresh_requested.is_set()
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
        state = self._evidence_state()
        return {
            "ok": True,
            "available": bool(state["listing"]),
            "artifacts": state["listing"],
            "evidence_summaries": state["summaries"],
            "truncated": state["truncated"],
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


def _benchmark_listing(artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    return sorted(listing, key=lambda item: item["batch_id"])


def _capability_matrix() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import runtime_capability_matrix

    return runtime_capability_matrix()


def _measured_findings() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import measured_findings

    return measured_findings()
