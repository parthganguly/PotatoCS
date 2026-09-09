from __future__ import annotations

import json
import os
import sys
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from odysseus_desktop_backend import __version__
from odysseus_desktop_backend.logging_config import get_logger, setup_logging
from odysseus_desktop_backend.progress import progress_operation
from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.campaign_service import CampaignService
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.eval_service import EvalService
from odysseus_desktop_backend.services.florence2_service import Florence2Backend
from odysseus_desktop_backend.services.image_eval_service import ImageEvalService
from odysseus_desktop_backend.services.job_service import JobService
from odysseus_desktop_backend.services.legacy_import_service import LegacyImportService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.ocr_service import LocalVLMTextExtractor, OCRService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.source_service import SourceService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.storage_service import StorageService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.services.vision_service import VisionService
from odysseus_desktop_backend.storage import Database


JsonDict = dict[str, Any]
logger = get_logger("rpc")


def sidecar_launch_info(profile_dir: str | Path) -> JsonDict:
    return {
        "profile_dir": str(Path(profile_dir)),
        "profile_dir_env": os.environ.get("ODYSSEUS_PROFILE_DIR", ""),
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "resource_dir": os.environ.get("ODYSSEUS_RESOURCE_DIR", ""),
        "dev_repo_root": os.environ.get("ODYSSEUS_DEV_REPO_ROOT", ""),
        "app_data_dir": os.environ.get("ODYSSEUS_APP_DATA_DIR", ""),
        "florence_model_dir_env": os.environ.get("ODYSSEUS_FLORENCE_MODEL_DIR", ""),
        "florence_model_dir_env_state": "set" if os.environ.get("ODYSSEUS_FLORENCE_MODEL_DIR") else "unset",
        "copied_backend_dir": str(Path(__file__).resolve().parent),
    }


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class SidecarApp:
    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self.launch_info = sidecar_launch_info(self.profile_dir)
        logger.info(
            "backend startup profile_dir=%s python_executable=%s cwd=%s resource_dir=%s dev_repo_root=%s app_data_dir=%s florence_model_dir_env=%s copied_backend_dir=%s",
            self.profile_dir,
            self.launch_info["python_executable"],
            self.launch_info["cwd"],
            self.launch_info["resource_dir"],
            self.launch_info["dev_repo_root"] or "unset",
            self.launch_info["app_data_dir"] or "unset",
            self.launch_info["florence_model_dir_env_state"],
            self.launch_info["copied_backend_dir"],
        )
        self.db = Database(self.profile_dir)
        self.settings = SettingsService(self.db)
        self.sessions = SessionService(self.db)
        self.models = ModelService(self.db)
        self.documents = DocumentService(self.db)
        self.embeddings = EmbeddingService(self.db)
        self.vector_store = SQLiteNumPyVectorStore(self.db)
        self.rag = RAGService(self.documents, self.embeddings, self.vector_store)
        self.ocr = OCRService(self.documents, self.rag)
        self.artifacts = ArtifactService(self.db, self.documents, self.rag)
        self.sources = SourceService(self.documents, self.artifacts, self.rag, ocr=self.ocr)
        self.florence = Florence2Backend(
            self.profile_dir,
            resource_dir=self.launch_info.get("resource_dir") or None,
            dev_repo_root=self.launch_info.get("dev_repo_root") or None,
            app_data_dir=self.launch_info.get("app_data_dir") or None,
        )
        self.ocr.set_vlm_text_extractor(LocalVLMTextExtractor(self.models, self.florence))
        self.vision = VisionService(self.artifacts, self.ocr, self.models, florence=self.florence)
        self.legacy_import = LegacyImportService(self.documents, self.rag, self.sessions, self.settings)
        self.chat = ChatService(
            self.sessions,
            self.settings,
            self.models,
            rag=self.rag,
            artifacts=self.artifacts,
            documents=self.documents,
            sources=self.sources,
            vision=self.vision,
            ocr=self.ocr,
        )
        self.evals = EvalService(self.db)
        self.image_evals = ImageEvalService(self.db, self.artifacts, self.vision)
        recovered_analyses = self.artifacts.recover_interrupted_analysis_runs()
        if recovered_analyses:
            logger.warning("recovered interrupted artifact analysis runs count=%s", recovered_analyses)
        recovered_runs = self.evals.recover_interrupted_runs(
            reason="Benchmark was interrupted because the Python sidecar stopped before returning a result."
        )
        if recovered_runs:
            logger.warning("recovered interrupted benchmark runs count=%s", recovered_runs)
        self.campaigns = CampaignService(self.db)
        recovered_campaigns = self.campaigns.recover_interrupted_campaigns()
        if recovered_campaigns:
            logger.warning("recovered interrupted benchmark campaigns count=%s", recovered_campaigns)
        repaired = self.documents.repair_startup_state()
        if repaired["purged_staging"] or repaired["ocr_reset"]:
            logger.warning(
                "repaired startup document state purged_staging=%s ocr_reset=%s",
                repaired["purged_staging"],
                repaired["ocr_reset"],
            )
        self.jobs = JobService(self.profile_dir)
        self.storage = StorageService(
            self.db,
            self.documents,
            self.artifacts,
            has_active_jobs=self.jobs.has_active_jobs,
        )
        self.shutdown_requested = False
        self.methods: dict[str, Callable[[JsonDict], Any]] = {
            "health.ping": self.health_ping,
            "diagnostics.get": self.diagnostics_get,
            "settings.get": self.settings_get,
            "settings.set": self.settings_set,
            "sessions.list": self.sessions_list,
            "sessions.create": self.sessions_create,
            "sessions.update": self.sessions_update,
            "sessions.delete": self.sessions_delete,
            "sessions.messages": self.sessions_messages,
            "chat.send": self.chat_send,
            "models.detect_ollama": self.models_detect_ollama,
            "models.list": self.models_list,
            "models.capabilities": self.models_capabilities,
            "models.inspect": self.models_inspect,
            "models.refresh_capabilities": self.models_refresh_capabilities,
            "florence.verify_pack": self.florence_verify_pack,
            "florence.unload": self.florence_unload,
            "florence.smoke_test": self.florence_smoke_test,
            "sources.list": self.sources_list,
            "sources.import": self.sources_import,
            "sources.promote": self.sources_promote,
            "sources.delete": self.sources_delete,
            "sources.conversation_context": self.sources_conversation_context,
            "sources.remove_from_conversation": self.sources_remove_from_conversation,
            "artifacts.list": self.artifacts_list,
            "artifacts.get": self.artifacts_get,
            "artifacts.import": self.artifacts_import,
            "artifacts.derivations": self.artifacts_derivations,
            "artifacts.crop": self.artifacts_crop,
            "artifacts.analyze": self.artifacts_analyze,
            "artifacts.analysis": self.artifacts_analysis,
            "artifacts.analysis_by_request": self.artifacts_analysis_by_request,
            "artifacts.index": self.artifacts_index,
            "artifacts.unindex": self.artifacts_unindex,
            "artifacts.delete": self.artifacts_delete,
            "documents.list": self.documents_list,
            "documents.import": self.documents_import,
            "documents.delete": self.documents_delete,
            "documents.reindex": self.documents_reindex,
            "documents.chunks": self.documents_chunks,
            "documents.ocr": self.documents_ocr,
            "documents.ocr_pages": self.documents_ocr_pages,
            "ocr.status": self.ocr_status,
            "jobs.submit_import": self.jobs_submit_import,
            "jobs.get": self.jobs_get,
            "jobs.list": self.jobs_list,
            "jobs.cancel": self.jobs_cancel,
            "storage.status": self.storage_status,
            "storage.cleanup_preview": self.storage_cleanup_preview,
            "storage.cleanup": self.storage_cleanup,
            "legacy.import": self.legacy_import_folder,
            "rag.search": self.rag_search,
            "rag.health": self.rag_health,
            "evals.list": self.evals_list,
            "evals.run": self.evals_run,
            "evals.history": self.evals_history,
            "evals.comparison": self.evals_comparison,
            "evals.clear_history": self.evals_clear_history,
            "image_evals.list": self.image_evals_list,
            "image_evals.run": self.image_evals_run,
            "image_evals.history": self.image_evals_history,
            "campaigns.models": self.campaigns_models,
            "campaigns.plan": self.campaigns_plan,
            "campaigns.create": self.campaigns_create,
            "campaigns.list": self.campaigns_list,
            "campaigns.get": self.campaigns_get,
            "campaigns.start": self.campaigns_start,
            "campaigns.pause": self.campaigns_pause,
            "campaigns.cancel": self.campaigns_cancel,
            "campaigns.resume": self.campaigns_resume,
            "campaigns.retry_job": self.campaigns_retry_job,
            "campaigns.delete": self.campaigns_delete,
            "campaigns.report_data": self.campaigns_report_data,
            "campaigns.generate_report": self.campaigns_generate_report,
            "campaigns.open_report_folder": self.campaigns_open_report_folder,
            "app.shutdown": self.app_shutdown,
        }

    def close(self) -> None:
        logger.info("backend shutdown profile_dir=%s", self.profile_dir)
        self.jobs.shutdown()
        self.db.close()

    def dispatch(self, method: str, params: JsonDict | None) -> Any:
        handler = self.methods.get(method)
        if handler is None:
            raise RpcError(-32601, f"method not found: {method}")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            raise RpcError(-32602, "params must be an object")
        return handler(params)

    def health_ping(self, _params: JsonDict) -> JsonDict:
        return {
            "ok": True,
            "version": __version__,
            "profile_dir": str(self.profile_dir),
            "db_path": str(self.db.path),
            "python_executable": sys.executable,
            "sidecar": self.launch_info,
        }

    def diagnostics_get(self, _params: JsonDict) -> JsonDict:
        settings = self.settings.get()
        return {
            "app_version": __version__,
            "profile_dir": str(self.profile_dir),
            "backend_ready": True,
            "db_path": str(self.db.path),
            "backend_log_path": str(self.profile_dir / "logs" / "backend.log"),
            "python_executable": sys.executable,
            "sidecar": self.launch_info,
            "current_model": str(settings.get("default_model") or "llama3.2"),
            "settings": settings,
            "ollama": self.models.detect_ollama(),
            "ollama_ps": self.models.ps(),
            "ocr": self.ocr.status(),
            "rag": self.rag.health(),
            "image_vision": {
                "artifacts": self.artifacts.diagnostics(),
                "model_capabilities": self.models.capabilities(refresh=False),
                "florence": self.florence.diagnostics(),
                "capture": {
                    "full_screen": sys.platform.startswith("win"),
                    "window": False,
                    "region": sys.platform.startswith("win"),
                    "message": "Window capture is reported as unsupported until a reliable Windows window picker is implemented.",
                },
                "sources": {
                    "library_count": len(self.sources.list(scope="library")),
                    "session_count": len(self.sources.list(scope="session")),
                },
            },
        }

    def settings_get(self, _params: JsonDict) -> JsonDict:
        return self.settings.get()

    def settings_set(self, params: JsonDict) -> JsonDict:
        values = params.get("values", params)
        return self.settings.set(values)

    def sessions_list(self, _params: JsonDict) -> list[JsonDict]:
        return self.sessions.list()

    def sessions_create(self, params: JsonDict) -> JsonDict:
        return self.sessions.create(
            title=params.get("title"),
            model=params.get("model"),
        )

    def sessions_update(self, params: JsonDict) -> JsonDict:
        session_id = require_str(params, "session_id")
        updates = params.get("updates") or {}
        if not isinstance(updates, dict):
            raise RpcError(-32602, "updates must be an object")
        return self.sessions.update(session_id, updates)

    def sessions_delete(self, params: JsonDict) -> JsonDict:
        session_id = require_str(params, "session_id")
        return self.sessions.delete(session_id)

    def sessions_messages(self, params: JsonDict) -> list[JsonDict]:
        session_id = require_str(params, "session_id")
        messages = self.documents.hydrate_messages(self.sessions.messages(session_id))
        return self.artifacts.hydrate_messages(messages)

    def chat_send(self, params: JsonDict) -> JsonDict:
        artifact_ids = optional_str_list(params, "artifact_ids")
        operation_id = optional_str(params, "analysis_request_id") or f"chat-{uuid.uuid4()}"
        with progress_operation(
            operation_id=operation_id,
            session_id=optional_str(params, "session_id"),
            artifact_id=artifact_ids[0] if artifact_ids else None,
        ):
            return self.chat.send(
                message=str(params.get("message") or ""),
                session_id=optional_str(params, "session_id"),
                model=optional_str(params, "model"),
                use_rag=optional_bool(params, "use_rag", False),
                document_ids=optional_str_list(params, "document_ids"),
                verify_rag=optional_bool(params, "verify_rag", False),
                answer_style=optional_str(params, "answer_style"),
                temperature=optional_float(params, "temperature", 0.0),
                rag_preset=optional_str(params, "rag_preset"),
                thinking_mode=optional_str(params, "thinking_mode") or "auto",
                attachment_document_ids=optional_str_list(params, "attachment_document_ids"),
                artifact_ids=artifact_ids,
                multimodal_mode=optional_str(params, "multimodal_mode") or "automatic",
                vision_model=optional_str(params, "vision_model") or "",
                vision_backend=optional_str(params, "vision_backend") or "",
                analysis_request_id=optional_str(params, "analysis_request_id") or "",
                crop_derivation_id=optional_str(params, "crop_derivation_id") or "",
            )

    def models_detect_ollama(self, _params: JsonDict) -> JsonDict:
        return self.models.detect_ollama()

    def models_list(self, _params: JsonDict) -> JsonDict:
        return self.models.detect_ollama()

    def models_capabilities(self, params: JsonDict) -> list[JsonDict]:
        return self.models.capabilities(refresh=optional_bool(params, "refresh", False))

    def models_inspect(self, params: JsonDict) -> JsonDict:
        return self.models.inspect(require_str(params, "model"))

    def models_refresh_capabilities(self, _params: JsonDict) -> list[JsonDict]:
        return self.models.refresh_capabilities()

    def florence_verify_pack(self, _params: JsonDict) -> JsonDict:
        return self.florence.verify_pack()

    def florence_unload(self, _params: JsonDict) -> JsonDict:
        return self.florence.unload()

    def florence_smoke_test(self, params: JsonDict) -> JsonDict:
        return self.florence.smoke_test(optional_str(params, "image_path"))

    def sources_list(self, params: JsonDict) -> list[JsonDict]:
        scope = optional_str(params, "scope")
        return self.sources.list(
            scope=scope if scope else "library",
            include_session=optional_bool(params, "include_session", False),
            include_deleted=optional_bool(params, "include_deleted", False),
            filter_kind=optional_str(params, "filter") or "all",
        )

    def sources_import(self, params: JsonDict) -> JsonDict:
        paths = params.get("paths")
        scope = optional_str(params, "scope") or "library"
        index = optional_bool(params, "index", True)
        source_kind = optional_str(params, "source_kind") or "file"
        with progress_operation(operation_id=f"source-import-{uuid.uuid4()}"):
            if isinstance(paths, list):
                return self.sources.import_many([str(path) for path in paths], scope=scope, index=index, source_kind=source_kind)
            return self.sources.import_path(require_str(params, "path"), scope=scope, index=index, source_kind=source_kind)

    def sources_promote(self, params: JsonDict) -> JsonDict:
        return self.sources.promote(
            require_str(params, "backend_kind"),
            require_str(params, "source_id"),
            index=optional_bool(params, "index", False),
            derivation_id=optional_str(params, "derivation_id") or "",
        )

    def sources_delete(self, params: JsonDict) -> JsonDict:
        backend_kind = require_str(params, "backend_kind")
        source_id = require_str(params, "source_id")
        self._release_source_for_delete(backend_kind, source_id)
        return self.sources.delete(backend_kind, source_id)

    def _release_source_for_delete(self, backend_kind: str, source_id: str) -> None:
        """Cancel-first bounded wait; fixed 'source_busy' failure on timeout.

        Deletion never runs concurrently with a job that owns the source
        (V04_ESSENTIAL_SEMANTICS.md §D; V04_STORAGE_CLEANUP_DESIGN.md §8).
        """
        released = self.jobs.release_source(
            document_id=source_id if backend_kind == "document" else "",
            artifact_id=source_id if backend_kind == "artifact" else "",
        )
        if not released:
            raise RpcError(-32051, "source_busy")

    def sources_conversation_context(self, params: JsonDict) -> list[JsonDict]:
        return self.sources.conversation_context(require_str(params, "session_id"))

    def sources_remove_from_conversation(self, params: JsonDict) -> list[JsonDict]:
        return self.sources.remove_from_conversation(
            require_str(params, "session_id"),
            require_str(params, "backend_kind"),
            require_str(params, "source_id"),
        )

    def artifacts_list(self, params: JsonDict) -> list[JsonDict]:
        return self.artifacts.list(include_deleted=optional_bool(params, "include_deleted", False))

    def artifacts_get(self, params: JsonDict) -> JsonDict:
        return self.artifacts.get(require_str(params, "artifact_id"))

    def artifacts_import(self, params: JsonDict) -> JsonDict:
        paths = params.get("paths")
        if isinstance(paths, list):
            return self.artifacts.import_many(
                [str(path) for path in paths],
                source_kind=optional_str(params, "source_kind") or "file",
                scope=optional_str(params, "scope") or "library",
            )
        return self.artifacts.import_path(
            require_str(params, "path"),
            source_kind=optional_str(params, "source_kind") or "file",
            name=optional_str(params, "name"),
            scope=optional_str(params, "scope") or "library",
        )

    def artifacts_derivations(self, params: JsonDict) -> list[JsonDict]:
        return self.artifacts.derivations(require_str(params, "artifact_id"))

    def artifacts_crop(self, params: JsonDict) -> JsonDict:
        crop = params.get("crop") or {}
        if not isinstance(crop, dict):
            raise RpcError(-32602, "crop must be an object")
        return self.artifacts.create_crop(require_str(params, "artifact_id"), crop)

    def artifacts_analyze(self, params: JsonDict) -> JsonDict:
        artifact_id = require_str(params, "artifact_id")
        request_id = optional_str(params, "request_id") or f"artifact-analysis-{uuid.uuid4()}"
        with progress_operation(operation_id=request_id, artifact_id=artifact_id):
            return self.vision.analyze(
                artifact_id,
                mode=require_str(params, "mode"),
                question=optional_str(params, "question") or "",
                vision_model=optional_str(params, "vision_model") or "",
                vision_backend=optional_str(params, "vision_backend") or "",
                request_id=request_id,
                crop_derivation_id=optional_str(params, "crop_derivation_id") or "",
            )

    def artifacts_analysis(self, params: JsonDict) -> JsonDict:
        return self.artifacts.analysis(require_str(params, "analysis_id"))

    def artifacts_analysis_by_request(self, params: JsonDict) -> JsonDict:
        analysis = self.artifacts.analysis_by_request(require_str(params, "request_id"))
        return analysis or {}

    def artifacts_index(self, params: JsonDict) -> JsonDict:
        return self.artifacts.index_derivation(
            require_str(params, "artifact_id"),
            require_str(params, "derivation_id"),
        )

    def artifacts_unindex(self, params: JsonDict) -> JsonDict:
        return self.artifacts.unindex(require_str(params, "artifact_id"))

    def artifacts_delete(self, params: JsonDict) -> JsonDict:
        return self.artifacts.delete(require_str(params, "artifact_id"))

    def documents_list(self, _params: JsonDict) -> list[JsonDict]:
        return self.documents.list()

    def documents_import(self, params: JsonDict) -> JsonDict:
        with progress_operation(operation_id=f"document-import-{uuid.uuid4()}"):
            document = self.documents.import_document(require_str(params, "path"), scope=optional_str(params, "scope") or "library")
            if params.get("index", True) is False:
                return {"document": document, "index": None}
            try:
                indexed = self.sources.index_document(document["id"])
            except Exception as exc:
                self.documents.mark_error(document["id"], f"Indexing failed: {exc}")
                logger.warning("document indexing failed document_id=%s error=%s", document["id"], exc)
                raise RuntimeError(f"Indexing failed: {exc}") from exc
            return {"document": indexed["document"], "index": indexed}

    def documents_delete(self, params: JsonDict) -> JsonDict:
        document_id = require_str(params, "document_id")
        self._release_source_for_delete("document", document_id)
        return self.rag.delete_document(document_id)

    def documents_reindex(self, params: JsonDict) -> JsonDict:
        return self.sources.index_document(require_str(params, "document_id"))

    def documents_chunks(self, params: JsonDict) -> list[JsonDict]:
        return self.documents.chunks(require_str(params, "document_id"))

    def documents_ocr(self, params: JsonDict) -> JsonDict:
        document_id = require_str(params, "document_id")
        with progress_operation(operation_id=f"document-ocr-{uuid.uuid4()}", source_id=document_id):
            return self.ocr.run_document_ocr(document_id)

    def documents_ocr_pages(self, params: JsonDict) -> list[JsonDict]:
        return self.documents.ocr_pages(require_str(params, "document_id"))

    def ocr_status(self, _params: JsonDict) -> JsonDict:
        return self.ocr.status()

    def jobs_submit_import(self, params: JsonDict) -> JsonDict:
        paths = params.get("paths")
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths):
            raise RpcError(-32602, "paths must be a list of strings")
        scope = optional_str(params, "scope") or "library"
        return {"jobs": self.jobs.submit_import(paths, scope=scope)}

    def jobs_get(self, params: JsonDict) -> JsonDict:
        return self.jobs.get(require_str(params, "job_id"))

    def jobs_list(self, _params: JsonDict) -> list[JsonDict]:
        return self.jobs.list()

    def jobs_cancel(self, params: JsonDict) -> JsonDict:
        return self.jobs.cancel(require_str(params, "job_id"))

    def storage_status(self, _params: JsonDict) -> JsonDict:
        return self.storage.status()

    def storage_cleanup_preview(self, _params: JsonDict) -> JsonDict:
        return self.storage.cleanup_preview()

    def storage_cleanup(self, _params: JsonDict) -> JsonDict:
        return self.storage.cleanup()

    def legacy_import_folder(self, params: JsonDict) -> JsonDict:
        return self.legacy_import.import_folder(require_str(params, "folder"))

    def rag_search(self, params: JsonDict) -> list[JsonDict]:
        metadata_filter = params.get("metadata_filter")
        if metadata_filter is not None and not isinstance(metadata_filter, dict):
            raise RpcError(-32602, "metadata_filter must be an object")
        with progress_operation(operation_id=f"rag-search-{uuid.uuid4()}"):
            return self.rag.search(
                require_str(params, "query"),
                limit=optional_int(params, "limit", 5),
                metadata_filter=metadata_filter,
                document_ids=optional_str_list(params, "document_ids"),
            )

    def rag_health(self, _params: JsonDict) -> JsonDict:
        return self.rag.health()

    def evals_list(self, _params: JsonDict) -> JsonDict:
        return self.evals.list_cases()

    def evals_run(self, params: JsonDict) -> JsonDict:
        return self.evals.run(
            model=require_str(params, "model"),
            verify=optional_bool(params, "verify", False),
            answer_style_override=optional_str(params, "answer_style"),
            temperature=optional_float(params, "temperature", 0.0),
            benchmark_mode=optional_str(params, "benchmark_mode") or "end_to_end",
            thinking_mode=optional_str(params, "thinking_mode") or "off",
            repeats=optional_int(params, "repeats", 1),
            num_predict=optional_int(params, "num_predict", 0),
        )

    def evals_history(self, params: JsonDict) -> list[JsonDict]:
        return self.evals.history(limit=optional_int(params, "limit", 20))

    def evals_comparison(self, params: JsonDict) -> JsonDict:
        return self.evals.comparison(limit=optional_int(params, "limit", 100))

    def evals_clear_history(self, _params: JsonDict) -> JsonDict:
        return self.evals.clear_history()

    def image_evals_list(self, _params: JsonDict) -> JsonDict:
        return self.image_evals.list_cases()

    def image_evals_run(self, params: JsonDict) -> JsonDict:
        return self.image_evals.run(
            mode=require_str(params, "mode"),
            model=optional_str(params, "model") or "",
        )

    def image_evals_history(self, params: JsonDict) -> list[JsonDict]:
        return self.image_evals.history(limit=optional_int(params, "limit", 20))

    def campaigns_models(self, _params: JsonDict) -> list[JsonDict]:
        return self.campaigns.installed_models()

    def campaigns_plan(self, params: JsonDict) -> JsonDict:
        return self.campaigns.plan(params)

    def campaigns_create(self, params: JsonDict) -> JsonDict:
        return self.campaigns.create(params)

    def campaigns_list(self, params: JsonDict) -> list[JsonDict]:
        return self.campaigns.list(limit=optional_int(params, "limit", 20))

    def campaigns_get(self, params: JsonDict) -> JsonDict:
        return self.campaigns.get(require_str(params, "campaign_id"))

    def campaigns_start(self, params: JsonDict) -> JsonDict:
        return self.campaigns.start(require_str(params, "campaign_id"))

    def campaigns_pause(self, params: JsonDict) -> JsonDict:
        return self.campaigns.pause(require_str(params, "campaign_id"))

    def campaigns_cancel(self, params: JsonDict) -> JsonDict:
        return self.campaigns.cancel(require_str(params, "campaign_id"))

    def campaigns_resume(self, params: JsonDict) -> JsonDict:
        return self.campaigns.resume(require_str(params, "campaign_id"))

    def campaigns_retry_job(self, params: JsonDict) -> JsonDict:
        return self.campaigns.retry_job(require_str(params, "job_id"))

    def campaigns_delete(self, params: JsonDict) -> JsonDict:
        return self.campaigns.delete(
            require_str(params, "campaign_id"),
            delete_benchmark_runs=optional_bool(params, "delete_benchmark_runs", False),
        )

    def campaigns_report_data(self, params: JsonDict) -> JsonDict:
        return self.campaigns.report_data(
            require_str(params, "campaign_id"),
            include_detailed_audit=optional_bool(params, "include_detailed_audit", False),
        )

    def campaigns_generate_report(self, params: JsonDict) -> JsonDict:
        screenshots = params.get("screenshots")
        if screenshots is not None and not isinstance(screenshots, list):
            raise RpcError(-32602, "screenshots must be an array")
        return self.campaigns.generate_report(
            require_str(params, "campaign_id"),
            output_folder=optional_str(params, "output_folder"),
            screenshots=screenshots,
            capture_failure_reason=optional_str(params, "capture_failure_reason"),
            include_detailed_audit=optional_bool_or_none(params, "include_detailed_audit"),
            generate_pdf=optional_bool(params, "generate_pdf", True),
            generate_html=optional_bool(params, "generate_html", True),
        )

    def campaigns_open_report_folder(self, params: JsonDict) -> JsonDict:
        path = optional_str(params, "path")
        if not path:
            campaign = self.campaigns.get(require_str(params, "campaign_id"))
            report_paths = campaign.get("report_paths") or {}
            target = report_paths.get("pdf") or report_paths.get("html") or report_paths.get("json")
            if not target:
                raise RpcError(-32602, "campaign has no report path")
            from pathlib import Path

            path = str(Path(str(target)).parent)
        return self.campaigns.open_report_path(path)

    def app_shutdown(self, _params: JsonDict) -> JsonDict:
        self.shutdown_requested = True
        return {"ok": True}


def require_str(params: JsonDict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RpcError(-32602, f"{key} must be a non-empty string")
    return value


def optional_str(params: JsonDict, key: str) -> str | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise RpcError(-32602, f"{key} must be a string")
    return value


def optional_int(params: JsonDict, key: str, default: int) -> int:
    value = params.get(key, default)
    if not isinstance(value, int):
        raise RpcError(-32602, f"{key} must be an integer")
    return value


def optional_bool(params: JsonDict, key: str, default: bool) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise RpcError(-32602, f"{key} must be a boolean")
    return value


def optional_bool_or_none(params: JsonDict, key: str) -> bool | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise RpcError(-32602, f"{key} must be a boolean")
    return value


def optional_float(params: JsonDict, key: str, default: float) -> float:
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RpcError(-32602, f"{key} must be a number")
    return float(value)


def optional_str_list(params: JsonDict, key: str) -> list[str] | None:
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise RpcError(-32602, f"{key} must be a list of strings")
    clean: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise RpcError(-32602, f"{key} must be a list of strings")
        item = item.strip()
        if item:
            clean.append(item)
    return clean


def make_response(request_id: Any, result: Any = None, error: RpcError | None = None) -> JsonDict:
    response: JsonDict = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload: JsonDict = {"code": error.code, "message": error.message}
        if error.data is not None:
            payload["data"] = error.data
        response["error"] = payload
    else:
        response["result"] = result
    return response


def write_json(payload: JsonDict) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    profile_dir = os.environ.get("ODYSSEUS_PROFILE_DIR")
    if not profile_dir:
        print("ODYSSEUS_PROFILE_DIR is required", file=sys.stderr)
        return 2

    setup_logging(profile_dir)
    logger.info("JSON-RPC sidecar starting version=%s", __version__)
    app = SidecarApp(profile_dir)
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            request_id: Any = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise RpcError(-32600, "request must be an object")
                request_id = request.get("id")
                if request.get("jsonrpc") != "2.0":
                    raise RpcError(-32600, "jsonrpc must be 2.0")
                method = request.get("method")
                if not isinstance(method, str) or not method:
                    raise RpcError(-32600, "method is required")
                result = app.dispatch(method, request.get("params") or {})
                write_json(make_response(request_id, result=result))
                if app.shutdown_requested:
                    break
            except RpcError as exc:
                logger.warning("JSON-RPC error id=%s code=%s message=%s", request_id, exc.code, exc.message)
                write_json(make_response(request_id, error=exc))
            except json.JSONDecodeError as exc:
                logger.warning("JSON-RPC parse error id=%s error=%s", request_id, exc)
                write_json(make_response(request_id, error=RpcError(-32700, "parse error", str(exc))))
            except KeyError as exc:
                logger.warning("JSON-RPC key error id=%s error=%s", request_id, exc)
                write_json(make_response(request_id, error=RpcError(-32004, str(exc))))
            except FileNotFoundError as exc:
                logger.warning("JSON-RPC file not found id=%s error=%s", request_id, exc)
                write_json(make_response(request_id, error=RpcError(-32044, str(exc))))
            except ValueError as exc:
                logger.warning("JSON-RPC value error id=%s error=%s", request_id, exc)
                write_json(make_response(request_id, error=RpcError(-32602, str(exc))))
            except RuntimeError as exc:
                logger.warning("JSON-RPC runtime error id=%s error=%s", request_id, exc)
                write_json(make_response(request_id, error=RpcError(-32000, str(exc))))
            except Exception as exc:  # noqa: BLE001 - JSON-RPC must not crash on handler errors
                logger.error("JSON-RPC unexpected error id=%s traceback=%s", request_id, traceback.format_exc())
                write_json(make_response(request_id, error=RpcError(-32000, str(exc))))
    finally:
        app.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
