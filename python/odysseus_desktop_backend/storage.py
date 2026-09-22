from __future__ import annotations

import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 14


def utc_ms() -> int:
    return int(time.time() * 1000)


class Database:
    """Profile-local SQLite database."""

    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.profile_dir / "app.db"
        self._stored_schema_version = self._read_stored_schema_version()
        if (
            self._stored_schema_version is not None
            and self._stored_schema_version > SCHEMA_VERSION
        ):
            raise RuntimeError(
                f"Current app schema version {SCHEMA_VERSION} cannot open stored DB "
                f"schema version {self._stored_schema_version} at {self.path}"
            )
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA busy_timeout = 5000")
        self.init_schema()

    def _read_stored_schema_version(self) -> int | None:
        if not self.path.exists():
            return None

        uri = f"{self.path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            app_meta_exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'app_meta'"
            ).fetchone()
            if app_meta_exists is None:
                return None
            row = conn.execute(
                "SELECT value FROM app_meta WHERE key = 'schema_version'"
            ).fetchone()

        if row is None:
            return None
        try:
            return int(row[0])
        except (TypeError, ValueError):
            return None

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

            CREATE TABLE IF NOT EXISTS search_runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                user_message_id TEXT NOT NULL DEFAULT '',
                assistant_message_id TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                executed_queries_json TEXT NOT NULL DEFAULT '[]',
                round_count INTEGER NOT NULL DEFAULT 0,
                budgets_json TEXT NOT NULL DEFAULT '{}',
                metrics_json TEXT NOT NULL DEFAULT '{}',
                operations_json TEXT NOT NULL DEFAULT '[]',
                error_code TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                completed_at INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_search_runs_session_time
                ON search_runs(session_id, created_at);

            CREATE TABLE IF NOT EXISTS search_evidence (
                id TEXT PRIMARY KEY,
                search_run_id TEXT NOT NULL,
                source_document_id TEXT NOT NULL,
                passage_id TEXT NOT NULL,
                exact_quote TEXT NOT NULL,
                quote_start INTEGER NOT NULL,
                quote_end INTEGER NOT NULL,
                provenance_kind TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (search_run_id) REFERENCES search_runs(id) ON DELETE CASCADE,
                FOREIGN KEY (source_document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_search_evidence_run
                ON search_evidence(search_run_id, created_at);

            CREATE INDEX IF NOT EXISTS idx_search_evidence_source
                ON search_evidence(source_document_id, passage_id);

            CREATE TABLE IF NOT EXISTS search_evidence_diagnostics (
                id TEXT PRIMARY KEY,
                search_run_id TEXT NOT NULL,
                selection_index INTEGER NOT NULL,
                selected_span_id TEXT NOT NULL DEFAULT '',
                selected_passage_id TEXT NOT NULL DEFAULT '',
                rejection_code TEXT NOT NULL DEFAULT '',
                source_origin TEXT NOT NULL DEFAULT '',
                pointer_resolved INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (search_run_id) REFERENCES search_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_search_evidence_diagnostics_run
                ON search_evidence_diagnostics(search_run_id, selection_index);

            -- Search v1 keeps rag_chunks authoritative. This table only maps
            -- stable chunk ids to integer FTS rowids; the contentless FTS
            -- table stores terms, not another retrievable evidence body.
            CREATE TABLE IF NOT EXISTS local_fts_rows (
                rowid INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id TEXT NOT NULL UNIQUE,
                document_id TEXT NOT NULL,
                FOREIGN KEY (chunk_id) REFERENCES rag_chunks(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_local_fts_rows_document
                ON local_fts_rows(document_id);

            CREATE VIRTUAL TABLE IF NOT EXISTS local_fts USING fts5(
                title,
                headings,
                body,
                content='',
                contentless_delete=1,
                tokenize="unicode61 remove_diacritics 2 tokenchars '_-'"
            );

            CREATE TABLE IF NOT EXISTS crawl_frontier (
                id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                canonical_url TEXT NOT NULL UNIQUE,
                domain TEXT NOT NULL,
                discovered_from_url TEXT NOT NULL DEFAULT '',
                anchor_text TEXT NOT NULL DEFAULT '',
                surrounding_text TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                discovery_kind TEXT NOT NULL CHECK (
                    discovery_kind IN ('manual_seed', 'source_pack', 'hyperlink', 'sitemap', 'rss', 'atom', 'cached')
                ),
                depth INTEGER NOT NULL DEFAULT 0,
                priority REAL NOT NULL DEFAULT 0,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                last_fetch_at INTEGER,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unfetched',
                failure_code TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_attempt_at INTEGER,
                next_retry_at INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS idx_crawl_frontier_status_priority
                ON crawl_frontier(status, priority DESC, depth, canonical_url);

            CREATE INDEX IF NOT EXISTS idx_crawl_frontier_domain
                ON crawl_frontier(domain, status);

            CREATE TABLE IF NOT EXISTS crawl_discoveries (
                id TEXT PRIMARY KEY,
                frontier_id TEXT NOT NULL,
                discovered_from_url TEXT NOT NULL DEFAULT '',
                anchor_text TEXT NOT NULL DEFAULT '',
                surrounding_text TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                discovery_kind TEXT NOT NULL,
                depth INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (frontier_id) REFERENCES crawl_frontier(id) ON DELETE CASCADE,
                UNIQUE(frontier_id, discovered_from_url, discovery_kind, anchor_text)
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS frontier_fts USING fts5(
                frontier_id UNINDEXED,
                canonical_url,
                anchor_text,
                surrounding_text,
                source_title,
                tokenize="unicode61 remove_diacritics 2 tokenchars '_-'"
            );

            CREATE TABLE IF NOT EXISTS source_packs (
                id TEXT PRIMARY KEY,
                path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                preferred_domains_json TEXT NOT NULL DEFAULT '[]',
                notes TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_feeds (
                id TEXT PRIMARY KEY,
                canonical_url TEXT NOT NULL UNIQUE,
                kind TEXT NOT NULL CHECK (kind IN ('rss', 'atom')),
                title TEXT NOT NULL DEFAULT '',
                last_checked_at INTEGER,
                etag TEXT NOT NULL DEFAULT '',
                last_modified TEXT NOT NULL DEFAULT '',
                failure_code TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS discovery_feed_entries (
                id TEXT PRIMARY KEY,
                feed_id TEXT NOT NULL,
                canonical_url TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                published_at TEXT NOT NULL DEFAULT '',
                updated_at_text TEXT NOT NULL DEFAULT '',
                observed_at INTEGER NOT NULL,
                FOREIGN KEY (feed_id) REFERENCES discovery_feeds(id) ON DELETE CASCADE,
                UNIQUE(feed_id, canonical_url)
            );

            CREATE TABLE IF NOT EXISTS robots_cache (
                domain TEXT PRIMARY KEY,
                robots_url TEXT NOT NULL,
                body TEXT NOT NULL DEFAULT '',
                fetched_at INTEGER NOT NULL,
                allowed INTEGER NOT NULL DEFAULT 0,
                failure_code TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS embedding_cache (
                content_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                vector_blob BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                PRIMARY KEY (content_hash, embedding_model)
            );

            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                name TEXT NOT NULL,
                original_filename TEXT NOT NULL DEFAULT '',
                mime_type TEXT NOT NULL DEFAULT '',
                original_extension TEXT NOT NULL DEFAULT '',
                stored_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                width INTEGER NOT NULL DEFAULT 0,
                height INTEGER NOT NULL DEFAULT 0,
                normalized_orientation INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'imported',
                error TEXT NOT NULL DEFAULT '',
                is_deleted INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_artifacts_status
                ON artifacts(kind, is_deleted, updated_at DESC);

            CREATE TABLE IF NOT EXISTS artifact_derivations (
                id TEXT PRIMARY KEY,
                artifact_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                stored_path TEXT NOT NULL DEFAULT '',
                text_content TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL DEFAULT '',
                producer_type TEXT NOT NULL DEFAULT '',
                producer_name TEXT NOT NULL DEFAULT '',
                producer_version TEXT NOT NULL DEFAULT '',
                producer_model TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                confidence REAL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'created',
                error TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_artifact_derivations_artifact
                ON artifact_derivations(artifact_id, kind, created_at DESC);

            CREATE TABLE IF NOT EXISTS message_artifacts (
                message_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (message_id, artifact_id),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_message_artifacts_artifact
                ON message_artifacts(artifact_id);

            CREATE TABLE IF NOT EXISTS message_documents (
                message_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                scope_at_link TEXT NOT NULL DEFAULT 'session',
                created_at INTEGER NOT NULL,
                PRIMARY KEY (message_id, document_id),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_message_documents_document
                ON message_documents(document_id);

            CREATE TABLE IF NOT EXISTS conversation_attachments (
                session_id TEXT NOT NULL,
                backend_kind TEXT NOT NULL CHECK (backend_kind IN ('document', 'artifact')),
                source_id TEXT NOT NULL,
                added_message_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                removed_at INTEGER,
                PRIMARY KEY (session_id, backend_kind, source_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_conversation_attachments_active
                ON conversation_attachments(session_id, removed_at, created_at);

            CREATE TABLE IF NOT EXISTS artifact_analysis_runs (
                id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL UNIQUE,
                artifact_id TEXT NOT NULL,
                message_id TEXT NOT NULL DEFAULT '',
                mode TEXT NOT NULL,
                user_question TEXT NOT NULL DEFAULT '',
                requested_vision_backend TEXT NOT NULL DEFAULT '',
                actual_vision_backend TEXT NOT NULL DEFAULT '',
                requested_vision_model TEXT NOT NULL DEFAULT '',
                actual_vision_model TEXT NOT NULL DEFAULT '',
                ocr_engine TEXT NOT NULL DEFAULT '',
                prompt_version TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                stage TEXT NOT NULL DEFAULT '',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                output_json TEXT NOT NULL DEFAULT '{}',
                evidence_json TEXT NOT NULL DEFAULT '{}',
                timings_json TEXT NOT NULL DEFAULT '{}',
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
            );

            CREATE INDEX IF NOT EXISTS idx_artifact_analysis_artifact
                ON artifact_analysis_runs(artifact_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS artifact_rag_documents (
                artifact_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                derivation_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (artifact_id, document_id),
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                FOREIGN KEY (derivation_id) REFERENCES artifact_derivations(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS model_capabilities (
                model TEXT PRIMARY KEY,
                digest TEXT NOT NULL DEFAULT '',
                size INTEGER NOT NULL DEFAULT 0,
                family TEXT NOT NULL DEFAULT '',
                parameter_size TEXT NOT NULL DEFAULT '',
                quantization_level TEXT NOT NULL DEFAULT '',
                context_length INTEGER NOT NULL DEFAULT 0,
                capabilities_json TEXT NOT NULL DEFAULT '[]',
                text_generation TEXT NOT NULL DEFAULT 'unknown',
                vision TEXT NOT NULL DEFAULT 'unknown',
                embedding TEXT NOT NULL DEFAULT 'unknown',
                tools TEXT NOT NULL DEFAULT 'unknown',
                thinking TEXT NOT NULL DEFAULT 'unknown',
                raw_json TEXT NOT NULL DEFAULT '{}',
                inspected_at INTEGER NOT NULL,
                error TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS multimodal_eval_runs (
                id TEXT PRIMARY KEY,
                suite_name TEXT NOT NULL,
                suite_version TEXT NOT NULL,
                prompt_version TEXT NOT NULL,
                mode TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                ocr_engine TEXT NOT NULL DEFAULT '',
                total_passed INTEGER NOT NULL DEFAULT 0,
                total_failed INTEGER NOT NULL DEFAULT 0,
                grader_review_count INTEGER NOT NULL DEFAULT 0,
                timeout_count INTEGER NOT NULL DEFAULT 0,
                runtime_error_count INTEGER NOT NULL DEFAULT 0,
                total_runtime_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'running',
                notes TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                completed_at INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_multimodal_eval_runs_created
                ON multimodal_eval_runs(created_at DESC);

            CREATE TABLE IF NOT EXISTS multimodal_eval_case_results (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                category TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL,
                passed INTEGER NOT NULL DEFAULT 0,
                grader_review_required INTEGER NOT NULL DEFAULT 0,
                reasons_json TEXT NOT NULL DEFAULT '[]',
                raw_output TEXT NOT NULL DEFAULT '',
                structured_output_json TEXT NOT NULL DEFAULT '{}',
                assertion_matches_json TEXT NOT NULL DEFAULT '[]',
                warnings_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                latency_ms INTEGER NOT NULL DEFAULT 0,
                image_hash TEXT NOT NULL DEFAULT '',
                image_width INTEGER NOT NULL DEFAULT 0,
                image_height INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (run_id) REFERENCES multimodal_eval_runs(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_multimodal_eval_cases_run
                ON multimodal_eval_case_results(run_id, case_id);

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
        self.ensure_column("messages", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("search_runs", "operations_json", "TEXT NOT NULL DEFAULT '[]'")
        self.ensure_column("documents", "ocr_status", "TEXT NOT NULL DEFAULT 'not_needed'")
        self.ensure_column("documents", "ocr_engine", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "ocr_error", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "indexed_embedding_model", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "indexed_embedding_backend", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "is_internal", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("documents", "source_artifact_id", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "generated_source_label", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "scope", "TEXT NOT NULL DEFAULT 'library'")
        self.ensure_column("documents", "promoted_at", "INTEGER")
        # Staged (not yet committed) job imports; hidden from every user
        # surface until commit. Dedicated column, not a status value, because
        # the mark_* helpers legitimately overwrite status fields mid-job
        # (V04_ESSENTIAL_SEMANTICS.md §B).
        self.ensure_column("documents", "is_staging", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("documents", "source_origin", "TEXT NOT NULL DEFAULT 'local'")
        self.ensure_column("documents", "canonical_url", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "final_url", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "fetched_at", "INTEGER")
        self.ensure_column("documents", "http_content_type", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "http_etag", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "http_last_modified", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("documents", "acquisition_metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("documents", "web_revision_current", "INTEGER NOT NULL DEFAULT 1")
        self.ensure_column("crawl_frontier", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("crawl_frontier", "last_attempt_at", "INTEGER")
        self.ensure_column("crawl_frontier", "next_retry_at", "INTEGER")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_web_revision "
            "ON documents(canonical_url, web_revision_current, fetched_at DESC)"
        )
        self.ensure_column("ocr_pages", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        self.ensure_column("artifacts", "scope", "TEXT NOT NULL DEFAULT 'library'")
        self.ensure_column("artifacts", "promoted_at", "INTEGER")
        self.ensure_column("artifacts", "original_format", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("artifacts", "original_color_mode", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("artifacts", "original_pixel_count", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("artifacts", "original_has_alpha", "INTEGER NOT NULL DEFAULT 0")
        self.ensure_column("artifacts", "original_exif_orientation", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("artifacts", "preprocessing_version", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("artifact_analysis_runs", "message_id", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("artifact_analysis_runs", "requested_vision_backend", "TEXT NOT NULL DEFAULT ''")
        self.ensure_column("artifact_analysis_runs", "actual_vision_backend", "TEXT NOT NULL DEFAULT ''")
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
        # This v10 compatibility rewrite intentionally remains value-gated so
        # pre-versioning and already-stamped old profiles retain their behavior.
        old_embedding_model = self.get_setting("embedding_model", None)
        old_embedding_backend = self.get_setting("embedding_backend", None)
        if old_embedding_model == "local-hash-v1" and old_embedding_backend is None:
            self.set_setting("embedding_backend", "auto")
            self.set_setting("embedding_model", "nomic-embed-text")
        self.set_setting_default("default_model", "llama3.2")
        self.set_setting_default("ollama_endpoint", "http://127.0.0.1:11434")
        self.set_setting_default("embedding_backend", "auto")
        self.set_setting_default("embedding_model", "nomic-embed-text")
        self.set_setting_default("vision_backend", "automatic")
        self.set_setting_default("search_max_queries", "4")
        self.set_setting_default("search_results_per_query", "5")
        self.set_setting_default("search_max_fetches", "8")
        self.set_setting_default("search_max_response_bytes", str(2 * 1024 * 1024))
        self.set_setting_default("search_max_concurrent_fetches", "3")
        self.set_setting_default("search_max_passages", "12")
        self.set_setting_default("search_max_dossier_chars", "24000")
        self.set_setting_default("search_max_rounds", "2")
        self.set_setting_default("search_max_model_calls", "5")
        self.set_setting_default("search_second_round_enabled", "true")
        self.set_setting_default("search_timeout_seconds", "90")
        self.set_setting_default("search_mode", "external")
        self.set_setting_default("search_local_max_new_urls", "4")
        self.set_setting_default("search_local_max_total_bytes", str(4 * 1024 * 1024))
        self.set_setting_default("search_local_max_depth", "2")
        self.set_setting_default("search_local_max_urls_per_domain", "3")
        self.set_setting_default("search_local_max_sitemap_entries", "100")
        self.set_setting_default("search_local_max_feed_entries", "50")
        self.set_meta("schema_version", str(SCHEMA_VERSION))
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

    def set_meta(self, key: str, value: str) -> None:
        now = utc_ms()
        self.conn.execute(
            """
            INSERT INTO app_meta(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
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
