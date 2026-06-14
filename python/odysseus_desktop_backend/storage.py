from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable


def utc_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """Profile-local SQLite database."""

    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.profile_dir / "app.db"
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_message_at INTEGER
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session_time
                ON messages(session_id, created_at);

            CREATE TABLE IF NOT EXISTS runtime_status (
                name TEXT PRIMARY KEY,
                reachable INTEGER NOT NULL DEFAULT 0,
                installed INTEGER NOT NULL DEFAULT 0,
                endpoint TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                models_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS documents (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                source_path TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_type TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'imported',
                index_status TEXT NOT NULL DEFAULT 'pending',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                is_low_text INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                indexed_at INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_documents_status
                ON documents(is_deleted, index_status, updated_at);

            CREATE TABLE IF NOT EXISTS document_pages (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                extraction_method TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_document_pages_document
                ON document_pages(document_id, page_number);

            CREATE TABLE IF NOT EXISTS ocr_pages (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                page_number INTEGER NOT NULL,
                engine_name TEXT NOT NULL,
                confidence REAL,
                text TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                chunk_ids_json TEXT NOT NULL DEFAULT '[]',
                index_status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_ocr_pages_document
                ON ocr_pages(document_id, page_number);

            CREATE TABLE IF NOT EXISTS rag_chunks (
                id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                page_start INTEGER,
                page_end INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                embedding_model TEXT NOT NULL,
                embedding_hash TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_rag_chunks_document_index
                ON rag_chunks(document_id, chunk_index);

            CREATE INDEX IF NOT EXISTS idx_rag_chunks_search
                ON rag_chunks(is_deleted, embedding_model, document_id);

            CREATE TABLE IF NOT EXISTS embedding_cache (
                content_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                vector_blob BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                PRIMARY KEY (content_hash, embedding_model)
            );

            CREATE TABLE IF NOT EXISTS benchmark_runs (
                id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                verify INTEGER NOT NULL DEFAULT 0,
                suite_name TEXT NOT NULL,
                suite_version TEXT NOT NULL,
                total_passed INTEGER NOT NULL,
                total_failed INTEGER NOT NULL,
                average_latency_ms INTEGER NOT NULL,
                total_runtime_ms INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_benchmark_runs_created
                ON benchmark_runs(created_at DESC);

            CREATE TABLE IF NOT EXISTS benchmark_case_results (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer_style TEXT NOT NULL,
                required_source_document TEXT NOT NULL,
                passed INTEGER NOT NULL,
                expected_passed INTEGER NOT NULL,
                forbidden_passed INTEGER NOT NULL,
                source_passed INTEGER NOT NULL,
                latency_ms INTEGER NOT NULL,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                created_at INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES benchmark_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_benchmark_case_results_run
                ON benchmark_case_results(run_id, case_id);

            CREATE TABLE IF NOT EXISTS benchmark_campaigns (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                preset TEXT NOT NULL,
                app_version TEXT NOT NULL,
                suite_version TEXT NOT NULL,
                status TEXT NOT NULL,
                selected_models_json TEXT NOT NULL DEFAULT '[]',
                selected_modes_json TEXT NOT NULL DEFAULT '[]',
                selected_thinking_modes_json TEXT NOT NULL DEFAULT '[]',
                verifier_settings_json TEXT NOT NULL DEFAULT '[]',
                repeat_count INTEGER NOT NULL DEFAULT 1,
                embedding_backend TEXT NOT NULL DEFAULT '',
                embedding_model TEXT NOT NULL DEFAULT '',
                temperature REAL NOT NULL DEFAULT 0,
                num_predict INTEGER NOT NULL DEFAULT 0,
                timeout_policy_json TEXT NOT NULL DEFAULT '{}',
                planned_job_count INTEGER NOT NULL DEFAULT 0,
                completed_job_count INTEGER NOT NULL DEFAULT 0,
                failed_job_count INTEGER NOT NULL DEFAULT 0,
                timed_out_job_count INTEGER NOT NULL DEFAULT 0,
                skipped_job_count INTEGER NOT NULL DEFAULT 0,
                estimated_runtime_ms INTEGER NOT NULL DEFAULT 0,
                estimated_min_runtime_ms INTEGER NOT NULL DEFAULT 0,
                actual_runtime_ms INTEGER NOT NULL DEFAULT 0,
                auto_generate_report INTEGER NOT NULL DEFAULT 1,
                report_status TEXT NOT NULL DEFAULT 'not_started',
                report_paths_json TEXT NOT NULL DEFAULT '{}',
                report_warnings_json TEXT NOT NULL DEFAULT '[]',
                report_schema_version TEXT NOT NULL DEFAULT '',
                output_folder TEXT NOT NULL DEFAULT '',
                include_detailed_audit INTEGER NOT NULL DEFAULT 0,
                requested_action TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_benchmark_campaigns_created
                ON benchmark_campaigns(created_at DESC);

            CREATE TABLE IF NOT EXISTS benchmark_campaign_jobs (
                id TEXT PRIMARY KEY,
                campaign_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                model TEXT NOT NULL,
                benchmark_mode TEXT NOT NULL,
                thinking_mode TEXT NOT NULL,
                verify INTEGER NOT NULL DEFAULT 0,
                repeat_count INTEGER NOT NULL DEFAULT 1,
                temperature REAL NOT NULL DEFAULT 0,
                num_predict INTEGER NOT NULL DEFAULT 0,
                timeout_policy_json TEXT NOT NULL DEFAULT '{}',
                benchmark_run_ids_json TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'queued',
                retry_count INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                estimated_runtime_ms INTEGER NOT NULL DEFAULT 0,
                estimated_min_runtime_ms INTEGER NOT NULL DEFAULT 0,
                model_info_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                FOREIGN KEY (campaign_id) REFERENCES benchmark_campaigns(id) ON DELETE CASCADE
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_benchmark_campaign_jobs_sequence
                ON benchmark_campaign_jobs(campaign_id, sequence);

            CREATE INDEX IF NOT EXISTS idx_benchmark_campaign_jobs_status
                ON benchmark_campaign_jobs(campaign_id, status, sequence);
            """
        )
        self.ensure_column("documents", "ocr_status", "TEXT NOT NULL DEFAULT 'not_needed'")
        self.ensure_column("documents", "ocr_engine", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "ocr_error", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "indexed_embedding_model", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "indexed_embedding_backend", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_runs", "embedding_backend", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_runs", "embedding_model", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_runs", "temperature", "REAL NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_runs", "app_version", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_runs", "prompt_version", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_runs", "benchmark_mode", "TEXT NOT NULL DEFAULT 'end_to_end'")
        self.ensure_column("benchmark_runs", "thinking_mode", "TEXT NOT NULL DEFAULT 'legacy/unrecorded'")
        self.ensure_column("benchmark_runs", "answer_style", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_runs", "status", "TEXT NOT NULL DEFAULT 'completed'")
        self.ensure_column("benchmark_runs", "repeat_count", "INTEGER NOT NULL DEFAULT 1")
        self.ensure_column("benchmark_runs", "num_predict", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_runs", "timeout_policy_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "model_info_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "retrieval_score_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "oracle_score_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "end_to_end_score_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "practical_score_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "adversarial_score_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_runs", "timeout_count", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_runs", "runtime_error_count", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_runs", "grader_review_count", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_runs", "completed_at", "INTEGER")
        self.ensure_column("benchmark_case_results", "retrieved_document_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("benchmark_case_results", "retrieved_chunk_ids_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("benchmark_case_results", "embedding_backend", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "embedding_model", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "temperature", "REAL NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_case_results", "case_category", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "case_difficulty", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "benchmark_mode", "TEXT NOT NULL DEFAULT 'end_to_end'")
        self.ensure_column("benchmark_case_results", "thinking_mode", "TEXT NOT NULL DEFAULT 'legacy/unrecorded'")
        self.ensure_column("benchmark_case_results", "repeat_index", "INTEGER NOT NULL DEFAULT 1")
        self.ensure_column("benchmark_case_results", "status", "TEXT NOT NULL DEFAULT 'completed'")
        self.ensure_column("benchmark_case_results", "stage", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "pipeline_diagnosis", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "counts_toward_primary", "INTEGER NOT NULL DEFAULT 1")
        self.ensure_column("benchmark_case_results", "grader_review_required", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_case_results", "answer_content", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "thinking_text", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "thinking_returned", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_case_results", "thinking_char_count", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_case_results", "prompt_text", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "corrected_answer", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_case_results", "model_response_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_case_results", "retrieval_metrics_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_case_results", "retrieval_candidates_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("benchmark_case_results", "supplied_evidence_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("benchmark_case_results", "grader_matches_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("benchmark_case_results", "timings_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("benchmark_case_results", "error_message", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_campaigns", "report_warnings_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("benchmark_campaigns", "report_schema_version", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_campaigns", "output_folder", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_campaigns", "include_detailed_audit", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_campaigns", "requested_action", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("benchmark_campaign_jobs", "estimated_min_runtime_ms", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("benchmark_campaign_jobs", "model_info_json", "TEXT NOT NULL DEFAULT '{}'")
        old_embedding_model = self.get_setting("embedding_model", None)
        old_embedding_backend = self.get_setting("embedding_backend", None)
        if old_embedding_model == "local-hash-v1" and old_embedding_backend is None:
            self.set_setting("embedding_backend", "auto")
            self.set_setting("embedding_model", "nomic-embed-text")
        self.set_meta_default("schema_version", "6")
        self.set_setting_default("default_model", "llama3.2")
        self.set_setting_default("ollama_endpoint", "http://127.0.0.1:11434")
        self.set_setting_default("embedding_backend", "auto")
        self.set_setting_default("embedding_model", "nomic-embed-text")
        self.conn.commit()

    def ensure_column(self, table: str, column: str, definition: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if any(row["name"] == column for row in rows):
            return
        self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def set_meta_default(self, key: str, value: str) -> None:
        now = utc_ms()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO app_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            """,
            (key, value, now),
        )

    def set_setting_default(self, key: str, value: Any) -> None:
        if self.get_setting(key, None) is None:
            self.set_setting(key, value)

    def get_setting(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except json.JSONDecodeError:
            return row["value"]

    def get_settings(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT key, value FROM settings ORDER BY key").fetchall()
        settings: dict[str, Any] = {}
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                settings[row["key"]] = row["value"]
        return settings

    def set_setting(self, key: str, value: Any) -> None:
        now = utc_ms()
        encoded = json.dumps(value)
        self.conn.execute(
            """
            INSERT INTO settings(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, encoded, now),
        )
        self.conn.commit()

    def rows_to_dicts(self, rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:
        return [dict(row) for row in rows]
