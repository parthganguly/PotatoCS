"""Read-only facade for runtime inventory, benchmark evidence, and planning.

Consumed by the `runtime.*` RPC methods. Strictly read-only: no settings
mutation, no downloads, no server control, no DB writes. Every method
returns a structured dict; failures degrade to partial results with
fixed error categories rather than raising provider exceptions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services import runtime_inventory as ri
from odysseus_desktop_backend.services import runtime_planner as rp
from odysseus_desktop_backend.storage import utc_ms

logger = get_logger("runtime_plan")

MAX_EVIDENCE_FILES = 200
MAX_EVIDENCE_FILE_BYTES = 4 * 1024 * 1024


def summarize_artifact(artifact: dict[str, Any]) -> dict[str, Any] | None:
    """Compress one benchmark artifact into planner evidence.

    Uses warm, quality-passing runs only; returns None when the artifact
    carries no usable warm measurement (failed runs are visible in the
    benchmarks listing, never silently promoted to evidence).
    """
    if artifact.get("schema_version") != 1:
        return None
    runs = [
        run
        for run in artifact.get("runs") or []
        if isinstance(run, dict)
        and not run.get("cold")
        and not run.get("error_category")
        and run.get("quality_check") in {"passed", "not_applicable"}
    ]
    ttfts = sorted(
        run["timings_ms"]["first_token"]
        for run in runs
        if isinstance(run.get("timings_ms"), dict) and run["timings_ms"].get("first_token") is not None
    )
    tpss = sorted(
        run["tokens"]["generation_tps"]
        for run in runs
        if isinstance(run.get("tokens"), dict) and run["tokens"].get("generation_tps") is not None
    )
    if not ttfts or not tpss:
        return None
    hardware = artifact.get("hardware") or {}
    return {
        "model_tag": str((artifact.get("model") or {}).get("tag") or ""),
        "shape": str(artifact.get("shape") or ""),
        "engine_kind": str(artifact.get("engine_kind") or "real"),
        "runtime_name": str((artifact.get("runtime") or {}).get("name") or ""),
        "warm_ttft_ms": ttfts[len(ttfts) // 2],
        "generation_tps": tpss[len(tpss) // 2],
        "captured_at_ms": int(hardware.get("captured_at_ms") or 0),
        "hardware_cpu": dict((hardware.get("cpu") or {})),
        "batch_ids": [str(artifact.get("batch_id") or "")],
    }


class RuntimePlanService:
    def __init__(self, evidence_dir: str | Path | None = None):
        # Research phase: benchmark evidence is a dev-machine artifact
        # directory; production profiles have none and the planner
        # degrades to derived/conservative confidence by design.
        self.evidence_dir = Path(evidence_dir) if evidence_dir else None

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
        summaries: dict[str, dict[str, Any]] = {}
        for artifact in self._load_artifacts():
            if artifact.get("engine_kind") == "stub":
                continue
            summary = summarize_artifact(artifact)
            if summary is None or not summary["model_tag"]:
                continue
            # Prefer the "medium" shape as the representative interactive
            # workload; otherwise keep the first summary seen per model.
            existing = summaries.get(summary["model_tag"])
            if existing is None or (summary["shape"] == "medium" and existing["shape"] != "medium"):
                summaries[summary["model_tag"]] = summary
            elif existing is not None and summary["shape"] == existing["shape"]:
                existing["batch_ids"] = sorted(set(existing["batch_ids"]) | set(summary["batch_ids"]))
        return [summaries[key] for key in sorted(summaries)]

    # -- RPC surface -----------------------------------------------------

    def inventory(self) -> dict[str, Any]:
        hardware = ri.hardware_inventory()
        runtimes = ri.runtime_inventory()
        models = ri.model_inventory()
        return {
            "ok": True,
            "hardware": hardware,
            "runtimes": runtimes["runtimes"],
            "models": models["models"],
            "model_error_category": models["error_category"],
            "captured_at_ms": utc_ms(),
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

    def _plan_inputs(self) -> dict[str, Any]:
        return {
            "hardware": ri.hardware_inventory(),
            "runtimes": ri.runtime_inventory()["runtimes"],
            "models": ri.model_inventory()["models"],
            "evidence": self._evidence_summaries(),
        }

    def plan(self, objective: str) -> dict[str, Any]:
        if objective not in rp.OBJECTIVES:
            return {
                "ok": False,
                "error_category": "invalid_objective",
                "error": "Objective must be one of: fast, balanced, deep.",
            }
        inputs = self._plan_inputs()
        plan = rp.build_plan(
            objective=objective,
            hardware=inputs["hardware"],
            models=inputs["models"],
            runtimes=inputs["runtimes"],
            evidence=inputs["evidence"],
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
        return {"ok": True, "plan": plan}

    def recommendations(self) -> dict[str, Any]:
        inputs = self._plan_inputs()
        now = utc_ms()
        plans = {
            objective: rp.build_plan(
                objective=objective,
                hardware=inputs["hardware"],
                models=inputs["models"],
                runtimes=inputs["runtimes"],
                evidence=inputs["evidence"],
                now_ms=now,
            )
            for objective in rp.OBJECTIVES
        }
        return {
            "ok": True,
            "objectives": list(rp.OBJECTIVES),
            "plans": plans,
            "capability_matrix": _capability_matrix(),
            "note": (
                "Estimates, not guarantees. Plans are recommendations only; "
                "nothing is applied automatically."
            ),
        }


def _capability_matrix() -> dict[str, Any]:
    from odysseus_desktop_backend.runtime_bench.capabilities import runtime_capability_matrix

    return runtime_capability_matrix()
