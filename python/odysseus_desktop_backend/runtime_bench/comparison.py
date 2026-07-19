"""Policy-free validation and measurement of paired benchmark arms."""

from __future__ import annotations

import json
import math
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
    "insufficient_cold_runs",
    "insufficient_warm_runs",
    "duplicate_run",
    "duplicate_execution_order",
    "unbalanced_execution_order",
    "repetition_set_mismatch",
}

POLICY_KEYS = {
    "schema_version", "max_pre_arm_cpu_percent_difference",
    "max_pre_arm_available_ram_difference_bytes",
    "max_pre_arm_gpu_used_difference_bytes", "max_elapsed_gap_ms",
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


def _validate_policy(policy: dict[str, Any] | None) -> dict[str, Any] | None:
    if policy is None:
        return None
    if (
        not isinstance(policy, dict)
        or set(policy) != POLICY_KEYS
        or isinstance(policy.get("schema_version"), bool)
        or policy.get("schema_version") != 1
    ):
        raise ValueError("interference policy must be a closed schema-version-1 object")
    for key in POLICY_KEYS - {"schema_version"}:
        value = policy[key]
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            raise ValueError(f"interference policy {key} must be null or non-negative")
        if key.endswith("_bytes") and not isinstance(value, int):
            raise ValueError(f"interference policy {key} must be an integer")
    return dict(policy)


def _paired_design(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], set[str]]:
    reasons: set[str] = set()
    by_role = {
        "baseline": _performance_runs(baseline),
        "candidate": _performance_runs(candidate),
    }
    maps: dict[str, dict[tuple[bool, int], dict[str, Any]]] = {}
    for role, runs in by_role.items():
        cold_count = sum(run["cold"] is True for run in runs)
        warm_count = sum(run["cold"] is False for run in runs)
        if cold_count != 1:
            reasons.add("insufficient_cold_runs")
        if warm_count < 3:
            reasons.add("insufficient_warm_runs")
        keys: set[tuple[bool, int]] = set()
        run_indexes: set[int] = set()
        role_map: dict[tuple[bool, int], dict[str, Any]] = {}
        for run in runs:
            key = (bool(run["cold"]), int(run["repetition_index"]))
            if key in keys:
                reasons.add("duplicate_run")
            else:
                role_map[key] = run
            keys.add(key)
        for run in (baseline if role == "baseline" else candidate)["runs"]:
            if int(run["run_index"]) in run_indexes:
                reasons.add("duplicate_run")
            run_indexes.add(int(run["run_index"]))
        maps[role] = role_map

    if set(maps["baseline"]) != set(maps["candidate"]):
        reasons.add("repetition_set_mismatch")
    all_runs = [
        (role, run)
        for role in ("baseline", "candidate")
        for run in by_role[role]
    ]
    orders = [int(run["execution_order"]) for _, run in all_runs]
    all_orders = [
        int(run["execution_order"])
        for artifact in (baseline, candidate)
        for run in artifact["runs"]
    ]
    if len(all_orders) != len(set(all_orders)):
        reasons.add("duplicate_execution_order")
    cancellation_orders = [
        int(run["execution_order"])
        for artifact in (baseline, candidate)
        for run in artifact["runs"]
        if run["cancellation"]["tested"]
    ]
    if cancellation_orders and orders and min(cancellation_orders) <= max(orders):
        reasons.add("unbalanced_execution_order")
    if not reasons & {"duplicate_run", "duplicate_execution_order", "repetition_set_mismatch"}:
        warm_repetitions = sorted(
            repetition for cold, repetition in maps["baseline"] if not cold
        )
        cold_repetitions = sorted(
            repetition for cold, repetition in maps["baseline"] if cold
        )
        if cold_repetitions != [0] or warm_repetitions != list(range(len(warm_repetitions))):
            reasons.add("unbalanced_execution_order")
        expected: list[tuple[str, bool, int]] = []
        if cold_repetitions:
            expected.extend(
                [("baseline", True, cold_repetitions[0]), ("candidate", True, cold_repetitions[0])]
            )
        for position, repetition in enumerate(warm_repetitions):
            roles = ("baseline", "candidate") if position % 2 == 0 else ("candidate", "baseline")
            expected.extend((role, False, repetition) for role in roles)
        ordered = sorted(all_runs, key=lambda item: int(item[1]["execution_order"]))
        actual = [(role, bool(run["cold"]), int(run["repetition_index"])) for role, run in ordered]
        if orders and (sorted(orders) != list(range(len(orders))) or actual != expected):
            reasons.add("unbalanced_execution_order")
    if reasons & {
        "duplicate_run", "duplicate_execution_order", "repetition_set_mismatch",
        "insufficient_cold_runs", "insufficient_warm_runs", "unbalanced_execution_order",
    }:
        return [], reasons
    keys = sorted(maps["baseline"])
    return [(maps["baseline"][key], maps["candidate"][key]) for key in keys], reasons


def _matched_runs(
    baseline: dict[str, Any], candidate: dict[str, Any]
) -> tuple[list[tuple[dict[str, Any], dict[str, Any]]], set[str]]:
    pairs, reasons = _paired_design(baseline, candidate)
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
            gpu_expected = bool((baseline["hardware"].get("gpus") or candidate["hardware"].get("gpus")))
            if gpu_expected and (
                run["pre_arm"]["gpu_snapshot"]["state"] != "available"
                or run["gpu"]["pre"]["state"] != "available"
                or run["gpu"]["post"]["state"] != "available"
                or run["gpu"]["sampling_state"] != "available"
                or run["gpu"]["during_peak_used_bytes"] is None
                or run["gpu"]["during_min_free_bytes"] is None
            ):
                reasons.add("incomplete_run")
            if (
                run["error_category"]
                or run["evidence_state"] != "complete"
                or run["memory"]["sampling_state"] != "available"
                or run["memory"]["cpu_sampling_state"] != "available"
                or run["pre_arm"]["interference"]["state"] != "available"
                or run["pre_arm"]["interference"]["system_cpu_percent"] is None
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


def _ambient_value(run: dict[str, Any], name: str) -> float | int | None:
    if name == "pre_arm_cpu_percent":
        return run["pre_arm"]["interference"]["system_cpu_percent"]
    if name == "pre_arm_available_ram_bytes":
        return run["pre_arm"]["available_ram_bytes"]
    if name == "pre_arm_gpu_used_bytes":
        return run["pre_arm"]["gpu_snapshot"]["used_bytes"]
    if name == "pre_arm_gpu_free_bytes":
        return run["pre_arm"]["gpu_snapshot"]["free_bytes"]
    if name == "elapsed_since_previous_arm_ms":
        return run["elapsed_since_previous_arm_ms"]
    if name == "during_cpu_mean_percent":
        return run["memory"]["system_cpu_mean_percent"]
    raise KeyError(name)


AMBIENT_METRICS = (
    "pre_arm_cpu_percent", "pre_arm_available_ram_bytes",
    "pre_arm_gpu_used_bytes", "pre_arm_gpu_free_bytes",
    "elapsed_since_previous_arm_ms", "during_cpu_mean_percent",
)


def _ambient_drift(pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in AMBIENT_METRICS:
        baseline_values = [_ambient_value(baseline, name) for baseline, _ in pairs]
        candidate_values = [_ambient_value(candidate, name) for _, candidate in pairs]
        differences = [
            abs(float(bvalue) - float(cvalue))
            for bvalue, cvalue in zip(baseline_values, candidate_values, strict=True)
            if bvalue is not None and cvalue is not None
        ]
        result[name] = {
            "baseline": _distribution(baseline_values),
            "candidate": _distribution(candidate_values),
            "paired_absolute_difference": _distribution(differences),
        }
    return result


def _policy_exceeded(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]], policy: dict[str, Any] | None
) -> bool:
    if policy is None:
        return False
    mappings = {
        "max_pre_arm_cpu_percent_difference": "pre_arm_cpu_percent",
        "max_pre_arm_available_ram_difference_bytes": "pre_arm_available_ram_bytes",
        "max_pre_arm_gpu_used_difference_bytes": "pre_arm_gpu_used_bytes",
        "max_elapsed_gap_ms": "elapsed_since_previous_arm_ms",
    }
    for policy_key, metric_name in mappings.items():
        threshold = policy[policy_key]
        if threshold is None:
            continue
        for baseline, candidate in pairs:
            bvalue = _ambient_value(baseline, metric_name)
            cvalue = _ambient_value(candidate, metric_name)
            if bvalue is not None and cvalue is not None and abs(float(bvalue) - float(cvalue)) > threshold:
                return True
    return False


def _cancellation_measurements(artifact: dict[str, Any]) -> dict[str, Any] | None:
    runs = [run for run in artifact["runs"] if run["cancellation"]["tested"]]
    if not runs:
        return None
    return {
        "request_to_cancel_ms": _distribution([run["cancellation"]["request_to_cancel_ms"] for run in runs]),
        "client_stream_closed_ms": _distribution([run["cancellation"]["client_stream_closed_ms"] for run in runs]),
        "runtime_idle_ms": _distribution([run["cancellation"]["runtime_idle_ms"] for run in runs]),
        "process_completion_ms": _distribution([run["cancellation"]["process_completion_ms"] for run in runs]),
        "resources_released": all(run["cancellation"]["resources_released"] is True for run in runs),
        "runtime_responsive": all(run["cancellation"]["runtime_responsive"] is True for run in runs),
    }


def compare_artifacts(
    artifacts: list[dict[str, Any]], policy: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Validate and compare all independent pairs without policy thresholds."""
    policy = _validate_policy(policy)
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
            if _policy_exceeded(pairs, policy):
                reasons.add("system_interference")
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
            "ambient_drift": _ambient_drift(pairs) if pairs else {},
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
        "interference_policy_applied": policy is not None,
        "interference_policy": policy,
    }


def load_and_compare(
    paths: list[str | Path], policy: dict[str, Any] | None = None
) -> dict[str, Any]:
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
    report = compare_artifacts(artifacts, policy=policy)
    if load_errors:
        report["input_errors"] = load_errors + report["input_errors"]
        report["invalid_pair_count"] += len(load_errors)
        report["invalid_reason_counts"]["malformed_artifact"] = (
            report["invalid_reason_counts"].get("malformed_artifact", 0) + len(load_errors)
        )
    return report
