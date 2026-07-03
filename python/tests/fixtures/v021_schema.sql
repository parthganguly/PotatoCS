-- Generated from git tag v0.2.1 by instantiating its historical Database class.
BEGIN TRANSACTION;
CREATE TABLE app_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
INSERT INTO "app_meta" VALUES('schema_version','10',1783062633163);
CREATE TABLE artifact_analysis_runs (
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
CREATE TABLE artifact_derivations (
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
CREATE TABLE artifact_rag_documents (
                artifact_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                derivation_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (artifact_id, document_id),
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE,
                FOREIGN KEY (derivation_id) REFERENCES artifact_derivations(id) ON DELETE CASCADE
            );
CREATE TABLE artifacts (
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
            , scope TEXT NOT NULL DEFAULT 'library', promoted_at INTEGER, original_format TEXT NOT NULL DEFAULT '', original_color_mode TEXT NOT NULL DEFAULT '', original_pixel_count INTEGER NOT NULL DEFAULT 0, original_has_alpha INTEGER NOT NULL DEFAULT 0, original_exif_orientation TEXT NOT NULL DEFAULT '', preprocessing_version TEXT NOT NULL DEFAULT '');
CREATE TABLE benchmark_campaign_jobs (
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
CREATE TABLE benchmark_campaigns (
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
CREATE TABLE benchmark_case_results (
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
                created_at INTEGER NOT NULL, retrieved_document_ids_json TEXT NOT NULL DEFAULT '[]', retrieved_chunk_ids_json TEXT NOT NULL DEFAULT '[]', embedding_backend TEXT NOT NULL DEFAULT '', embedding_model TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0, case_category TEXT NOT NULL DEFAULT '', case_difficulty TEXT NOT NULL DEFAULT '', benchmark_mode TEXT NOT NULL DEFAULT 'end_to_end', thinking_mode TEXT NOT NULL DEFAULT 'legacy/unrecorded', repeat_index INTEGER NOT NULL DEFAULT 1, status TEXT NOT NULL DEFAULT 'completed', stage TEXT NOT NULL DEFAULT '', pipeline_diagnosis TEXT NOT NULL DEFAULT '', counts_toward_primary INTEGER NOT NULL DEFAULT 1, grader_review_required INTEGER NOT NULL DEFAULT 0, answer_content TEXT NOT NULL DEFAULT '', thinking_text TEXT NOT NULL DEFAULT '', thinking_returned INTEGER NOT NULL DEFAULT 0, thinking_char_count INTEGER NOT NULL DEFAULT 0, prompt_text TEXT NOT NULL DEFAULT '', corrected_answer TEXT NOT NULL DEFAULT '', model_response_json TEXT NOT NULL DEFAULT '{}', retrieval_metrics_json TEXT NOT NULL DEFAULT '{}', retrieval_candidates_json TEXT NOT NULL DEFAULT '[]', supplied_evidence_json TEXT NOT NULL DEFAULT '[]', grader_matches_json TEXT NOT NULL DEFAULT '[]', timings_json TEXT NOT NULL DEFAULT '{}', error_message TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (run_id) REFERENCES benchmark_runs(id) ON DELETE CASCADE
            );
CREATE TABLE benchmark_runs (
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
            , embedding_backend TEXT NOT NULL DEFAULT '', embedding_model TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0, app_version TEXT NOT NULL DEFAULT '', prompt_version TEXT NOT NULL DEFAULT '', benchmark_mode TEXT NOT NULL DEFAULT 'end_to_end', thinking_mode TEXT NOT NULL DEFAULT 'legacy/unrecorded', answer_style TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'completed', repeat_count INTEGER NOT NULL DEFAULT 1, num_predict INTEGER NOT NULL DEFAULT 0, timeout_policy_json TEXT NOT NULL DEFAULT '{}', model_info_json TEXT NOT NULL DEFAULT '{}', retrieval_score_json TEXT NOT NULL DEFAULT '{}', oracle_score_json TEXT NOT NULL DEFAULT '{}', end_to_end_score_json TEXT NOT NULL DEFAULT '{}', practical_score_json TEXT NOT NULL DEFAULT '{}', adversarial_score_json TEXT NOT NULL DEFAULT '{}', timeout_count INTEGER NOT NULL DEFAULT 0, runtime_error_count INTEGER NOT NULL DEFAULT 0, grader_review_count INTEGER NOT NULL DEFAULT 0, completed_at INTEGER);
CREATE TABLE conversation_attachments (
                session_id TEXT NOT NULL,
                backend_kind TEXT NOT NULL CHECK (backend_kind IN ('document', 'artifact')),
                source_id TEXT NOT NULL,
                added_message_id TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                removed_at INTEGER,
                PRIMARY KEY (session_id, backend_kind, source_id),
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
CREATE TABLE document_pages (
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
CREATE TABLE documents (
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
            , ocr_status TEXT NOT NULL DEFAULT 'not_needed', ocr_engine TEXT NOT NULL DEFAULT '', ocr_error TEXT NOT NULL DEFAULT '', indexed_embedding_model TEXT NOT NULL DEFAULT '', indexed_embedding_backend TEXT NOT NULL DEFAULT '', is_internal INTEGER NOT NULL DEFAULT 0, source_artifact_id TEXT NOT NULL DEFAULT '', generated_source_label TEXT NOT NULL DEFAULT '', scope TEXT NOT NULL DEFAULT 'library', promoted_at INTEGER);
CREATE TABLE embedding_cache (
                content_hash TEXT NOT NULL,
                embedding_model TEXT NOT NULL,
                vector_blob BLOB NOT NULL,
                dimensions INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                last_used_at INTEGER NOT NULL,
                PRIMARY KEY (content_hash, embedding_model)
            );
CREATE TABLE message_artifacts (
                message_id TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                PRIMARY KEY (message_id, artifact_id),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
            );
CREATE TABLE message_documents (
                message_id TEXT NOT NULL,
                document_id TEXT NOT NULL,
                display_order INTEGER NOT NULL DEFAULT 0,
                scope_at_link TEXT NOT NULL DEFAULT 'session',
                created_at INTEGER NOT NULL,
                PRIMARY KEY (message_id, document_id),
                FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE RESTRICT
            );
CREATE TABLE messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant')),
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
CREATE TABLE model_capabilities (
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
CREATE TABLE multimodal_eval_case_results (
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
CREATE TABLE multimodal_eval_runs (
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
CREATE TABLE ocr_pages (
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
                updated_at INTEGER NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY (document_id) REFERENCES documents(id) ON DELETE CASCADE
            );
CREATE TABLE rag_chunks (
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
CREATE TABLE runtime_status (
                name TEXT PRIMARY KEY,
                reachable INTEGER NOT NULL DEFAULT 0,
                installed INTEGER NOT NULL DEFAULT 0,
                endpoint TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                models_json TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                updated_at INTEGER NOT NULL
            );
CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                last_message_at INTEGER
            );
CREATE TABLE settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
INSERT INTO "settings" VALUES('default_model','"llama3.2"',1783062633163);
INSERT INTO "settings" VALUES('ollama_endpoint','"http://127.0.0.1:11434"',1783062633167);
INSERT INTO "settings" VALUES('embedding_backend','"auto"',1783062633170);
INSERT INTO "settings" VALUES('embedding_model','"nomic-embed-text"',1783062633173);
INSERT INTO "settings" VALUES('vision_backend','"automatic"',1783062633193);
CREATE INDEX idx_messages_session_time
                ON messages(session_id, created_at);
CREATE INDEX idx_documents_status
                ON documents(is_deleted, index_status, updated_at);
CREATE INDEX idx_document_pages_document
                ON document_pages(document_id, page_number);
CREATE INDEX idx_ocr_pages_document
                ON ocr_pages(document_id, page_number);
CREATE UNIQUE INDEX idx_rag_chunks_document_index
                ON rag_chunks(document_id, chunk_index);
CREATE INDEX idx_rag_chunks_search
                ON rag_chunks(is_deleted, embedding_model, document_id);
CREATE INDEX idx_artifacts_status
                ON artifacts(kind, is_deleted, updated_at DESC);
CREATE INDEX idx_artifact_derivations_artifact
                ON artifact_derivations(artifact_id, kind, created_at DESC);
CREATE INDEX idx_message_artifacts_artifact
                ON message_artifacts(artifact_id);
CREATE INDEX idx_message_documents_document
                ON message_documents(document_id);
CREATE INDEX idx_conversation_attachments_active
                ON conversation_attachments(session_id, removed_at, created_at);
CREATE INDEX idx_artifact_analysis_artifact
                ON artifact_analysis_runs(artifact_id, created_at DESC);
CREATE INDEX idx_multimodal_eval_runs_created
                ON multimodal_eval_runs(created_at DESC);
CREATE INDEX idx_multimodal_eval_cases_run
                ON multimodal_eval_case_results(run_id, case_id);
CREATE INDEX idx_benchmark_runs_created
                ON benchmark_runs(created_at DESC);
CREATE INDEX idx_benchmark_case_results_run
                ON benchmark_case_results(run_id, case_id);
CREATE INDEX idx_benchmark_campaigns_created
                ON benchmark_campaigns(created_at DESC);
CREATE UNIQUE INDEX idx_benchmark_campaign_jobs_sequence
                ON benchmark_campaign_jobs(campaign_id, sequence);
CREATE INDEX idx_benchmark_campaign_jobs_status
                ON benchmark_campaign_jobs(campaign_id, status, sequence);
COMMIT;
