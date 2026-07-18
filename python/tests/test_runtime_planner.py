from __future__ import annotations

import json
import os

import pytest

from odysseus_desktop_backend.services import runtime_planner as rp

GB = 1024**3
ARTIFACT_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "projects", "odysseus", "benchmarks", "local-runtime"
)

# Real KV geometries verified live against Ollama 0.31.1 /api/show.
GEOM_LLAMA32_1B = {"layers": 16, "kv_heads": 8, "key_length": 64, "value_length": 64}
GEOM_LLAMA32_3B = {"layers": 28, "kv_heads": 8, "key_length": 128, "value_length": 128}
GEOM_QWEN3_8B = {"layers": 36, "kv_heads": 8, "key_length": 128, "value_length": 128}


def _hardware(*, ram_total=16 * GB, ram_available=10 * GB, vram_free=3 * GB, cores=6, gpus=True):
    return {
        "os": {"name": "Windows", "version": "x", "arch": "AMD64"},
        "cpu": {
            "logical_threads": cores * 2,
            "physical_cores": cores,
            "physical_cores_source": "measured",
            "isa": {"avx2": True, "avx512f": False},
        },
        "ram": {"total_bytes": ram_total, "available_bytes": ram_available},
        "gpus": (
            [
                {
                    "vendor": "nvidia",
                    "name": "RTX 3050",
                    "vram_total_bytes": 4 * GB,
                    "vram_free_bytes": vram_free,
                }
            ]
            if gpus
            else []
        ),
    }


def _model(
    tag="llama3.2:1b",
    disk=1_321_098_329,
    params="1.2B",
    quant="Q8_0",
    capabilities=("completion",),
    geometry=GEOM_LLAMA32_1B,
    digest="sha256:aaa",
):
    return {
        "tag": tag,
        "digest": digest,
        "disk_bytes": disk,
        "parameter_size": params,
        "quantization": quant,
        "capabilities": list(capabilities),
        "kv_geometry": dict(geometry) if geometry else None,
    }


def _runtimes(healthy=True, version="0.31.1"):
    return [
        {"name": "ollama", "installed": True, "reachable": healthy, "healthy": healthy, "version": version},
        {"name": "llamacpp", "installed": False, "reachable": False, "healthy": False, "version": ""},
    ]


def _evidence_for(model, hardware, *, shape="medium", context="default", ttft=300.0, tps=120.0,
                  placement="full_gpu", captured_at_ms=1_000, runtime_version="0.31.1",
                  tuning=None, server_env=None, batch_ids=("batch-1",), sample_count=3):
    fingerprint = {
        "runtime": "ollama",
        "runtime_version": runtime_version,
        "model_tag": model["tag"],
        "model_digest": model.get("digest") or "",
        "quantization": model["quantization"],
        "model_disk_bytes": model["disk_bytes"],
        "shape": shape,
        "context": context,
        "tuning_options": dict(tuning or {}),
        "server_env": dict(server_env or {}),
        "placement_band": placement,
        **rp.hardware_fingerprint_fields(hardware),
    }
    return {
        "fingerprint": fingerprint,
        "model_tag": model["tag"],
        "shape": shape,
        "sample_count": sample_count,
        "warm_ttft_ms": ttft,
        "ttft_range_ms": [ttft, ttft],
        "generation_tps": tps,
        "tps_range": [tps, tps],
        "batch_ids": list(batch_ids),
        "captured_at_ms": captured_at_ms,
    }


def _plan(objective="fast", models=None, hardware=None, runtimes=None, evidence=None, now_ms=0):
    return rp.build_plan(
        objective=objective,
        hardware=hardware or _hardware(),
        models=models if models is not None else [_model()],
        runtimes=runtimes or _runtimes(),
        evidence=evidence,
        now_ms=now_ms,
    )


# -- KV-cache estimation (review finding 1) ------------------------------


def test_kv_cache_bytes_llama_1b_geometry() -> None:
    # (64+64) * 8 heads * 16 layers * 2 bytes = 32 KiB per token.
    bytes_, known = rp.kv_cache_bytes(GEOM_LLAMA32_1B, 8192)
    assert known is True
    assert bytes_ == 32 * 1024 * 8192  # 256 MiB


def test_kv_cache_bytes_llama_3b_gqa_geometry() -> None:
    # (128+128) * 8 * 28 * 2 = 112 KiB per token; 4096 tokens = 448 MiB.
    bytes_, known = rp.kv_cache_bytes(GEOM_LLAMA32_3B, 4096)
    assert known is True
    assert bytes_ == 114_688 * 4096


def test_kv_cache_bytes_qwen3_8b_geometry() -> None:
    # (128+128) * 8 * 36 * 2 = 144 KiB per token.
    bytes_, known = rp.kv_cache_bytes(GEOM_QWEN3_8B, 4096)
    assert known is True
    assert bytes_ == 147_456 * 4096


def test_unknown_geometry_uses_conservative_upper_bound() -> None:
    for geometry in (None, {}, {"layers": 16}, {"layers": 16, "kv_heads": 0, "key_length": 64, "value_length": 64}):
        bytes_, known = rp.kv_cache_bytes(geometry, 4096)
        assert known is False
        assert bytes_ == rp.UNKNOWN_KV_BYTES_PER_TOKEN * 4096
        # The bound must exceed every known geometry in this repo.
        assert bytes_ > rp.kv_cache_bytes(GEOM_QWEN3_8B, 4096)[0]


def test_old_formula_scale_error_is_gone() -> None:
    # The pre-review formula gave ~0.3 MB for 3.2B @ 4096; the real KV
    # is ~448 MB. Guard against any regression to a per-B heuristic.
    estimate = rp.estimate_memory_bytes(_model(tag="llama3.2:latest", disk=2_019_393_189, params="3.2B", geometry=GEOM_LLAMA32_3B), 4096)
    assert estimate["kv_cache"] > 400 * 1024 * 1024


def _residency_observation(batch_file: str) -> tuple[int, int]:
    """(reported_total_bytes, weights_bytes) from a committed artifact's
    residency: Ollama's `size` covers weights + KV + graph buffers."""
    path = os.path.join(ARTIFACT_DIR, batch_file)
    artifact = json.loads(open(path, encoding="utf-8").read())
    weights = int(artifact["model"]["disk_bytes"])
    for run in artifact["runs"]:
        residency = run.get("residency")
        if residency and residency.get("size_bytes"):
            return int(residency["size_bytes"]), weights
    pytest.skip(f"no residency recorded in {batch_file}")


def test_estimate_vs_committed_residency_3b_default_ctx() -> None:
    """Estimator vs the paired-default artifact (f16 KV, ctx 4096).

    Documented tolerance: estimated KV+overhead must be >= 60% of the
    observed non-weight residency (the observation includes flash/graph
    buffers our overhead constant models coarsely) and <= 4x it (no
    absurd over-reserve for known geometry).
    """
    observed_total, weights = _residency_observation("ollama-0311-llama32-3b-medium-paired-default.json")
    observed_nonweight = observed_total - weights
    estimate = rp.estimate_memory_bytes(
        _model(tag="llama3.2:latest", disk=weights, params="3.2B", geometry=GEOM_LLAMA32_3B), 4096
    )
    estimated_nonweight = estimate["kv_cache"] + rp.RUNTIME_OVERHEAD_BYTES
    assert estimated_nonweight >= 0.6 * observed_nonweight
    assert estimated_nonweight <= 4 * observed_nonweight


def test_estimate_vs_committed_residency_qwen3_default_ctx() -> None:
    observed_total, weights = _residency_observation("ollama-0311-qwen3-8b-medium-baseline.json")
    observed_nonweight = observed_total - weights
    estimate = rp.estimate_memory_bytes(
        _model(tag="qwen3:8b", disk=weights, params="8.2B", geometry=GEOM_QWEN3_8B), 4096
    )
    estimated_nonweight = estimate["kv_cache"] + rp.RUNTIME_OVERHEAD_BYTES
    assert estimated_nonweight >= 0.6 * observed_nonweight
    assert estimated_nonweight <= 4 * observed_nonweight


def test_unknown_geometry_never_claims_fit() -> None:
    model = _model(geometry=None)
    fit, estimate = rp.classify_fit(model, _hardware(), 4096)
    assert fit == "unknown_kv_geometry"
    assert estimate["kv_geometry_known"] is False
    plan = _plan(models=[model])
    assert plan["model"] is None
    assert plan["rejected_alternatives"][0]["reason_code"] == rp.REJECT_UNKNOWN_KV_GEOMETRY


# -- basic contract ------------------------------------------------------


def test_plan_is_versioned_and_selects_runtime_and_model() -> None:
    plan = _plan()
    assert plan["plan_version"] == rp.PLAN_VERSION
    assert plan["runtime"] == {"name": "ollama", "version": "0.31.1"}
    assert plan["model"]["tag"] == "llama3.2:1b"
    assert plan["fit_class"] == "fits_gpu_full"
    assert plan["options"] == {"num_ctx": rp.CONTEXT_BY_OBJECTIVE["fast"]}
    assert plan["thresholds"]["provisional"] is True


def test_invalid_objective_raises() -> None:
    with pytest.raises(ValueError):
        _plan(objective="turbo")


def test_planner_is_deterministic() -> None:
    models = [_model(), _model(tag="qwen3:8b", disk=5_225_388_164, params="8.2B", quant="Q4_K_M", geometry=GEOM_QWEN3_8B, digest="sha256:bbb")]
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
    big = _model(tag="huge:70b", disk=40 * GB, params="70B", geometry={"layers": 80, "kv_heads": 8, "key_length": 128, "value_length": 128})
    plan = _plan(objective="fast", models=[big])
    assert plan["model"] is None
    reasons = {item["model"]: item["reason_code"] for item in plan["rejected_alternatives"]}
    assert reasons["huge:70b"] in {rp.REJECT_EXCEEDS_RAM, rp.REJECT_EXCEEDS_TOTAL_RAM}


def test_model_never_claimed_to_fit_when_estimate_exceeds_margin() -> None:
    borderline = _model(tag="borderline:9b", disk=9 * GB, params="9B", geometry=GEOM_QWEN3_8B)
    for objective in ("fast", "balanced"):
        plan = _plan(objective=objective, models=[borderline], hardware=_hardware(vram_free=0))
        assert plan["model"] is None, objective


def test_deep_objective_reaches_via_memory_reclaim_state() -> None:
    borderline = _model(tag="borderline:9b", disk=9 * GB, params="9B", geometry=GEOM_QWEN3_8B)
    plan = _plan(objective="deep", models=[borderline], hardware=_hardware(vram_free=0))
    assert plan["model"]["tag"] == "borderline:9b"
    assert plan["fit_class"] == "reachable_after_memory_reclaim"
    assert rp.WARN_MEMORY_RECLAIM in plan["warnings"]
    # Deep Local terminology must never appear in an Ollama plan.
    assert "deep_local" not in json.dumps(plan)
    assert plan["runtime"]["name"] == "ollama"


def test_reclaim_state_is_persisted_job_not_interactive() -> None:
    borderline = _model(tag="borderline:9b", disk=9 * GB, params="9B", geometry=GEOM_QWEN3_8B)
    plan = _plan(objective="deep", models=[borderline], hardware=_hardware(vram_free=0))
    assert plan["execution_class"] == "persisted_job"


def test_truly_giant_model_is_not_runnable_even_deep() -> None:
    giant = _model(tag="giant:120b", disk=70 * GB, params="120B", geometry={"layers": 120, "kv_heads": 8, "key_length": 128, "value_length": 128})
    plan = _plan(objective="deep", models=[giant])
    assert plan["model"] is None
    assert plan["rejected_alternatives"][0]["reason_code"] == rp.REJECT_EXCEEDS_TOTAL_RAM


def test_embedding_model_rejected_for_chat_planning() -> None:
    embed = _model(tag="nomic-embed-text:latest", disk=274_000_000, params="", capabilities=("embedding",), geometry=None)
    plan = _plan(models=[embed, _model()])
    assert plan["model"]["tag"] == "llama3.2:1b"
    reasons = {item["model"]: item["reason_code"] for item in plan["rejected_alternatives"]}
    assert reasons["nomic-embed-text:latest"] == rp.REJECT_NOT_TEXT_MODEL


# -- conservative degradation --------------------------------------------


def test_unknown_hardware_produces_conservative_defaults() -> None:
    plan = _plan(hardware={"cpu": {}, "ram": {}, "gpus": []})
    assert plan["confidence"] == "conservative_default"
    assert rp.WARN_UNKNOWN_HARDWARE in plan["warnings"]


def test_runtime_unavailable_fails_safe() -> None:
    plan = _plan(runtimes=_runtimes(healthy=False))
    assert plan["model"] is None
    assert plan["warnings"] == ["runtime_unavailable"]


def test_no_gpu_probe_adds_warning_and_cpu_path() -> None:
    plan = _plan(hardware=_hardware(gpus=False))
    assert rp.WARN_NO_GPU_PROBE in plan["warnings"]
    assert plan["fit_class"] == "fits_cpu_ram"
    assert plan["estimates"]["vram_bytes"] == 0


# -- unmeasured-speed honesty (review finding 5) -------------------------


def test_unmeasured_plan_has_no_numeric_speed_claims() -> None:
    plan = _plan(evidence=[])
    assert plan["estimates"]["basis"] == "unmeasured"
    assert plan["estimates"]["ttft_ms"] is None
    assert plan["estimates"]["generation_tps"] is None
    assert rp.WARN_SPEED_UNMEASURED in plan["warnings"]
    assert plan["confidence"] == "derived"


def test_unmeasured_configuration_is_never_interactive() -> None:
    # A tiny model that WOULD be fast is still not classifiable as
    # interactive without a compatible measurement.
    plan = _plan(models=[_model()], evidence=[])
    assert plan["execution_class"] != "interactive"
    assert plan["execution_class"] == "slow_interactive"


def test_derived_score_is_ranking_only() -> None:
    models = [
        _model(),
        _model(tag="qwen3:8b", disk=5_225_388_164, params="8.2B", quant="Q4_K_M", geometry=GEOM_QWEN3_8B, digest="sha256:bbb"),
    ]
    plan = _plan(objective="fast", models=models, evidence=[])
    assert plan["model"]["tag"] == "llama3.2:1b"  # ranked first
    rejected = {item["model"]: item for item in plan["rejected_alternatives"]}
    assert rejected["qwen3:8b"]["reason_code"] == rp.REJECT_RANKED_BELOW_SELECTED
    assert "ranking_score" in rejected["qwen3:8b"]["detail_numbers"]
    # But no speed estimate surfaced for the unmeasured selection.
    assert plan["estimates"]["generation_tps"] is None


# -- evidence fingerprint compatibility (review finding 2) ---------------


def test_fully_compatible_evidence_gives_measured_confidence() -> None:
    hardware = _hardware()
    model = _model()
    evidence = [_evidence_for(model, hardware)]
    plan = _plan(models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan["confidence"] == "measured"
    assert plan["execution_class"] == "interactive"
    assert plan["estimates"]["basis"] == "measured"
    assert plan["estimates"]["generation_tps"] == 120.0
    assert plan["evidence"]["benchmark_batch_ids"] == ["batch-1"]
    assert plan["evidence"]["sample_count"] == 3


def test_tag_and_threads_alone_never_grant_measured() -> None:
    """The exact failure mode from the review: same tag, same logical
    threads, different machine — must NOT be measured."""
    hardware = _hardware()
    other_machine = _hardware(ram_total=32 * GB, ram_available=20 * GB)
    model = _model()
    evidence = [_evidence_for(model, other_machine)]  # fingerprinted on the other machine
    plan = _plan(models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan["confidence"] == "derived"
    assert plan["estimates"]["generation_tps"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        {"runtime_version": "0.99.0"},
        {"shape": "tiny"},
        {"context": 8192},
        {"tuning": {"num_gpu": 0}},
        {"server_env": {"OLLAMA_FLASH_ATTENTION": "1"}},
        {"placement": "partial_gpu"},
    ],
)
def test_any_fingerprint_mismatch_disqualifies_evidence(mutation) -> None:
    hardware = _hardware()
    model = _model()
    kwargs = dict(mutation)
    evidence = [_evidence_for(model, hardware, **kwargs)]
    plan = _plan(models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan["confidence"] == "derived", mutation


def test_quantization_mismatch_disqualifies_evidence() -> None:
    hardware = _hardware()
    model = _model()
    other = dict(model, quantization="Q4_K_M")
    evidence = [_evidence_for(other, hardware)]
    plan = _plan(models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan["confidence"] == "derived"


def test_digest_fallback_uses_disk_bytes() -> None:
    hardware = _hardware()
    model = _model(digest="")  # inventory digest missing
    evidence_model = dict(model, digest="")
    evidence = [_evidence_for(evidence_model, hardware)]
    plan = _plan(models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan["confidence"] == "measured"
    # Different byte size = different weights: no match.
    smaller = dict(model, disk_bytes=123)
    plan2 = _plan(models=[smaller], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan2["confidence"] == "derived"


def test_stale_evidence_lowers_confidence() -> None:
    hardware = _hardware()
    model = _model()
    month_ms = rp.EVIDENCE_MAX_AGE_DAYS * 86_400_000
    evidence = [_evidence_for(model, hardware, captured_at_ms=1_000)]
    plan = _plan(models=[model], hardware=hardware, evidence=evidence, now_ms=month_ms + 100_000)
    assert plan["confidence"] == "derived"
    assert rp.WARN_STALE_EVIDENCE in plan["warnings"]
    assert plan["estimates"]["generation_tps"] is None


def test_default_context_evidence_matches_fast_plan_only() -> None:
    hardware = _hardware()
    model = _model()
    evidence = [_evidence_for(model, hardware, context="default")]
    fast = _plan(objective="fast", models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert fast["confidence"] == "measured"  # fast plans num_ctx 4096 == measured default
    balanced = _plan(objective="balanced", models=[model], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert balanced["confidence"] == "derived"  # 8192 != measured 4096


# -- objectives ----------------------------------------------------------


def test_balanced_prefers_more_capable_usable_model_with_evidence() -> None:
    hardware = _hardware(ram_available=12 * GB)
    small = _model()
    big = _model(tag="qwen3:8b", disk=5_225_388_164, params="8.2B", quant="Q4_K_M", geometry=GEOM_QWEN3_8B, digest="sha256:bbb")
    evidence = [
        _evidence_for(small, hardware, context=8192, tps=110.0, ttft=300.0),
        _evidence_for(big, hardware, context=8192, tps=4.6, ttft=520.0, placement="partial_gpu"),
    ]
    plan = _plan(objective="balanced", models=[small, big], hardware=hardware, evidence=evidence, now_ms=2_000)
    assert plan["model"]["tag"] == "qwen3:8b"
    assert plan["execution_class"] == "slow_interactive"
    assert plan["confidence"] == "measured"


def test_classify_execution_boundaries() -> None:
    assert rp.classify_execution(1_000, 50.0) == "interactive"
    assert rp.classify_execution(30_000, 2.0) == "slow_interactive"
    assert rp.classify_execution(120_000, 2.0) == "persisted_job"
    assert rp.classify_execution(5_000, 0.5) == "persisted_job"
    assert rp.classify_execution(None, None) == "persisted_job"


def test_unsupported_flags_are_never_emitted() -> None:
    plan = _plan()
    assert plan["options"] == {"num_ctx": 4096}
    assert plan["server_env"] == {}
