from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image

from odysseus_desktop_backend import __version__
from odysseus_desktop_backend.services.model_service import canonical_model_tag
from odysseus_desktop_backend.vision_benchmarks.reports import write_report_bundle
from odysseus_desktop_backend.vision_benchmarks.routes import check_static_route_availability, installed_model_match
from odysseus_desktop_backend.vision_benchmarks.schema import DEFAULT_ROUTES_PATH, DEFAULT_SUITE_PATH, load_routes, load_suite


def run_benchmark(
    *,
    suite_path: str | Path = DEFAULT_SUITE_PATH,
    routes_path: str | Path = DEFAULT_ROUTES_PATH,
    route_id: str,
    out_dir: str | Path | None = None,
    run_id: str = "",
    smoke: bool = False,
    case_id: str = "",
    limit: int = 0,
    local_image_dir: str | Path | None = None,
    include_local_paths: bool = False,
    profile_dir: str | Path | None = None,
) -> dict[str, Any]:
    suite = load_suite(suite_path, local_image_dir=local_image_dir, require_images=False)
    routes = load_routes(routes_path)
    route = next((item for item in routes if item["route_id"] == route_id), None)
    if route is None:
        raise KeyError(f"route not found: {route_id}")
    cases = selected_cases(suite["cases"], case_id=case_id, limit=1 if smoke and not limit else limit)
    stable_run_id = run_id or build_run_id(suite, route, cases, smoke=smoke)
    output_dir = Path(out_dir) if out_dir else Path("reports") / "vision_common_sense" / stable_run_id
    output_dir = output_dir.resolve()
    executor: BenchmarkExecutor = SmokeExecutor() if smoke or route.get("smoke_only") else ChatSidecarExecutor(profile_dir=profile_dir)
    availability = executor.check_route(route)
    run = run_metadata(suite, route, stable_run_id, smoke=smoke, availability=availability)
    results: list[dict[str, Any]] = []
    for case in cases:
        image = suite["images"][case["image_id"]]
        image_path = Path(str(image["resolved_path"]))
        if not image_path.is_file():
            results.extend(skipped_case_results(run, route, case, image, "skipped_missing_image", f"Image file is missing: {image['path']}", include_local_paths))
            continue
        if not availability.get("available"):
            results.extend(skipped_case_results(run, route, case, image, str(availability["status"]), str(availability["reason"]), include_local_paths))
            continue
        results.extend(executor.run_case(run, route, case, image, include_local_paths=include_local_paths))
    paths = write_report_bundle(run, results, output_dir)
    executor.close()
    return {**run, "output_dir": str(output_dir), "paths": paths, "results": results, "summary": json.loads(Path(paths["summary_json"]).read_text(encoding="utf-8"))["summary"]}


class BenchmarkExecutor:
    def check_route(self, route: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def run_case(
        self,
        run: dict[str, Any],
        route: dict[str, Any],
        case: dict[str, Any],
        image: dict[str, Any],
        *,
        include_local_paths: bool,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


class SmokeExecutor(BenchmarkExecutor):
    def check_route(self, route: dict[str, Any]) -> dict[str, Any]:
        return {"available": True, "status": "available", "reason": "smoke route uses deterministic fake responses"}

    def run_case(
        self,
        run: dict[str, Any],
        route: dict[str, Any],
        case: dict[str, Any],
        image: dict[str, Any],
        *,
        include_local_paths: bool,
    ) -> list[dict[str, Any]]:
        dimensions = image_dimensions(Path(str(image["resolved_path"])))
        results = []
        for turn_index, turn in enumerate(case["turns"]):
            started = time.perf_counter()
            expected = ", ".join(turn["expected_good"][:3])
            answer = f"Smoke benchmark plumbing answer for {turn['question']}. Expected concepts include: {expected}."
            elapsed = max(1, int((time.perf_counter() - started) * 1000))
            results.append(
                base_result(
                    run,
                    route,
                    case,
                    turn,
                    turn_index,
                    image,
                    include_local_paths=include_local_paths,
                    image_dimensions=dimensions,
                    answer_text=answer,
                    status="completed",
                    perception_completed=True,
                    synthesis_completed=True,
                    raw_evidence_reused=bool(turn_index > 0),
                    curated_evidence_recomputed=bool(turn_index > 0),
                    vision_rerun=False,
                    total_wall_time_ms=elapsed,
                    vision_time_ms=0,
                    synthesis_time_ms=elapsed,
                    notes="Smoke route validates benchmark plumbing only; it is not a model-quality result.",
                )
            )
        return results


class ChatSidecarExecutor(BenchmarkExecutor):
    def __init__(self, profile_dir: str | Path | None = None):
        self.profile_dir = Path(profile_dir).resolve() if profile_dir else None
        self._temp_profile: tempfile.TemporaryDirectory[str] | None = None
        self.app: Any | None = None
        self._old_dev_repo_root: str | None = None

    def _ensure_app(self) -> Any:
        if self.app is not None:
            return self.app
        from rpc_server import SidecarApp

        if self.profile_dir is None:
            self._temp_profile = tempfile.TemporaryDirectory(prefix="odysseus-vision-benchmark-profile-")
            self.profile_dir = Path(self._temp_profile.name)
        self._old_dev_repo_root = os.environ.get("ODYSSEUS_DEV_REPO_ROOT")
        os.environ.setdefault("ODYSSEUS_DEV_REPO_ROOT", str(Path(__file__).resolve().parents[3]))
        self.app = SidecarApp(self.profile_dir)
        return self.app

    def check_route(self, route: dict[str, Any]) -> dict[str, Any]:
        app = self._ensure_app()
        ollama = app.models.detect_ollama()
        installed = [str(item) for item in ollama.get("models") or []]
        florence_ready = bool(app.florence.status(check_hashes=False).get("ready"))
        capabilities: dict[str, dict[str, Any]] = {}
        if route.get("requires_vision_capability"):
            target = str(route.get("final_model") or route.get("vision_model") or "")
            matched = installed_model_match(target, installed)
            if matched:
                try:
                    cap = app.models.inspect(matched)
                    capabilities[canonical_model_tag(matched)] = cap
                except Exception as exc:  # noqa: BLE001 - skip instead of repairing or installing
                    return {"available": False, "status": "skipped_missing_backend", "reason": f"Vision capability inspection failed: {exc}"}
        return check_static_route_availability(route, installed_models=installed, florence_ready=florence_ready, model_capabilities=capabilities)

    def run_case(
        self,
        run: dict[str, Any],
        route: dict[str, Any],
        case: dict[str, Any],
        image: dict[str, Any],
        *,
        include_local_paths: bool,
    ) -> list[dict[str, Any]]:
        app = self._ensure_app()
        artifact = app.artifacts.import_path(str(image["resolved_path"]), source_kind="file", name=image["id"], scope="session")
        session_id = ""
        results: list[dict[str, Any]] = []
        for turn_index, turn in enumerate(case["turns"]):
            started = time.perf_counter()
            status = "completed"
            answer = ""
            analysis: dict[str, Any] = {}
            error = ""
            try:
                response = app.chat.send(
                    message=turn["question"],
                    session_id=session_id or None,
                    model=route.get("final_model") or None,
                    artifact_ids=[artifact["id"]] if turn_index == 0 else [],
                    multimodal_mode=str(route.get("multimodal_mode") or "automatic"),
                    vision_model=str(route.get("vision_model") or ""),
                    vision_backend=str(route.get("vision_backend") or ""),
                    thinking_mode="off",
                    temperature=0.0,
                    timeout=300,
                    analysis_request_id=f"{run['run_id']}:{case['id']}:{turn['id']}",
                )
                session_id = str((response.get("session") or {}).get("id") or session_id)
                assistant = response.get("assistant_message") if isinstance(response.get("assistant_message"), dict) else {}
                answer = str(assistant.get("content") or "")
                analysis = response.get("artifact_analysis") if isinstance(response.get("artifact_analysis"), dict) else {}
                if not answer and analysis:
                    answer = str((analysis.get("output") or {}).get("answer") or "")
                if not assistant:
                    status = str(analysis.get("status") or "error")
            except Exception as exc:  # noqa: BLE001 - per-case persistence surface
                status = "error"
                error = str(exc)
            elapsed = int((time.perf_counter() - started) * 1000)
            results.append(result_from_analysis(run, route, case, turn, turn_index, image, analysis, include_local_paths, answer, status, error, elapsed))
        return results

    def close(self) -> None:
        if self.app is not None:
            self.app.close()
            self.app = None
        if self._old_dev_repo_root is None:
            os.environ.pop("ODYSSEUS_DEV_REPO_ROOT", None)
        else:
            os.environ["ODYSSEUS_DEV_REPO_ROOT"] = self._old_dev_repo_root
        if self._temp_profile is not None:
            self._temp_profile.cleanup()
            self._temp_profile = None


def result_from_analysis(
    run: dict[str, Any],
    route: dict[str, Any],
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_index: int,
    image: dict[str, Any],
    analysis: dict[str, Any],
    include_local_paths: bool,
    answer: str,
    status: str,
    error: str,
    elapsed_ms: int,
) -> dict[str, Any]:
    output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
    evidence = analysis.get("evidence") if isinstance(analysis.get("evidence"), dict) else {}
    timings = analysis.get("timings") if isinstance(analysis.get("timings"), dict) else {}
    provenance = output.get("provenance") if isinstance(output.get("provenance"), dict) else {}
    preprocessing = evidence.get("preprocessing") if isinstance(evidence.get("preprocessing"), dict) else {}
    original = preprocessing.get("original") if isinstance(preprocessing.get("original"), dict) else {}
    vision_input = preprocessing.get("vision_input") if isinstance(preprocessing.get("vision_input"), dict) else {}
    ocr_input = preprocessing.get("ocr_input") if isinstance(preprocessing.get("ocr_input"), dict) else {}
    return base_result(
        run,
        route,
        case,
        turn,
        turn_index,
        image,
        include_local_paths=include_local_paths,
        image_dimensions={"width": int(original.get("width") or 0), "height": int(original.get("height") or 0)},
        vision_input_dimensions={"width": int(vision_input.get("width") or 0), "height": int(vision_input.get("height") or 0)},
        ocr_input_dimensions={"width": int(ocr_input.get("width") or 0), "height": int(ocr_input.get("height") or 0)},
        answer_text=answer,
        status=status,
        failed_stage=error or str(provenance.get("failed_stage") or analysis.get("stage") or ""),
        perception_completed=bool(provenance.get("perception_completed") or evidence.get("perception_completed") or output.get("ocr_text")),
        synthesis_completed=bool(provenance.get("synthesis_started") and answer),
        ocr_text_detected=bool(str(output.get("ocr_text") or "").strip()),
        raw_evidence_reused=bool(provenance.get("raw_evidence_reused")),
        curated_evidence_recomputed=bool(provenance.get("curated_evidence_recomputed")),
        vision_rerun=bool(provenance.get("vision_rerun")),
        grounding_guard_triggered=bool(provenance.get("grounding_guard_triggered")),
        safe_fallback_used=bool(provenance.get("safe_fallback_used")),
        total_wall_time_ms=elapsed_ms,
        vision_time_ms=int(((evidence.get("vision") or {}).get("elapsed_ms") if isinstance(evidence.get("vision"), dict) else 0) or 0),
        synthesis_time_ms=int(timings.get("synthesis_elapsed_ms") or 0),
        vision_backend_actual=str(provenance.get("vision_backend") or analysis.get("actual_vision_backend") or ""),
        vision_model_actual=str(provenance.get("vision_inspection_model") or analysis.get("actual_vision_model") or ""),
        final_model_actual=str(provenance.get("final_answer_model") or ""),
        notes="; ".join(str(item) for item in analysis.get("warnings") or [] if str(item)),
    )


def base_result(
    run: dict[str, Any],
    route: dict[str, Any],
    case: dict[str, Any],
    turn: dict[str, Any],
    turn_index: int,
    image: dict[str, Any],
    *,
    include_local_paths: bool,
    image_dimensions: dict[str, int] | None = None,
    vision_input_dimensions: dict[str, int] | None = None,
    ocr_input_dimensions: dict[str, int] | None = None,
    answer_text: str = "",
    status: str = "completed",
    failed_stage: str = "",
    perception_completed: bool = False,
    synthesis_completed: bool = False,
    ocr_text_detected: bool = False,
    raw_evidence_reused: bool = False,
    curated_evidence_recomputed: bool = False,
    vision_rerun: bool = False,
    grounding_guard_triggered: bool = False,
    safe_fallback_used: bool = False,
    total_wall_time_ms: int = 0,
    vision_time_ms: int = 0,
    synthesis_time_ms: int = 0,
    vision_backend_actual: str = "",
    vision_model_actual: str = "",
    final_model_actual: str = "",
    notes: str = "",
) -> dict[str, Any]:
    turn_id = str(turn["id"])
    return {
        "result_id": f"{run['run_id']}:{case['id']}:{turn_id}",
        "run_id": run["run_id"],
        "timestamp": run["timestamp"],
        "app_version": run["app_version"],
        "git_head": run["git_head"],
        "working_tree_dirty": run["working_tree_dirty"],
        "build_variant": run.get("build_variant") or "",
        "route_id": route["route_id"],
        "vision_backend_requested": route.get("vision_backend") or "",
        "vision_backend_actual": vision_backend_actual or route.get("vision_backend") or "",
        "vision_model_actual": vision_model_actual or route.get("vision_model") or "",
        "final_model_actual": final_model_actual or route.get("final_model") or "",
        "image_id": image["id"],
        "image_path": image_reference(image, include_local_paths),
        "image_dimensions": image_dimensions or {"width": 0, "height": 0},
        "vision_input_dimensions": vision_input_dimensions or image_dimensions or {"width": 0, "height": 0},
        "ocr_input_dimensions": ocr_input_dimensions or image_dimensions or {"width": 0, "height": 0},
        "case_id": case["id"],
        "category": case["category"],
        "question_id": turn_id,
        "turn_id": turn_id,
        "question_text": turn["question"],
        "question_type": turn.get("question_type") or case.get("question_type") or "",
        "turn_index": turn_index,
        "answer_text": answer_text,
        "status": status,
        "failed_stage": failed_stage,
        "perception_completed": perception_completed,
        "synthesis_completed": synthesis_completed,
        "ocr_text_detected": ocr_text_detected,
        "raw_evidence_reused": raw_evidence_reused,
        "curated_evidence_recomputed": curated_evidence_recomputed,
        "vision_rerun": vision_rerun,
        "grounding_guard_triggered": grounding_guard_triggered,
        "safe_fallback_used": safe_fallback_used,
        "total_wall_time_ms": total_wall_time_ms,
        "vision_time_ms": vision_time_ms,
        "synthesis_time_ms": synthesis_time_ms,
        "expected_good": turn["expected_good"],
        "acceptable": turn["acceptable"],
        "must_not_include": turn["must_not_include"],
        "correct_abstention_expected": bool(turn["correct_abstention"]),
        "followup_should_reuse_evidence": bool(turn["followup_should_reuse_evidence"]),
        "notes": notes or turn.get("notes") or case.get("notes") or "",
    }


def skipped_case_results(
    run: dict[str, Any],
    route: dict[str, Any],
    case: dict[str, Any],
    image: dict[str, Any],
    status: str,
    reason: str,
    include_local_paths: bool,
) -> list[dict[str, Any]]:
    dimensions = image_dimensions(Path(str(image["resolved_path"]))) if Path(str(image["resolved_path"])).is_file() else {"width": 0, "height": 0}
    return [
        base_result(
            run,
            route,
            case,
            turn,
            index,
            image,
            include_local_paths=include_local_paths,
            image_dimensions=dimensions,
            status=status,
            failed_stage=reason,
            notes=reason,
        )
        for index, turn in enumerate(case["turns"])
    ]


def run_metadata(
    suite: dict[str, Any],
    route: dict[str, Any],
    run_id: str,
    *,
    smoke: bool,
    availability: dict[str, Any],
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp": utc_timestamp(),
        "app_version": __version__,
        "git_head": git_text(["rev-parse", "HEAD"]),
        "working_tree_dirty": bool(git_text(["status", "--porcelain"]).strip()),
        "build_variant": os.environ.get("ODYSSEUS_BUILD_VARIANT", ""),
        "suite_id": suite["suite_id"],
        "suite_name": suite["suite_name"],
        "suite_version": suite["suite_version"],
        "route_id": route["route_id"],
        "route": route,
        "smoke": smoke,
        "availability": availability,
    }


def build_run_id(suite: dict[str, Any], route: dict[str, Any], cases: list[dict[str, Any]], *, smoke: bool) -> str:
    payload = {
        "suite_id": suite["suite_id"],
        "suite_version": suite["suite_version"],
        "route_id": route["route_id"],
        "cases": [case["id"] for case in cases],
        "smoke": smoke,
    }
    digest = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    prefix = "smoke" if smoke else "run"
    return f"{prefix}-{suite['suite_id']}-{route['route_id']}-{digest}"


def selected_cases(cases: list[dict[str, Any]], *, case_id: str, limit: int) -> list[dict[str, Any]]:
    selected = [case for case in cases if not case_id or case["id"] == case_id]
    if case_id and not selected:
        raise KeyError(f"benchmark case not found: {case_id}")
    if limit > 0:
        selected = selected[:limit]
    return selected


def image_dimensions(path: Path) -> dict[str, int]:
    with Image.open(path) as image:
        return {"width": int(image.width), "height": int(image.height)}


def image_reference(image: dict[str, Any], include_local_paths: bool) -> str:
    if image.get("private") and not include_local_paths:
        return f"local_or_private_image:{image['id']}"
    return str(image.get("path") or image["id"])


def git_text(args: list[str]) -> str:
    try:
        proc = subprocess.run(["git", *args], cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=5)
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def utc_timestamp() -> str:
    import datetime as _dt

    return _dt.datetime.now(tz=_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
