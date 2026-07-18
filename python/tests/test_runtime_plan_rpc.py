from __future__ import annotations

import glob
import json
import os
import threading
import time

import pytest

from odysseus_desktop_backend.runtime_bench.artifacts import validate_artifact, write_artifact
from odysseus_desktop_backend.services import runtime_plan_service as rps
from odysseus_desktop_backend.services.runtime_plan_service import (
    RuntimePlanService,
    evidence_fingerprint,
    placement_band,
    summarize_artifacts,
)

GB = 1024**3
ARTIFACT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "projects", "odysseus", "benchmarks", "local-runtime"
)


def _hardware_snapshot():
    return {
        "schema_version": 1,
        "os": {"name": "Windows", "version": "x", "arch": "AMD64"},
        "cpu": {
            "logical_threads": 12,
            "physical_cores": 6,
            "physical_cores_source": "measured",
            "isa": {"ssse3": True, "sse4_1": True, "sse4_2": True, "avx": True, "avx2": True, "avx512f": False},
        },
        "ram": {"total_bytes": 16 * GB, "available_bytes": 10 * GB},
        "gpus": [
            {"vendor": "nvidia", "name": "RTX 3050", "vram_total_bytes": 4 * GB, "vram_free_bytes": 3 * GB}
        ],
        "npu": "none_detected",
        "storage": {"profile_disk_free_bytes": None, "model_store_disk_free_bytes": 1, "kind": "unknown"},
        "captured_at_ms": 1_000,
    }


def _run(*, cold=False, ttft=310.0, tps=110.0, options=None, gpu_fraction=1.0, quality="passed", error=""):
    run = {
        "run_index": 0,
        "cold": cold,
        "options": options if options is not None else {"temperature": 0, "seed": 42, "num_predict": 256},
        "timings_ms": {"total": 400.0, "load": 0.0, "prompt_eval": 10.0, "generation": 300.0, "first_token": ttft},
        "tokens": {"prompt": 100, "generated": 50, "prompt_tps": 10000.0, "generation_tps": tps},
        "memory": {"runtime_peak_rss_bytes": 1, "system_min_available_bytes": 1, "vram_peak_used_bytes": None, "sampler_interval_ms": 250},
        "quality_check": quality,
        "error_category": error,
    }
    if gpu_fraction is not None:
        run["residency"] = {"size_bytes": 100, "size_vram_bytes": int(100 * gpu_fraction), "gpu_fraction": gpu_fraction, "context_length": 4096}
    return run


def _artifact(batch_id="batch-a", tag="llama3.2:1b", shape="medium", engine_kind="real",
              server_env=None, runs=None, quant="Q8_0", digest=""):
    model = {"tag": tag, "quantization": quant, "format": "gguf", "parameter_size": "1.2B", "disk_bytes": 1_321_098_329}
    if digest:
        model["digest"] = digest
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "captured_at": "2026-07-17T00:00:00Z",
        "hardware": _hardware_snapshot(),
        "runtime": {"name": "ollama", "version": "0.31.1", "server_env": dict(server_env or {})},
        "model": model,
        "shape": shape,
        "mode": "interactive",
        "engine_kind": engine_kind,
        "runs": runs if runs is not None else [_run(cold=True, ttft=3200.0), _run(), _run(ttft=305.0, tps=112.0)],
    }


# -- fingerprints --------------------------------------------------------


def test_placement_bands() -> None:
    assert placement_band(1.0) == "full_gpu"
    assert placement_band(0.995) == "full_gpu"
    assert placement_band(0.4) == "partial_gpu"
    assert placement_band(0.0) == "cpu"
    assert placement_band(None) == "unknown"


def test_fingerprint_covers_required_identity_fields() -> None:
    fingerprint = evidence_fingerprint(_artifact())
    assert fingerprint is not None
    for key in (
        "runtime", "runtime_version", "model_tag", "model_digest", "quantization",
        "model_disk_bytes", "shape", "context", "tuning_options", "server_env",
        "placement_band", "cpu_arch", "physical_cores", "logical_threads",
        "avx2", "avx512f", "gpus", "ram_total_bytes",
    ):
        assert key in fingerprint, key
    assert fingerprint["context"] == "default"
    assert fingerprint["placement_band"] == "full_gpu"
    assert fingerprint["gpus"] == [f"nvidia/RTX 3050/{4 * GB}"]


def test_perf_neutral_env_is_stripped_from_fingerprint() -> None:
    with_no_cloud = evidence_fingerprint(_artifact(server_env={"OLLAMA_NO_CLOUD": "1"}))
    without = evidence_fingerprint(_artifact())
    assert with_no_cloud == without


def test_flash_env_changes_fingerprint() -> None:
    flash = evidence_fingerprint(_artifact(server_env={"OLLAMA_FLASH_ATTENTION": "1"}))
    base = evidence_fingerprint(_artifact())
    assert flash != base


# -- aggregation ---------------------------------------------------------


def test_same_fingerprint_artifacts_aggregate_with_stats() -> None:
    artifacts = [
        _artifact(batch_id="batch-a"),
        _artifact(batch_id="batch-b", runs=[_run(ttft=320.0, tps=100.0), _run(ttft=330.0, tps=105.0)]),
    ]
    summaries = summarize_artifacts(artifacts)
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["sample_count"] == 4  # warm passing runs across both
    assert summary["batch_ids"] == ["batch-a", "batch-b"]
    assert summary["tps_range"][0] == 100.0
    assert summary["tps_range"][1] == 112.0
    assert summary["ttft_range_ms"] == [305.0, 330.0]
    assert summary["warm_ttft_ms"] == 315.0  # median of 305,310,320,330


def test_variant_configurations_never_merge() -> None:
    """Baseline / flash-KV / CPU-only / thread variants keep separate
    summaries — the exact review failure (merged batch ids, one
    variant's metrics) can no longer occur."""
    artifacts = [
        _artifact(batch_id="baseline"),
        _artifact(batch_id="flashkv", server_env={"OLLAMA_FLASH_ATTENTION": "1", "OLLAMA_KV_CACHE_TYPE": "q8_0"}),
        _artifact(batch_id="gpu0", runs=[_run(options={"temperature": 0, "seed": 42, "num_predict": 256, "num_gpu": 0}, gpu_fraction=0.0)]),
        _artifact(batch_id="threads6", runs=[_run(options={"temperature": 0, "seed": 42, "num_predict": 256, "num_thread": 6})]),
    ]
    summaries = summarize_artifacts(artifacts)
    assert len(summaries) == 4
    for summary in summaries:
        assert len(summary["batch_ids"]) == 1


def test_stub_artifacts_never_become_evidence() -> None:
    summaries = summarize_artifacts([_artifact(engine_kind="stub")])
    assert summaries == []


def test_heterogeneous_options_artifact_is_rejected() -> None:
    """One artifact whose warm runs used different tuning options must
    never produce an evidence summary (review round 2, finding 6)."""
    mixed = _artifact(
        batch_id="mixed-options",
        runs=[
            _run(options={"temperature": 0, "seed": 42, "num_predict": 256, "num_thread": 6}),
            _run(options={"temperature": 0, "seed": 42, "num_predict": 256, "num_thread": 12}),
        ],
    )
    assert evidence_fingerprint(mixed) is None
    assert summarize_artifacts([mixed]) == []


def test_heterogeneous_placement_artifact_is_rejected() -> None:
    """Warm runs measured under different placements (e.g. the model
    was reloaded mid-batch onto different hardware) never combine."""
    mixed = _artifact(
        batch_id="mixed-placement",
        runs=[
            _run(gpu_fraction=1.0),
            _run(gpu_fraction=0.4),
        ],
    )
    assert evidence_fingerprint(mixed) is None
    assert summarize_artifacts([mixed]) == []


def test_mixed_context_artifact_is_rejected() -> None:
    mixed = _artifact(
        batch_id="mixed-context",
        runs=[
            _run(options={"temperature": 0, "seed": 42, "num_predict": 256, "num_ctx": 4096}),
            _run(options={"temperature": 0, "seed": 42, "num_predict": 256, "num_ctx": 8192}),
        ],
    )
    assert evidence_fingerprint(mixed) is None
    assert summarize_artifacts([mixed]) == []


def test_missing_residency_on_one_warm_run_is_heterogeneous() -> None:
    mixed = _artifact(
        batch_id="mixed-residency",
        runs=[
            _run(gpu_fraction=1.0),
            _run(gpu_fraction=None),  # residency lost on one run
        ],
    )
    assert evidence_fingerprint(mixed) is None


def test_cold_run_options_do_not_affect_homogeneity() -> None:
    """Cold runs are not contributing measurements; only warm passing
    runs must agree."""
    artifact = _artifact(
        batch_id="cold-differs",
        runs=[
            _run(cold=True, options={"temperature": 0, "seed": 42, "num_predict": 256, "num_gpu": 99}),
            _run(),
            _run(ttft=305.0, tps=112.0),
        ],
    )
    fingerprint = evidence_fingerprint(artifact)
    assert fingerprint is not None
    assert fingerprint["tuning_options"] == {}


def test_cold_and_failed_runs_excluded_from_stats() -> None:
    artifacts = [
        _artifact(
            runs=[
                _run(cold=True, ttft=9999.0),
                _run(quality="failed"),
                _run(error="timeout", quality="not_applicable"),
                _run(ttft=300.0, tps=100.0),
            ]
        )
    ]
    summaries = summarize_artifacts(artifacts)
    assert len(summaries) == 1
    assert summaries[0]["sample_count"] == 1
    assert summaries[0]["warm_ttft_ms"] == 300.0


def test_committed_artifacts_all_validate_and_summarize() -> None:
    """The 31 committed artifacts remain schema-valid under the hardened
    validator and produce fingerprinted summaries without merging
    variants."""
    paths = sorted(glob.glob(os.path.join(ARTIFACT_DIR, "*.json")))
    assert len(paths) >= 31
    artifacts = [json.loads(open(path, encoding="utf-8").read()) for path in paths]
    for path, artifact in zip(paths, artifacts):
        assert validate_artifact(artifact) == [], path
    summaries = summarize_artifacts(artifacts)
    assert summaries, "expected evidence summaries from committed artifacts"
    # No summary may mix a flash-KV batch with a non-flash batch.
    for summary in summaries:
        has_flash = [bid for bid in summary["batch_ids"] if "flashkv" in bid]
        assert not has_flash or has_flash == summary["batch_ids"], summary["batch_ids"]


# -- service over a temp evidence dir ------------------------------------


@pytest.fixture()
def evidence_dir(tmp_path):
    write_artifact(_artifact(batch_id="batch-a"), tmp_path)
    write_artifact(_artifact(batch_id="batch-stub", tag="stub-model", engine_kind="stub"), tmp_path)
    return tmp_path


def test_benchmarks_lists_artifacts_and_summaries(evidence_dir) -> None:
    service = RuntimePlanService(evidence_dir)
    result = service.benchmarks()
    assert result["ok"] is True
    assert [item["batch_id"] for item in result["artifacts"]] == ["batch-a", "batch-stub"]
    assert [item["model_tag"] for item in result["evidence_summaries"]] == ["llama3.2:1b"]


def test_no_evidence_dir_yields_empty_benchmarks() -> None:
    service = RuntimePlanService(None)
    result = service.benchmarks()
    assert result["available"] is False
    assert result["artifacts"] == []
    assert result["evidence_summaries"] == []


def test_malformed_artifact_files_are_skipped(tmp_path) -> None:
    (tmp_path / "junk.json").write_text("not json", encoding="utf-8")
    service = RuntimePlanService(tmp_path)
    assert service.benchmarks()["artifacts"] == []


def test_plan_rejects_invalid_objective() -> None:
    service = RuntimePlanService(None)
    result = service.plan("turbo")
    assert result["ok"] is False
    assert result["error_category"] == "invalid_objective"


# -- background refresher (review round 2, findings 1-2) -----------------


def _await_snapshot(service: RuntimePlanService, timeout: float = 5.0) -> dict:
    """Poll the non-blocking RPC surface until the background refresh
    publishes a snapshot."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = service.inventory()
        if result.get("ok"):
            return result
        time.sleep(0.02)
    pytest.fail("background refresh never published a snapshot")


def _patch_inventories(monkeypatch, *, details_complete=True, collect_counter=None):
    from odysseus_desktop_backend.services import runtime_inventory as ri

    def fake_hw(**kw):
        if collect_counter is not None:
            collect_counter["hardware"] = collect_counter.get("hardware", 0) + 1
        return _hardware_snapshot()

    monkeypatch.setattr(ri, "hardware_inventory", fake_hw)
    monkeypatch.setattr(
        ri,
        "runtime_inventory",
        lambda **kw: {
            "schema_version": 1,
            "runtimes": [
                {"name": "ollama", "installed": True, "reachable": True, "healthy": True, "version": "0.31.1"}
            ],
            "captured_at_ms": 1,
        },
    )
    monkeypatch.setattr(
        ri,
        "model_inventory",
        lambda **kw: {
            "schema_version": 1,
            "runtime": "ollama",
            "models": [
                {
                    "tag": "llama3.2:1b",
                    "digest": "",
                    "disk_bytes": 1_321_098_329,
                    "parameter_size": "1.2B",
                    "quantization": "Q8_0",
                    "capabilities": ["completion"],
                    "kv_geometry": {"layers": 16, "kv_heads": 8, "key_length": 64, "value_length": 64},
                }
            ],
            "details_complete": details_complete,
            "error_category": "",
            "captured_at_ms": 1,
        },
    )


def test_cold_call_returns_refreshing_then_snapshot(monkeypatch) -> None:
    _patch_inventories(monkeypatch)
    service = RuntimePlanService(None)
    first = service.inventory()
    assert first["ok"] is False
    assert first["status"] == "refreshing"
    assert first["error_category"] == rps.ERROR_SNAPSHOT_UNAVAILABLE
    assert first["refresh_state"] in {rps.REFRESH_RUNNING, rps.REFRESH_IDLE}
    ready = _await_snapshot(service)
    assert ready["partial"] is False
    assert ready["cache_age_ms"] >= 0
    assert ready["last_success_at_ms"] > 0
    again = service.inventory()
    assert again["models"] == ready["models"]


def test_snapshot_is_cached_within_ttl(monkeypatch) -> None:
    counter: dict[str, int] = {}
    _patch_inventories(monkeypatch, collect_counter=counter)
    service = RuntimePlanService(None)
    _await_snapshot(service)
    service.inventory()
    service.plan("fast")
    service.recommendations()
    assert counter["hardware"] == 1  # one refresh served every call


def test_partial_snapshot_is_published_and_flagged(monkeypatch) -> None:
    counter: dict[str, int] = {}
    _patch_inventories(monkeypatch, details_complete=False, collect_counter=counter)
    service = RuntimePlanService(None)
    ready = _await_snapshot(service)
    assert ready["partial"] is True
    service.inventory()
    service.inventory()
    assert counter["hardware"] == 1  # partial is served, not re-probed per call


def test_plan_returns_refreshing_when_cold(monkeypatch) -> None:
    _patch_inventories(monkeypatch)
    service = RuntimePlanService(None)
    result = service.plan("fast")
    assert result["ok"] is False
    assert result["status"] == "refreshing"
    _await_snapshot(service)
    result = service.plan("fast")
    assert result["ok"] is True
    assert result["plan"]["model"]["tag"] == "llama3.2:1b"


def test_rpc_path_never_blocks_on_hanging_probes(monkeypatch) -> None:
    """All probes hang forever: every RPC call must still return in
    milliseconds with the fixed refreshing result."""
    from odysseus_desktop_backend.services import runtime_inventory as ri

    def hang(**kw):
        time.sleep(60)
        return {}

    monkeypatch.setattr(ri, "hardware_inventory", hang)
    monkeypatch.setattr(ri, "runtime_inventory", hang)
    monkeypatch.setattr(ri, "model_inventory", hang)
    service = RuntimePlanService(None)
    started = time.perf_counter()
    for _ in range(5):
        for call in (service.inventory, lambda: service.plan("fast"), service.recommendations):
            result = call()
            assert result["ok"] is False
            assert result["status"] == "refreshing"
    elapsed = time.perf_counter() - started
    assert elapsed < 1.0, f"RPC path blocked for {elapsed:.2f}s"


def test_single_refresh_worker_despite_repeated_calls(monkeypatch) -> None:
    """Repeated RPC calls while a hanging refresh is in flight must not
    spawn more workers (review round 2, finding 2)."""
    from odysseus_desktop_backend.services import runtime_inventory as ri

    started_workers = {"count": 0}
    release = threading.Event()

    def slow_hw(**kw):
        started_workers["count"] += 1
        release.wait(30)
        return _hardware_snapshot()

    monkeypatch.setattr(ri, "hardware_inventory", slow_hw)
    service = RuntimePlanService(None)
    try:
        for _ in range(10):
            result = service.inventory()
            assert result["ok"] is False
        time.sleep(0.1)
        for _ in range(10):
            service.plan("balanced")
            service.recommendations()
        assert started_workers["count"] == 1
        # The service holds exactly one live worker thread object; every
        # repeated call observed it instead of starting another.
        with service._lock:
            worker = service._refresh_thread
        assert worker is not None and worker.is_alive()
        service.inventory()
        with service._lock:
            assert service._refresh_thread is worker
    finally:
        release.set()


def test_published_snapshot_never_mutated_by_late_worker(monkeypatch) -> None:
    """A published snapshot must be immutable: a later refresh (or a
    timeout-ignoring probe) can never change an already-returned result."""
    _patch_inventories(monkeypatch)
    service = RuntimePlanService(None)
    ready = _await_snapshot(service)
    frozen = json.dumps(ready["models"], sort_keys=True)
    # Force a new refresh generation with different data.
    from odysseus_desktop_backend.services import runtime_inventory as ri

    monkeypatch.setattr(
        ri,
        "model_inventory",
        lambda **kw: {
            "schema_version": 1,
            "runtime": "ollama",
            "models": [],
            "details_complete": True,
            "error_category": "",
            "captured_at_ms": 2,
        },
    )
    with service._lock:
        service._published_at = 0.0  # make the snapshot stale
    service.inventory()  # starts refresh with new data
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = service.inventory()
        if current.get("ok") and current["models"] == []:
            break
        time.sleep(0.02)
    # The result captured BEFORE the second refresh is untouched.
    assert json.dumps(ready["models"], sort_keys=True) == frozen


def test_obsolete_generation_result_is_discarded(monkeypatch) -> None:
    """A hung worker whose generation was superseded must not publish."""
    _patch_inventories(monkeypatch)
    service = RuntimePlanService(None)
    ready = _await_snapshot(service)
    assert ready["ok"] is True
    with service._lock:
        stale_generation = service._generation
        service._generation += 1  # supersede
    service._refresh_worker(stale_generation)  # runs to completion…
    with service._lock:
        # …but did not publish: last_success timestamp unchanged.
        assert service._last_success_at_ms == ready["last_success_at_ms"]


def test_hanging_detail_probes_stay_within_ceiling() -> None:
    """32 models, every detail probe hangs far beyond its timeout: the
    inventory must return within the detail budget, flagged partial."""
    from odysseus_desktop_backend.services import runtime_inventory as ri

    tags = {"models": [{"name": f"hang{i}:latest", "size": 1, "digest": f"sha256:{i}", "details": {}} for i in range(32)]}

    def hanging_post(url, payload, timeout=None, **kw):
        time.sleep(30)  # deliberately ignores its timeout
        return {}

    started = time.perf_counter()
    inventory = ri.model_inventory(
        get_json=lambda url, **kw: tags,
        post_json=hanging_post,
        detail_budget_seconds=1.0,
    )
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0, f"inventory took {elapsed:.1f}s"
    assert elapsed < rps.REFRESH_WORST_CASE_SECONDS
    assert inventory["details_complete"] is False
    assert len(inventory["models"]) == 32
    # Capped models are marked; probed-but-unanswered models are timeout.
    statuses = {model["detail_status"] for model in inventory["models"]}
    assert statuses <= {"timeout", "probe_cap_reached"}


def test_full_plan_and_recommendations_shape(monkeypatch, tmp_path) -> None:
    _patch_inventories(monkeypatch)
    service = RuntimePlanService(None)
    _await_snapshot(service)
    result = service.plan("fast")
    assert result["ok"] is True
    assert result["plan"]["model"]["tag"] == "llama3.2:1b"
    assert result["inventory_partial"] is False
    recommendations = service.recommendations()
    assert set(recommendations["plans"]) == {"fast", "balanced", "deep"}
    assert "guarantees" in recommendations["note"]
    findings = recommendations["research_findings"]
    assert findings and all(entry["status"] == "measured_exploratory" for entry in findings.values())
    assert list(tmp_path.iterdir()) == []


def test_rpc_methods_registered_and_fixture_in_sync() -> None:
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "ipc_contract.golden.json")
    with open(fixture_path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    for method in ("runtime.inventory", "runtime.benchmarks", "runtime.plan", "runtime.recommendations"):
        assert method in fixture["python_rpc_methods"], method
    assert not any(method.startswith("runtime.") for method in fixture["frontend_rpc_methods"])


def test_no_settings_mutation_surface() -> None:
    service = RuntimePlanService(None)
    public = [name for name in dir(service) if not name.startswith("_")]
    assert sorted(public) == ["benchmarks", "evidence_dir", "inventory", "plan", "recommendations"]


def test_dispatcher_stays_responsive_while_probes_hang(monkeypatch, tmp_path) -> None:
    """Integration-style proof (review round 2, finding 1): with every
    inventory probe hanging, runtime.* dispatch and health.ping both
    answer immediately on the single dispatcher thread."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import rpc_server
    from odysseus_desktop_backend.services import runtime_inventory as ri

    def hang(**kw):
        time.sleep(60)
        return {}

    monkeypatch.setattr(ri, "hardware_inventory", hang)
    monkeypatch.setattr(ri, "runtime_inventory", hang)
    monkeypatch.setattr(ri, "model_inventory", hang)

    app = rpc_server.SidecarApp(tmp_path / "profile")
    try:
        started = time.perf_counter()
        runtime_result = app.runtime_inventory({})
        ping_result = app.health_ping({})
        plan_result = app.runtime_plan_get({"objective": "fast"})
        elapsed = time.perf_counter() - started
        assert elapsed < 2.0, f"dispatcher blocked for {elapsed:.2f}s"
        assert runtime_result["ok"] is False
        assert runtime_result["status"] == "refreshing"
        assert plan_result["ok"] is False
        assert ping_result.get("ok") or ping_result.get("status") or ping_result
    finally:
        shutdown = getattr(app, "shutdown", None) or getattr(app, "close", None)
        if callable(shutdown):
            try:
                shutdown()
            except TypeError:
                pass
