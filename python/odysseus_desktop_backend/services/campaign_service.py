from __future__ import annotations

import json
import statistics
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from odysseus_desktop_backend import __version__
from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.embedding_service import DEFAULT_EMBEDDING_MODEL
from odysseus_desktop_backend.services.eval_service import (
    EVAL_SUITE_VERSION,
    EvalService,
    benchmark_modes_for_case,
    default_cases_dir,
    load_cases,
    normalize_benchmark_mode,
    timeout_policy,
)
from odysseus_desktop_backend.services.model_service import ModelService, normalize_thinking_mode
from odysseus_desktop_backend.services.report_service import (
    REPORT_AWAITING_CAPTURE,
    REPORT_CAPTURING,
    REPORT_ERROR,
    REPORT_GENERATING,
    ReportService,
    campaign_row_dict,
    job_row_dict,
)
from odysseus_desktop_backend.storage import Database, utc_ms


CAMPAIGN_STATUSES = {
    "draft",
    "queued",
    "running",
    "paused",
    "completed",
    "completed_with_errors",
    "cancelled",
    "interrupted",
    "error",
}
JOB_TERMINAL_STATUSES = {"completed", "failed", "timed_out", "skipped", "cancelled"}
MODE_ORDER = ["retrieval_only", "oracle_generation", "end_to_end"]
THINKING_ORDER = ["off", "auto", "on"]
VERIFIER_ORDER = [False, True]
QUICK_NUM_PREDICT = 192
UNKNOWN_MODEL_JOB_MS = 180_000
RETRIEVAL_FALLBACK_JOB_MS = 45_000
ORACLE_FALLBACK_CASE_MS = 18_000
END_TO_END_FALLBACK_CASE_MS = 24_000
logger = get_logger("campaigns")


class CampaignService:
    def __init__(self, db: Database):
        self.db = db
        self.profile_dir = db.profile_dir
        self._workers: dict[str, threading.Thread] = {}
        self._worker_lock = threading.Lock()

    def installed_models(self) -> list[dict[str, Any]]:
        models = ModelService(self.db)
        detected = models.detect_ollama()
        loaded = models.ps()
        loaded_by_name = {
            str(item.get("name") or item.get("model") or ""): item
            for item in loaded.get("models", [])
            if isinstance(item, dict)
        }
        details_by_name = {
            str(item.get("name") or ""): item
            for item in detected.get("model_details", [])
            if isinstance(item, dict)
        }
        results = []
        for name in detected.get("models", []):
            model_name = str(name)
            details = details_by_name.get(model_name, {})
            ps = loaded_by_name.get(model_name, {})
            merged = {**details, **ps}
            capability = classify_model_capability(model_name, merged)
            results.append(
                {
                    "name": model_name,
                    "capability": capability,
                    "auto_select_chat": capability == "chat",
                    "parameter_size": str(merged.get("parameter_size") or ""),
                    "quantization_level": str(merged.get("quantization_level") or ""),
                    "size": int(merged.get("size") or details.get("size") or 0),
                    "size_vram": int(merged.get("size_vram") or 0),
                    "context_length": int(merged.get("context_length") or 0),
                    "estimated_gpu_loaded_fraction": merged.get("estimated_gpu_loaded_fraction"),
                    "estimated_cpu_loaded_fraction": merged.get("estimated_cpu_loaded_fraction"),
                    "partially_cpu_offloaded": bool(merged.get("partially_cpu_offloaded")),
                    "historical_average_latency_ms": self._historical_average_latency(model_name),
                    "warning": capability_warning(capability, merged),
                    "raw": merged,
                }
            )
        return results

    def plan(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request = normalize_plan_request(params or {}, self.db)
        installed_models = self.installed_models()
        model_map = {item["name"]: item for item in installed_models}
        jobs = planned_jobs_for_request(request, model_map)
        estimate = estimate_campaign_runtime(self.db, jobs, case_count_for_modes())
        warnings = planning_warnings(jobs, estimate)
        return {
            "request": request,
            "installed_models": installed_models,
            "planned_jobs": jobs,
            "planned_job_count": len(jobs),
            "estimate": estimate,
            "warnings": warnings,
        }

    def create(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        plan = params.get("plan") if isinstance(params.get("plan"), dict) else self.plan(params)
        request = plan.get("request") if isinstance(plan.get("request"), dict) else normalize_plan_request(params, self.db)
        jobs = list(plan.get("planned_jobs") or [])
        if not jobs:
            raise ValueError("campaign plan contains no jobs")
        estimate = plan.get("estimate") if isinstance(plan.get("estimate"), dict) else estimate_campaign_runtime(self.db, jobs, case_count_for_modes())
        campaign_id = str(uuid.uuid4())
        now = utc_ms()
        self.db.conn.execute(
            """
            INSERT INTO benchmark_campaigns(
                id, title, preset, app_version, suite_version, status,
                selected_models_json, selected_modes_json, selected_thinking_modes_json,
                verifier_settings_json, repeat_count, embedding_backend, embedding_model,
                temperature, num_predict, timeout_policy_json, planned_job_count,
                estimated_runtime_ms, estimated_min_runtime_ms, auto_generate_report,
                output_folder, include_detailed_audit, notes, created_at
            )
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                str(request.get("title") or "Benchmark campaign").strip(),
                str(request.get("preset") or "quick"),
                __version__,
                EVAL_SUITE_VERSION,
                json.dumps(request.get("models") or []),
                json.dumps(request.get("modes") or []),
                json.dumps(request.get("thinking_modes") or []),
                json.dumps(request.get("verifier_settings") or []),
                int(request.get("repeats") or 1),
                str(request.get("embedding_backend") or ""),
                str(request.get("embedding_model") or DEFAULT_EMBEDDING_MODEL),
                float(request.get("temperature") or 0),
                int(request.get("num_predict") or 0),
                json.dumps(timeout_policy()),
                len(jobs),
                int(estimate.get("likely_ms") or 0),
                int(estimate.get("min_ms") or 0),
                1 if request.get("auto_generate_report", True) else 0,
                str(request.get("output_folder") or ""),
                1 if request.get("include_detailed_audit") else 0,
                str(request.get("notes") or ""),
                now,
            ),
        )
        for index, job in enumerate(jobs, start=1):
            self.db.conn.execute(
                """
                INSERT INTO benchmark_campaign_jobs(
                    id, campaign_id, sequence, model, benchmark_mode, thinking_mode,
                    verify, repeat_count, temperature, num_predict, timeout_policy_json,
                    status, estimated_runtime_ms, estimated_min_runtime_ms, model_info_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    campaign_id,
                    index,
                    str(job["model"]),
                    str(job["benchmark_mode"]),
                    str(job["thinking_mode"]),
                    1 if job.get("verify") else 0,
                    int(job.get("repeat_count") or 1),
                    float(job.get("temperature") or 0),
                    int(job.get("num_predict") or 0),
                    json.dumps(timeout_policy()),
                    int(job.get("estimated_runtime_ms") or 0),
                    int(job.get("estimated_min_runtime_ms") or 0),
                    json.dumps(job.get("model_info") or {}),
                    now,
                ),
            )
        self.db.conn.commit()
        return self.get(campaign_id)

    def list(self, *, limit: int = 20) -> list[dict[str, Any]]:
        clean_limit = min(max(int(limit or 20), 1), 100)
        rows = self.db.conn.execute(
            """
            SELECT *
            FROM benchmark_campaigns
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (clean_limit,),
        ).fetchall()
        return [self._campaign_with_jobs(row["id"], include_jobs=True) for row in rows]

    def get(self, campaign_id: str) -> dict[str, Any]:
        return self._campaign_with_jobs(campaign_id, include_jobs=True)

    def start(self, campaign_id: str) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        if campaign["status"] in {"completed", "completed_with_errors", "cancelled"}:
            return self.get(campaign_id)
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET status = CASE WHEN status = 'draft' THEN 'queued' ELSE status END,
                requested_action = '',
                started_at = COALESCE(started_at, ?)
            WHERE id = ?
            """,
            (utc_ms(), campaign_id),
        )
        self.db.conn.commit()
        self._ensure_worker(campaign_id)
        return self.get(campaign_id)

    def resume(self, campaign_id: str) -> dict[str, Any]:
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET status = 'queued',
                requested_action = '',
                started_at = COALESCE(started_at, ?)
            WHERE id = ?
              AND status IN ('paused', 'interrupted', 'error', 'draft', 'queued')
            """,
            (utc_ms(), campaign_id),
        )
        self.db.conn.execute(
            """
            UPDATE benchmark_campaign_jobs
            SET status = 'queued',
                error = ''
            WHERE campaign_id = ?
              AND status = 'interrupted'
            """,
            (campaign_id,),
        )
        self.db.conn.commit()
        self._ensure_worker(campaign_id)
        return self.get(campaign_id)

    def pause(self, campaign_id: str) -> dict[str, Any]:
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET requested_action = 'pause',
                status = CASE WHEN status IN ('queued', 'draft') THEN 'paused' ELSE status END
            WHERE id = ?
            """,
            (campaign_id,),
        )
        self.db.conn.commit()
        return self.get(campaign_id)

    def cancel(self, campaign_id: str) -> dict[str, Any]:
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET requested_action = 'cancel'
            WHERE id = ?
            """,
            (campaign_id,),
        )
        if not self._worker_alive(campaign_id):
            self._cancel_remaining_jobs(campaign_id)
            self._finalize_campaign(campaign_id, status="cancelled")
        self.db.conn.commit()
        return self.get(campaign_id)

    def retry_job(self, job_id: str) -> dict[str, Any]:
        row = self.db.conn.execute(
            "SELECT * FROM benchmark_campaign_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"campaign job not found: {job_id}")
        self.db.conn.execute(
            """
            UPDATE benchmark_campaign_jobs
            SET status = 'queued',
                error = '',
                retry_count = retry_count + 1,
                started_at = NULL,
                completed_at = NULL
            WHERE id = ?
            """,
            (job_id,),
        )
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET status = 'queued',
                requested_action = '',
                completed_at = NULL
            WHERE id = ?
            """,
            (row["campaign_id"],),
        )
        self.db.conn.commit()
        self._ensure_worker(row["campaign_id"])
        return self.get(row["campaign_id"])

    def delete(self, campaign_id: str, *, delete_benchmark_runs: bool = False) -> dict[str, Any]:
        if delete_benchmark_runs:
            raise ValueError("Deleting linked benchmark history is not supported without a dedicated confirmation flow.")
        self.db.conn.execute("DELETE FROM benchmark_campaigns WHERE id = ?", (campaign_id,))
        self.db.conn.commit()
        return {"deleted": True, "benchmark_history_deleted": False}

    def report_data(self, campaign_id: str, *, include_detailed_audit: bool = False) -> dict[str, Any]:
        return ReportService(self.db).build_report_data(
            campaign_id,
            include_detailed_audit=include_detailed_audit,
        )

    def generate_report(
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
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET report_status = ?
            WHERE id = ?
            """,
            (REPORT_CAPTURING if screenshots else REPORT_GENERATING, campaign_id),
        )
        self.db.conn.commit()
        return ReportService(self.db).generate_campaign_report(
            campaign_id,
            output_folder=output_folder,
            screenshots=screenshots,
            capture_failure_reason=capture_failure_reason,
            include_detailed_audit=include_detailed_audit,
            generate_pdf=generate_pdf,
            generate_html=generate_html,
        )

    def open_report_path(self, path: str) -> dict[str, Any]:
        return ReportService(self.db).open_report_path(path)

    def recover_interrupted_campaigns(self) -> int:
        now = utc_ms()
        rows = self.db.conn.execute(
            "SELECT id FROM benchmark_campaigns WHERE status = 'running'",
        ).fetchall()
        for row in rows:
            self.db.conn.execute(
                """
                UPDATE benchmark_campaigns
                SET status = 'interrupted',
                    requested_action = '',
                    completed_at = ?,
                    notes = trim(notes || char(10) || 'Campaign was interrupted because the sidecar stopped.')
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            self.db.conn.execute(
                """
                UPDATE benchmark_campaign_jobs
                SET status = 'interrupted',
                    completed_at = ?,
                    error = 'Campaign was interrupted because the sidecar stopped.'
                WHERE campaign_id = ?
                  AND status = 'running'
                """,
                (now, row["id"]),
            )
        self.db.conn.commit()
        return len(rows)

    def _campaign_with_jobs(self, campaign_id: str, *, include_jobs: bool) -> dict[str, Any]:
        campaign = self._campaign(campaign_id)
        jobs = self._jobs(campaign_id) if include_jobs else []
        current_job = next((job for job in jobs if job.get("status") == "running"), None)
        campaign["jobs"] = jobs
        campaign["current_job"] = current_job
        campaign["progress"] = campaign_progress(campaign, jobs)
        return campaign

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

    def _ensure_worker(self, campaign_id: str) -> None:
        with self._worker_lock:
            worker = self._workers.get(campaign_id)
            if worker and worker.is_alive():
                return
            thread = threading.Thread(
                target=run_campaign_worker,
                args=(str(self.profile_dir), campaign_id),
                daemon=True,
                name=f"odysseus-campaign-{campaign_id[:8]}",
            )
            self._workers[campaign_id] = thread
            thread.start()

    def _worker_alive(self, campaign_id: str) -> bool:
        with self._worker_lock:
            worker = self._workers.get(campaign_id)
            return bool(worker and worker.is_alive())

    def _run_campaign(self, campaign_id: str) -> None:
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET status = 'running',
                started_at = COALESCE(started_at, ?)
            WHERE id = ?
            """,
            (utc_ms(), campaign_id),
        )
        self.db.conn.commit()
        while True:
            campaign = self._campaign(campaign_id)
            action = str(campaign.get("requested_action") or "")
            if action == "cancel":
                self._cancel_remaining_jobs(campaign_id)
                self._finalize_campaign(campaign_id, status="cancelled")
                return
            if action == "pause":
                self.db.conn.execute(
                    """
                    UPDATE benchmark_campaigns
                    SET status = 'paused',
                        requested_action = ''
                    WHERE id = ?
                    """,
                    (campaign_id,),
                )
                self.db.conn.commit()
                return
            next_job = self._next_job(campaign_id)
            if next_job is None:
                self._finalize_campaign(campaign_id)
                campaign = self._campaign(campaign_id)
                if campaign.get("auto_generate_report") and campaign.get("status") != "cancelled":
                    self._await_report_capture(campaign_id)
                return
            self._run_job(campaign_id, next_job)

    def _next_job(self, campaign_id: str) -> dict[str, Any] | None:
        row = self.db.conn.execute(
            """
            SELECT *
            FROM benchmark_campaign_jobs
            WHERE campaign_id = ?
              AND status IN ('queued', 'interrupted')
            ORDER BY sequence ASC
            LIMIT 1
            """,
            (campaign_id,),
        ).fetchone()
        return job_row_dict(row) if row is not None else None

    def _run_job(self, campaign_id: str, job: dict[str, Any]) -> None:
        started = utc_ms()
        self.db.conn.execute(
            """
            UPDATE benchmark_campaign_jobs
            SET status = 'running',
                started_at = COALESCE(started_at, ?),
                error = ''
            WHERE id = ?
            """,
            (started, job["id"]),
        )
        self.db.conn.execute(
            "UPDATE benchmark_campaigns SET status = 'running' WHERE id = ?",
            (campaign_id,),
        )
        self.db.conn.commit()
        try:
            result = EvalService(self.db).run(
                model=str(job["model"]),
                verify=bool(job.get("verify")),
                temperature=float(job.get("temperature") or 0),
                benchmark_mode=str(job["benchmark_mode"]),
                thinking_mode=str(job["thinking_mode"]),
                repeats=int(job.get("repeat_count") or 1),
                num_predict=int(job.get("num_predict") or 0),
            )
            run_ids = list(job.get("benchmark_run_ids") or [])
            run_ids.append(str(result["id"]))
            status = job_status_for_run(result)
            error = "" if status == "completed" else job_error_for_run(result)
            self.db.conn.execute(
                """
                UPDATE benchmark_campaign_jobs
                SET status = ?,
                    benchmark_run_ids_json = ?,
                    error = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (status, json.dumps(run_ids), error, utc_ms(), job["id"]),
            )
        except Exception as exc:  # noqa: BLE001 - one failed job must not abort the campaign
            logger.exception("campaign job failed campaign_id=%s job_id=%s error=%s", campaign_id, job["id"], exc)
            self.db.conn.execute(
                """
                UPDATE benchmark_campaign_jobs
                SET status = 'failed',
                    error = ?,
                    completed_at = ?
                WHERE id = ?
                """,
                (str(exc), utc_ms(), job["id"]),
            )
        self._refresh_campaign_counts(campaign_id)
        self.db.conn.commit()

    def _cancel_remaining_jobs(self, campaign_id: str) -> None:
        now = utc_ms()
        self.db.conn.execute(
            """
            UPDATE benchmark_campaign_jobs
            SET status = 'cancelled',
                completed_at = COALESCE(completed_at, ?),
                error = CASE WHEN error = '' THEN 'Campaign cancelled before this job started.' ELSE error END
            WHERE campaign_id = ?
              AND status IN ('queued', 'interrupted')
            """,
            (now, campaign_id),
        )
        self._refresh_campaign_counts(campaign_id)

    def _finalize_campaign(self, campaign_id: str, *, status: str | None = None) -> None:
        self._refresh_campaign_counts(campaign_id)
        campaign = self._campaign(campaign_id)
        final_status = status or final_campaign_status(campaign)
        actual_runtime = campaign_actual_runtime_ms(campaign)
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET status = ?,
                requested_action = '',
                actual_runtime_ms = ?,
                completed_at = COALESCE(completed_at, ?)
            WHERE id = ?
            """,
            (final_status, actual_runtime, utc_ms(), campaign_id),
        )
        self.db.conn.commit()

    def _refresh_campaign_counts(self, campaign_id: str) -> None:
        rows = self.db.conn.execute(
            """
            SELECT status, COUNT(*) AS count
            FROM benchmark_campaign_jobs
            WHERE campaign_id = ?
            GROUP BY status
            """,
            (campaign_id,),
        ).fetchall()
        counts = {str(row["status"]): int(row["count"] or 0) for row in rows}
        self.db.conn.execute(
            """
            UPDATE benchmark_campaigns
            SET completed_job_count = ?,
                failed_job_count = ?,
                timed_out_job_count = ?,
                skipped_job_count = ?
            WHERE id = ?
            """,
            (
                counts.get("completed", 0),
                counts.get("failed", 0),
                counts.get("timed_out", 0),
                counts.get("skipped", 0) + counts.get("cancelled", 0),
                campaign_id,
            ),
        )

    def _await_report_capture(self, campaign_id: str) -> None:
        try:
            self.db.conn.execute(
                """
                UPDATE benchmark_campaigns
                SET report_status = ?
                WHERE id = ?
                """,
                (REPORT_AWAITING_CAPTURE, campaign_id),
            )
            self.db.conn.commit()
        except Exception as exc:  # noqa: BLE001
            logger.exception("automatic campaign report handoff failed campaign_id=%s error=%s", campaign_id, exc)
            self.db.conn.execute(
                """
                UPDATE benchmark_campaigns
                SET report_status = ?,
                    report_warnings_json = ?
                WHERE id = ?
                """,
                (REPORT_ERROR, json.dumps([str(exc)]), campaign_id),
            )
            self.db.conn.commit()

    def _historical_average_latency(self, model: str) -> int:
        row = self.db.conn.execute(
            """
            SELECT AVG(average_latency_ms) AS average_latency_ms
            FROM benchmark_runs
            WHERE model = ?
              AND status = 'completed'
              AND suite_version = ?
            """,
            (model, EVAL_SUITE_VERSION),
        ).fetchone()
        return int(row["average_latency_ms"] or 0) if row else 0


def run_campaign_worker(profile_dir: str, campaign_id: str) -> None:
    db = Database(Path(profile_dir))
    try:
        CampaignService(db)._run_campaign(campaign_id)
    finally:
        db.close()


def normalize_plan_request(params: dict[str, Any], db: Database) -> dict[str, Any]:
    preset = normalize_preset(str(params.get("preset") or "quick"))
    settings = db.get_settings()
    models = clean_str_list(params.get("models") or params.get("selected_models") or [])
    modes = clean_modes(params.get("modes") or params.get("selected_modes") or [])
    thinking_modes = clean_thinking_modes(params.get("thinking_modes") or [])
    verifier_settings = clean_verifier_settings(params.get("verifier_settings") or [])
    repeats = 3 if int(params.get("repeats") or params.get("repeat_count") or 1) == 3 else 1
    num_predict = int(params.get("num_predict") or 0)
    if preset == "quick":
        modes = ["end_to_end"]
        thinking_modes = ["off"]
        verifier_settings = [False]
        repeats = 1
        num_predict = num_predict or QUICK_NUM_PREDICT
    elif preset == "standard":
        modes = ["retrieval_only", "oracle_generation", "end_to_end"]
        thinking_modes = ["off"]
        verifier_settings = [False]
        repeats = 1
    elif preset == "thorough":
        modes = modes or ["end_to_end"]
        thinking_modes = thinking_modes or ["off"]
        verifier_settings = verifier_settings or [False]
    return {
        "title": str(params.get("title") or default_campaign_title(preset)).strip(),
        "preset": preset,
        "models": models,
        "modes": modes,
        "thinking_modes": thinking_modes,
        "verifier_settings": verifier_settings,
        "repeats": repeats,
        "temperature": float(params.get("temperature") or 0),
        "num_predict": num_predict,
        "embedding_backend": str(settings.get("embedding_backend") or "auto"),
        "embedding_model": str(settings.get("embedding_model") or DEFAULT_EMBEDDING_MODEL),
        "auto_generate_report": bool(params.get("auto_generate_report", True)),
        "output_folder": str(params.get("output_folder") or ""),
        "include_detailed_audit": bool(params.get("include_detailed_audit", False)),
        "notes": str(params.get("notes") or ""),
    }


def planned_jobs_for_request(request: dict[str, Any], model_map: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    selected_models = list(request.get("models") or [])
    chat_models = [
        model
        for model in selected_models
        if model_map.get(model, {}).get("capability") != "embedding"
    ]
    jobs: list[dict[str, Any]] = []
    if "retrieval_only" in request.get("modes", []):
        retrieval_model = chat_models[0] if chat_models else "retrieval-only"
        jobs.append(make_job(request, retrieval_model, "retrieval_only", "off", False, model_map.get(retrieval_model)))
    for model in chat_models:
        for mode in MODE_ORDER:
            if mode == "retrieval_only" or mode not in request.get("modes", []):
                continue
            for thinking in request.get("thinking_modes") or ["off"]:
                for verify in request.get("verifier_settings") or [False]:
                    if verify and mode != "end_to_end":
                        continue
                    jobs.append(make_job(request, model, mode, thinking, bool(verify), model_map.get(model)))
    requested_jobs = request.get("jobs")
    if isinstance(requested_jobs, list):
        allowed_keys = {job_key(job) for job in requested_jobs if isinstance(job, dict)}
        jobs = [job for job in jobs if job_key(job) in allowed_keys]
    model_order = {model: index for index, model in enumerate(selected_models)}
    jobs.sort(
        key=lambda job: (
            MODE_ORDER.index(job["benchmark_mode"]),
            model_order.get(str(job.get("model") or ""), len(model_order)),
            THINKING_ORDER.index(job["thinking_mode"]),
            int(job["verify"]),
        )
    )
    for index, job in enumerate(jobs, start=1):
        job["sequence"] = index
        job["key"] = job_key(job)
    return jobs


def make_job(
    request: dict[str, Any],
    model: str,
    mode: str,
    thinking: str,
    verify: bool,
    model_info: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "model": model,
        "benchmark_mode": normalize_benchmark_mode(mode),
        "thinking_mode": normalize_thinking_mode(thinking),
        "verify": bool(verify),
        "repeat_count": int(request.get("repeats") or 1),
        "temperature": float(request.get("temperature") or 0),
        "num_predict": int(request.get("num_predict") or 0),
        "timeout_policy": timeout_policy(),
        "model_info": model_info or {},
        "warnings": job_warnings(verify, model_info or {}),
    }


def estimate_campaign_runtime(db: Database, jobs: list[dict[str, Any]], case_counts: dict[str, int]) -> dict[str, Any]:
    total_likely = 0
    total_min = 0
    uncertain = False
    history_counts: list[int] = []
    generations = 0
    verifier_calls = 0
    for job in jobs:
        estimate = estimate_job_runtime(db, job, case_counts.get(job["benchmark_mode"], 0))
        job["estimated_runtime_ms"] = estimate["likely_ms"]
        job["estimated_min_runtime_ms"] = estimate["min_ms"]
        job["estimate_source"] = estimate["source"]
        job["estimate_confidence"] = estimate["confidence"]
        job["estimate_detail"] = estimate["detail"]
        history_counts.append(int(estimate.get("compatible_history_count") or 0))
        total_likely += estimate["likely_ms"]
        total_min += estimate["min_ms"]
        uncertain = uncertain or estimate["source"] == "fallback"
        if job["benchmark_mode"] != "retrieval_only":
            generations += case_counts.get(job["benchmark_mode"], 0) * int(job.get("repeat_count") or 1)
        if job.get("verify") and job["benchmark_mode"] == "end_to_end":
            verifier_calls += case_counts.get(job["benchmark_mode"], 0) * int(job.get("repeat_count") or 1)
    return {
        "min_ms": int(total_min),
        "likely_ms": int(total_likely),
        "uncertain": uncertain,
        "job_count": len(jobs),
        "model_generation_count": generations,
        "verifier_call_count": verifier_calls,
        "confidence": "low" if uncertain else ("high" if all(count > 1 for count in history_counts) else "medium"),
        "source_detail": (
            f"Estimated from {sum(history_counts)} compatible local campaign(s)"
            if sum(history_counts)
            else "No compatible history; conservative estimate"
        ),
    }


def estimate_job_runtime(db: Database, job: dict[str, Any], case_count: int) -> dict[str, Any]:
    historical = historical_job_runtime(db, job, case_count)
    if historical["values"]:
        likely = int(statistics.median(historical["values"]))
        count = len(historical["values"])
        return {
            "likely_ms": likely,
            "min_ms": max(int(likely * 0.75), 1),
            "source": "historical",
            "confidence": "high" if count >= 3 else "medium",
            "detail": f"Estimated from {count} compatible local campaign(s)",
            "compatible_history_count": count,
        }
    repeats = int(job.get("repeat_count") or 1)
    if job["benchmark_mode"] == "retrieval_only":
        likely = RETRIEVAL_FALLBACK_JOB_MS * repeats
    elif job["benchmark_mode"] == "oracle_generation":
        likely = ORACLE_FALLBACK_CASE_MS * max(case_count, 1) * repeats
    else:
        likely = END_TO_END_FALLBACK_CASE_MS * max(case_count, 1) * repeats
    if job.get("thinking_mode") in {"auto", "on"}:
        likely = int(likely * 1.35)
    if job.get("verify"):
        likely = int(likely * 2.0)
    if (job.get("model_info") or {}).get("partially_cpu_offloaded"):
        likely = int(likely * 1.5)
    if job["benchmark_mode"] != "retrieval_only" and not job.get("model_info"):
        likely = max(likely, UNKNOWN_MODEL_JOB_MS)
    return {
        "likely_ms": likely,
        "min_ms": max(int(likely * 0.6), 1),
        "source": "fallback",
        "confidence": "low",
        "detail": "No compatible history; conservative estimate",
        "compatible_history_count": 0,
    }


def historical_job_runtime(db: Database, job: dict[str, Any], case_count: int) -> dict[str, Any]:
    rows = db.conn.execute(
        """
        SELECT total_runtime_ms
        FROM benchmark_runs
        WHERE status = 'completed'
          AND suite_version = ?
          AND benchmark_mode = ?
          AND model = ?
          AND thinking_mode = ?
          AND verify = ?
          AND temperature = ?
          AND num_predict = ?
          AND COALESCE(repeat_count, 1) = ?
        ORDER BY created_at DESC
        LIMIT 7
        """,
        (
            EVAL_SUITE_VERSION,
            job["benchmark_mode"],
            job["model"],
            job["thinking_mode"],
            1 if job.get("verify") else 0,
            float(job.get("temperature") or 0),
            int(job.get("num_predict") or 0),
            int(job.get("repeat_count") or 1),
        ),
    ).fetchall()
    values = [int(row["total_runtime_ms"] or 0) for row in rows if int(row["total_runtime_ms"] or 0) > 0]
    return {"values": values, "case_count": case_count}


def case_count_for_modes() -> dict[str, int]:
    counts = {mode: 0 for mode in MODE_ORDER}
    for case in load_cases(default_cases_dir()):
        for mode in benchmark_modes_for_case(case):
            counts[mode] += 1
    return counts


def planning_warnings(jobs: list[dict[str, Any]], estimate: dict[str, Any]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    likely = int(estimate.get("likely_ms") or 0)
    if likely > 2 * 60 * 60 * 1000:
        warnings.append({"level": "strong", "message": "Estimated runtime is over 2 hours; the computer may remain heavily loaded."})
    elif likely > 30 * 60 * 1000:
        warnings.append({"level": "confirm", "message": "Estimated runtime is over 30 minutes; confirm before starting."})
    elif likely > 10 * 60 * 1000:
        warnings.append({"level": "normal", "message": "Estimated runtime is over 10 minutes."})
    for job in jobs:
        for message in job.get("warnings") or []:
            warnings.append({"level": "normal", "message": message})
    return dedupe_warnings(warnings)


def dedupe_warnings(warnings: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for warning in warnings:
        key = (warning["level"], warning["message"])
        if key in seen:
            continue
        seen.add(key)
        result.append(warning)
    return result


def classify_model_capability(name: str, details: dict[str, Any]) -> str:
    lower_name = name.lower()
    family = str(details.get("family") or "").lower()
    if "embed" in lower_name or "embedding" in lower_name or family in {"bert", "nomic-bert"}:
        return "embedding"
    if family or details.get("parameter_size"):
        return "chat"
    return "unknown"


def capability_warning(capability: str, details: dict[str, Any]) -> str:
    if capability == "embedding":
        return "Embedding-only model; excluded from automatic chat benchmark selection."
    if capability == "unknown":
        return "Capability could not be determined; manual selection may fail."
    if details.get("partially_cpu_offloaded"):
        return "This model is partially CPU-offloaded and may run slowly."
    return ""


def job_warnings(verify: bool, model_info: dict[str, Any]) -> list[str]:
    warnings = []
    if model_info.get("partially_cpu_offloaded"):
        warnings.append("This model is partially CPU-offloaded and may run slowly.")
    if verify:
        warnings.append("Verifier may multiply generation time.")
    if model_info.get("capability") == "unknown":
        warnings.append("Model capability is unknown; the job may fail.")
    return warnings


def job_status_for_run(run: dict[str, Any]) -> str:
    if str(run.get("status") or "") != "completed":
        return "failed"
    if int(run.get("runtime_error_count") or 0) > 0:
        return "failed"
    if int(run.get("timeout_count") or 0) > 0:
        return "timed_out"
    return "completed"


def job_error_for_run(run: dict[str, Any]) -> str:
    if int(run.get("timeout_count") or 0) > 0:
        return f"{run.get('timeout_count')} benchmark case(s) timed out."
    if int(run.get("runtime_error_count") or 0) > 0:
        return f"{run.get('runtime_error_count')} benchmark case(s) had runtime errors."
    if str(run.get("status") or "") != "completed":
        return f"Benchmark run ended with status {run.get('status')}."
    return ""


def final_campaign_status(campaign: dict[str, Any]) -> str:
    planned = int(campaign.get("planned_job_count") or 0)
    completed = int(campaign.get("completed_job_count") or 0)
    failed = int(campaign.get("failed_job_count") or 0)
    timed_out = int(campaign.get("timed_out_job_count") or 0)
    skipped = int(campaign.get("skipped_job_count") or 0)
    if completed + failed + timed_out + skipped < planned:
        return "interrupted"
    if failed or timed_out or skipped:
        return "completed_with_errors"
    return "completed"


def campaign_progress(campaign: dict[str, Any], jobs: list[dict[str, Any]]) -> dict[str, Any]:
    elapsed = campaign_actual_runtime_ms(campaign)
    completed_like = sum(1 for job in jobs if job.get("status") in JOB_TERMINAL_STATUSES)
    remaining_estimate = sum(
        int(job.get("estimated_runtime_ms") or 0)
        for job in jobs
        if job.get("status") not in JOB_TERMINAL_STATUSES
    )
    return {
        "job_index": completed_like + (1 if any(job.get("status") == "running" for job in jobs) else 0),
        "job_count": len(jobs),
        "completed_terminal_jobs": completed_like,
        "elapsed_ms": elapsed,
        "estimated_remaining_ms": remaining_estimate,
    }


def campaign_actual_runtime_ms(campaign: dict[str, Any]) -> int:
    started = int(campaign.get("started_at") or 0)
    if not started:
        return int(campaign.get("actual_runtime_ms") or 0)
    end = int(campaign.get("completed_at") or 0) or utc_ms()
    return max(0, end - started)


def clean_str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def clean_modes(value: Any) -> list[str]:
    result = []
    for item in clean_str_list(value):
        mode = normalize_benchmark_mode(item)
        if mode not in result:
            result.append(mode)
    return sorted(result, key=MODE_ORDER.index)


def clean_thinking_modes(value: Any) -> list[str]:
    result = []
    for item in clean_str_list(value):
        mode = normalize_thinking_mode(item)
        if mode != "legacy/unrecorded" and mode not in result:
            result.append(mode)
    return sorted(result, key=THINKING_ORDER.index)


def clean_verifier_settings(value: Any) -> list[bool]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        flag = bool(item)
        if flag not in result:
            result.append(flag)
    return sorted(result, key=VERIFIER_ORDER.index)


def normalize_preset(value: str) -> str:
    normalized = (value or "quick").strip().lower().replace(" ", "_").replace("-", "_")
    if normalized in {"quick", "quick_comparison"}:
        return "quick"
    if normalized in {"standard", "standard_diagnostic"}:
        return "standard"
    if normalized in {"thorough", "thorough_comparison"}:
        return "thorough"
    raise ValueError("campaign preset must be quick, standard, or thorough")


def default_campaign_title(preset: str) -> str:
    return {
        "quick": "Quick benchmark comparison",
        "standard": "Standard benchmark diagnostic",
        "thorough": "Thorough benchmark comparison",
    }.get(preset, "Benchmark campaign")


def job_key(job: dict[str, Any]) -> str:
    return "|".join(
        [
            str(job.get("model") or ""),
            str(job.get("benchmark_mode") or ""),
            str(job.get("thinking_mode") or ""),
            "verify" if job.get("verify") else "noverify",
            str(int(job.get("repeat_count") or 1)),
            str(int(job.get("num_predict") or 0)),
        ]
    )
