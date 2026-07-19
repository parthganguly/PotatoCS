"""Policy-free validation and measurement of paired benchmark arms."""

from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.runtime_bench.artifacts import validate_artifact
from odysseus_desktop_backend.runtime_bench.paired_artifacts import PAIRED_ARTIFACT_SCHEMA_VERSION

INVALID_REASONS = {
    "model_mismatch",
    "model_digest_mismatch",
    "prompt_fixture_mismatch",
    "tokenizer_mismatch",
    "template_mismatch",
    "context_mismatch",
    "output_limit_mismatch",
    "sampling_mismatch",
    "generated_token_count_mismatch",
    "runtime_version_mismatch",
    "cold_warm_mismatch",
    "placement_not_recorded",
    "hardware_snapshot_missing",
    "system_interference",
    "truncated_prompt",
    "incomplete_run",
    "quality_mismatch",
    "missing_arm",
    "duplicate_arm",
    "malformed_artifact",
    "unsupported_schema",
    "engine_kind_mismatch",
}


def _hardware_identity(artifact: dict[str, Any]) -> tuple[Any, ...] | None:
    hardware = artifact.get("hardware") or {}
    cpu = hardware.get("cpu") or {}
    isa = cpu.get("isa") or {}
    ram = hardware.get("ram") or {}
    gpus = hardware.get("gpus")
    if not cpu or not ram or not isinstance(gpus, list):
        return None
    gpu_identity = tuple(
        sorted(
            (
                str(gpu.get("vendor") or ""),
                str(gpu.get("name") or ""),
                int(gpu.get("vram_total_bytes") or 0),
                str(gpu.get("driver_version") or ""),
            )
            for gpu in gpus
        )
    )
    return (
        str((hardware.get("os") or {}).get("arch") or ""),
        int(cpu.get("physical_cores") or 0),
        int(cpu.get("logical_threads") or 0),
        bool(isa.get("avx2")),
        int(ram.get("total_bytes") or 0),
        gpu_identity,
    )


def _protected_reasons(baseline: dict[str, Any], candidate: dict[str, Any]) -> set[str]:
    reasons: set[str] = set()
    bmodel, cmodel = baseline["model"], candidate["model"]
    model_material_fields = (
        "tag", "file_identity", "format", "quantization", "architecture",
        "total_parameters", "active_parameters", "disk_bytes",
    )
    if any(bmodel[field] != cmodel[field] for field in model_material_fields):
        reasons.add("model_mismatch")
    if bmodel["digest"] != cmodel["digest"] or "unavailable" in {bmodel["digest"], cmodel["digest"]}:
        reasons.add("model_digest_mismatch")
    if baseline["fixture"] != candidate["fixture"] or baseline["shape"] != candidate["shape"]:
        reasons.add("prompt_fixture_mismatch")
    if bmodel["tokenizer_identity"] != cmodel["tokenizer_identity"]:
        reasons.add("tokenizer_mismatch")
    if bmodel["chat_template_identity"] != cmodel["chat_template_identity"]:
        reasons.add("template_mismatch")
    breq, creq = baseline["requirements"], candidate["requirements"]
    if breq["context_limit"] != creq["context_limit"]:
        reasons.add("context_mismatch")
    if breq["output_token_limit"] != creq["output_token_limit"]:
        reasons.add("output_limit_mismatch")
    if breq["sampling"] != creq["sampling"]:
        reasons.add("sampling_mismatch")
    bruntime, cruntime = baseline["runtime"], candidate["runtime"]
    if (bruntime["name"], bruntime["version"]) != (cruntime["name"], cruntime["version"]):
        reasons.add("runtime_version_mismatch")
    if baseline["engine_kind"] != candidate["engine_kind"] or baseline["mode"] != candidate["mode"]:
        reasons.add("engine_kind_mismatch")
    if baseline["placement"]["state"] != "recorded" or candidate["placement"]["state"] != "recorded":
        reasons.add("placement_not_recorded")
    bhardware = _hardware_identity(baseline)
    chardware = _hardware_identity(candidate)
    if bhardware is None or chardware is None or bhardware != chardware:
        reasons.add("hardware_snapshot_missing")
    return reasons


def _performance_runs(artifact: dict[str, Any]) -> list[dict[str, Any]]:
    return [run for run in artifact["runs"] if not run["cancellation"]["tested"]]


def _matched_runs(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], set[str]]:
    reasons: set[str] = set()
    b_runs = _performance_runs(baseline)
    c_runs = _performance_runs(candidate)
    bmap = {(run["cold"], run["repetition_index"]): run for run in b_runs}
    cmap = {(run["cold"], run["repetition_index"]): run for run in c_runs}
    if set(bmap) != set(cmap):
        reasons.add("cold_warm_mismatch")
    pairs = [(bmap[key], cmap[key]) for key in sorted(set(bmap) & set(cmap))]
    for brun, crun in pairs:
        boptions, coptions = brun["options"], crun["options"]
        if (
            boptions.get("num_ctx") != coptions.get("num_ctx")
            or boptions.get("num_ctx") != baseline["requirements"]["context_limit"]
            or coptions.get("num_ctx") != candidate["requirements"]["context_limit"]
        ):
            reasons.add("context_mismatch")
        b_output_limit = boptions.get("num_predict", boptions.get("n_predict"))
        c_output_limit = coptions.get("num_predict", coptions.get("n_predict"))
        if (
            b_output_limit != c_output_limit
            or b_output_limit != baseline["requirements"]["output_token_limit"]
            or c_output_limit != candidate["requirements"]["output_token_limit"]
        ):
            reasons.add("output_limit_mismatch")
        if (
            any(boptions.get(key) != coptions.get(key) for key in ("temperature", "seed", "top_p", "top_k", "think"))
            or boptions.get("temperature") != baseline["requirements"]["sampling"]["temperature"]
            or coptions.get("temperature") != candidate["requirements"]["sampling"]["temperature"]
            or boptions.get("seed") != baseline["requirements"]["sampling"]["seed"]
            or coptions.get("seed") != candidate["requirements"]["sampling"]["seed"]
            or boptions.get("top_p") != baseline["requirements"]["sampling"]["top_p"]
            or coptions.get("top_p") != candidate["requirements"]["sampling"]["top_p"]
            or boptions.get("top_k") != baseline["requirements"]["sampling"]["top_k"]
            or coptions.get("top_k") != candidate["requirements"]["sampling"]["top_k"]
        ):
            reasons.add("sampling_mismatch")
        if brun["tokens"]["generated"] != crun["tokens"]["generated"]:
            reasons.add("generated_token_count_mismatch")
        if brun["tokens"]["prompt"] != crun["tokens"]["prompt"]:
            reasons.add("prompt_fixture_mismatch")
        quality_states = {brun["quality"]["state"], crun["quality"]["state"]}
        if brun["quality"]["score"] > crun["quality"]["score"] or len(quality_states) > 1:
            reasons.add("quality_mismatch")
        if quality_states != {"passed"}:
            reasons.add("incomplete_run")
        if brun["truncation_state"] == "truncated_prompt" or crun["truncation_state"] == "truncated_prompt":
            reasons.add("truncated_prompt")
        for run in (brun, crun):
            if run["pre_arm"]["interference"]["detected"]:
                reasons.add("system_interference")
            gpu_expected = bool((baseline["hardware"].get("gpus") or candidate["hardware"].get("gpus")))
            if gpu_expected and (
                run["gpu"]["pre"]["state"] != "available"
                or run["gpu"]["post"]["state"] != "available"
                or run["gpu"]["sampling_state"] != "available"
            ):
                reasons.add("hardware_snapshot_missing")
            if (
                run["error_category"]
                or run["evidence_state"] != "complete"
                or run["memory"]["sampling_state"] != "available"
                or run["placement_state"] != "recorded"
            ):
                reasons.add("incomplete_run")
    if not pairs:
        reasons.add("incomplete_run")
    return pairs, reasons


def _distribution(values: list[float | int]) -> dict[str, Any] | None:
    clean = [float(value) for value in values if value is not None]
    if not clean:
        return None
    clean.sort()
    return {
        "count": len(clean),
        "minimum": round(clean[0], 3),
        "median": round(float(statistics.median(clean)), 3),
        "maximum": round(clean[-1], 3),
    }


def _metric(run: dict[str, Any], name: str) -> float | int | None:
    if name == "time_to_first_token_ms":
        return run["timings_ms"]["first_token"]
    if name == "prompt_tokens_per_second":
        return run["tokens"]["prompt_tps"]
    if name == "generation_tokens_per_second":
        return run["tokens"]["generation_tps"]
    if name == "wall_clock_ms":
        return run["timings_ms"]["total"]
    if name == "load_ms":
        return run["timings_ms"]["load"]
    if name == "minimum_available_ram_bytes":
        return run["memory"]["min_available_ram_bytes"]
    if name == "process_peak_rss_bytes":
        return run["memory"]["process_peak_rss_bytes"]
    if name == "vram_peak_used_bytes":
        return run["gpu"]["during_peak_used_bytes"]
    if name == "disk_read_bytes":
        return run["disk"]["read_bytes"]
    raise KeyError(name)


METRICS = (
    "time_to_first_token_ms",
    "prompt_tokens_per_second",
    "generation_tokens_per_second",
    "wall_clock_ms",
    "load_ms",
    "minimum_available_ram_bytes",
    "process_peak_rss_bytes",
    "vram_peak_used_bytes",
    "disk_read_bytes",
)


def _state_measurements(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], cold: bool
) -> dict[str, Any]:
    selected = [(baseline, candidate) for baseline, candidate in pairs if baseline["cold"] is cold]
    metrics: dict[str, Any] = {}
    for name in METRICS:
        baseline_values = [_metric(baseline, name) for baseline, _ in selected]
        candidate_values = [_metric(candidate, name) for _, candidate in selected]
        baseline_distribution = _distribution(baseline_values)
        candidate_distribution = _distribution(candidate_values)
        absolute_difference = None
        ratio = None
        if baseline_distribution is not None and candidate_distribution is not None:
            bmedian = baseline_distribution["median"]
            cmedian = candidate_distribution["median"]
            absolute_difference = round(cmedian - bmedian, 3)
            ratio = round(cmedian / bmedian, 6) if bmedian != 0 else None
        metrics[name] = {
            "baseline": baseline_distribution,
            "candidate": candidate_distribution,
            "absolute_difference": absolute_difference,
            "baseline_to_candidate_ratio": ratio,
        }
    return {"pair_count": len(selected), "metrics": metrics}


def _cancellation_measurements(artifact: dict[str, Any]) -> dict[str, Any] | None:
    runs = [run for run in artifact["runs"] if run["cancellation"]["tested"]]
    if not runs:
        return None
    return {
        "request_to_cancel_ms": _distribution([run["cancellation"]["request_to_cancel_ms"] for run in runs]),
        "cancel_acknowledgement_ms": _distribution([run["cancellation"]["cancel_acknowledgement_ms"] for run in runs]),
        "cancel_latency_ms": _distribution([run["cancellation"]["cancel_latency_ms"] for run in runs]),
        "process_completion_ms": _distribution([run["cancellation"]["process_completion_ms"] for run in runs]),
        "resources_released": all(run["cancellation"]["resources_released"] is True for run in runs),
        "runtime_responsive": all(run["cancellation"]["runtime_responsive"] is True for run in runs),
    }


def compare_artifacts(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate and compare all independent pairs without policy thresholds."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    input_errors: list[dict[str, Any]] = []
    for input_index, artifact in enumerate(artifacts):
        try:
            problems = validate_artifact(artifact)
        except Exception:  # noqa: BLE001 - malformed input is isolated
            problems = ["validator_failure"]
        if problems:
            input_errors.append({"input_index": input_index, "error_category": "malformed_artifact"})
            continue
        if artifact.get("schema_version") != PAIRED_ARTIFACT_SCHEMA_VERSION:
            input_errors.append({"input_index": input_index, "error_category": "unsupported_schema"})
            continue
        groups.setdefault((artifact["experiment_id"], artifact["pair_id"]), []).append(artifact)

    comparisons: list[dict[str, Any]] = []
    reason_counts: dict[str, int] = {}
    for experiment_id, pair_id in sorted(groups):
        group = groups[(experiment_id, pair_id)]
        baselines = [artifact for artifact in group if artifact["arm_role"] == "baseline"]
        candidates = [artifact for artifact in group if artifact["arm_role"] == "candidate"]
        reasons: set[str] = set()
        if not baselines or not candidates:
            reasons.add("missing_arm")
        if len(baselines) > 1 or len(candidates) > 1:
            reasons.add("duplicate_arm")
        pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if len(baselines) == 1 and len(candidates) == 1:
            baseline, candidate = baselines[0], candidates[0]
            reasons |= _protected_reasons(baseline, candidate)
            pairs, run_reasons = _matched_runs(baseline, candidate)
            reasons |= run_reasons
        comparison: dict[str, Any] = {
            "pair_id": pair_id,
            "experiment_id": experiment_id,
            "engine_kind": baselines[0]["engine_kind"] if len(baselines) == 1 else None,
            "comparison_state": "invalid_comparison" if reasons else "valid_comparison",
            "invalid_reasons": sorted(reasons),
            "generated_token_parity": bool(pairs) and all(
                baseline_run["tokens"]["generated"] == candidate_run["tokens"]["generated"]
                for baseline_run, candidate_run in pairs
            ),
            "quality_parity": bool(pairs) and all(
                baseline_run["quality"]["state"] == "passed"
                and candidate_run["quality"]["state"] == "passed"
                and candidate_run["quality"]["score"] >= baseline_run["quality"]["score"]
                for baseline_run, candidate_run in pairs
            ),
            "evidence_completeness": {
                "baseline_arm_present": len(baselines) == 1,
                "candidate_arm_present": len(candidates) == 1,
                "matched_run_count": len(pairs),
                "complete": not reasons,
            },
            "cold": None,
            "warm": None,
            "cancellation": {"baseline": None, "candidate": None},
        }
        if len(baselines) == 1 and len(candidates) == 1:
            comparison["cancellation"] = {
                "baseline": _cancellation_measurements(baselines[0]),
                "candidate": _cancellation_measurements(candidates[0]),
            }
        if not reasons and baselines and candidates:
            comparison["cold"] = _state_measurements(pairs, True)
            comparison["warm"] = _state_measurements(pairs, False)
        for reason in reasons:
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        comparisons.append(comparison)

    for error in input_errors:
        reason = error["error_category"]
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    valid_count = sum(item["comparison_state"] == "valid_comparison" for item in comparisons)
    invalid_count = len(comparisons) - valid_count + len(input_errors)
    return {
        "report_schema_version": 1,
        "report_kind": "paired_benchmark_measurements",
        "valid_pair_count": valid_count,
        "invalid_pair_count": invalid_count,
        "invalid_reason_counts": dict(sorted(reason_counts.items())),
        "input_errors": input_errors,
        "comparisons": comparisons,
        "policy_thresholds_applied": False,
    }


def load_and_compare(paths: list[str | Path]) -> dict[str, Any]:
    artifacts: list[dict[str, Any]] = []
    load_errors: list[dict[str, Any]] = []
    for input_index, path in enumerate(paths):
        try:
            decoded = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(decoded, dict):
                raise ValueError("not an object")
            artifacts.append(decoded)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            load_errors.append({"input_index": input_index, "error_category": "malformed_artifact"})
    report = compare_artifacts(artifacts)
    if load_errors:
        report["input_errors"] = load_errors + report["input_errors"]
        report["invalid_pair_count"] += len(load_errors)
        report["invalid_reason_counts"]["malformed_artifact"] = (
            report["invalid_reason_counts"].get("malformed_artifact", 0) + len(load_errors)
        )
    return report
