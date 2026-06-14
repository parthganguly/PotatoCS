from __future__ import annotations

import base64
import html
import json
import os
import platform
import re
import shutil
import tempfile
import textwrap
import time
import uuid
from pathlib import Path
from typing import Any

from pypdf import PdfReader

from odysseus_desktop_backend import __version__
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.embedding_service import DEFAULT_EMBEDDING_MODEL
from odysseus_desktop_backend.services.eval_service import (
    EVAL_SUITE_NAME,
    EVAL_SUITE_VERSION,
    PROMPT_VERSION,
    EvalService,
    benchmark_comparison,
    canonical_pipeline_diagnosis,
    json_loads,
    primary_case_bucket,
    timeout_policy,
)
from odysseus_desktop_backend.storage import Database, utc_ms


REPORT_SCHEMA_VERSION = "1"
DEFAULT_REPORT_DIR_NAME = "Odysseus Reports"
PDF_NAME = "odysseus-benchmark-report.pdf"
HTML_NAME = "odysseus-benchmark-report.html"
JSON_NAME = "odysseus-benchmark-data.json"
SCREENSHOTS_DIR_NAME = "screenshots"
DATA_URL_RE = re.compile(r"^data:image/(png|jpeg);base64,", re.IGNORECASE)
REMOTE_URL_RE = re.compile(r"https?://", re.IGNORECASE)
REPORT_PENDING = "pending"
REPORT_AWAITING_CAPTURE = "awaiting_capture"
REPORT_CAPTURING = "capturing"
REPORT_GENERATING = "generating"
REPORT_COMPLETED = "completed"
REPORT_COMPLETED_WITH_WARNINGS = "completed_with_warnings"
REPORT_ERROR = "error"
logger = get_logger("reports")


class ReportService:
    def __init__(self, db: Database):
        self.db = db

    def build_report_data(
        self,
        campaign_id: str,
        *,
        include_detailed_audit: bool = False,
        screenshot_manifest: list[dict[str, Any]] | None = None,
        report_generation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        jobs = self._jobs(campaign_id)
        runs = self._runs_for_jobs(jobs, include_detailed_audit=include_detailed_audit)
        comparison = benchmark_comparison(runs, current_suite_version=EVAL_SUITE_VERSION)
        pipeline = pipeline_diagnoses(runs)
        recommendations = report_recommendations(comparison)
        runtime = self._runtime_context(campaign, jobs, runs)
        timeouts = collect_timeouts_and_errors(jobs, runs)
        data = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "report_status": str(campaign.get("report_status") or ""),
            "campaign": public_campaign(campaign),
            "application": {
                "name": "Odysseus Desktop",
                "version": __version__,
                "local_only": True,
                "privacy_statement": (
                    "All benchmark execution and report generation were performed locally. "
                    "Full private profile paths are omitted from this report."
                ),
            },
            "eval_suite": {
                "name": EVAL_SUITE_NAME,
                "version": EVAL_SUITE_VERSION,
                "prompt_version": PROMPT_VERSION,
            },
            "runtime": runtime,
            "embedding": {
                "backend": str(campaign.get("embedding_backend") or ""),
                "model": str(campaign.get("embedding_model") or DEFAULT_EMBEDDING_MODEL),
            },
            "job_matrix": [public_job(job) for job in jobs],
            "benchmark_runs": runs,
            "comparison": comparison,
            "recommendation": recommendations,
            "case_difficulty": comparison.get("case_difficulty") or {},
            "pipeline_diagnoses": pipeline,
            "timeouts_errors": timeouts,
            "report_generation": report_generation or {
                "generated_at": utc_ms(),
                "schema_version": REPORT_SCHEMA_VERSION,
            },
            "screenshot_manifest": screenshot_manifest or [],
        }
        data["view_model"] = build_report_view_model(data)
        return data

    def generate_campaign_report(
        self,
        campaign_id: str,
        *,
        output_folder: str | None = None,
        screenshots: list[dict[str, Any]] | None = None,
        capture_failure_reason: str | None = None,
        include_detailed_audit: bool | None = None,
        generate_pdf: bool = True,
        generate_html: bool = True,
    ) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        output_dir = unique_report_dir(output_folder, str(campaign.get("title") or "campaign"))
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging_dir = output_dir.parent / f".{output_dir.name}.tmp-{uuid.uuid4().hex[:8]}"
        staging_dir.mkdir(parents=True, exist_ok=False)
        warnings: list[str] = []
        if capture_failure_reason:
            warnings.append(f"DOM screenshot capture was unavailable: {capture_failure_reason}")
        detailed = bool(
            campaign.get("include_detailed_audit")
            if include_detailed_audit is None
            else include_detailed_audit
        )
        generation = {
            "generated_at": utc_ms(),
            "schema_version": REPORT_SCHEMA_VERSION,
            "status": REPORT_GENERATING,
            "warnings": warnings,
        }
        try:
            data = self.build_report_data(
                campaign_id,
                include_detailed_audit=detailed,
                screenshot_manifest=[],
                report_generation=generation,
            )
            screenshot_manifest = self._write_screenshots(staging_dir, screenshots or [], warnings, data)
            final_paths: dict[str, str] = {"json": str(output_dir / JSON_NAME)}
            if generate_html:
                final_paths["html"] = str(output_dir / HTML_NAME)
            if generate_pdf:
                final_paths["pdf"] = str(output_dir / PDF_NAME)

            pdf_status = "not_requested"
            html_status = "not_requested"
            status = REPORT_GENERATING
            generation["warnings"] = warnings
            data = self.build_report_data(
                campaign_id,
                include_detailed_audit=detailed,
                screenshot_manifest=screenshot_manifest,
                report_generation=generation,
            )
            data["campaign"]["report_files"] = {name: Path(path).name for name, path in final_paths.items()}
            data["campaign"]["report_schema_version"] = REPORT_SCHEMA_VERSION
            data["campaign"]["report_status"] = REPORT_GENERATING
            data["report_files"] = data["campaign"]["report_files"]

            html_path = staging_dir / HTML_NAME
            pdf_path = staging_dir / PDF_NAME
            if generate_html:
                html_text = render_html_report(data, staging_dir)
                if REMOTE_URL_RE.search(html_text):
                    warnings.append("HTML report contained a remote URL and was not written.")
                    html_status = "failed"
                    final_paths.pop("html", None)
                else:
                    html_path.write_text(html_text, encoding="utf-8")
                    html_status = "completed"

            if generate_pdf:
                try:
                    render_pdf_report(data, pdf_path, staging_dir)
                    validate_pdf(pdf_path)
                    pdf_status = "completed"
                except Exception as exc:  # noqa: BLE001 - HTML/JSON remain usable
                    logger.warning("PDF report generation failed campaign_id=%s error=%s", campaign_id, exc)
                    warnings.append(f"PDF generation failed: {exc}")
                    pdf_status = "failed"
                    final_paths.pop("pdf", None)

            usable_outputs = ["json"] + ([name for name in ("html", "pdf") if name in final_paths])
            status = REPORT_COMPLETED if (pdf_status in {"completed", "not_requested"} and html_status in {"completed", "not_requested"} and not warnings) else REPORT_COMPLETED_WITH_WARNINGS
            if usable_outputs == ["json"] and (generate_html or generate_pdf) and warnings:
                status = REPORT_COMPLETED_WITH_WARNINGS

            generation["status"] = status
            generation["warnings"] = warnings
            data = self.build_report_data(
                campaign_id,
                include_detailed_audit=detailed,
                screenshot_manifest=screenshot_manifest,
                report_generation=generation,
            )
            data["report_status"] = status
            data["report_files"] = {name: Path(path).name for name, path in final_paths.items()}
            data["campaign"]["report_status"] = status
            data["campaign"]["report_schema_version"] = REPORT_SCHEMA_VERSION
            data["campaign"]["report_files"] = data["report_files"]
            data["view_model"] = build_report_view_model(data)
            if generate_html and "html" in final_paths:
                html_text = render_html_report(data, staging_dir)
                if REMOTE_URL_RE.search(html_text):
                    warnings.append("Final HTML report contained a remote URL and was not written.")
                    final_paths.pop("html", None)
                    data["report_files"] = {name: Path(path).name for name, path in final_paths.items()}
                    data["campaign"]["report_files"] = data["report_files"]
                    data["report_generation"]["warnings"] = warnings
                    data["view_model"] = build_report_view_model(data)
                else:
                    html_path.write_text(html_text, encoding="utf-8")
            if warnings and data["report_generation"].get("status") == REPORT_COMPLETED:
                data["report_generation"]["status"] = REPORT_COMPLETED_WITH_WARNINGS
                data["report_status"] = REPORT_COMPLETED_WITH_WARNINGS
                data["campaign"]["report_status"] = REPORT_COMPLETED_WITH_WARNINGS
                status = REPORT_COMPLETED_WITH_WARNINGS
                data["view_model"] = build_report_view_model(data)
            json_path = staging_dir / JSON_NAME
            atomic_write_json(json_path, data)
            validate_report_artifacts(staging_dir, final_paths, screenshot_manifest)
            shutil.move(str(staging_dir), str(output_dir))
            paths = {name: str(output_dir / Path(path).name) for name, path in final_paths.items()}
            result = {
                "status": status,
                "paths": paths,
                "warnings": warnings,
                "report_schema_version": REPORT_SCHEMA_VERSION,
                "screenshot_manifest": screenshot_manifest,
                "pdf_status": pdf_status,
            }
            try:
                self._store_report_result(campaign_id, result)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Report artifacts were written, but SQLite final-state persistence failed: {exc}")
                result["status"] = REPORT_COMPLETED_WITH_WARNINGS
                result["warnings"] = warnings
                data["report_status"] = result["status"]
                data["report_generation"]["status"] = result["status"]
                data["report_generation"]["warnings"] = warnings
                data["campaign"]["report_status"] = result["status"]
                data["view_model"] = build_report_view_model(data)
                atomic_write_json(output_dir / JSON_NAME, data)
            verify_final_json(output_dir / JSON_NAME, result)
            return result
        except Exception:
            shutil.rmtree(staging_dir, ignore_errors=True)
            raise

    def open_report_path(self, path: str) -> dict[str, Any]:
        clean = Path(path)
        if not clean.exists():
            raise FileNotFoundError(f"report path does not exist: {path}")
        os.startfile(str(clean))  # type: ignore[attr-defined]
        return {"opened": True, "path": str(clean)}

    def _campaign(self, campaign_id: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT * FROM benchmark_campaigns WHERE id = ?",
            (campaign_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"campaign not found: {campaign_id}")
        return campaign_row_dict(row)

    def _jobs(self, campaign_id: str) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            """
            SELECT *
            FROM benchmark_campaign_jobs
            WHERE campaign_id = ?
            ORDER BY sequence ASC
            """,
            (campaign_id,),
        ).fetchall()
        return [job_row_dict(row) for row in rows]

    def _runs_for_jobs(self, jobs: list[dict[str, Any]], *, include_detailed_audit: bool) -> list[dict[str, Any]]:
        ordered_ids: list[str] = []
        seen: set[str] = set()
        for job in jobs:
            for run_id in job.get("benchmark_run_ids") or []:
                if run_id and run_id not in seen:
                    seen.add(str(run_id))
                    ordered_ids.append(str(run_id))
        if not ordered_ids:
            return []
        service = EvalService(self.db)
        runs_by_id: dict[str, dict[str, Any]] = {}
        placeholders = ",".join("?" for _ in ordered_ids)
        rows = self.db.conn.execute(
            f"SELECT * FROM benchmark_runs WHERE id IN ({placeholders})",
            ordered_ids,
        ).fetchall()
        for row in rows:
            run = service._run_with_cases(row)
            runs_by_id[str(run["id"])] = sanitize_run_for_report(run, include_detailed_audit)
        return [runs_by_id[run_id] for run_id in ordered_ids if run_id in runs_by_id]

    def _runtime_context(self, campaign: dict[str, Any], jobs: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
        runtime = self.db.conn.execute(
            "SELECT * FROM runtime_status WHERE name = 'ollama'",
        ).fetchone()
        ollama = {}
        if runtime is not None:
            ollama = {
                "reachable": bool(runtime["reachable"]),
                "installed": bool(runtime["installed"]),
                "version": str(runtime["version"] or ""),
                "model_count": len(json_loads(runtime["models_json"], [])),
                "error": str(runtime["error"] or ""),
            }
        return {
            "operating_system": f"{platform.system()} {platform.release()}".strip(),
            "python": platform.python_version(),
            "ollama": ollama,
            "selected_models": sorted({str(job.get("model") or "") for job in jobs if job.get("model")}),
            "model_metadata": model_metadata_for_report(jobs, runs),
            "timeout_policy": json_loads(campaign.get("timeout_policy_json") or "{}", timeout_policy()),
        }

    def _write_screenshots(
        self,
        output_dir: Path,
        screenshots: list[dict[str, Any]],
        warnings: list[str],
        report_data: dict[str, Any],
    ) -> list[dict[str, Any]]:
        manifest: list[dict[str, Any]] = []
        if not screenshots:
            warnings.append("No DOM screenshots were provided; generated local fallback report snapshots.")
            return write_fallback_screenshots(output_dir, report_data, warnings)
        screenshots_dir = output_dir / SCREENSHOTS_DIR_NAME
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        for index, item in enumerate(screenshots, start=1):
            name = safe_filename(str(item.get("name") or f"snapshot-{index}.png"))
            if not name.lower().endswith(".png"):
                name = f"{Path(name).stem}.png"
            data_url = str(item.get("data_url") or "")
            if not DATA_URL_RE.match(data_url):
                warnings.append(f"Screenshot {name} was skipped because it was not a PNG/JPEG data URL.")
                continue
            try:
                encoded = DATA_URL_RE.sub("", data_url)
                raw = base64.b64decode(encoded, validate=True)
                if not raw.startswith(b"\x89PNG\r\n\x1a\n") and not raw.startswith(b"\xff\xd8"):
                    warnings.append(f"Screenshot {name} was skipped because decoded bytes were not a valid PNG/JPEG.")
                    continue
                if len(raw) < 2048:
                    warnings.append(f"Screenshot {name} was skipped because it was too small to be useful.")
                    continue
                path = screenshots_dir / name
                path.write_bytes(raw)
                manifest.append(
                    {
                        "name": name,
                        "label": str(item.get("label") or Path(name).stem.replace("-", " ").title()),
                        "relative_path": f"{SCREENSHOTS_DIR_NAME}/{name}",
                        "bytes": len(raw),
                        "generated_by": "dom",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"Screenshot {name} could not be written: {exc}")
        if not manifest:
            warnings.append("Screenshot capture failed; report generation continued without snapshots.")
            return write_fallback_screenshots(output_dir, report_data, warnings)
        return manifest

    def _store_report_result(self, campaign_id: str, result: dict[str, Any]) -> None:
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET report_status = ?,
                report_paths_json = ?,
                report_warnings_json = ?,
                report_schema_version = ?
            WHERE id = ?
            """,
            (
                str(result.get("status") or "unknown"),
                json.dumps(result.get("paths") or {}),
                json.dumps(result.get("warnings") or []),
                REPORT_SCHEMA_VERSION,
                campaign_id,
            ),
        )
        self.db.conn.commit()


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


def validate_report_artifacts(output_dir: Path, paths: dict[str, str], screenshot_manifest: list[dict[str, Any]]) -> None:
    for name, target in paths.items():
        artifact = output_dir / Path(target).name
        if not artifact.exists():
            raise RuntimeError(f"{name} report file was not created")
        if artifact.stat().st_size <= 0:
            raise RuntimeError(f"{name} report file was empty")
    for item in screenshot_manifest:
        relative = str(item.get("relative_path") or "")
        if relative and not (output_dir / relative).exists():
            raise RuntimeError(f"screenshot listed in manifest was missing: {relative}")


def verify_final_json(json_path: Path, result: dict[str, Any]) -> None:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    generation = data.get("report_generation") or {}
    campaign = data.get("campaign") or {}
    if campaign.get("report_status") != generation.get("status"):
        raise RuntimeError("final JSON report status did not match report_generation status")
    if not data.get("report_schema_version"):
        raise RuntimeError("final JSON report_schema_version was empty")
    files = data.get("report_files") or campaign.get("report_files") or {}
    if not files:
        raise RuntimeError("final JSON report_files was empty")
    for name, path in result.get("paths", {}).items():
        if not Path(str(path)).exists():
            raise RuntimeError(f"final report path listed for {name} did not exist")


def campaign_row_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "preset": row["preset"],
        "app_version": row["app_version"],
        "suite_version": row["suite_version"],
        "status": row["status"],
        "selected_models": json_loads(row["selected_models_json"], []),
        "selected_modes": json_loads(row["selected_modes_json"], []),
        "selected_thinking_modes": json_loads(row["selected_thinking_modes_json"], []),
        "verifier_settings": json_loads(row["verifier_settings_json"], []),
        "repeat_count": int(row["repeat_count"] or 1),
        "embedding_backend": row["embedding_backend"],
        "embedding_model": row["embedding_model"],
        "temperature": float(row["temperature"] or 0),
        "num_predict": int(row["num_predict"] or 0),
        "timeout_policy": json_loads(row["timeout_policy_json"], timeout_policy()),
        "timeout_policy_json": row["timeout_policy_json"],
        "planned_job_count": int(row["planned_job_count"] or 0),
        "completed_job_count": int(row["completed_job_count"] or 0),
        "failed_job_count": int(row["failed_job_count"] or 0),
        "timed_out_job_count": int(row["timed_out_job_count"] or 0),
        "skipped_job_count": int(row["skipped_job_count"] or 0),
        "estimated_runtime_ms": int(row["estimated_runtime_ms"] or 0),
        "estimated_min_runtime_ms": int(row["estimated_min_runtime_ms"] or 0),
        "actual_runtime_ms": int(row["actual_runtime_ms"] or 0),
        "auto_generate_report": bool(row["auto_generate_report"]),
        "report_status": row["report_status"],
        "report_paths": json_loads(row["report_paths_json"], {}),
        "report_warnings": json_loads(row["report_warnings_json"], []),
        "report_schema_version": row["report_schema_version"],
        "output_folder": row["output_folder"],
        "include_detailed_audit": bool(row["include_detailed_audit"]),
        "requested_action": row["requested_action"],
        "notes": row["notes"],
        "created_at": int(row["created_at"] or 0),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def job_row_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "campaign_id": row["campaign_id"],
        "sequence": int(row["sequence"] or 0),
        "model": row["model"],
        "benchmark_mode": row["benchmark_mode"],
        "thinking_mode": row["thinking_mode"],
        "verify": bool(row["verify"]),
        "repeat_count": int(row["repeat_count"] or 1),
        "temperature": float(row["temperature"] or 0),
        "num_predict": int(row["num_predict"] or 0),
        "timeout_policy": json_loads(row["timeout_policy_json"], timeout_policy()),
        "benchmark_run_ids": json_loads(row["benchmark_run_ids_json"], []),
        "status": row["status"],
        "retry_count": int(row["retry_count"] or 0),
        "error": row["error"],
        "estimated_runtime_ms": int(row["estimated_runtime_ms"] or 0),
        "estimated_min_runtime_ms": int(row["estimated_min_runtime_ms"] or 0),
        "model_info": json_loads(row["model_info_json"], {}),
        "created_at": int(row["created_at"] or 0),
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
    }


def public_campaign(campaign: dict[str, Any]) -> dict[str, Any]:
    public = {
        key: value
        for key, value in campaign.items()
        if key not in {"output_folder", "requested_action", "timeout_policy_json", "report_paths"}
    }
    report_paths = campaign.get("report_paths") or {}
    public["report_files"] = {
        str(name): Path(str(path)).name
        for name, path in report_paths.items()
        if path
    }
    return public


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"campaign_id"}
    }


def model_metadata_for_report(jobs: list[dict[str, Any]], runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    source_by_model: dict[str, dict[str, Any]] = {}
    for job in jobs:
        model = str(job.get("model") or "")
        if not model:
            continue
        info = job.get("model_info") if isinstance(job.get("model_info"), dict) else {}
        source_by_model.setdefault(model, {}).update(info)
    for run in runs:
        model = str(run.get("model") or "")
        if not model:
            continue
        info = run.get("model_info") if isinstance(run.get("model_info"), dict) else {}
        source_by_model.setdefault(model, {}).update(info)
    by_model: dict[str, dict[str, Any]] = {}
    for model, info in source_by_model.items():
        observed = info.get("observed_after_run") if isinstance(info.get("observed_after_run"), dict) else {}
        source = {**info, **observed}
        by_model[model] = {
            "model": model,
            "parameter_size": str(source.get("parameter_size") or ""),
            "quantization_level": str(source.get("quantization_level") or ""),
            "size": int(source.get("size") or 0),
            "size_vram": int(source.get("size_vram") or 0),
            "context_length": int(source.get("context_length") or 0),
            "estimated_gpu_loaded_fraction": source.get("estimated_gpu_loaded_fraction"),
            "estimated_cpu_loaded_fraction": source.get("estimated_cpu_loaded_fraction"),
            "partially_cpu_offloaded": bool(source.get("partially_cpu_offloaded")),
            "observed_after_run": bool(observed),
        }
    return list(by_model.values())


def build_report_view_model(data: dict[str, Any]) -> dict[str, Any]:
    campaign = data.get("campaign") or {}
    report_generation = data.get("report_generation") or {}
    pipeline = normalized_pipeline_counts(data.get("pipeline_diagnoses") or {})
    case_totals = aggregate_case_outcomes(data.get("benchmark_runs") or [])
    recommendation = data.get("recommendation") or {}
    screenshot_manifest = data.get("screenshot_manifest") or []
    screenshot_sources = sorted({str(item.get("generated_by") or "dom") for item in screenshot_manifest})
    status = str(report_generation.get("status") or data.get("report_status") or campaign.get("report_status") or "")
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_status": status,
        "job_status": {
            "planned": int(campaign.get("planned_job_count") or 0),
            "completed": int(campaign.get("completed_job_count") or 0),
            "execution_failures": int(campaign.get("failed_job_count") or 0),
            "timed_out": int(campaign.get("timed_out_job_count") or 0),
            "skipped": int(campaign.get("skipped_job_count") or 0),
        },
        "case_outcomes": case_totals,
        "pipeline_taxonomy": {
            "counts": pipeline,
            "labels": pipeline_display_labels(),
        },
        "recommendation": recommendation,
        "deployment_readiness": recommendation.get("deployment_readiness") or "not benchmarked",
        "case_difficulty": data.get("case_difficulty") or {},
        "errors": data.get("timeouts_errors") or {},
        "hardware": data.get("runtime") or {},
        "screenshot_status": {
            "count": len(screenshot_manifest),
            "source": "none" if not screenshot_sources else ",".join(screenshot_sources),
            "warnings": list(report_generation.get("warnings") or []),
        },
    }


def aggregate_case_outcomes(runs: list[dict[str, Any]]) -> dict[str, Any]:
    attempted = 0
    passed = 0
    failed = 0
    grader_review = 0
    timeout = 0
    runtime_error = 0
    for run in runs:
        for case in run.get("cases") or []:
            attempted += 1
            bucket = primary_case_bucket(case)
            if bucket == "passed":
                passed += 1
            elif bucket == "grader_review":
                grader_review += 1
            elif bucket == "timeout":
                timeout += 1
            elif bucket == "runtime_error":
                runtime_error += 1
            else:
                failed += 1
    adjudicated_total = passed + failed
    return {
        "attempted": attempted,
        "passed": passed,
        "failed": failed,
        "grader_review": grader_review,
        "timeout": timeout,
        "runtime_error": runtime_error,
        "adjudicated_total": adjudicated_total,
        "adjudicated_pass_rate": passed / adjudicated_total if adjudicated_total else 0.0,
        "coverage": adjudicated_total / attempted if attempted else 0.0,
    }


def normalized_pipeline_counts(pipeline: dict[str, Any]) -> dict[str, int]:
    counts = {
        "passed": 0,
        "retrieval_only": 0,
        "generation_only": 0,
        "both": 0,
        "grader_review": 0,
        "timeout": 0,
        "runtime_error": 0,
    }
    for key, value in pipeline.items():
        counts[canonical_pipeline_diagnosis(str(key))] += int(value or 0)
    return counts


def pipeline_display_labels() -> dict[str, str]:
    return {
        "passed": "passed",
        "retrieval_only": "retrieval-caused only",
        "generation_only": "generation-caused only",
        "both": "both retrieval and generation",
        "grader_review": "grader review",
        "timeout": "timeout",
        "runtime_error": "runtime error",
    }


def write_fallback_screenshots(
    output_dir: Path,
    report_data: dict[str, Any],
    warnings: list[str],
) -> list[dict[str, Any]]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:  # noqa: BLE001 - fallback snapshots are optional
        warnings.append(f"Fallback screenshot generation failed because Pillow was unavailable: {exc}")
        return []

    screenshots_dir = output_dir / SCREENSHOTS_DIR_NAME
    screenshots_dir.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    try:
        font = ImageFont.truetype("arial.ttf", 18)
        title_font = ImageFont.truetype("arial.ttf", 26)
        meta_font = ImageFont.truetype("arial.ttf", 14)
    except Exception:  # noqa: BLE001
        font = ImageFont.load_default()
        title_font = ImageFont.load_default()
        meta_font = ImageFont.load_default()
    for name, label, lines in fallback_snapshot_specs(report_data):
        path = screenshots_dir / safe_filename(name)
        wrapped_lines: list[str] = []
        for line in lines:
            wrapped = textwrap.wrap(clean_snapshot_text(line), width=88) or [""]
            wrapped_lines.extend(wrapped)
            wrapped_lines.append("")
        height = min(max(180 + len(wrapped_lines) * 28, 280), 980)
        width = 1080
        image = Image.new("RGB", (width, height), "#ffffff")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, width - 1, height - 1), outline="#d9ded8", width=2)
        draw.rectangle((0, 0, width - 1, 92), fill="#edf3f0")
        draw.text((36, 28), label, fill="#18231f", font=title_font)
        draw.text((36, 64), f"Odysseus Desktop report snapshot - schema {REPORT_SCHEMA_VERSION}", fill="#52615b", font=meta_font)
        y = 122
        for wrapped in wrapped_lines:
            if y > height - 48:
                draw.text((48, y), "...", fill="#52615b", font=font)
                break
            draw.text((48, y), wrapped, fill="#18231f", font=font)
            y += 28 if wrapped else 12
        image.save(path, "PNG")
        manifest.append(
            {
                "name": path.name,
                "label": label,
                "relative_path": f"{SCREENSHOTS_DIR_NAME}/{path.name}",
                "bytes": path.stat().st_size,
                "generated_by": "backend-fallback",
            }
        )
    return manifest


def fallback_snapshot_specs(report_data: dict[str, Any]) -> list[tuple[str, str, list[str]]]:
    campaign = report_data.get("campaign") or {}
    comparison = report_data.get("comparison") or {}
    recommendation = report_data.get("recommendation") or {}
    balanced = recommendation.get("balanced") or {}
    groups = comparison.get("groups") or []
    pipeline = report_data.get("pipeline_diagnoses") or {}
    case_difficulty = report_data.get("case_difficulty") or {}
    runtime = report_data.get("runtime") or {}
    embedding = report_data.get("embedding") or {}
    jobs = report_data.get("job_matrix") or []

    return [
        (
            "01-executive-summary.png",
            "Executive Summary",
            [
                f"Campaign: {campaign.get('title') or 'Benchmark campaign'}",
                f"Status: {campaign.get('status') or 'unknown'}",
                f"Jobs completed: {campaign.get('completed_job_count') or 0}/{campaign.get('planned_job_count') or 0}",
                f"Job execution failures: {campaign.get('failed_job_count') or 0}; timed out jobs: {campaign.get('timed_out_job_count') or 0}",
                f"Recommended balanced configuration: {balanced.get('model') or 'none'}",
                str(recommendation.get("reason") or "No eligible recommendation yet."),
            ],
        ),
        (
            "02-model-comparison.png",
            "Model Comparison",
            comparison_snapshot_lines(groups),
        ),
        (
            "03-pipeline-metrics.png",
            "Pipeline Metrics",
            [
                f"Retrieval-caused only: {pipeline.get('retrieval_only') or 0}",
                f"Generation-caused only: {pipeline.get('generation_only') or 0}",
                f"Both retrieval and generation: {pipeline.get('both') or 0}",
                f"Grader review: {pipeline.get('grader_review') or 0}",
                f"Timeouts: {pipeline.get('timeout') or 0}",
                f"Runtime errors: {pipeline.get('runtime_error') or 0}",
            ],
        ),
        (
            "04-case-difficulty.png",
            "Case Difficulty",
            case_difficulty_snapshot_lines(case_difficulty),
        ),
        (
            "05-hardware-summary.png",
            "Hardware Summary",
            [
                f"Operating system: {runtime.get('operating_system') or 'unknown'}",
                f"Embedding: {embedding.get('backend') or 'unknown'}/{embedding.get('model') or 'unknown'}",
                f"Selected models: {', '.join(runtime.get('selected_models') or []) or 'none'}",
                f"Timeout policy: {json.dumps(runtime.get('timeout_policy') or {}, sort_keys=True)}",
            ],
        ),
        (
            "06-per-model-summary.png",
            "Per-Model Summary",
            per_model_snapshot_lines(groups, jobs),
        ),
    ]


def comparison_snapshot_lines(groups: list[dict[str, Any]]) -> list[str]:
    if not groups:
        return ["No comparable completed benchmark runs were available."]
    lines = []
    for group in groups[:10]:
        lines.append(
            (
                f"{group.get('model')} | {group.get('benchmark_mode')} | thinking {group.get('thinking_mode')} | "
                f"latest {group.get('latest_run_passed')}/{group.get('latest_run_total')} | "
                f"mean {format_report_percent(group.get('mean_pass_rate'))} | "
                f"median latency {int(group.get('median_avg_latency_ms') or 0)} ms"
            )
        )
    return lines


def case_difficulty_snapshot_lines(case_difficulty: dict[str, Any]) -> list[str]:
    lines = []
    for key, label in (
        ("usually_pass", "Passing cases"),
        ("usually_fail", "Failing cases"),
        ("frequent_source_failures", "Frequent source failures"),
        ("frequent_forbidden_failures", "Frequent forbidden-claim failures"),
    ):
        items = case_difficulty.get(key) or []
        if items:
            summary = ", ".join(str(item.get("case_id") or "") for item in items[:5])
        else:
            summary = "none"
        lines.append(f"{label}: {summary}")
    return lines


def per_model_snapshot_lines(groups: list[dict[str, Any]], jobs: list[dict[str, Any]]) -> list[str]:
    models = sorted({str(group.get("model") or "") for group in groups if group.get("model")})
    if not models:
        models = sorted({str(job.get("model") or "") for job in jobs if job.get("model")})
    if not models:
        return ["No model summaries were available."]
    lines = []
    for model in models[:10]:
        model_groups = [group for group in groups if group.get("model") == model]
        best = sorted(model_groups, key=lambda group: float(group.get("latest_run_pass_rate") or 0), reverse=True)
        if best:
            group = best[0]
            lines.append(
                f"{model}: best {group.get('latest_run_passed')}/{group.get('latest_run_total')} in {group.get('benchmark_mode')}; "
                f"median latency {int(group.get('median_avg_latency_ms') or 0)} ms"
            )
        else:
            model_jobs = [job for job in jobs if job.get("model") == model]
            statuses = sorted({str(job.get("status") or "planned") for job in model_jobs})
            lines.append(f"{model}: jobs {len(model_jobs)}; statuses {', '.join(statuses) or 'unknown'}")
    return lines


def clean_snapshot_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def format_report_percent(value: Any) -> str:
    try:
        return f"{float(value) * 100:.0f}%"
    except Exception:  # noqa: BLE001
        return "n/a"


def sanitize_run_for_report(run: dict[str, Any], include_detailed_audit: bool) -> dict[str, Any]:
    clean = dict(run)
    clean.pop("summary_markdown", None)
    cases = []
    for case in run.get("cases") or []:
        item = dict(case)
        if not include_detailed_audit:
            for key in (
                "answer_content",
                "thinking_text",
                "prompt_text",
                "corrected_answer",
                "model_response",
                "supplied_evidence",
                "retrieval_candidates",
            ):
                item.pop(key, None)
        item["pipeline_diagnosis"] = primary_case_bucket(item)
        cases.append(item)
    clean["cases"] = cases
    return clean


def pipeline_diagnoses(runs: list[dict[str, Any]]) -> dict[str, int]:
    counts = normalized_pipeline_counts({})
    for run in runs:
        for case in run.get("cases") or []:
            counts[primary_case_bucket(case)] += 1
    return counts


def collect_timeouts_and_errors(jobs: list[dict[str, Any]], runs: list[dict[str, Any]]) -> dict[str, Any]:
    case_errors: list[dict[str, Any]] = []
    for run in runs:
        for case in run.get("cases") or []:
            if case.get("status") in {"timeout", "error"}:
                case_errors.append(
                    {
                        "run_id": run.get("id"),
                        "case_id": case.get("case_id"),
                        "status": case.get("status"),
                        "stage": case.get("stage"),
                        "error": truncate(str(case.get("error_message") or ""), 280),
                    }
                )
    return {
        "jobs": [
            {
                "id": job["id"],
                "sequence": job["sequence"],
                "model": job["model"],
                "status": job["status"],
                "error": truncate(str(job.get("error") or ""), 280),
            }
            for job in jobs
            if job.get("status") not in {"queued", "completed"}
        ],
        "cases": case_errors,
        "benchmark_case_failures": [
            {
                "run_id": run.get("id"),
                "case_id": case.get("case_id"),
                "diagnosis": primary_case_bucket(case),
                "reasons": case.get("reasons") or [],
            }
            for run in runs
            for case in run.get("cases") or []
            if primary_case_bucket(case) in {"retrieval_only", "generation_only", "both"}
        ],
        "grader_review_cases": [
            {
                "run_id": run.get("id"),
                "case_id": case.get("case_id"),
                "diagnosis": "grader_review",
                "reasons": case.get("reasons") or [],
            }
            for run in runs
            for case in run.get("cases") or []
            if primary_case_bucket(case) == "grader_review"
        ],
    }


def report_recommendations(comparison: dict[str, Any]) -> dict[str, Any]:
    groups = list(comparison.get("groups") or [])
    eligible = [group for group in groups if group.get("recommendation_eligible")]
    recommended = comparison.get("recommended")
    quality_sorted = sorted(
        eligible,
        key=lambda item: (
            -float(item.get("mean_practical_pass_rate") or item.get("mean_pass_rate") or 0),
            -float(item.get("worst_run_practical_pass_rate") or 0),
            float(item.get("timeout_rate") or 0),
        ),
    )
    best_quality = quality_recommendation(quality_sorted)
    best_speed = sorted(
        eligible,
        key=lambda item: (
            int(item.get("median_avg_latency_ms") or 0),
            -float(item.get("mean_practical_pass_rate") or item.get("mean_pass_rate") or 0),
        ),
    )[:1]
    weak_hardware = sorted(
        eligible,
        key=lambda item: (
            int(item.get("median_avg_latency_ms") or 0),
            bool(item.get("verify")),
            str(item.get("thinking_mode") or "") != "off",
            -float(item.get("mean_practical_pass_rate") or item.get("mean_pass_rate") or 0),
        ),
    )[:1]
    return {
        "balanced": recommended,
        "best_quality": best_quality,
        "best_speed": best_speed[0] if best_speed else None,
        "weak_hardware": weak_hardware[0] if weak_hardware else None,
        "reason": comparison.get("recommendation_reason") or "",
        "reliability": reliability_label(recommended),
        "deployment_readiness": deployment_readiness(recommended),
        "deployment_ready": deployment_readiness(recommended) == "yes",
    }


def quality_recommendation(sorted_groups: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not sorted_groups:
        return None
    top = sorted_groups[0]
    tied = [
        group
        for group in sorted_groups
        if quality_signature(group) == quality_signature(top)
    ]
    if len(tied) > 1:
        return {
            "tie": True,
            "label": "Tie / inconclusive",
            "models": [str(group.get("model") or "") for group in tied],
            "reason": "Quality metrics are equal; latency is not used to declare best quality.",
        }
    return top


def quality_signature(group: dict[str, Any]) -> tuple[float, float, float]:
    return (
        round(float(group.get("mean_practical_pass_rate") or group.get("mean_pass_rate") or 0), 6),
        round(float(group.get("worst_run_practical_pass_rate") or 0), 6),
        round(float(group.get("timeout_rate") or 0), 6),
    )


def deployment_readiness(group: dict[str, Any] | None) -> str:
    if not group:
        return "no"
    if not group.get("recommendation_eligible"):
        return "no"
    if int(group.get("run_count") or 0) < 3:
        return "no"
    practical = float(group.get("mean_practical_pass_rate") or group.get("mean_pass_rate") or 0.0)
    coverage = float(group.get("mean_coverage") or group.get("latest_run_coverage") or 0.0)
    if practical >= 0.85 and coverage >= 0.95 and float(group.get("timeout_rate") or 0.0) == 0.0:
        return "yes"
    return "no"


def reliability_label(group: dict[str, Any] | None) -> str:
    if not group:
        return "not benchmarked"
    if not group.get("recommendation_eligible"):
        return "not recommendation-eligible"
    if int(group.get("run_count") or 0) < 3:
        return "provisional"
    if float(group.get("mean_coverage") or group.get("latest_run_coverage") or 0.0) < 0.95:
        return "provisional"
    return "benchmarked"


def render_html_report(data: dict[str, Any], output_dir: Path) -> str:
    campaign = data["campaign"]
    view = data.get("view_model") or build_report_view_model(data)
    jobs = view.get("job_status") or {}
    cases = view.get("case_outcomes") or {}
    recommendations = data.get("recommendation") or {}
    groups = (data.get("comparison") or {}).get("groups") or []
    screenshots = screenshot_images_for_html(data.get("screenshot_manifest") or [], output_dir)
    rows = "\n".join(
        "<tr>"
        f"<td>{esc(group.get('model'))}</td>"
        f"<td>{esc(group.get('benchmark_mode'))}</td>"
        f"<td>{esc(group.get('thinking_mode'))}</td>"
        f"<td>{'on' if group.get('verify') else 'off'}</td>"
        f"<td>{int(group.get('latest_run_passed') or 0)}/{int(group.get('latest_run_total') or 0)}</td>"
        f"<td>{percent(group.get('mean_pass_rate'))}</td>"
        f"<td>{percent(group.get('timeout_rate'))}</td>"
        f"<td>{int(group.get('median_avg_latency_ms') or 0)} ms</td>"
        f"<td>{'yes' if group.get('recommendation_eligible') else 'no'}</td>"
        "</tr>"
        for group in groups
    )
    image_blocks = "\n".join(
        f"<figure><img alt=\"{esc(item['label'])}\" src=\"{item['src']}\" />"
        f"<figcaption>{esc(item['label'])}</figcaption></figure>"
        for item in screenshots
    )
    case_details = render_case_details(data)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Odysseus Desktop Benchmark Report</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 32px; color: #17211f; line-height: 1.45; }}
h1, h2, h3 {{ margin: 0.8em 0 0.35em; }}
table {{ border-collapse: collapse; width: 100%; margin: 12px 0 24px; font-size: 13px; }}
th, td {{ border: 1px solid #ccd6d2; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #edf3f0; }}
.muted {{ color: #5f6f6a; }}
.grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }}
.metric {{ border: 1px solid #d8dfdc; padding: 10px; border-radius: 6px; }}
img {{ max-width: 100%; height: auto; border: 1px solid #d8dfdc; }}
figure {{ margin: 18px 0; page-break-inside: avoid; }}
details {{ border: 1px solid #d8dfdc; padding: 8px 10px; margin: 8px 0; }}
@media print {{ body {{ margin: 18mm; }} details {{ break-inside: avoid; }} }}
</style>
</head>
<body>
<h1>Odysseus Desktop Benchmark Report</h1>
<p class="muted">{esc(campaign.get('title'))} - generated {esc(format_ms(data['report_generation'].get('generated_at')))}</p>
<p>Local-only report. No benchmark data, screenshots, or telemetry were uploaded.</p>
<h2>Executive Summary</h2>
<div class="grid">
<div class="metric"><strong>Status</strong><br>{esc(campaign.get('status'))}</div>
<div class="metric"><strong>Jobs completed</strong><br>{jobs.get('completed')} / {jobs.get('planned')}</div>
<div class="metric"><strong>Job execution failures</strong><br>{jobs.get('execution_failures')}</div>
<div class="metric"><strong>Benchmark cases</strong><br>{cases.get('passed')} passed, {cases.get('failed')} failed, {cases.get('grader_review')} review</div>
<div class="metric"><strong>Recommendation</strong><br>{esc((recommendations.get('balanced') or {}).get('model') or 'none')}</div>
</div>
<p>{esc(recommendations.get('reason') or 'No completed eligible current-suite end-to-end runs are available.')}</p>
<h2>Machine And Runtime Context</h2>
<p>{esc((data.get('runtime') or {}).get('operating_system'))}; Python {esc((data.get('runtime') or {}).get('python'))}; Ollama {esc(((data.get('runtime') or {}).get('ollama') or {}).get('version'))}; app {esc((data.get('application') or {}).get('version'))}; suite {esc((data.get('eval_suite') or {}).get('version'))}.</p>
{render_model_metadata_html((data.get('runtime') or {}).get('model_metadata') or [])}
<h2>Campaign Configuration</h2>
<p>Preset: {esc(campaign.get('preset'))}. Models: {esc(', '.join(campaign.get('selected_models') or []))}. Modes: {esc(', '.join(campaign.get('selected_modes') or []))}.</p>
<h2>Model Comparison</h2>
<table>
<thead><tr><th>Model</th><th>Mode</th><th>Think</th><th>Verif.</th><th>Latest</th><th>Mean</th><th>TO</th><th>Median latency</th><th>Use</th></tr></thead>
<tbody>{rows or '<tr><td colspan="9">No compatible completed runs.</td></tr>'}</tbody>
</table>
<h2>Recommended Configuration</h2>
{render_recommendation_html(recommendations)}
<h2>Retrieval, Generation, And End-To-End Analysis</h2>
{render_score_blocks(data)}
<h2>Case Difficulty</h2>
{render_case_difficulty_html(data.get('case_difficulty') or {})}
<h2>Per-Model Summaries</h2>
{render_per_model_html(groups)}
<h2>Errors And Incomplete Work</h2>
{render_errors_html(data.get('timeouts_errors') or {})}
<h2>Visual Appendix</h2>
{image_blocks or '<p class="muted">No screenshots were captured for this report.</p>'}
<h2>Reproducibility Appendix</h2>
<p>Campaign ID: {esc(campaign.get('id'))}. Prompt version: {esc((data.get('eval_suite') or {}).get('prompt_version'))}. Report schema: {esc(data.get('report_schema_version'))}.</p>
{case_details}
</body>
</html>
"""


def render_pdf_report(data: dict[str, Any], pdf_path: Path, output_dir: Path) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            Image,
            PageBreak,
            Paragraph,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:
        raise RuntimeError("ReportLab is not installed in the Python runtime") from exc

    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=letter,
        rightMargin=0.45 * inch,
        leftMargin=0.45 * inch,
        topMargin=0.55 * inch,
        bottomMargin=0.55 * inch,
    )
    story: list[Any] = []
    campaign = data["campaign"]
    view = data.get("view_model") or build_report_view_model(data)
    job_status = view.get("job_status") or {}
    case_outcomes = view.get("case_outcomes") or {}
    story.append(Paragraph("Odysseus Desktop Benchmark Report", styles["Title"]))
    story.append(Paragraph(str(campaign.get("title") or ""), styles["Heading2"]))
    story.append(Paragraph(f"Generated: {format_ms(data['report_generation'].get('generated_at'))}", styles["Normal"]))
    story.append(Paragraph(f"App version: {data['application']['version']}", styles["Normal"]))
    story.append(Paragraph(f"Eval suite: {data['eval_suite']['version']}", styles["Normal"]))
    story.append(Paragraph(f"Campaign ID: {campaign.get('id')}", styles["Normal"]))
    story.append(Paragraph(data["application"]["privacy_statement"], styles["Normal"]))

    add_heading(story, styles, "Executive Summary")
    recommendation = (data.get("recommendation") or {}).get("balanced") or {}
    summary_rows = [
        ["Status", str(campaign.get("status") or "")],
        ["Jobs completed", f"{job_status.get('completed')} / {job_status.get('planned')}"],
        ["Job execution failures", str(job_status.get("execution_failures") or 0)],
        ["Benchmark cases passed", str(case_outcomes.get("passed") or 0)],
        ["Benchmark cases failed", str(case_outcomes.get("failed") or 0)],
        ["Grader-review cases", str(case_outcomes.get("grader_review") or 0)],
        ["Timeouts", str(case_outcomes.get("timeout") or 0)],
        ["Runtime errors", str(case_outcomes.get("runtime_error") or 0)],
        ["Recommended", str(recommendation.get("model") or "none")],
        ["Reliability", str((data.get("recommendation") or {}).get("reliability") or "provisional")],
        ["Deployment-ready", str((data.get("recommendation") or {}).get("deployment_readiness") or "no")],
    ]
    story.append(pdf_table(summary_rows, [1.7 * inch, 4.8 * inch], colors))
    story.append(Paragraph(str((data.get("recommendation") or {}).get("reason") or ""), styles["BodyText"]))

    add_heading(story, styles, "Machine And Runtime Context")
    runtime = data.get("runtime") or {}
    story.append(Paragraph(f"Operating system: {runtime.get('operating_system', '')}", styles["BodyText"]))
    story.append(Paragraph(f"Python version: {runtime.get('python', '')}", styles["BodyText"]))
    story.append(Paragraph(f"Ollama version: {(runtime.get('ollama') or {}).get('version', '')}", styles["BodyText"]))
    story.append(Paragraph(f"Embedding: {data['embedding'].get('backend')}/{data['embedding'].get('model')}", styles["BodyText"]))
    metadata_rows = model_metadata_rows(runtime.get("model_metadata") or [])
    if len(metadata_rows) > 1:
        story.append(pdf_table(metadata_rows, [1.5 * inch, 0.8 * inch, 0.9 * inch, 0.7 * inch, 0.7 * inch, 0.9 * inch], colors))

    add_heading(story, styles, "Campaign Configuration")
    story.append(Paragraph(f"Preset: {campaign.get('preset')}", styles["BodyText"]))
    story.append(Paragraph(f"Models: {', '.join(campaign.get('selected_models') or [])}", styles["BodyText"]))
    story.append(Paragraph(f"Modes: {', '.join(campaign.get('selected_modes') or [])}", styles["BodyText"]))

    add_heading(story, styles, "Model Comparison")
    comparison_rows = [["Model", "Mode", "Think", "Verif.", "Latest", "Mean", "TO", "Latency", "Use"]]
    for group in (data.get("comparison") or {}).get("groups") or []:
        comparison_rows.append(
            [
                str(group.get("model") or ""),
                str(group.get("benchmark_mode") or ""),
                str(group.get("thinking_mode") or ""),
                "on" if group.get("verify") else "off",
                f"{int(group.get('latest_run_passed') or 0)}/{int(group.get('latest_run_total') or 0)}",
                percent(group.get("mean_pass_rate")),
                percent(group.get("timeout_rate")),
                f"{int(group.get('median_avg_latency_ms') or 0)} ms",
                "yes" if group.get("recommendation_eligible") else "no",
            ]
        )
    if len(comparison_rows) == 1:
        comparison_rows.append(["No compatible completed runs.", "", "", "", "", "", "", "", ""])
    story.append(pdf_table(comparison_rows, [1.25 * inch, 0.85 * inch, 0.75 * inch, 0.65 * inch, 0.55 * inch, 0.55 * inch, 0.55 * inch, 0.75 * inch, 0.55 * inch], colors))

    add_heading(story, styles, "Recommended Configuration")
    for key in ("balanced", "best_quality", "best_speed", "weak_hardware"):
        group = (data.get("recommendation") or {}).get(key)
        if group:
            if isinstance(group, dict) and group.get("tie"):
                story.append(
                    Paragraph(
                        f"{key.replace('_', ' ').title()}: {group.get('label')} "
                        f"({', '.join(group.get('models') or [])}). {group.get('reason')}",
                        styles["BodyText"],
                    )
                )
                continue
            story.append(
                Paragraph(
                    f"{key.replace('_', ' ').title()}: {group.get('model')} "
                    f"({group.get('benchmark_mode')}, thinking {group.get('thinking_mode')}, "
                    f"verifier {'on' if group.get('verify') else 'off'})",
                    styles["BodyText"],
                )
            )
    story.append(Paragraph(f"Deployment readiness: {(data.get('recommendation') or {}).get('deployment_readiness', '')}", styles["BodyText"]))

    add_heading(story, styles, "Retrieval, Generation, And End-To-End Analysis")
    story.append(pdf_table(score_rows(data), [2.4 * inch, 1.2 * inch, 1.2 * inch], colors))
    add_heading(story, styles, "Case Difficulty")
    story.append(pdf_table(case_difficulty_rows(data.get("case_difficulty") or {}, compact=True), [1.45 * inch, 2.6 * inch, 2.45 * inch], colors))

    story.append(PageBreak())
    add_heading(story, styles, "Errors And Incomplete Work")
    story.append(pdf_table(error_rows(data.get("timeouts_errors") or {}), [1.25 * inch, 2.1 * inch, 3.15 * inch], colors))

    add_heading(story, styles, "Visual Appendix")
    screenshots = data.get("screenshot_manifest") or []
    if not screenshots:
        story.append(Paragraph("No screenshots were captured for this report.", styles["BodyText"]))
    visual_cells = []
    for shot in screenshots[:4]:
        image_path = output_dir / str(shot.get("relative_path") or "")
        if image_path.exists():
            visual_cells.append(
                [
                    Paragraph(str(shot.get("label") or shot.get("name") or "Snapshot"), styles["Heading4"]),
                    Image(str(image_path), width=3.25 * inch, height=2.05 * inch, kind="proportional"),
                ]
            )
    if visual_cells:
        rows = []
        for index in range(0, len(visual_cells), 2):
            left = visual_cells[index]
            right = visual_cells[index + 1] if index + 1 < len(visual_cells) else ["", ""]
            rows.append([left[0], right[0]])
            rows.append([left[1], right[1]])
        sheet = Table(rows, colWidths=[3.35 * inch, 3.35 * inch])
        sheet.setStyle(
            TableStyle(
                [
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]
            )
        )
        story.append(sheet)

    add_heading(story, styles, "Reproducibility Appendix")
    story.append(Paragraph(f"Report schema version: {data.get('report_schema_version')}", styles["BodyText"]))
    story.append(Paragraph(f"Prompt version: {data['eval_suite'].get('prompt_version')}", styles["BodyText"]))
    story.append(Paragraph("Benchmark run IDs: " + ", ".join(str(run.get("id")) for run in data.get("benchmark_runs") or []), styles["BodyText"]))

    def on_page(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.drawString(0.45 * inch, 0.3 * inch, "Odysseus Desktop Benchmark Report")
        canvas.drawRightString(letter[0] - 0.45 * inch, 0.3 * inch, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=on_page, onLaterPages=on_page)


def validate_pdf(path: Path) -> None:
    if not path.exists():
        raise RuntimeError("PDF file was not created")
    if path.stat().st_size == 0:
        raise RuntimeError("PDF file is empty")
    if path.read_bytes()[:5] != b"%PDF-":
        raise RuntimeError("PDF header is invalid")
    reader = PdfReader(str(path))
    if len(reader.pages) <= 0:
        raise RuntimeError("PDF has no pages")


def add_heading(story: list[Any], styles: Any, text: str) -> None:
    from reportlab.platypus import Paragraph, Spacer

    story.append(Spacer(1, 8))
    story.append(Paragraph(text, styles["Heading2"]))


def pdf_table(rows: list[list[Any]], widths: list[float], colors_module: Any) -> Any:
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.platypus import Paragraph, Table, TableStyle

    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "OdysseusTableBody",
        parent=styles["BodyText"],
        fontSize=8,
        leading=10,
        wordWrap="LTR",
        splitLongWords=False,
    )
    header_style = ParagraphStyle(
        "OdysseusTableHeader",
        parent=body_style,
        fontName="Helvetica-Bold",
    )
    wrapped = [
        [Paragraph(pdf_cell_text(cell), header_style if row_index == 0 else body_style) for cell in row]
        for row_index, row in enumerate(rows)
    ]
    table = Table(wrapped, colWidths=widths, repeatRows=1 if len(rows) > 1 else 0)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors_module.HexColor("#edf3f0")),
                ("GRID", (0, 0), (-1, -1), 0.35, colors_module.HexColor("#ccd6d2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def pdf_cell_text(value: Any) -> str:
    text = html.escape(str(value))
    return text.replace("_", '_<font size="0"> </font>')


def score_rows(data: dict[str, Any]) -> list[list[str]]:
    rows = [["Metric", "Value", "Notes"]]
    for group_name in ("retrieval", "oracle_generation", "end_to_end", "practical", "adversarial"):
        aggregate = aggregate_scores(data.get("benchmark_runs") or [], group_name)
        if aggregate.get("total"):
            rows.append([
                group_name,
                percent(aggregate.get("adjudicated_pass_rate")),
                (
                    f"{aggregate.get('passed', 0)}/{aggregate.get('adjudicated_total', 0)} adjudicated; "
                    f"coverage {percent(aggregate.get('coverage'))}"
                ),
            ])
        else:
            rows.append([group_name, "N/A", "Not run"])
    pipeline = data.get("pipeline_diagnoses") or {}
    labels = pipeline_display_labels()
    for key in ("retrieval_only", "generation_only", "both", "grader_review", "timeout", "runtime_error"):
        rows.append([labels[key], str(pipeline.get(key, 0)), "primary case bucket"])
    return rows


def aggregate_scores(runs: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    cases = []
    for run in runs:
        for case in run.get("cases") or []:
            if mode in {"practical", "adversarial"}:
                if mode == "practical" and case.get("counts_toward_primary") and case.get("case_category") != "negation_adversarial":
                    cases.append(case)
                elif mode == "adversarial" and case.get("case_category") == "negation_adversarial":
                    cases.append(case)
            elif case.get("benchmark_mode") == mode:
                cases.append(case)
    total = len(cases)
    passed = sum(1 for case in cases if case.get("passed"))
    failed = sum(1 for case in cases if primary_case_bucket(case) in {"retrieval_only", "generation_only", "both"})
    grader_review = sum(1 for case in cases if primary_case_bucket(case) == "grader_review")
    timeout = sum(1 for case in cases if primary_case_bucket(case) == "timeout")
    runtime_error = sum(1 for case in cases if primary_case_bucket(case) == "runtime_error")
    adjudicated = passed + failed
    return {
        "total": total,
        "attempted": total,
        "passed": passed,
        "failed": failed,
        "grader_review": grader_review,
        "timeout": timeout,
        "runtime_error": runtime_error,
        "adjudicated_total": adjudicated,
        "adjudicated_pass_rate": passed / adjudicated if adjudicated else 0.0,
        "coverage": adjudicated / total if total else 0.0,
        "pass_rate": passed / adjudicated if adjudicated else 0.0,
    }


def case_difficulty_rows(summary: dict[str, Any], *, compact: bool = False) -> list[list[str]]:
    rows = [["Bucket", "Case", "Result"]]
    for bucket in ("usually_pass", "usually_fail", "frequent_source_failures", "frequent_forbidden_failures"):
        for item in (summary.get(bucket) or [])[:6]:
            if bucket == "frequent_source_failures":
                result = f"{item.get('source_failures', 0)} source failure(s), {percent(item.get('source_failure_rate'))}"
            elif bucket == "frequent_forbidden_failures":
                result = f"{item.get('forbidden_failures', 0)} forbidden failure(s), {percent(item.get('forbidden_failure_rate'))}"
            elif compact:
                result = compact_case_observation_label(item)
            else:
                result = str(item.get("observation_label") or percent(item.get("pass_rate")))
            rows.append([case_bucket_label(bucket, item), str(item.get("case_id") or ""), result])
    if len(rows) == 1:
        rows.append(["none", "No case difficulty data.", ""])
    return rows


def compact_case_observation_label(item: dict[str, Any]) -> str:
    attempts = int(item.get("attempts") or 0)
    passes = int(item.get("passes") or 0)
    label = "provisional" if attempts < 3 else "benchmarked"
    noun = "observation" if attempts == 1 else "observations"
    return f"{passes}/{attempts} passed \u00b7 {label} ({attempts} {noun})"


def case_bucket_label(bucket: str, item: dict[str, Any]) -> str:
    attempts = int(item.get("attempts") or 0)
    if bucket == "usually_pass":
        return "Passed in all tested configurations" if attempts < 3 else "Usually passing"
    if bucket == "usually_fail":
        return "Failed in all tested configurations" if attempts < 3 else "Usually failing"
    if bucket == "frequent_source_failures":
        return "Source failures"
    if bucket == "frequent_forbidden_failures":
        return "Forbidden-claim failures"
    return bucket


def error_rows(errors: dict[str, Any]) -> list[list[str]]:
    rows = [["Type", "Item", "Detail"]]
    for job in errors.get("jobs") or []:
        rows.append(["Job execution error", f"{job.get('sequence')} {job.get('model')}", job.get("error") or job.get("status") or ""])
    for case in errors.get("cases") or []:
        rows.append(["Timeout/runtime case", str(case.get("case_id") or ""), case.get("error") or case.get("status") or ""])
    for case in errors.get("benchmark_case_failures") or []:
        rows.append(["Benchmark case failed", str(case.get("case_id") or ""), f"{case.get('diagnosis')}: {truncate('; '.join(str(item) for item in case.get('reasons') or []), 180)}"])
    for case in errors.get("grader_review_cases") or []:
        rows.append(["Grader review needed", str(case.get("case_id") or ""), truncate("; ".join(str(item) for item in case.get("reasons") or []), 180)])
    if len(rows) == 1:
        rows.append(["none", "No job execution errors, benchmark assertion failures, grader-review cases, or timeouts.", ""])
    return rows


def render_recommendation_html(recommendations: dict[str, Any]) -> str:
    blocks = []
    for key in ("balanced", "best_quality", "best_speed", "weak_hardware"):
        group = recommendations.get(key)
        if not group:
            continue
        if isinstance(group, dict) and group.get("tie"):
            blocks.append(
                f"<p><strong>{esc(key.replace('_', ' ').title())}</strong>: "
                f"{esc(group.get('label'))} ({esc(', '.join(group.get('models') or []))}). "
                f"{esc(group.get('reason'))}</p>"
            )
            continue
        blocks.append(
            f"<p><strong>{esc(key.replace('_', ' ').title())}</strong>: "
            f"{esc(group.get('model'))} / {esc(group.get('benchmark_mode'))}, "
            f"thinking {esc(group.get('thinking_mode'))}, verifier {'on' if group.get('verify') else 'off'}.</p>"
        )
    if recommendations.get("deployment_readiness"):
        blocks.append(f"<p><strong>Reliability</strong>: {esc(recommendations.get('reliability') or 'provisional')}.</p>")
        blocks.append(f"<p><strong>Deployment-ready</strong>: {esc(recommendations.get('deployment_readiness'))}.</p>")
    return "\n".join(blocks) or "<p>No eligible recommendation.</p>"


def render_score_blocks(data: dict[str, Any]) -> str:
    rows = "\n".join(
        f"<tr><td>{esc(row[0])}</td><td>{esc(row[1])}</td><td>{esc(row[2])}</td></tr>"
        for row in score_rows(data)[1:]
    )
    return f"<table><thead><tr><th>Metric</th><th>Value</th><th>Notes</th></tr></thead><tbody>{rows}</tbody></table>"


def render_case_difficulty_html(summary: dict[str, Any]) -> str:
    rows = "\n".join(
        f"<tr><td>{esc(row[0])}</td><td>{esc(row[1])}</td><td>{esc(row[2])}</td></tr>"
        for row in case_difficulty_rows(summary)[1:]
    )
    return f"<table><thead><tr><th>Bucket</th><th>Case</th><th>Pass rate</th></tr></thead><tbody>{rows}</tbody></table>"


def render_per_model_html(groups: list[dict[str, Any]]) -> str:
    by_model: dict[str, list[dict[str, Any]]] = {}
    for group in groups:
        by_model.setdefault(str(group.get("model") or "unknown"), []).append(group)
    if not by_model:
        return "<p>No per-model summaries available.</p>"
    blocks = []
    for model, model_groups in sorted(by_model.items()):
        best = sorted(model_groups, key=lambda item: -float(item.get("latest_run_pass_rate") or 0))[0]
        blocks.append(
            f"<section><h3>{esc(model)}</h3><p>Configurations tested: {len(model_groups)}. "
            f"Best latest score: {int(best.get('latest_run_passed') or 0)}/{int(best.get('latest_run_total') or 0)}. "
            f"Median latency: {int(best.get('median_avg_latency_ms') or 0)} ms.</p></section>"
        )
    return "\n".join(blocks)


def render_model_metadata_html(items: list[dict[str, Any]]) -> str:
    if not items:
        return "<p class=\"muted\">No loaded-model hardware metadata was available.</p>"
    rows = "\n".join(
        "<tr>"
        f"<td>{esc(item.get('model'))}</td>"
        f"<td>{esc(item.get('parameter_size'))}</td>"
        f"<td>{esc(item.get('quantization_level'))}</td>"
        f"<td>{format_bytes(item.get('size'))}</td>"
        f"<td>{esc(item.get('context_length'))}</td>"
        f"<td>{offload_label(item)}</td>"
        "</tr>"
        for item in items
    )
    return (
        "<table><thead><tr><th>Model</th><th>Params</th><th>Quant</th><th>Size</th>"
        "<th>Context</th><th>Observed offload</th></tr></thead><tbody>"
        f"{rows}</tbody></table>"
    )


def model_metadata_rows(items: list[dict[str, Any]]) -> list[list[str]]:
    rows = [["Model", "Params", "Quant", "Size", "Context", "Offload"]]
    for item in items:
        rows.append(
            [
                str(item.get("model") or ""),
                str(item.get("parameter_size") or ""),
                str(item.get("quantization_level") or ""),
                format_bytes(item.get("size")),
                str(item.get("context_length") or ""),
                offload_label(item),
            ]
        )
    return rows


def offload_label(item: dict[str, Any]) -> str:
    gpu = item.get("estimated_gpu_loaded_fraction")
    cpu = item.get("estimated_cpu_loaded_fraction")
    if item.get("partially_cpu_offloaded"):
        return f"partial CPU/GPU ({percent(gpu)} GPU, {percent(cpu)} CPU)"
    if gpu is not None:
        return f"{percent(gpu)} GPU"
    if cpu is not None:
        return f"{percent(cpu)} CPU"
    return "unknown"


def format_bytes(value: Any) -> str:
    try:
        size = int(value or 0)
    except Exception:  # noqa: BLE001
        return "unknown"
    if size <= 0:
        return "unknown"
    if size >= 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024 * 1024):.1f} GB"
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    return f"{size} B"


def render_errors_html(errors: dict[str, Any]) -> str:
    rows = "\n".join(
        f"<tr><td>{esc(row[0])}</td><td>{esc(row[1])}</td><td>{esc(row[2])}</td></tr>"
        for row in error_rows(errors)[1:]
    )
    return f"<table><thead><tr><th>Type</th><th>Item</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table>"


def render_case_details(data: dict[str, Any]) -> str:
    if not data.get("benchmark_runs"):
        return ""
    details = []
    for run in data.get("benchmark_runs") or []:
        details.append(f"<details><summary>{esc(run.get('model'))} - {esc(run.get('id'))}</summary>")
        details.append("<ul>")
        for case in run.get("cases") or []:
            details.append(
                f"<li>{esc(case.get('case_id'))}: {esc(case.get('status'))}, "
                f"passed={bool(case.get('passed'))}, diagnosis={esc(primary_case_bucket(case))}</li>"
            )
        details.append("</ul></details>")
    return "\n".join(details)


def screenshot_images_for_html(manifest: list[dict[str, Any]], output_dir: Path) -> list[dict[str, str]]:
    images = []
    for item in manifest:
        path = output_dir / str(item.get("relative_path") or "")
        if not path.exists():
            continue
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        images.append(
            {
                "label": str(item.get("label") or item.get("name") or "Snapshot"),
                "src": f"data:image/png;base64,{encoded}",
            }
        )
    return images


def unique_report_dir(output_folder: str | None, title: str) -> Path:
    if output_folder:
        root = Path(output_folder).expanduser()
    else:
        root = Path.home() / "Downloads" / DEFAULT_REPORT_DIR_NAME
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = root / f"{safe_filename(title) or 'benchmark-campaign'}-{stamp}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = Path(f"{base}-{suffix}")
        suffix += 1
    return candidate


def safe_filename(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return clean[:90] or "report"


def format_ms(value: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(value or 0) / 1000))
    except Exception:
        return ""


def percent(value: Any) -> str:
    try:
        return f"{float(value or 0) * 100:.0f}%"
    except Exception:
        return "0%"


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def truncate(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."
