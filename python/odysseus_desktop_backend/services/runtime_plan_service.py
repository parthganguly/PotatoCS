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
# Strict budgets for one background evidence-index pass: total bytes
# parsed and wall-clock time are both capped; exceeding either stops
# the pass with an explicit truncation reason. Per-file and total byte
# limits are enforced WHILE reading, not only from a pre-read stat.
EVIDENCE_TOTAL_BYTE_BUDGET = 16 * 1024 * 1024
EVIDENCE_TIME_BUDGET_SECONDS = 1.0
EVIDENCE_INDEX_TTL_SECONDS = 60.0

INVENTORY_CACHE_TTL_SECONDS = 60.0

# Worst-case BACKGROUND refresh duration (every probe at its cap, all
# bounds from runtime_inventory): hardware = nvidia-smi 3.0s; runtimes =
# tcp 0.5s + ollama version 2.5s + llama-server --version 3.0s; models =
# tags 2.5s + detail budget 2.0s (+0.5s join slack) = 14.0s. This bounds
# the refresher thread, NOT the dispatcher: RPC calls never wait for it.
REFRESH_WORST_CASE_SECONDS = 14.0
# A collection older than worst case + margin is presumed hung: the
# state machine reports `refresh_hung`, no replacement thread is ever
# started, and honest copy says a sidecar restart is required for
# future refreshes.
REFRESH_HUNG_THRESHOLD_SECONDS = REFRESH_WORST_CASE_SECONDS + 6.0
# close() waits through the bounded ceiling plus margin for a live
# collection before reporting worker_still_running.
CLOSE_WAIT_MARGIN_SECONDS = 2.0

ERROR_SNAPSHOT_UNAVAILABLE = "snapshot_unavailable"
ERROR_REFRESH_FAILED = "refresh_failed"
ERROR_RESTART_REQUIRED = "restart_required"
ERROR_EVIDENCE_REFRESHING = "evidence_refreshing"

REFRESH_IDLE = "idle"
REFRESH_RUNNING = "refreshing"
REFRESH_HUNG = "refresh_hung"
REFRESH_CLOSED = "closed"

TRUNCATION_FILE_COUNT = "file_count"
TRUNCATION_BYTE_BUDGET = "byte_budget"
TRUNCATION_TIME_BUDGET = "time_budget"

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


class _PersistentRefresher:
    """One persistent daemon worker (never replaced, never stacked).

    State machine:

        idle + no result   --observe--> signal refresh, report refreshing
        idle + fresh       --observe--> serve result
        idle + stale       --observe--> serve result + age, signal refresh
        refreshing         --observe--> serve result/`refreshing`; the
                                        signal is level-triggered, no
                                        second thread ever starts
        collect done       --publish--> immutable deep-copied result,
                                        generation-checked (a collect
                                        superseded by close() never
                                        publishes)
        collect older than REFRESH_HUNG_THRESHOLD_SECONDS
                           --observe--> `refresh_hung`: no replacement
                                        thread, no more signals; honest
                                        copy says a sidecar restart is
                                        required; the last completed
                                        result keeps being served
        close()            ----------> loop exits at next wakeup; close
                                        reports worker_stopped vs
                                        worker_still_running honestly
    """

    def __init__(self, name: str, collect):
        self._name = name
        self._collect_fn = collect
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._requested = threading.Event()
        self._closed = threading.Event()
        self._collecting = False
        self._collect_started_at = 0.0
        self._generation = 0
        self._published: dict[str, Any] | None = None
        self._published_at = 0.0
        self._last_success_at_ms = 0
        self._failure_category = ""

    def _finish_collect(self, generation: int, result: dict[str, Any] | None, failure: str) -> None:
        with self._lock:
            self._collecting = False
            if generation != self._generation or self._closed.is_set():
                return  # superseded (close): never publish
            if result is not None:
                # Publish an immutable private copy; the collector's own
                # references are dropped here and never touched again.
                self._published = copy.deepcopy(result)
                self._published_at = time.monotonic()
                self._last_success_at_ms = utc_ms()
                self._failure_category = ""
            else:
                self._failure_category = failure

    def _loop(self) -> None:
        while not self._closed.is_set():
            self._requested.wait()
            if self._closed.is_set():
                return
            self._requested.clear()
            with self._lock:
                self._generation += 1
                generation = self._generation
                self._collecting = True
                self._collect_started_at = time.monotonic()
            try:
                result = self._collect_fn()
                failure = ""
            except Exception:  # noqa: BLE001 - fixed category, no raw detail
                result = None
                failure = ERROR_REFRESH_FAILED
            self._finish_collect(generation, result, failure)

    def request_refresh(self) -> None:
        with self._lock:
            if self._closed.is_set():
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
                self._thread.start()
            self._requested.set()

    def publish(self, result: dict[str, Any]) -> None:
        """Eagerly publish a known result without a worker (used for the
        trivial empty-evidence case so no thread is needed)."""
        with self._lock:
            self._published = copy.deepcopy(result)
            self._published_at = time.monotonic()
            self._last_success_at_ms = utc_ms()

    def observe(self, ttl_seconds: float) -> dict[str, Any]:
        """Non-blocking observation; may signal one refresh. Never scans,
        reads, or parses anything on the caller's thread."""
        with self._lock:
            now = time.monotonic()
            published = self._published
            age_seconds = (now - self._published_at) if published is not None else None
            collecting = self._collecting
            collect_age = (now - self._collect_started_at) if collecting else None
            closed = self._closed.is_set()
            hung = bool(collecting and collect_age is not None and collect_age > REFRESH_HUNG_THRESHOLD_SECONDS)
            result_copy = copy.deepcopy(published) if published is not None else None
            last_success = self._last_success_at_ms
            failure = self._failure_category
        if closed:
            state = REFRESH_CLOSED
        elif hung:
            state = REFRESH_HUNG
        elif collecting or self._requested.is_set():
            state = REFRESH_RUNNING
        else:
            state = REFRESH_IDLE
        stale = published is None or (age_seconds is not None and age_seconds > ttl_seconds)
        if stale and state == REFRESH_IDLE:
            self.request_refresh()
            state = REFRESH_RUNNING
        return {
            "result": result_copy,
            "age_ms": int(age_seconds * 1000) if age_seconds is not None else None,
            "refresh_state": state,
            "refresh_age_ms": int(collect_age * 1000) if collect_age is not None else None,
            "last_success_at_ms": last_success,
            "failure_category": ERROR_RESTART_REQUIRED if hung else failure,
        }

    def close(self) -> dict[str, Any]:
        """Stop the loop. Waits CLOSE_WAIT_MARGIN_SECONDS and then
        reports honestly: a worker mid-collect may legitimately outlive
        close (it can never publish afterwards — generation + closed
        checks); a timeout-ignoring probe is reported as
        worker_still_running, never claimed terminated."""
        with self._lock:
            self._closed.set()
            self._generation += 1  # invalidate any in-flight collect
            self._requested.set()  # wake the loop so it exits
            thread = self._thread
        if thread is None:
            return {"worker_stopped": True, "worker_still_running": False}
        thread.join(timeout=CLOSE_WAIT_MARGIN_SECONDS)
        stopped = not thread.is_alive()
        return {"worker_stopped": stopped, "worker_still_running": not stopped}


class RuntimePlanService:
    """Two persistent background workers — inventory probes and the
    evidence index — with a never-probing, never-parsing RPC path."""

    def __init__(self, evidence_dir: str | Path | None = None):
        # Research phase: benchmark evidence is a dev-machine artifact
        # directory; production profiles have none and the planner
        # degrades to derived/conservative confidence by design.
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None
        self._inventory = _PersistentRefresher("runtime-inventory-refresh", self._collect_snapshot)
        self._evidence = _PersistentRefresher("evidence-index-refresh", self._build_evidence_index)
        if self.evidence_dir is None:
            # No evidence source: the empty index is known without I/O.
            self._evidence.publish(_empty_evidence_index())

    # -- background collectors (worker threads only) ---------------------

    def _collect_snapshot(self) -> dict[str, Any]:
        """Probe sequence; runs ONLY on the inventory refresher thread."""
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

    def _build_evidence_index(self) -> dict[str, Any]:
        """Discover, validate, parse, and summarize evidence artifacts;
        runs ONLY on the evidence refresher thread. Every enforcement is
        performed while reading: symlinks/reparse points are skipped,
        per-file and total byte limits are checked against actual bytes
        read, schema validation gates every decoded object, and one bad
        artifact can never abort the pass."""
        import os

        from odysseus_desktop_backend.runtime_bench.artifacts import validate_artifact

        stats = {
            "files_seen": 0,
            "files_parsed": 0,
            "skipped_malformed": 0,
            "skipped_invalid_schema": 0,
            "skipped_oversized": 0,
            "skipped_not_regular": 0,
            "skipped_unreadable": 0,
            "total_parsed_bytes": 0,
            "truncation_reasons": [],
        }
        artifacts: list[dict[str, Any]] = []
        root = self.evidence_dir
        if root is not None and root.is_dir():
            root_resolved = root.resolve()
            names: list[str] = []
            try:
                with os.scandir(root) as entries:
                    for entry in entries:
                        if not entry.name.endswith(".json"):
                            continue
                        stats["files_seen"] += 1
                        try:
                            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                                stats["skipped_not_regular"] += 1
                                continue
                        except OSError:
                            stats["skipped_unreadable"] += 1
                            continue
                        names.append(entry.name)
            except OSError:
                names = []
            names.sort()
            if len(names) > MAX_EVIDENCE_FILES:
                names = names[:MAX_EVIDENCE_FILES]
                stats["truncation_reasons"].append(TRUNCATION_FILE_COUNT)
            deadline = time.monotonic() + EVIDENCE_TIME_BUDGET_SECONDS
            for name in names:
                if time.monotonic() > deadline:
                    stats["truncation_reasons"].append(TRUNCATION_TIME_BUDGET)
                    break
                path = root / name
                try:
                    if path.resolve().parent != root_resolved:
                        stats["skipped_not_regular"] += 1
                        continue
                    with open(path, "rb") as handle:
                        data = handle.read(MAX_EVIDENCE_FILE_BYTES + 1)
                except OSError:
                    stats["skipped_unreadable"] += 1
                    continue
                if len(data) > MAX_EVIDENCE_FILE_BYTES:
                    stats["skipped_oversized"] += 1
                    continue
                if stats["total_parsed_bytes"] + len(data) > EVIDENCE_TOTAL_BYTE_BUDGET:
                    stats["truncation_reasons"].append(TRUNCATION_BYTE_BUDGET)
                    break
                try:
                    decoded = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    stats["skipped_malformed"] += 1
                    continue
                if not isinstance(decoded, dict) or validate_artifact(decoded):
                    stats["skipped_invalid_schema"] += 1
                    continue
                stats["total_parsed_bytes"] += len(data)
                stats["files_parsed"] += 1
                artifacts.append(decoded)
        return {
            "listing": _benchmark_listing(artifacts),
            "summaries": summarize_artifacts(artifacts),
            "stats": stats,
            "captured_at_ms": utc_ms(),
        }

    # -- lifecycle -------------------------------------------------------

    def close(self) -> dict[str, Any]:
        """Stop both workers; returns per-worker stopped state honestly.
        Bounded: at most 2 x CLOSE_WAIT_MARGIN_SECONDS. A worker stuck
        in a timeout-ignoring probe is reported worker_still_running —
        it can never publish after close, and it dies with the process."""
        inventory_result = self._inventory.close()
        evidence_result = self._evidence.close()
        result = {
            "inventory_worker": inventory_result,
            "evidence_worker": evidence_result,
            "all_stopped": inventory_result["worker_stopped"] and evidence_result["worker_stopped"],
        }
        logger.info(
            "runtime plan service closed inventory_stopped=%s evidence_stopped=%s",
            inventory_result["worker_stopped"],
            evidence_result["worker_stopped"],
        )
        return result

    # -- observation helpers ---------------------------------------------

    def _observe(self) -> dict[str, Any]:
        observed = self._inventory.observe(INVENTORY_CACHE_TTL_SECONDS)
        observed["snapshot"] = observed.pop("result")
        observed["cache_age_ms"] = observed.pop("age_ms")
        return observed

    def _observe_evidence(self) -> dict[str, Any]:
        observed = self._evidence.observe(EVIDENCE_INDEX_TTL_SECONDS)
        observed["index"] = observed.pop("result")
        return observed

    def _unavailable(self, observed: dict[str, Any]) -> dict[str, Any]:
        if observed["refresh_state"] == REFRESH_HUNG:
            return {
                "ok": False,
                "status": "unavailable",
                "error_category": ERROR_RESTART_REQUIRED,
                "error": (
                    "The runtime inventory refresher is stuck and cannot refresh "
                    "again until the app is restarted. No inventory snapshot is "
                    "available."
                ),
                "refresh_state": observed["refresh_state"],
                "refresh_age_ms": observed["refresh_age_ms"],
                "last_success_at_ms": observed["last_success_at_ms"],
            }
        return {
            "ok": False,
            "status": "refreshing",
            "error_category": observed["failure_category"] or ERROR_SNAPSHOT_UNAVAILABLE,
            "error": (
                "The runtime inventory snapshot is not ready yet; a background "
                "refresh is in progress."
            ),
            "refresh_state": observed["refresh_state"],
            "refresh_age_ms": observed["refresh_age_ms"],
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
            "refresh_age_ms": observed["refresh_age_ms"],
            "last_success_at_ms": observed["last_success_at_ms"],
            "captured_at_ms": snapshot["captured_at_ms"],
        }

    def benchmarks(self) -> dict[str, Any]:
        observed = self._observe_evidence()
        index = observed["index"]
        if index is None:
            return {
                "ok": False,
                "status": "evidence_refreshing",
                "error_category": ERROR_EVIDENCE_REFRESHING,
                "error": "The benchmark evidence index is being built in the background.",
                "refresh_state": observed["refresh_state"],
                "last_success_at_ms": observed["last_success_at_ms"],
            }
        return {
            "ok": True,
            "available": bool(index["listing"]),
            "artifacts": index["listing"],
            "evidence_summaries": index["summaries"],
            "stats": index["stats"],
            "truncated": bool(index["stats"]["truncation_reasons"]),
            "index_age_ms": observed["age_ms"],
            "index_captured_at_ms": index["captured_at_ms"],
            "refresh_state": observed["refresh_state"],
        }

    def _evidence_summaries_snapshot(self) -> list[dict[str, Any]]:
        """Planner evidence from the LAST COMPLETED index only — never
        computed on the request path. No index yet = no evidence (the
        planner degrades to derived confidence honestly)."""
        observed = self._observe_evidence()
        index = observed["index"]
        return index["summaries"] if index is not None else []

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
            evidence=self._evidence_summaries_snapshot(),
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
        evidence = self._evidence_summaries_snapshot()
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


def _empty_evidence_index() -> dict[str, Any]:
    return {
        "listing": [],
        "summaries": [],
        "stats": {
            "files_seen": 0,
            "files_parsed": 0,
            "skipped_malformed": 0,
            "skipped_invalid_schema": 0,
            "skipped_oversized": 0,
            "skipped_not_regular": 0,
            "skipped_unreadable": 0,
            "total_parsed_bytes": 0,
            "truncation_reasons": [],
        },
        "captured_at_ms": utc_ms(),
    }


def _capability_matrix() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import runtime_capability_matrix

    return runtime_capability_matrix()


def _measured_findings() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import measured_findings

    return measured_findings()
