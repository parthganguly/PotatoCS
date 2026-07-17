from __future__ import annotations

import json

import pytest

from odysseus_desktop_backend.services import runtime_planner as rp

GB = 1024**3


def _hardware(*, ram_total=16 * GB, ram_available=10 * GB, vram_free=3 * GB, cores=6, gpus=True):
    return {
        "cpu": {"logical_threads": cores * 2, "physical_cores": cores, "physical_cores_source": "measured"},
        "ram": {"total_bytes": ram_total, "available_bytes": ram_available},
        "gpus": (
            [{"vendor": "nvidia", "name": "RTX", "vram_total_bytes": 4 * GB, "vram_free_bytes": vram_free}]
            if gpus
            else []
        ),
    }


def _model(tag="llama3.2:1b", disk=1_321_098_329, params="1.2B", quant="Q8_0", capabilities=("completion",)):
    return {
        "tag": tag,
        "disk_bytes": disk,
        "parameter_size": params,
        "quantization": quant,
        "capabilities": list(capabilities),
    }


def _runtimes(healthy=True, version="0.31.1"):
    return [
        {"name": "ollama", "installed": True, "reachable": healthy, "healthy": healthy, "version": version},
        {"name": "llamacpp", "installed": False, "reachable": False, "healthy": False, "version": ""},
    ]


def _plan(objective="fast", models=None, hardware=None, runtimes=None, evidence=None, now_ms=0):
    return rp.build_plan(
        objective=objective,
        hardware=hardware or _hardware(),
        models=models if models is not None else [_model()],
        runtimes=runtimes or _runtimes(),
        evidence=evidence,
        now_ms=now_ms,
    )


# -- basic contract ------------------------------------------------------


def test_plan_is_versioned_and_selects_runtime_and_model() -> None:
    plan = _plan()
    assert plan["plan_version"] == rp.PLAN_VERSION
    assert plan["runtime"] == {"name": "ollama", "version": "0.31.1"}
    assert plan["model"]["tag"] == "llama3.2:1b"
    assert plan["fit_class"] == "fits_gpu_full"
    assert plan["options"]["num_ctx"] == rp.CONTEXT_BY_OBJECTIVE["fast"]
    assert plan["options"]["num_thread"] == 6
    assert plan["thresholds"]["provisional"] is True


def test_invalid_objective_raises() -> None:
    with pytest.raises(ValueError):
        _plan(objective="turbo")


def test_planner_is_deterministic() -> None:
    models = [_model(), _model(tag="qwen3:8b", disk=5_200_000_000, params="8.2B", quant="Q4_K_M")]
    first = _plan(objective="balanced", models=models)
    second = _plan(objective="balanced", models=models)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_planner_never_mutates_inputs() -> None:
    hardware = _hardware()
    models = [_model()]
    runtimes = _runtimes()
    before = json.dumps([hardware, models, runtimes], sort_keys=True)
    _plan(models=models, hardware=hardware, runtimes=runtimes)
    assert json.dumps([hardware, models, runtimes], sort_keys=True) == before


# -- safety margins ------------------------------------------------------


def test_model_exceeding_available_ram_is_rejected_for_fast() -> None:
    big = _model(tag="huge:70b", disk=40 * GB, params="70B")
    plan = _plan(objective="fast", models=[big])
    assert plan["model"] is None
    reasons = {item["model"]: item["reason_code"] for item in plan["rejected_alternatives"]}
    assert reasons["huge:70b"] in {rp.REJECT_EXCEEDS_RAM, rp.REJECT_EXCEEDS_DEEP}


def test_model_never_claimed_to_fit_when_estimate_exceeds_margin() -> None:
    # 9 GB weights, 10 GB available RAM, 16 GB total -> margin ~1.9 GB
    # -> budget ~8.1 GB < estimate: must NOT be selected for fast/balanced.
    borderline = _model(tag="borderline:9b", disk=9 * GB, params="9B")
    for objective in ("fast", "balanced"):
        plan = _plan(objective=objective, models=[borderline], hardware=_hardware(vram_free=0))
        assert plan["model"] is None, objective


def test_deep_objective_reaches_via_total_ram_envelope() -> None:
    borderline = _model(tag="borderline:9b", disk=9 * GB, params="9B")
    plan = _plan(objective="deep", models=[borderline], hardware=_hardware(vram_free=0))
    assert plan["model"]["tag"] == "borderline:9b"
    assert plan["fit_class"] in {"fits_gpu_partial", "fits_cpu_ram", "reachable_deep_local"}


def test_truly_giant_model_is_not_runnable_even_deep() -> None:
    giant = _model(tag="giant:120b", disk=70 * GB, params="120B")
    plan = _plan(objective="deep", models=[giant])
    assert plan["model"] is None
    assert plan["rejected_alternatives"][0]["reason_code"] == rp.REJECT_EXCEEDS_DEEP
    detail = plan["rejected_alternatives"][0]["detail_numbers"]
    assert detail["estimated_total_bytes"] > detail["ram_total_bytes"]


def test_embedding_model_rejected_for_chat_planning() -> None:
    embed = _model(tag="nomic-embed-text:latest", disk=274_000_000, params="", capabilities=("embedding",))
    plan = _plan(models=[embed, _model()])
    assert plan["model"]["tag"] == "llama3.2:1b"
    reasons = {item["model"]: item["reason_code"] for item in plan["rejected_alternatives"]}
    assert reasons["nomic-embed-text:latest"] == rp.REJECT_NOT_TEXT_MODEL


# -- conservative degradation --------------------------------------------


def test_unknown_hardware_produces_conservative_defaults() -> None:
    plan = _plan(hardware={"cpu": {}, "ram": {}, "gpus": []})
    assert plan["confidence"] == "conservative_default"
    assert rp.WARN_UNKNOWN_HARDWARE in plan["warnings"]
    # No RAM info -> no fit claims possible -> nothing selected.
    assert plan["model"] is None or plan["confidence"] == "conservative_default"


def test_runtime_unavailable_fails_safe() -> None:
    plan = _plan(runtimes=_runtimes(healthy=False))
    assert plan["model"] is None
    assert plan["warnings"] == ["runtime_unavailable"]
    assert all(item["reason_code"] == rp.REJECT_RUNTIME_UNAVAILABLE for item in plan["rejected_alternatives"])


def test_no_gpu_probe_adds_warning_and_cpu_path() -> None:
    plan = _plan(hardware=_hardware(gpus=False))
    assert rp.WARN_NO_GPU_PROBE in plan["warnings"]
    assert plan["fit_class"] == "fits_cpu_ram"
    assert plan["estimates"]["vram_bytes"] == 0


def test_num_thread_omitted_on_tiny_cpus() -> None:
    plan = _plan(hardware=_hardware(cores=2))
    assert "num_thread" not in plan["options"]


# -- evidence and confidence ---------------------------------------------


def _evidence(tag="llama3.2:1b", captured_at_ms=1_000, threads=12, ttft=300.0, tps=120.0):
    return {
        "model_tag": tag,
        "captured_at_ms": captured_at_ms,
        "hardware_cpu": {"logical_threads": threads},
        "warm_ttft_ms": ttft,
        "generation_tps": tps,
        "batch_ids": ["batch-1"],
    }


def test_fresh_matching_evidence_gives_measured_confidence() -> None:
    plan = _plan(evidence=[_evidence()], now_ms=2_000)
    assert plan["confidence"] == "measured"
    assert plan["estimates"]["generation_tps"] == 120.0
    assert plan["evidence"]["benchmark_batch_ids"] == ["batch-1"]
    assert rp.WARN_NO_EVIDENCE not in plan["warnings"]


def test_stale_evidence_lowers_confidence() -> None:
    month_ms = rp.EVIDENCE_MAX_AGE_DAYS * 86_400_000
    plan = _plan(evidence=[_evidence(captured_at_ms=1_000)], now_ms=month_ms + 100_000)
    assert plan["confidence"] == "derived"
    assert rp.WARN_STALE_EVIDENCE in plan["warnings"]
    assert plan["evidence"]["stale"] is True


def test_mismatched_hardware_evidence_is_treated_as_stale() -> None:
    plan = _plan(evidence=[_evidence(threads=99)], now_ms=2_000)
    assert plan["confidence"] == "derived"
    assert rp.WARN_STALE_EVIDENCE in plan["warnings"]


def test_no_evidence_yields_derived_with_warning() -> None:
    plan = _plan(evidence=[])
    assert plan["confidence"] == "derived"
    assert rp.WARN_NO_EVIDENCE in plan["warnings"]


# -- objectives and execution class --------------------------------------


def test_fast_prefers_fastest_interactive_model() -> None:
    models = [
        _model(),
        _model(tag="qwen3:8b", disk=5_200_000_000, params="8.2B", quant="Q4_K_M"),
    ]
    plan = _plan(objective="fast", models=models)
    assert plan["model"]["tag"] == "llama3.2:1b"
    reasons = {item["model"]: item["reason_code"] for item in plan["rejected_alternatives"]}
    assert reasons["qwen3:8b"] == rp.REJECT_SLOWER_ALTERNATIVE


def test_balanced_prefers_more_capable_usable_model() -> None:
    models = [
        _model(),
        _model(tag="qwen3:8b", disk=5_200_000_000, params="8.2B", quant="Q4_K_M"),
    ]
    plan = _plan(objective="balanced", models=models)
    assert plan["model"]["tag"] == "qwen3:8b"
    reasons = {item["model"]: item["reason_code"] for item in plan["rejected_alternatives"]}
    assert reasons["llama3.2:1b"] == rp.REJECT_LESS_CAPABLE


def test_classify_execution_boundaries() -> None:
    assert rp.classify_execution(1_000, 50.0) == "interactive"
    assert rp.classify_execution(30_000, 2.0) == "slow_interactive"
    assert rp.classify_execution(120_000, 2.0) == "persisted_job"
    assert rp.classify_execution(5_000, 0.5) == "persisted_job"
    assert rp.classify_execution(None, None) == "persisted_job"


def test_persisted_job_class_never_labeled_interactive() -> None:
    # Deep objective with a slow CPU-bound estimate -> not "interactive".
    big = _model(tag="big:14b", disk=8 * GB, params="14B")
    plan = _plan(objective="deep", models=[big], hardware=_hardware(ram_available=12 * GB, vram_free=0))
    assert plan["model"]["tag"] == "big:14b"
    assert plan["execution_class"] in {"slow_interactive", "persisted_job"}
    assert rp.WARN_SLOW_CLASS in plan["warnings"]


# -- memory estimation ---------------------------------------------------


def test_memory_estimate_components_and_conservatism() -> None:
    estimate = rp.estimate_memory_bytes(_model(), 4096)
    assert estimate["weights"] == 1_321_098_329
    assert estimate["kv_cache"] == int(4096 * 1.2 * rp.KV_CACHE_BYTES_PER_TOKEN_PER_B_PARAMS)
    assert estimate["total"] == estimate["weights"] + estimate["kv_cache"] + rp.RUNTIME_OVERHEAD_BYTES


def test_memory_estimate_without_disk_size_estimates_high() -> None:
    estimate = rp.estimate_memory_bytes({"tag": "x", "parameter_size": "8B", "disk_bytes": 0}, 4096)
    assert estimate["weights"] >= int(8 * 1.0 * GB)


def test_larger_context_increases_estimate() -> None:
    small = rp.estimate_memory_bytes(_model(), 2048)
    large = rp.estimate_memory_bytes(_model(), 16384)
    assert large["total"] > small["total"]


def test_unsupported_flags_are_never_emitted() -> None:
    plan = _plan()
    # Only capability-proven per-request options may appear.
    assert set(plan["options"]) <= {"num_ctx", "num_thread", "num_gpu", "num_batch", "keep_alive"}
    # num_gpu/num_batch are unproven until Phase 4 evidence: must be absent.
    assert "num_gpu" not in plan["options"]
    assert "num_batch" not in plan["options"]
    assert plan["server_env"] == {}
