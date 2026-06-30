from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from odysseus_desktop_backend import __version__
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.vision_service import VisionService
from odysseus_desktop_backend.storage import Database, utc_ms


IMAGE_EVAL_SUITE_NAME = "local-image-understanding"
IMAGE_EVAL_SUITE_VERSION = "v0.2.0"
IMAGE_EVAL_PROMPT_VERSION = "image-analysis-v0.2.0"
IMAGE_EVAL_CASES_DIR = Path(__file__).resolve().parents[3] / "evals" / "image_cases_v020"
logger = get_logger("image_evals")


class ImageEvalService:
    def __init__(self, db: Database, artifacts: ArtifactService, vision: VisionService):
        self.db = db
        self.artifacts = artifacts
        self.vision = vision

    def list_cases(self) -> dict[str, Any]:
        cases = load_cases()
        return {
            "suite_name": IMAGE_EVAL_SUITE_NAME,
            "suite_version": IMAGE_EVAL_SUITE_VERSION,
            "prompt_version": IMAGE_EVAL_PROMPT_VERSION,
            "cases_dir": str(IMAGE_EVAL_CASES_DIR),
            "case_count": len(cases),
            "cases": [case_summary(case) for case in cases],
        }

    def run(self, *, mode: str, model: str = "") -> dict[str, Any]:
        if mode not in {"ocr_only", "vision_only", "combined"}:
            raise ValueError("image eval mode must be ocr_only, vision_only, or combined")
        run_id = str(uuid.uuid4())
        now = utc_ms()
        self.db.conn.execute(
            """
            INSERT INTO multimodal_eval_runs(
                id, suite_name, suite_version, prompt_version, mode, model, ocr_engine,
                total_passed, total_failed, grader_review_count, timeout_count,
                runtime_error_count, total_runtime_ms, status, notes, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, '', 0, 0, 0, 0, 0, 0, 'running', ?, ?, NULL)
            """,
            (
                run_id,
                IMAGE_EVAL_SUITE_NAME,
                IMAGE_EVAL_SUITE_VERSION,
                IMAGE_EVAL_PROMPT_VERSION,
                mode,
                model,
                f"app_version={__version__}",
                now,
            ),
        )
        self.db.conn.commit()
        started = time.perf_counter()
        cases = [case for case in load_cases() if mode in case.get("modes", [])]
        passed = failed = review = timeouts = errors = 0
        for case in cases:
            result = self._run_case(run_id, case, mode=mode, model=model)
            if result["status"] == "timeout":
                timeouts += 1
            elif result["status"] == "error":
                errors += 1
            elif result["grader_review_required"]:
                review += 1
            elif result["passed"]:
                passed += 1
            else:
                failed += 1
        total_runtime_ms = int((time.perf_counter() - started) * 1000)
        status = "completed"
        self.db.conn.execute(
            """
            UPDATE multimodal_eval_runs
            SET total_passed = ?, total_failed = ?, grader_review_count = ?,
                timeout_count = ?, runtime_error_count = ?, total_runtime_ms = ?,
                status = ?, completed_at = ?
            WHERE id = ?
            """,
            (passed, failed, review, timeouts, errors, total_runtime_ms, status, utc_ms(), run_id),
        )
        self.db.conn.commit()
        return self._run_with_cases(run_id)

    def history(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT *
            FROM multimodal_eval_runs
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (min(max(limit, 1), 100),),
        ).fetchall()
        return [self._run_dict(row, include_cases=True) for row in rows]

    def _run_case(self, run_id: str, case: dict[str, Any], *, mode: str, model: str) -> dict[str, Any]:
        started = time.perf_counter()
        status = "completed"
        analysis: dict[str, Any] = {}
        reasons: list[str] = []
        artifact = None
        try:
            image_path = IMAGE_EVAL_CASES_DIR / str(case["image"])
            artifact = self.artifacts.import_path(str(image_path), source_kind="file", name=f"eval-{case['id']}")
            crop_id = ""
            if isinstance(case.get("crop"), dict):
                crop = self.artifacts.create_crop(artifact["id"], case["crop"])
                crop_id = str(crop["id"])
            analysis = self.vision.analyze(
                artifact["id"],
                mode=mode,
                question=str(case.get("question") or ""),
                vision_model=model,
                request_id=f"image-eval-{run_id}-{case['id']}-{mode}",
                crop_derivation_id=crop_id,
            )
            if analysis["status"] == "timeout":
                status = "timeout"
            elif analysis["status"] in {"error", "interrupted"}:
                status = "error"
        except Exception as exc:  # noqa: BLE001 - individual cases should be persisted
            status = "error"
            reasons.append(str(exc))
            analysis = {"output": {}, "warnings": [], "error": str(exc)}
        grade = grade_case(case, analysis) if status == "completed" else {"passed": False, "review": False, "reasons": reasons or [analysis.get("error") or status], "matches": []}
        latency_ms = int((time.perf_counter() - started) * 1000)
        image_hash = artifact.get("content_hash", "") if artifact else ""
        image_width = int(artifact.get("width") or 0) if artifact else 0
        image_height = int(artifact.get("height") or 0) if artifact else 0
        row_id = str(uuid.uuid4())
        self.db.conn.execute(
            """
            INSERT INTO multimodal_eval_case_results(
                id, run_id, case_id, category, mode, status, passed,
                grader_review_required, reasons_json, raw_output, structured_output_json,
                assertion_matches_json, warnings_json, error, latency_ms, image_hash,
                image_width, image_height, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row_id,
                run_id,
                case["id"],
                case["category"],
                mode,
                status,
                1 if grade["passed"] else 0,
                1 if grade["review"] else 0,
                json.dumps(grade["reasons"]),
                json.dumps(analysis.get("output") or {}, ensure_ascii=False),
                json.dumps(analysis.get("output") or {}),
                json.dumps(grade["matches"]),
                json.dumps(analysis.get("warnings") or []),
                str(analysis.get("error") or ""),
                latency_ms,
                image_hash,
                image_width,
                image_height,
                utc_ms(),
            ),
        )
        self.db.conn.commit()
        return self._case_result(row_id)

    def _run_with_cases(self, run_id: str) -> dict[str, Any]:
        row = self.db.conn.execute("SELECT * FROM multimodal_eval_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"image eval run not found: {run_id}")
        return self._run_dict(row, include_cases=True)

    def _run_dict(self, row: Any, *, include_cases: bool = False) -> dict[str, Any]:
        item = dict(row)
        if include_cases:
            rows = self.db.conn.execute(
                """
                SELECT *
                FROM multimodal_eval_case_results
                WHERE run_id = ?
                ORDER BY created_at ASC
                """,
                (item["id"],),
            ).fetchall()
            item["cases"] = [case_result_dict(case_row) for case_row in rows]
        return item

    def _case_result(self, result_id: str) -> dict[str, Any]:
        row = self.db.conn.execute("SELECT * FROM multimodal_eval_case_results WHERE id = ?", (result_id,)).fetchone()
        if row is None:
            raise KeyError(f"image eval case result not found: {result_id}")
        return case_result_dict(row)


def load_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(IMAGE_EVAL_CASES_DIR.glob("*.json")):
        cases.append(json.loads(path.read_text(encoding="utf-8")))
    return cases


def case_summary(case: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": case["id"],
        "category": case["category"],
        "modes": case.get("modes", []),
        "question": case.get("question", ""),
        "image": case.get("image", ""),
    }


def grade_case(case: dict[str, Any], analysis: dict[str, Any]) -> dict[str, Any]:
    output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
    text = normalized_blob(output)
    reasons: list[str] = []
    matches: list[dict[str, Any]] = []
    review = False
    assertions = case.get("assertions") if isinstance(case.get("assertions"), list) else []
    for assertion in assertions:
        kind = assertion.get("type")
        values = [str(value) for value in assertion.get("any", [])]
        if kind in {"exact_text", "required_object", "spatial_relation", "count"}:
            matched = any(normalize(value) in text for value in values)
            matches.append({"type": kind, "matched": matched, "values": values})
            if not matched:
                reasons.append(f"missing {kind}: {' / '.join(values)}")
        elif kind == "forbidden_object":
            matched = any(normalize(value) in text for value in values)
            matches.append({"type": kind, "matched": matched, "values": values})
            if matched:
                reasons.append(f"forbidden object present: {' / '.join(values)}")
        elif kind == "abstention":
            matched = any(phrase in text for phrase in ("not visible", "not determinable", "cannot determine", "not shown"))
            matches.append({"type": kind, "matched": matched})
            if not matched:
                reasons.append("missing not-visible abstention")
        elif kind == "review":
            review = True
    return {"passed": not reasons and not review, "review": review, "reasons": reasons, "matches": matches}


def normalized_blob(output: dict[str, Any]) -> str:
    return normalize(json.dumps(output, ensure_ascii=False))


def normalize(value: str) -> str:
    return " ".join((value or "").lower().split())


def case_result_dict(row: Any) -> dict[str, Any]:
    item = dict(row)
    item["passed"] = bool(item["passed"])
    item["grader_review_required"] = bool(item["grader_review_required"])
    item["reasons"] = json.loads(item.pop("reasons_json") or "[]")
    item["structured_output"] = json.loads(item.pop("structured_output_json") or "{}")
    item["assertion_matches"] = json.loads(item.pop("assertion_matches_json") or "[]")
    item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
    return item
