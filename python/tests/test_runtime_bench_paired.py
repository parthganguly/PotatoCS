from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

from odysseus_desktop_backend.runtime_bench import validate_artifact, write_artifact
from odysseus_desktop_backend.runtime_bench.__main__ import main
from odysseus_desktop_backend.runtime_bench.comparison import compare_artifacts, load_and_compare
from odysseus_desktop_backend.runtime_bench.paired import (
    arm_fits_preflight,
    balanced_execution_plan,
    require_loopback_endpoint,
    run_paired_ollama_batch,
)
from odysseus_desktop_backend.runtime_bench.sampler import ResourceSampler


def _gpu_snapshot(state: str = "available") -> dict:
    return {
        "state": state,
        "used_bytes": 256 if state == "available" else None,
        "total_bytes": 1024 if state == "available" else None,
    }


def _run(*, cold: bool, repetition: int, order: int, generated: int = 32) -> dict:
    return {
        "run_index": repetition,
        "repetition_index": repetition,
        "execution_order": order,
        "captured_at": "2026-07-19T00:00:00Z",
        "cold": cold,
        "elapsed_since_previous_arm_ms": None if order == 0 else 10.0,
        "pre_arm": {
            "available_ram_bytes": 8_000,
            "gpu_snapshot": _gpu_snapshot(),
            "interference": {
                "state": "available",
                "system_cpu_percent": 10.0,
                "memory_load_percent": 50,
                "detected": False,
            },
        },
        "options": {"temperature": 0, "seed": 42, "top_p": 1, "top_k": 0, "num_predict": 32, "num_ctx": 4096},
        "timings_ms": {
            "total": 100.0 if cold else 50.0,
            "load": 20.0 if cold else 0.0,
            "prompt_eval": 10.0,
            "generation": 30.0,
            "first_token": 25.0 if cold else 5.0,
        },
        "tokens": {"prompt": 20, "generated": generated, "prompt_tps": 200.0, "generation_tps": 100.0},
        "memory": {
            "total_ram_bytes": 16_000,
            "available_ram_before_bytes": 8_000,
            "min_available_ram_bytes": 7_000,
            "process_peak_rss_bytes": 2_000,
            "pagefile_used_peak_bytes": 1_000,
            "sampler_interval_ms": 250,
            "sample_count": 1,
            "sampling_state": "available",
            "sampling_failure_category": "",
            "system_memory_samples": [
                {"elapsed_ms": 0, "available_ram_bytes": 7_000, "memory_load_percent": 50, "pagefile_used_bytes": 1_000}
            ],
            "safety_floor_bytes": 1_500,
            "safety_floor_crossed": False,
        },
        "gpu": {
            "pre": _gpu_snapshot(),
            "during_peak_used_bytes": 500,
            "post": _gpu_snapshot(),
            "sampling_state": "available",
            "sampling_failure_category": "",
        },
        "disk": {"state": "available", "read_bytes": 100},
        "cache_state": "cold" if cold else "warm",
        "placement_state": "recorded",
        "quality": {
            "state": "passed",
            "assertion_count": 1,
            "score": 1,
            "deterministic_answer_sha256": "a" * 64,
            "unsupported_category": "",
        },
        "cancellation": {
            "tested": False,
            "request_to_cancel_ms": None,
            "cancel_acknowledgement_ms": None,
            "cancel_latency_ms": None,
            "process_completion_ms": None,
            "final_state": "not_tested",
            "resources_released": None,
            "runtime_responsive": None,
        },
        "error_category": "",
        "truncation_state": "complete",
        "evidence_state": "complete",
    }


def _artifact(role: str, *, pair_id: str = "pair-1") -> dict:
    return {
        "schema_version": 2,
        "batch_id": f"batch-{role}",
        "captured_at": "2026-07-19T00:00:00Z",
        "experiment_id": "experiment-1",
        "pair_id": pair_id,
        "arm_id": f"{pair_id}-{role}",
        "arm_role": role,
        "hardware": {
            "schema_version": 1,
            "os": {"name": "Windows", "version": "10", "arch": "AMD64"},
            "cpu": {
                "logical_threads": 12,
                "physical_cores": 6,
                "physical_cores_source": "measured",
                "isa": {"ssse3": True, "sse4_1": True, "sse4_2": True, "avx": True, "avx2": True, "avx512f": False},
            },
            "ram": {"total_bytes": 16_000, "available_bytes": 8_000},
            "gpus": [{"vendor": "nvidia", "name": "gpu", "vram_total_bytes": 1024, "vram_free_bytes": 512, "driver_version": "1", "source": "nvidia-smi"}],
            "npu": "none_detected",
            "storage": {"profile_disk_free_bytes": None, "model_store_disk_free_bytes": 1_000_000, "kind": "unknown"},
            "errors": {},
            "captured_at_ms": 1,
        },
        "runtime": {
            "name": "ollama",
            "version": "0.32.1",
            "server_env": {},
            "backend_options": {"temperature": 0, "seed": 42, "top_p": 1, "top_k": 0, "num_predict": 32, "num_ctx": 4096},
        },
        "model": {
            "tag": "llama3.2:1b",
            "digest": "b" * 64,
            "file_identity": "b" * 64,
            "format": "gguf",
            "quantization": "Q8_0",
            "architecture": "dense",
            "total_parameters": 1_200_000_000,
            "active_parameters": 1_200_000_000,
            "disk_bytes": 1_300_000_000,
            "tokenizer_identity": "gpt2",
            "chat_template_identity": "c" * 64,
        },
        "fixture": {
            "identity": "runtime-bench-tiny-v1",
            "sha256": "d" * 64,
            "task_requirement_id": "tiny-task-v1",
            "quality_criteria_id": "tiny-quality-v1",
        },
        "requirements": {
            "context_limit": 4096,
            "output_token_limit": 32,
            "sampling": {"temperature": 0, "seed": 42, "top_p": 1, "top_k": 0},
        },
        "placement": {"state": "recorded", "cpu": "unused", "gpu": "full"},
        "shape": "tiny",
        "mode": "interactive",
        "engine_kind": "stub",
        "file_cache_state": "unknown_warmish",
        "runs": [_run(cold=True, repetition=0, order=0 if role == "baseline" else 1)]
        + [_run(cold=False, repetition=i, order=2 + i * 2 + (0 if (i % 2 == 0) == (role == "baseline") else 1)) for i in range(3)],
    }


def test_schema_v2_is_closed_and_validates_ranges() -> None:
    artifact = _artifact("baseline")
    assert validate_artifact(artifact) == []
    artifact["runs"][0]["memory"]["private_note"] = "nope"
    artifact["runs"][1]["memory"]["system_memory_samples"][0]["memory_load_percent"] = 101
    problems = validate_artifact(artifact)
    assert any("not allow-listed" in problem for problem in problems)
    assert any("must be <= 100" in problem for problem in problems)


def test_schema_v1_remains_supported() -> None:
    from test_runtime_bench import _minimal_artifact

    assert validate_artifact(_minimal_artifact()) == []


def test_balanced_order_records_ab_then_ba() -> None:
    assert balanced_execution_plan(3) == [
        ("baseline", 0, True),
        ("candidate", 0, True),
        ("baseline", 0, False),
        ("candidate", 0, False),
        ("candidate", 1, False),
        ("baseline", 1, False),
        ("baseline", 2, False),
        ("candidate", 2, False),
    ]
    with pytest.raises(ValueError):
        balanced_execution_plan(2)


def test_preflight_reuses_reviewed_gpu_and_ram_budgets() -> None:
    gib = 1024**3
    status = {
        "total_ram_bytes": 16 * gib,
        "available_ram_bytes": 2 * gib,
        "memory_load_percent": 80,
        "pagefile_total_bytes": 32 * gib,
        "pagefile_available_bytes": 10 * gib,
    }
    gpu = {"state": "available", "used_bytes": 1 * gib, "total_bytes": 4 * gib}
    assert arm_fits_preflight(status=status, estimated_required_bytes=2 * gib, resident=None, pre_gpu=gpu, options={})
    assert not arm_fits_preflight(status=status, estimated_required_bytes=2 * gib, resident=None, pre_gpu=gpu, options={"num_gpu": 0})


def test_paired_endpoint_is_loopback_only() -> None:
    require_loopback_endpoint("http://127.0.0.1:11434")
    require_loopback_endpoint("http://localhost:11434")
    with pytest.raises(ValueError):
        require_loopback_endpoint("https://example.com")


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        (lambda artifact: artifact["model"].update(digest="e" * 64), "model_digest_mismatch"),
        (lambda artifact: artifact["requirements"].update(context_limit=8192), "context_mismatch"),
        (lambda artifact: artifact["requirements"].update(output_token_limit=16), "output_limit_mismatch"),
        (lambda artifact: artifact["model"].update(tokenizer_identity="other"), "tokenizer_mismatch"),
        (lambda artifact: artifact["model"].update(chat_template_identity="e" * 64), "template_mismatch"),
        (lambda artifact: artifact["fixture"].update(sha256="e" * 64), "prompt_fixture_mismatch"),
        (lambda artifact: artifact["requirements"]["sampling"].update(seed=1), "sampling_mismatch"),
        (lambda artifact: artifact["runtime"].update(version="0.33.0"), "runtime_version_mismatch"),
    ],
)
def test_material_mismatches_are_invalid(mutation, reason: str) -> None:
    baseline, candidate = _artifact("baseline"), _artifact("candidate")
    mutation(candidate)
    comparison = compare_artifacts([baseline, candidate])["comparisons"][0]
    assert comparison["comparison_state"] == "invalid_comparison"
    assert reason in comparison["invalid_reasons"]


def test_shortened_output_cannot_count_as_uplift() -> None:
    baseline, candidate = _artifact("baseline"), _artifact("candidate")
    candidate["runs"][2]["tokens"]["generated"] = 8
    comparison = compare_artifacts([baseline, candidate])["comparisons"][0]
    assert "generated_token_count_mismatch" in comparison["invalid_reasons"]
    assert comparison["cold"] is None and comparison["warm"] is None


def test_truncation_interference_and_incomplete_runs_are_invalid() -> None:
    baseline, candidate = _artifact("baseline"), _artifact("candidate")
    candidate["runs"][0]["truncation_state"] = "truncated_prompt"
    candidate["runs"][0]["evidence_state"] = "incomplete"
    candidate["runs"][1]["pre_arm"]["interference"]["detected"] = True
    candidate["runs"][2]["evidence_state"] = "incomplete"
    reasons = compare_artifacts([baseline, candidate])["comparisons"][0]["invalid_reasons"]
    assert {"truncated_prompt", "system_interference", "incomplete_run"} <= set(reasons)


def test_missing_gpu_snapshots_are_unavailable_not_zero_and_invalidate_gpu_machine() -> None:
    baseline, candidate = _artifact("baseline"), _artifact("candidate")
    candidate["runs"][0]["gpu"]["pre"] = _gpu_snapshot("unavailable")
    assert candidate["runs"][0]["gpu"]["pre"]["used_bytes"] is None
    comparison = compare_artifacts([baseline, candidate])["comparisons"][0]
    assert "hardware_snapshot_missing" in comparison["invalid_reasons"]


def test_only_valid_pairs_enter_aggregates_and_cold_warm_stay_separate() -> None:
    valid = [_artifact("baseline", pair_id="valid"), _artifact("candidate", pair_id="valid")]
    invalid = [_artifact("baseline", pair_id="invalid"), _artifact("candidate", pair_id="invalid")]
    invalid[1]["model"]["digest"] = "e" * 64
    report = compare_artifacts(valid + invalid)
    assert report["valid_pair_count"] == 1
    assert report["invalid_pair_count"] == 1
    valid_result = next(item for item in report["comparisons"] if item["pair_id"] == "valid")
    assert valid_result["cold"]["pair_count"] == 1
    assert valid_result["warm"]["pair_count"] == 3
    assert valid_result["cold"]["metrics"]["time_to_first_token_ms"]["baseline"]["median"] == 25
    assert valid_result["warm"]["metrics"]["time_to_first_token_ms"]["baseline"]["median"] == 5


def test_cancellation_latency_is_recorded_separately() -> None:
    baseline, candidate = _artifact("baseline"), _artifact("candidate")
    for artifact in (baseline, candidate):
        probe = copy.deepcopy(artifact["runs"][-1])
        probe["repetition_index"] = 99
        probe["execution_order"] = 99
        probe["error_category"] = "cancelled"
        probe["evidence_state"] = "incomplete"
        probe["quality"] = {"state": "not_applicable", "assertion_count": 0, "score": 0, "deterministic_answer_sha256": None, "unsupported_category": ""}
        probe["cancellation"] = {
            "tested": True,
            "request_to_cancel_ms": 100.0,
            "cancel_acknowledgement_ms": 110.0,
            "cancel_latency_ms": 10.0,
            "process_completion_ms": 125.0,
            "final_state": "cancelled",
            "resources_released": True,
            "runtime_responsive": True,
        }
        artifact["runs"].append(probe)
    report = compare_artifacts([baseline, candidate])
    cancellation = report["comparisons"][0]["cancellation"]
    assert cancellation["baseline"]["process_completion_ms"]["median"] == 125
    assert cancellation["baseline"]["cancel_latency_ms"]["median"] == 10
    assert cancellation["candidate"]["resources_released"] is True


def test_one_malformed_artifact_does_not_poison_batch_or_disclose_path(tmp_path: Path) -> None:
    baseline_path = write_artifact(_artifact("baseline"), tmp_path)
    candidate_path = write_artifact(_artifact("candidate"), tmp_path)
    hostile = tmp_path / "private-user-prompt.json"
    hostile.write_text('{"prompt":"private text"}', encoding="utf-8")
    report = load_and_compare([baseline_path, hostile, candidate_path])
    serialized = json.dumps(report)
    assert report["valid_pair_count"] == 1
    assert report["invalid_pair_count"] == 1
    assert str(hostile) not in serialized
    assert "private text" not in serialized


def test_comparison_cli_reports_measurements_without_threshold_policy(tmp_path: Path, capsys) -> None:
    baseline_path = write_artifact(_artifact("baseline"), tmp_path)
    candidate_path = write_artifact(_artifact("candidate"), tmp_path)
    assert main(["compare", str(baseline_path), str(candidate_path)]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert report["policy_thresholds_applied"] is False
    assert "promoted" not in output
    assert "recommended" not in output
    assert "uplift_candidate" not in output


def test_low_available_ram_aborts_before_launch(monkeypatch, tmp_path: Path) -> None:
    total = 16 * 1024**3
    low_status = {
        "total_ram_bytes": total,
        "available_ram_bytes": 100,
        "memory_load_percent": 99,
        "pagefile_total_bytes": 32 * 1024**3,
        "pagefile_available_bytes": 1,
    }
    descriptor = _artifact("baseline")["model"]
    inventory_entry = {"disk_bytes": 1_000, "kv_geometry": {"layers": 1, "kv_heads": 1, "key_length": 1, "value_length": 1}}
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.model_descriptor_v2", lambda *args, **kwargs: (descriptor, inventory_entry))
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.system_memory_status", lambda: low_status)
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ri._nvidia_smi_path", lambda: None)
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ri.detect_ollama_runtime", lambda: {"version": "0.32.1"})
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ri.hardware_inventory", lambda: copy.deepcopy(_artifact("baseline")["hardware"]) | {"gpus": []})
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ollama_unload", lambda *args, **kwargs: True)
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired._resident_model", lambda *args, **kwargs: None)
    launches = []
    result = run_paired_ollama_batch(
        model="installed:model",
        shape="tiny",
        experiment_id="experiment-low-ram",
        pair_id="pair-low-ram",
        batch_id="batch-low-ram",
        artifact_dir=str(tmp_path),
        execute_arm=lambda **kwargs: launches.append(kwargs),
    )
    assert result["aborted"] is True
    assert launches == []
    assert result["artifacts"][0]["runs"][0]["error_category"] == "preflight_safety_abort"


def test_fake_paired_batch_writes_valid_balanced_artifacts(monkeypatch, tmp_path: Path) -> None:
    total = 16 * 1024**3
    healthy = {
        "total_ram_bytes": total,
        "available_ram_bytes": 12 * 1024**3,
        "memory_load_percent": 25,
        "pagefile_total_bytes": 32 * 1024**3,
        "pagefile_available_bytes": 24 * 1024**3,
    }
    descriptor = _artifact("baseline")["model"]
    inventory_entry = {"disk_bytes": 1_000, "kv_geometry": {"layers": 1, "kv_heads": 1, "key_length": 1, "value_length": 1}}
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.model_descriptor_v2", lambda *args, **kwargs: (descriptor, inventory_entry))
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.system_memory_status", lambda: healthy)
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ri._nvidia_smi_path", lambda: None)
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ri.detect_ollama_runtime", lambda: {"version": "0.32.1"})
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ri.hardware_inventory", lambda: copy.deepcopy(_artifact("baseline")["hardware"]) | {"gpus": []})
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired.ollama_unload", lambda *args, **kwargs: True)
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.paired._resident_model", lambda *args, **kwargs: None)

    def fake_execute(**kwargs):
        run = _run(cold=kwargs["cold"], repetition=kwargs["repetition_index"], order=kwargs["execution_order"])
        run["run_index"] = kwargs["run_index"]
        run["options"] = dict(kwargs["options"])
        run["_placement"] = {"state": "recorded", "cpu": "unused", "gpu": "full"}
        if kwargs["cancel_probe"]:
            run["error_category"] = "cancelled"
            run["truncation_state"] = "incomplete_evidence"
            run["evidence_state"] = "incomplete"
            run["quality"] = {"state": "not_applicable", "assertion_count": 0, "score": 0, "deterministic_answer_sha256": None, "unsupported_category": ""}
            run["cancellation"] = {
                "tested": True,
                "request_to_cancel_ms": 100.0,
                "cancel_acknowledgement_ms": 110.0,
                "cancel_latency_ms": 10.0,
                "process_completion_ms": 125.0,
                "final_state": "cancelled",
                "resources_released": True,
                "runtime_responsive": True,
            }
        return run

    result = run_paired_ollama_batch(
        model="installed:model",
        shape="tiny",
        experiment_id="experiment-fake",
        pair_id="pair-fake",
        batch_id="batch-fake",
        artifact_dir=str(tmp_path),
        cancel_probe=True,
        execute_arm=fake_execute,
    )
    assert result["aborted"] is False
    assert len(result["artifacts"]) == 2
    assert all(validate_artifact(artifact) == [] for artifact in result["artifacts"])
    assert sorted(path.name for path in tmp_path.glob("*.json")) == ["batch-fake-baseline.json", "batch-fake-candidate.json"]
    order = sorted(
        (run["execution_order"], artifact["arm_role"])
        for artifact in result["artifacts"]
        for run in artifact["runs"]
    )
    assert [role for _, role in order] == ["baseline", "candidate", "baseline", "candidate", "candidate", "baseline", "baseline", "candidate", "baseline", "candidate"]
    for artifact in result["artifacts"]:
        assert len([run for run in artifact["runs"] if not run["cold"] and not run["cancellation"]["tested"]]) == 3
        assert len([run for run in artifact["runs"] if run["cancellation"]["tested"]]) == 1
    report = compare_artifacts(result["artifacts"])
    assert report["valid_pair_count"] == 1
    assert report["comparisons"][0]["cancellation"]["baseline"]["cancel_latency_ms"]["median"] == 10


def test_crossing_safety_floor_requests_bounded_cancellation(monkeypatch) -> None:
    statuses = iter(
        [
            {"total_ram_bytes": 10_000, "available_ram_bytes": 2_000, "memory_load_percent": 80, "pagefile_total_bytes": 20_000, "pagefile_available_bytes": 10_000},
            {"total_ram_bytes": 10_000, "available_ram_bytes": 900, "memory_load_percent": 91, "pagefile_total_bytes": 20_000, "pagefile_available_bytes": 9_000},
        ]
    )
    last = {"total_ram_bytes": 10_000, "available_ram_bytes": 900, "memory_load_percent": 91, "pagefile_total_bytes": 20_000, "pagefile_available_bytes": 9_000}
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.sampler.process_tree_metrics", lambda _: (1, 1))
    monkeypatch.setattr("odysseus_desktop_backend.runtime_bench.sampler.system_memory_status", lambda: next(statuses, last))
    cancelled = []
    with ResourceSampler(exe_substring="fake", interval_ms=5, safety_floor_bytes=1_000, on_safety_floor=lambda: cancelled.append(True)) as sampler:
        deadline = time.monotonic() + 0.2
        while not cancelled and time.monotonic() < deadline:
            time.sleep(0.005)
    assert cancelled == [True]
    assert sampler.safety_floor_crossed is True
