from __future__ import annotations

import json
import os

import pytest

from odysseus_desktop_backend.runtime_bench.artifacts import write_artifact
from odysseus_desktop_backend.services import runtime_plan_service as rps
from odysseus_desktop_backend.services.runtime_plan_service import (
    RuntimePlanService,
    summarize_artifact,
)


def _artifact(batch_id="batch-a", tag="llama3.2:1b", shape="medium", engine_kind="real", runs=None):
    default_runs = [
        {
            "run_index": 0,
            "cold": True,
            "options": {},
            "timings_ms": {"total": 3300.0, "load": 3000.0, "prompt_eval": 100.0, "generation": 200.0, "first_token": 3200.0},
            "tokens": {"prompt": 100, "generated": 50, "prompt_tps": 1000.0, "generation_tps": 100.0},
            "memory": {"runtime_peak_rss_bytes": 1, "system_min_available_bytes": 1, "vram_peak_used_bytes": None, "sampler_interval_ms": 250},
            "quality_check": "passed",
            "error_category": "",
        },
        {
            "run_index": 1,
            "cold": False,
            "options": {},
            "timings_ms": {"total": 400.0, "load": 0.0, "prompt_eval": 10.0, "generation": 300.0, "first_token": 310.0},
            "tokens": {"prompt": 100, "generated": 50, "prompt_tps": 10000.0, "generation_tps": 110.0},
            "memory": {"runtime_peak_rss_bytes": 1, "system_min_available_bytes": 1, "vram_peak_used_bytes": None, "sampler_interval_ms": 250},
            "quality_check": "passed",
            "error_category": "",
        },
    ]
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "captured_at": "2026-07-17T00:00:00Z",
        "hardware": {"cpu": {"logical_threads": 12}, "ram": {"total_bytes": 1}, "captured_at_ms": 1_000},
        "runtime": {"name": "ollama", "version": "0.31.1", "server_env": {}},
        "model": {"tag": tag, "quantization": "Q8_0"},
        "shape": shape,
        "mode": "interactive",
        "engine_kind": engine_kind,
        "runs": runs if runs is not None else default_runs,
    }


# -- summarize_artifact --------------------------------------------------


def test_summary_uses_warm_passing_runs_only() -> None:
    summary = summarize_artifact(_artifact())
    assert summary is not None
    assert summary["model_tag"] == "llama3.2:1b"
    assert summary["warm_ttft_ms"] == 310.0
    assert summary["generation_tps"] == 110.0
    assert summary["batch_ids"] == ["batch-a"]


def test_summary_none_when_only_cold_or_failed_runs() -> None:
    artifact = _artifact()
    artifact["runs"][1]["error_category"] = "timeout"
    artifact["runs"][1]["quality_check"] = "not_applicable"
    assert summarize_artifact(artifact) is None


def test_summary_none_for_unknown_schema() -> None:
    artifact = _artifact()
    artifact["schema_version"] = 99
    assert summarize_artifact(artifact) is None


def test_summary_excludes_quality_failed_runs() -> None:
    artifact = _artifact()
    artifact["runs"][1]["quality_check"] = "failed"
    assert summarize_artifact(artifact) is None


# -- service over a temp evidence dir ------------------------------------


@pytest.fixture()
def evidence_dir(tmp_path):
    write_artifact(_artifact(), tmp_path)
    write_artifact(_artifact(batch_id="batch-b", tag="qwen3:8b", shape="medium"), tmp_path)
    write_artifact(_artifact(batch_id="batch-stub", tag="stub-model", engine_kind="stub"), tmp_path)
    return tmp_path


def test_benchmarks_lists_artifacts_and_summaries(evidence_dir) -> None:
    service = RuntimePlanService(evidence_dir)
    result = service.benchmarks()
    assert result["ok"] is True
    assert result["available"] is True
    batch_ids = [item["batch_id"] for item in result["artifacts"]]
    assert batch_ids == ["batch-a", "batch-b", "batch-stub"]
    summary_tags = [item["model_tag"] for item in result["evidence_summaries"]]
    assert summary_tags == ["llama3.2:1b", "qwen3:8b"]


def test_stub_artifacts_never_become_planner_evidence(evidence_dir) -> None:
    service = RuntimePlanService(evidence_dir)
    summaries = service._evidence_summaries()
    assert all(item["model_tag"] != "stub-model" for item in summaries)
    assert all(item["engine_kind"] == "real" for item in summaries)


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


def test_plan_and_recommendations_are_read_only(monkeypatch, tmp_path) -> None:
    from odysseus_desktop_backend.services import runtime_inventory as ri

    monkeypatch.setattr(
        ri,
        "hardware_inventory",
        lambda **kw: {
            "cpu": {"logical_threads": 12, "physical_cores": 6},
            "ram": {"total_bytes": 16 * 1024**3, "available_bytes": 10 * 1024**3},
            "gpus": [],
            "captured_at_ms": 1,
        },
    )
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
                    "disk_bytes": 1_321_098_329,
                    "parameter_size": "1.2B",
                    "quantization": "Q8_0",
                    "capabilities": ["completion"],
                }
            ],
            "error_category": "",
            "captured_at_ms": 1,
        },
    )
    service = RuntimePlanService(None)
    result = service.plan("fast")
    assert result["ok"] is True
    assert result["plan"]["model"]["tag"] == "llama3.2:1b"
    recommendations = service.recommendations()
    assert set(recommendations["plans"]) == {"fast", "balanced", "deep"}
    assert "guarantees" in recommendations["note"]
    # Read-only proof: no files created anywhere in the temp profile dir.
    assert list(tmp_path.iterdir()) == []


def test_rpc_methods_registered_and_fixture_in_sync() -> None:
    fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "ipc_contract.golden.json")
    with open(fixture_path, encoding="utf-8") as handle:
        fixture = json.load(handle)
    for method in ("runtime.inventory", "runtime.benchmarks", "runtime.plan", "runtime.recommendations"):
        assert method in fixture["python_rpc_methods"], method
    # runtime.* has no UI yet: it must NOT appear in the frontend list.
    assert not any(method.startswith("runtime.") for method in fixture["frontend_rpc_methods"])


def test_no_settings_mutation_surface() -> None:
    service = RuntimePlanService(None)
    public = [name for name in dir(service) if not name.startswith("_")]
    assert sorted(public) == ["benchmarks", "evidence_dir", "inventory", "plan", "recommendations"]
