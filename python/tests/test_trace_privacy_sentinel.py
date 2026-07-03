from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from PIL import Image

from odysseus_desktop_backend.logging_config import LOGGER_NAME, setup_logging
from odysseus_desktop_backend.progress import PROGRESS_DISCRIMINATOR, progress_operation
from odysseus_desktop_backend.services import campaign_service as campaign_module
from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.campaign_service import CampaignService
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.report_service import ReportService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database, utc_ms


ALLOWLISTED_DB_BODY_FIELDS = {
    ("messages", "content"),
    ("document_pages", "text"),
    ("ocr_pages", "text"),
    ("rag_chunks", "content"),
    ("artifact_derivations", "text_content"),
}


class _CampaignModelService:
    def __init__(self, db: Database):
        self.db = db

    def detect_ollama(self) -> dict[str, Any]:
        return {
            "models": ["llama3.2:1b"],
            "model_details": [
                {
                    "name": "llama3.2:1b",
                    "parameter_size": "1B",
                    "quantization_level": "Q4",
                    "size": 1_000,
                }
            ],
        }

    def ps(self) -> dict[str, Any]:
        return {"models": []}


def _contains_sentinel(value: object, sentinel: str) -> bool:
    if isinstance(value, bytes):
        return sentinel.encode("utf-8") in value
    return sentinel in str(value or "")


def _allowlisted_db_body(table: str, column: str, row: dict[str, Any]) -> str | None:
    if (table, column) not in ALLOWLISTED_DB_BODY_FIELDS:
        return None
    if table == "messages":
        return "user-visible message body" if row.get("role") == "user" else None
    if table in {"document_pages", "ocr_pages"}:
        return "original source/document body"
    if table == "rag_chunks":
        return "RAG source body"
    if table == "artifact_derivations" and row.get("kind") in {
        "ocr_text",
        "combined_evidence",
    }:
        return "artifact OCR source body"
    return None


def _scan_database(db: Database, sentinel: str) -> tuple[list[str], set[str]]:
    leaks: list[str] = []
    allowed_categories: set[str] = set()
    tables = [
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        quoted_table = table.replace('"', '""')
        for sqlite_row in db.conn.execute(f'SELECT * FROM "{quoted_table}"'):
            row = dict(sqlite_row)
            for column, value in row.items():
                if not _contains_sentinel(value, sentinel):
                    continue
                allowed = _allowlisted_db_body(table, column, row)
                if allowed:
                    allowed_categories.add(allowed)
                else:
                    row_id = row.get("id") or row.get("key") or "<compound-row>"
                    leaks.append(f"{table}.{column} row={row_id}")
    return leaks, allowed_categories


def _scan_files(
    root: Path,
    sentinel: str,
    *,
    allowed_paths: set[Path] | None = None,
    skip_database_files: bool = False,
) -> list[str]:
    allowed = {path.resolve() for path in (allowed_paths or set())}
    leaks: list[str] = []
    if not root.exists():
        return leaks
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if skip_database_files and path.name.startswith("app.db"):
            continue
        if sentinel.encode("utf-8") not in path.read_bytes():
            continue
        if path.resolve() not in allowed:
            leaks.append(str(path))
    return leaks


def _flush_profile_logs(profile_dir: Path) -> str:
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers:
        handler.flush()
    return "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in sorted((profile_dir / "logs").glob("*"))
        if path.is_file()
    )


def _close_profile_log_handlers(profile_dir: Path) -> None:
    logger = logging.getLogger(LOGGER_NAME)
    profile_root = profile_dir.resolve()
    for handler in list(logger.handlers):
        base_filename = getattr(handler, "baseFilename", None)
        if not base_filename:
            continue
        try:
            Path(base_filename).resolve().relative_to(profile_root)
        except ValueError:
            continue
        logger.removeHandler(handler)
        handler.close()


def _create_json_report(
    db: Database,
    output_root: Path,
    monkeypatch,
) -> tuple[dict[str, Any], dict[str, Any]]:
    monkeypatch.setattr(campaign_module, "ModelService", _CampaignModelService)
    campaigns = CampaignService(db)
    plan = campaigns.plan(
        {
            "title": "Privacy sentinel campaign",
            "preset": "quick",
            "models": ["llama3.2:1b"],
            "auto_generate_report": False,
        }
    )
    campaign = campaigns.create({"plan": plan})
    job = campaign["jobs"][0]
    run_id = f"run-{uuid.uuid4()}"
    now = utc_ms()
    db.conn.execute(
        """
        INSERT INTO benchmark_runs(
            id, model, verify, suite_name, suite_version, total_passed, total_failed,
            average_latency_ms, total_runtime_ms, notes, created_at, app_version,
            prompt_version, benchmark_mode, thinking_mode, num_predict, status, completed_at
        )
        VALUES (?, 'llama3.2:1b', 0, 'local-rag', 'v0.1.12', 1, 0,
            100, 100, '', ?, '0.2.1', 'rag-benchmark-v0.1.12',
            'end_to_end', 'off', 192, 'completed', ?)
        """,
        (run_id, now, now),
    )
    db.conn.execute(
        """
        UPDATE benchmark_campaigns
        SET status='completed', completed_job_count=1, started_at=?, completed_at=?
        WHERE id=?
        """,
        (now - 100, now, campaign["id"]),
    )
    db.conn.execute(
        """
        UPDATE benchmark_campaign_jobs
        SET status='completed', benchmark_run_ids_json=?, started_at=?, completed_at=?
        WHERE id=?
        """,
        (json.dumps([run_id]), now - 100, now, job["id"]),
    )
    db.conn.commit()

    reports = ReportService(db)
    report_data = reports.build_report_data(campaign["id"])
    report_result = reports.generate_campaign_report(
        campaign["id"],
        output_folder=str(output_root),
        generate_html=False,
        generate_pdf=False,
    )
    return report_data, report_result


def test_private_sentinel_is_confined_to_explicit_user_visible_storage(
    tmp_path: Path,
    capfd,
    monkeypatch,
) -> None:
    sentinel = "PRIVATE_SENTINEL_DO_NOT_LEAK_7F3A9C21"
    profile_dir = tmp_path / "profile"
    setup_logging(profile_dir)
    db = Database(profile_dir)
    try:
        db.set_setting("embedding_backend", "lexical")
        documents = DocumentService(db)
        embeddings = EmbeddingService(db)
        rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
        artifacts = ArtifactService(db, documents, rag)
        sessions = SessionService(db)

        session = sessions.create(title="Privacy sentinel session")
        user_message = sessions.add_message(
            session["id"],
            "user",
            f"User-visible private message body: {sentinel}",
        )

        source_path = tmp_path / "private-note.txt"
        source_path.write_text(
            f"User-owned source body containing {sentinel}.\n"
            "This second sentence makes the source useful for deterministic retrieval.",
            encoding="utf-8",
        )

        image_path = tmp_path / "private-image.png"
        Image.new("RGB", (32, 24), "white").save(image_path, format="PNG")

        with progress_operation(
            operation_id=sentinel,
            session_id=session["id"],
            message_id=user_message["id"],
        ) as emitter:
            emitter.emit(sentinel, detail=sentinel)
            document = documents.import_document(str(source_path), scope="session")
            emitter.bind(source_id=document["id"])
            indexed = rag.index_document(document["id"])
            search_results = rag.search(sentinel, document_ids=[document["id"]])

            artifact = artifacts.import_path(str(image_path), scope="session")
            emitter.bind(artifact_id=artifact["id"])
            ocr_derivation = artifacts.insert_text_derivation(
                artifact["id"],
                "ocr_text",
                f"User-visible OCR source body: {sentinel}",
                producer_type="ocr",
                producer_name="privacy-sentinel-fixture",
                metadata={"fixture": "privacy-sentinel"},
            )

        assert indexed["chunks"]
        assert search_results
        assert ocr_derivation["text_content"].endswith(sentinel)

        chat = ChatService(sessions, SettingsService(db), ModelService(db))
        trace = chat.build_operation_trace(
            model_response={
                "model": "llama3.2:latest",
                "content": sentinel,
                "thinking": sentinel,
                "done_reason": "stop",
                "prompt_eval_count": 12,
                "eval_count": 4,
                "raw": {"prompt": sentinel, "response": sentinel},
            },
            analysis={
                "actual_vision_backend": "ocr_only",
                "ocr_engine": "privacy-sentinel-fixture",
                "output": {"ocr_text": sentinel, "private_diagnostic": sentinel},
                "evidence": {"ocr": {"text": sentinel}},
                "timings": {"elapsed_ms": 3},
                "warnings": [],
            },
            grounding={
                "verifier": {"enabled": False, "status": "not_run"},
                "unsupported_claims": [sentinel],
            },
            timings={"answer_latency_ms": 4},
            rag_enabled=True,
            verifier_enabled=False,
            retrieved_snippets=[
                {
                    "chunk_id": result["chunk_id"],
                    "document_id": result["document_id"],
                    "text": result["content"],
                    "page_start": result.get("page_start"),
                    "page_end": result.get("page_end"),
                }
                for result in search_results
            ],
            selected_model="llama3.2:latest",
            selected_rag_preset="standard",
            selected_thinking_mode="off",
            answer_style="precise",
            embedding_model="local-hash-v1",
            embedding_backend="lexical",
        )
        assert sentinel not in json.dumps(trace, sort_keys=True)
        sessions.add_message(
            session["id"],
            "assistant",
            "Safe assistant answer.",
            metadata={"operation_trace": trace},
        )

        report_data, report_result = _create_json_report(
            db,
            tmp_path / "reports",
            monkeypatch,
        )
        assert sentinel not in json.dumps(report_data, sort_keys=True)
        assert report_result["status"] in {"completed", "completed_with_warnings"}
        report_root = Path(report_result["paths"]["json"]).parent
        assert _scan_files(report_root, sentinel) == []

        stderr = capfd.readouterr().err
        progress_events = [
            json.loads(line)
            for line in stderr.splitlines()
            if PROGRESS_DISCRIMINATOR in line
        ]
        assert progress_events
        assert sentinel not in json.dumps(progress_events, sort_keys=True)
        assert all(event["detail"] is None for event in progress_events)
        assert sentinel not in stderr

        log_text = _flush_profile_logs(profile_dir)
        assert sentinel not in log_text

        db_leaks, allowed_categories = _scan_database(db, sentinel)
        assert db_leaks == []
        assert allowed_categories == {
            "user-visible message body",
            "original source/document body",
            "RAG source body",
            "artifact OCR source body",
        }

        stored_document = Path(document["stored_path"])
        assert sentinel in source_path.read_text(encoding="utf-8")
        assert sentinel in stored_document.read_text(encoding="utf-8")
        assert _scan_files(
            profile_dir,
            sentinel,
            allowed_paths={stored_document},
            skip_database_files=True,
        ) == []
    finally:
        db.close()
        _close_profile_log_handlers(profile_dir)
