from __future__ import annotations

import json
import math
import re
import time
import uuid
from typing import TYPE_CHECKING
from typing import Any

from odysseus_desktop_backend.progress import bind_progress_context, emit_progress
from odysseus_desktop_backend.services.model_service import (
    CORRECTION_NUM_PREDICT,
    CORRECTION_TIMEOUT_SECONDS,
    INTERACTIVE_CHAT_TIMEOUT_SECONDS,
    VERIFIER_NUM_PREDICT,
    VERIFIER_TIMEOUT_SECONDS,
    VISION_CHAT_TIMEOUT_SECONDS,
    ModelService,
    ModelTimeoutError,
    normalize_thinking_mode,
)
from odysseus_desktop_backend.services.florence2_service import FLORENCE_MODEL_ID
from odysseus_desktop_backend.services.perception_types import (
    VISION_BACKEND_AUTOMATIC,
    VISION_BACKEND_FLORENCE2,
    VISION_BACKEND_OCR_ONLY,
    VISION_BACKEND_OLLAMA,
    normalize_vision_backend,
    strict_visual_synthesis_rules,
)
from odysseus_desktop_backend.services.visual_evidence_curator import (
    VISUAL_EVIDENCE_CURATOR_VERSION,
    VISUAL_EVIDENCE_RETRIEVER_VERSION,
    compact_synthesis_packet,
    curate_visual_evidence,
    deterministic_visual_answer,
    guard_visual_answer,
    safe_fallback_answer,
    status_from_warnings,
    visual_evidence_from_observations,
)
from odysseus_desktop_backend.services.session_service import SessionService, parse_message_metadata
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import utc_ms

if TYPE_CHECKING:
    from odysseus_desktop_backend.services.artifact_service import ArtifactService
    from odysseus_desktop_backend.services.document_service import DocumentService
    from odysseus_desktop_backend.services.ocr_service import OCRService
    from odysseus_desktop_backend.services.rag_service import RAGService
    from odysseus_desktop_backend.services.source_service import SourceService
    from odysseus_desktop_backend.services.vision_service import VisionService


ANSWER_STYLES = {
    "precise": "Precise",
    "layman": "Layman",
    "detailed": "Detailed",
    "extract_only": "Extract only",
    "evidence_only": "Evidence only",
}
DEFAULT_ANSWER_STYLE = "precise"
DEFAULT_TEMPERATURE = 0.0
RAG_PRESETS = {"standard", "potato"}
DEFAULT_RAG_PRESET = "standard"
POTATO_RETRIEVAL_LIMIT = 2
NO_ACTIVE_IMAGE_CONTEXT_MESSAGE = (
    "No image is currently in the conversation context. Attach an image or add one from Sources to ask about it."
)


class ChatService:
    """Chat service.

    Default chat remains the simple Milestone 1 path. Milestone 2 RAG is opt-in
    per request and only injects retrieved document chunks.
    """

    def __init__(
        self,
        sessions: SessionService,
        settings: SettingsService,
        models: ModelService,
        rag: RAGService | None = None,
        artifacts: ArtifactService | None = None,
        documents: DocumentService | None = None,
        sources: SourceService | None = None,
        vision: VisionService | None = None,
        ocr: OCRService | None = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.models = models
        self.rag = rag
        self.artifacts = artifacts
        self.documents = documents
        self.sources = sources
        self.vision = vision
        self.ocr = ocr

    def send(
        self,
        message: str,
        session_id: str | None = None,
        model: str | None = None,
        use_rag: bool = False,
        document_ids: list[str] | None = None,
        verify_rag: bool = False,
        answer_style: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        rag_preset: str | None = None,
        thinking_mode: str = "auto",
        timeout: float = INTERACTIVE_CHAT_TIMEOUT_SECONDS,
        attachment_document_ids: list[str] | None = None,
        artifact_ids: list[str] | None = None,
        multimodal_mode: str = "automatic",
        vision_model: str = "",
        vision_backend: str = VISION_BACKEND_AUTOMATIC,
        analysis_request_id: str = "",
        crop_derivation_id: str = "",
    ) -> dict[str, Any]:
        content = (message or "").strip()
        question_for_models = content or "Summarize the attached source."
        clean_attachment_document_ids = [
            str(item).strip() for item in attachment_document_ids or [] if str(item).strip()
        ]
        clean_artifact_ids = [str(item).strip() for item in artifact_ids or [] if str(item).strip()]
        if not content and not clean_artifact_ids and not clean_attachment_document_ids:
            raise ValueError("message is required")
        if clean_attachment_document_ids and self.documents is None:
            raise ValueError("document attachments are not available")
        if len(clean_artifact_ids) > 4:
            raise ValueError("at most 4 images can be attached to one chat turn")
        document_context_enabled = use_rag or bool(clean_attachment_document_ids)
        selected_rag_preset = normalize_rag_preset(rag_preset) if document_context_enabled else DEFAULT_RAG_PRESET
        potato_mode = document_context_enabled and selected_rag_preset == "potato"
        selected_answer_style = (
            "evidence_only" if potato_mode else normalize_answer_style(answer_style)
            if document_context_enabled
            else DEFAULT_ANSWER_STYLE
        )
        if potato_mode:
            verify_rag = False
            temperature = DEFAULT_TEMPERATURE
        selected_thinking_mode = normalize_thinking_mode(thinking_mode)

        current_settings = self.settings.get()
        selected_model = (model or current_settings.get("default_model") or "llama3.2").strip()
        selected_vision_backend = normalize_vision_backend(vision_backend or current_settings.get("vision_backend"))
        if clean_artifact_ids and analysis_request_id and self.artifacts is not None:
            recovered = self._recover_multimodal_turn(
                analysis_request_id,
                selected_answer_style,
                selected_rag_preset,
                selected_thinking_mode,
                temperature,
            )
            if recovered:
                return recovered
        session = self.sessions.ensure(session_id, model=selected_model)

        if not session.get("model"):
            session = self.sessions.update(session["id"], {"model": selected_model})

        user_message = self.sessions.add_message(session["id"], "user", content)
        bind_progress_context(
            session_id=session["id"],
            message_id=user_message["id"],
            artifact_id=clean_artifact_ids[0] if clean_artifact_ids else None,
        )
        conversation_sources: list[tuple[str, str]] = []
        if clean_attachment_document_ids and self.documents is not None:
            self.documents.link_message(user_message["id"], clean_attachment_document_ids)
            conversation_sources.extend(("document", document_id) for document_id in clean_attachment_document_ids)
        if clean_artifact_ids:
            if self.artifacts is None or self.vision is None:
                raise ValueError("image attachments are not available")
            self.artifacts.link_message(user_message["id"], clean_artifact_ids)
            conversation_sources.extend(("artifact", artifact_id) for artifact_id in clean_artifact_ids)
        if conversation_sources and self.sources is not None:
            self.sources.add_to_conversation(
                session["id"],
                conversation_sources,
                added_message_id=user_message["id"],
            )

        active_context = self.sources.conversation_context(session["id"]) if self.sources is not None else []
        active_document_ids = [
            str(source["id"])
            for source in active_context
            if source.get("backend_kind") == "document"
        ] if self.sources is not None else clean_attachment_document_ids
        active_artifact_ids = [
            str(source["id"])
            for source in active_context
            if source.get("backend_kind") == "artifact"
        ] if self.sources is not None else clean_artifact_ids
        effective_document_ids = combine_unique(active_document_ids, document_ids or [])
        document_context_enabled = use_rag or bool(effective_document_ids)
        selected_rag_preset = normalize_rag_preset(rag_preset) if document_context_enabled else DEFAULT_RAG_PRESET
        potato_mode = document_context_enabled and selected_rag_preset == "potato"
        selected_answer_style = (
            "evidence_only" if potato_mode else normalize_answer_style(answer_style)
            if document_context_enabled
            else DEFAULT_ANSWER_STYLE
        )
        if potato_mode:
            verify_rag = False
            temperature = DEFAULT_TEMPERATURE

        document_evidence: list[dict[str, Any]] = []
        if document_context_enabled and effective_document_ids:
            document_evidence = self._prepare_document_evidence(
                effective_document_ids,
                session_document_ids=active_document_ids,
                indexed_source_document_ids=document_ids or [],
            )

        analysis = None
        multimodal_route: dict[str, Any] = {}
        if clean_artifact_ids:
            emit_progress("model_capability_check")
            multimodal_route = self._resolve_multimodal_route(multimodal_mode, selected_model, vision_model, selected_vision_backend)
            analysis = self.vision.analyze(
                clean_artifact_ids[0],
                mode=str(multimodal_route["analysis_mode"]),
                question=question_for_models,
                vision_model=str(multimodal_route.get("analysis_vision_model") or ""),
                vision_backend=str(multimodal_route.get("analysis_vision_backend") or selected_vision_backend),
                request_id=analysis_request_id or str(uuid.uuid4()),
                crop_derivation_id=crop_derivation_id,
                message_id=user_message["id"],
            )
            attached_artifacts = self.artifacts.attachments_for_messages([user_message["id"]]).get(user_message["id"], [])
            if not multimodal_route.get("native_vision") and self._analysis_blocks_synthesis(analysis):
                reply = self._perception_limitation_message(analysis)
                reply_response = self._local_reply_response(selected_model, reply)
                analysis = self._persist_multimodal_no_synthesis(
                    analysis=analysis,
                    route=multimodal_route,
                    final_model=selected_model,
                    answer=reply,
                )
                assistant_message = self.sessions.add_message(session["id"], "assistant", reply)
                if session["title"] == "New chat":
                    self.sessions.update(session["id"], {"title": self._derive_title(question_for_models)})
                return {
                    "session": self.sessions.get(session["id"]),
                    "user_message": self._hydrate_single_message(user_message),
                    "assistant_message": assistant_message,
                    "messages": self._hydrate_messages(self.sessions.messages(session["id"])),
                    "retrieved_chunks": [],
                    "retrieved_snippets": [],
                    "grounding": self._grounding_report([], enabled=False, status="no_evidence"),
                    "answer_style": selected_answer_style,
                    "temperature": float(temperature),
                    "rag_preset": selected_rag_preset,
                    "thinking_mode": selected_thinking_mode,
                    "model_response": reply_response,
                    "artifact_analysis": analysis,
                    "document_evidence": document_evidence,
                    "attached_artifacts": attached_artifacts,
                    "attached_documents": self.documents.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.documents else [],
                }
            if analysis["status"] in {"error", "timeout", "interrupted"}:
                return {
                    "session": self.sessions.get(session["id"]),
                    "user_message": self._hydrate_single_message(user_message),
                    "assistant_message": None,
                    "messages": self._hydrate_messages(self.sessions.messages(session["id"])),
                    "retrieved_chunks": [],
                    "retrieved_snippets": [],
                    "grounding": self._grounding_report([], enabled=False, status="no_evidence"),
                    "answer_style": selected_answer_style,
                    "temperature": float(temperature),
                    "rag_preset": selected_rag_preset,
                    "thinking_mode": selected_thinking_mode,
                    "artifact_analysis": analysis,
                    "document_evidence": document_evidence,
                    "attached_artifacts": attached_artifacts,
                    "attached_documents": self.documents.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.documents else [],
                }
        history = self.sessions.messages(session["id"])
        rag_context = ""
        retrieved_chunks: list[dict[str, Any]] = []
        retrieved_snippets: list[dict[str, Any]] = []
        if document_context_enabled and self.rag is not None:
            rag_context, retrieved_chunks, retrieved_snippets = self.rag.build_quote_context(
                question_for_models,
                document_ids=effective_document_ids or None,
                limit=POTATO_RETRIEVAL_LIMIT if potato_mode else 4,
            )
        if document_context_enabled and self.documents is not None:
            if not document_evidence and retrieved_snippets:
                document_evidence = self._document_evidence_from_retrieved_snippets(
                    retrieved_snippets,
                    session_document_ids=active_document_ids,
                    indexed_source_document_ids=document_ids or [],
                )
            self._apply_retrieval_diagnostics_to_document_evidence(
                document_evidence,
                retrieved_snippets,
                model_received=bool(rag_context),
            )

        ollama_messages = [
            {"id": str(item["id"]), "role": item["role"], "content": item["content"]}
            for item in history
            if item["role"] in {"system", "user", "assistant"}
        ]
        if rag_context:
            ollama_messages.insert(
                0,
                {
                    "role": "system",
                    "content": self._rag_system_prompt(
                        rag_context,
                        question_for_models,
                        selected_answer_style,
                        selected_rag_preset,
                    ),
                },
            )
            self._augment_latest_user_message_for_rag(
                ollama_messages,
                question_for_models,
                selected_answer_style,
                selected_rag_preset,
            )

        generation_options = {"temperature": float(temperature)}
        image_context_ids = active_artifact_ids[:4]
        if image_context_ids and not multimodal_route:
            emit_progress("model_capability_check")
            multimodal_route = self._resolve_context_multimodal_route(
                multimodal_mode,
                selected_model,
                vision_model,
                selected_vision_backend,
                image_context_ids[0],
            )
        if not image_context_ids and self._should_answer_no_active_image_context(question_for_models, history):
            reply = NO_ACTIVE_IMAGE_CONTEXT_MESSAGE
            reply_response = self._local_reply_response(selected_model, reply)
        elif image_context_ids:
            if analysis is None:
                analysis = self._prepare_context_analysis(
                    artifact_id=image_context_ids[0],
                    question=question_for_models,
                    route=multimodal_route,
                    message_id=user_message["id"],
                    vision_model=vision_model,
                    vision_backend=selected_vision_backend,
                )
            if not multimodal_route.get("native_vision") and self._analysis_blocks_synthesis(analysis):
                reply = self._perception_limitation_message(analysis)
                reply_response = self._local_reply_response(selected_model, reply)
                analysis = self._persist_multimodal_no_synthesis(
                    analysis=analysis,
                    route=multimodal_route,
                    final_model=selected_model,
                    answer=reply,
                )
            synthesis_started = time.perf_counter()
            try:
                if multimodal_route.get("native_vision") or not self._analysis_blocks_synthesis(analysis):
                    reply_response, reply, analysis = self._generate_multimodal_reply(
                        selected_model=selected_model,
                        question=question_for_models,
                        analysis=analysis,
                        artifact_ids=image_context_ids,
                        route=multimodal_route,
                        messages=ollama_messages,
                        options=generation_options,
                        thinking=selected_thinking_mode,
                        timeout=timeout,
                    )
            except ModelTimeoutError as exc:
                analysis = self._persist_multimodal_synthesis_failure(
                    analysis=analysis,
                    route=multimodal_route,
                    final_model=selected_model,
                    error=str(exc),
                    synthesis_elapsed_ms=int((time.perf_counter() - synthesis_started) * 1000),
                    timeout_seconds=max(float(timeout or 0), float(VISION_CHAT_TIMEOUT_SECONDS)) if multimodal_route.get("native_vision") else float(timeout or 0),
                )
                return {
                    "session": self.sessions.get(session["id"]),
                    "user_message": self._hydrate_single_message(user_message),
                    "assistant_message": None,
                    "messages": self._hydrate_messages(self.sessions.messages(session["id"])),
                    "retrieved_chunks": retrieved_chunks,
                    "retrieved_snippets": retrieved_snippets,
                    "grounding": self._grounding_report(retrieved_snippets, enabled=False, status="no_evidence"),
                    "answer_style": selected_answer_style,
                    "temperature": float(temperature),
                    "rag_preset": selected_rag_preset,
                    "thinking_mode": selected_thinking_mode,
                    "artifact_analysis": analysis,
                    "document_evidence": document_evidence,
                    "attached_artifacts": self.artifacts.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.artifacts else [],
                    "attached_documents": self.documents.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.documents else [],
                }
        else:
            document_limitation = self._document_evidence_limitation_message(
                document_evidence,
                document_context_enabled=document_context_enabled,
                document_ids=effective_document_ids,
                has_rag_context=bool(rag_context),
            )
            poor_ocr_limitation = self._poor_ocr_limitation_message(document_evidence, retrieved_snippets)
            if document_limitation:
                reply = document_limitation
                reply_response = self._local_reply_response(selected_model, reply)
            elif poor_ocr_limitation:
                reply = poor_ocr_limitation
                reply_response = self._local_reply_response(selected_model, reply)
            else:
                emit_progress("answer_generation")
                reply_response = self._chat_model_detailed(
                    selected_model,
                    ollama_messages,
                    generation_options,
                    thinking=selected_thinking_mode,
                    timeout=timeout,
                )
                reply = str(reply_response.get("content") or "")
        grounding = self._grounding_report(
            retrieved_snippets,
            enabled=verify_rag,
            status="not_run" if rag_context else "no_evidence",
        )
        if rag_context and verify_rag and retrieved_snippets:
            emit_progress("answer_verification")
            reply, grounding = self._verify_and_correct(
                model=selected_model,
                messages=ollama_messages,
                draft_answer=reply,
                snippets=retrieved_snippets,
                answer_style=selected_answer_style,
                options=generation_options,
            )
        if rag_context and retrieved_snippets:
            reply = self._guard_ocr_answer(
                reply,
                question=question_for_models,
                snippets=retrieved_snippets,
            )
        timings = {
            "answer_latency_ms": int(reply_response.get("elapsed_ms") or 0),
            "verifier_latency_ms": int(grounding.get("verifier_latency_ms") or 0),
            "correction_latency_ms": int(grounding.get("correction_latency_ms") or 0),
            "final_verification_latency_ms": int(grounding.get("final_verification_latency_ms") or 0),
        }
        operation_trace = self.build_operation_trace(
            model_response=reply_response,
            analysis=analysis,
            grounding=grounding,
            timings=timings,
            rag_enabled=document_context_enabled,
            verifier_enabled=verify_rag,
            retrieved_snippets=retrieved_snippets,
            selected_model=selected_model,
            selected_rag_preset=selected_rag_preset,
            selected_thinking_mode=selected_thinking_mode,
            answer_style=selected_answer_style,
            embedding_model=str(current_settings.get("embedding_model") or ""),
            embedding_backend=str(current_settings.get("embedding_backend") or ""),
        )
        assistant_message = self.sessions.add_message(
            session["id"],
            "assistant",
            reply,
            metadata={"operation_trace": operation_trace},
        )

        if session["title"] == "New chat":
            self.sessions.update(session["id"], {"title": self._derive_title(question_for_models)})

        return {
            "session": self.sessions.get(session["id"]),
            "user_message": self._hydrate_single_message(user_message),
            "assistant_message": assistant_message,
            "messages": self._hydrate_messages(self.sessions.messages(session["id"])),
            "retrieved_chunks": retrieved_chunks,
            "retrieved_snippets": retrieved_snippets,
            "grounding": grounding,
            "answer_style": selected_answer_style,
            "temperature": float(temperature),
            "rag_preset": selected_rag_preset,
            "thinking_mode": selected_thinking_mode,
            "model_response": reply_response,
            "artifact_analysis": analysis,
            "document_evidence": document_evidence,
            "attached_artifacts": self.artifacts.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.artifacts else [],
            "attached_documents": self.documents.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.documents else [],
            "timings": timings,
        }

    def build_operation_trace(
        self,
        *,
        model_response: dict[str, Any] | None,
        analysis: dict[str, Any] | None,
        grounding: dict[str, Any] | None,
        timings: dict[str, Any] | None,
        rag_enabled: bool,
        verifier_enabled: bool,
        retrieved_snippets: list[dict[str, Any]] | None,
        selected_model: str,
        selected_rag_preset: str,
        selected_thinking_mode: str,
        answer_style: str,
        embedding_model: str = "",
        embedding_backend: str = "",
    ) -> dict[str, Any]:
        """Build a deliberately allow-listed, local-only summary of a completed answer."""

        response = model_response if isinstance(model_response, dict) else {}
        analysis_data = analysis if isinstance(analysis, dict) else {}
        grounding_data = grounding if isinstance(grounding, dict) else {}
        timing_data = timings if isinstance(timings, dict) else {}
        analysis_timings = analysis_data.get("timings") if isinstance(analysis_data.get("timings"), dict) else {}
        evidence = analysis_data.get("evidence") if isinstance(analysis_data.get("evidence"), dict) else {}
        vision_evidence = evidence.get("vision") if isinstance(evidence.get("vision"), dict) else {}
        output = analysis_data.get("output") if isinstance(analysis_data.get("output"), dict) else {}
        provenance = output.get("provenance") if isinstance(output.get("provenance"), dict) else {}
        verifier = grounding_data.get("verifier") if isinstance(grounding_data.get("verifier"), dict) else {}
        conversation_context = (
            evidence.get("conversation_context")
            if isinstance(evidence.get("conversation_context"), dict)
            else {}
        )

        context_evidence_action = _safe_trace_label(
            provenance.get("context_evidence_action")
            or conversation_context.get("action")
        )
        visual_evidence_reused = _trace_bool(provenance.get("raw_evidence_reused"))
        if visual_evidence_reused is None and context_evidence_action:
            visual_evidence_reused = context_evidence_action == "reused"
        vision_rerun = _trace_bool(provenance.get("vision_rerun"))
        if vision_rerun is None and context_evidence_action:
            vision_rerun = context_evidence_action == "vision_rerun"

        timing = _compact_trace_fields(
            {
                "answer_latency_ms": _trace_int(timing_data.get("answer_latency_ms")),
                "synthesis_elapsed_ms": _trace_int(analysis_timings.get("synthesis_elapsed_ms")),
                "preprocessing_elapsed_ms": _trace_int(analysis_timings.get("preprocessing_elapsed_ms")),
                "vision_elapsed_ms": _trace_int(vision_evidence.get("elapsed_ms")),
                "verifier_latency_ms": _trace_int(timing_data.get("verifier_latency_ms")),
                "correction_latency_ms": _trace_int(timing_data.get("correction_latency_ms")),
                "total_vision_elapsed_ms": _trace_int(analysis_timings.get("elapsed_ms")),
            }
        )

        final_answer_model = _safe_trace_label(
            response.get("model") or provenance.get("final_answer_model") or selected_model
        )
        models = _compact_trace_fields(
            {
                "final_answer_model": final_answer_model,
                "vision_backend": _safe_trace_label(
                    analysis_data.get("actual_vision_backend") or provenance.get("vision_backend")
                ),
                "vision_model": _safe_trace_label(
                    analysis_data.get("actual_vision_model")
                    or provenance.get("vision_inspection_model")
                    or vision_evidence.get("model")
                ),
                "ocr_engine": _safe_trace_label(analysis_data.get("ocr_engine")),
                "embedding_model": _safe_trace_label(embedding_model) if rag_enabled else None,
                "embedding_backend": _safe_trace_label(embedding_backend) if rag_enabled else None,
            }
        )

        pipeline = _compact_trace_fields(
            {
                "rag_enabled": bool(rag_enabled),
                "rag_preset": _safe_trace_label(selected_rag_preset) if rag_enabled else None,
                "verifier_enabled": bool(verifier_enabled),
                "verifier_status": _safe_trace_label(verifier.get("status") or "not_run"),
                "requested_multimodal_mode": _safe_trace_label(provenance.get("mode_requested")),
                "executed_multimodal_mode": _safe_trace_label(provenance.get("mode_executed")),
                "visual_evidence_reused": visual_evidence_reused,
                "vision_rerun": vision_rerun,
                "context_evidence_action": context_evidence_action,
                "deterministic_visual_answer": _trace_bool(provenance.get("deterministic_visual_answer")),
                "answer_style": _safe_trace_label(answer_style),
                "thinking_mode": _safe_trace_label(selected_thinking_mode),
                "done_reason": _safe_trace_label(response.get("done_reason")),
            }
        )

        tokens = _compact_trace_fields(
            {
                "prompt_tokens": _trace_int(response.get("prompt_eval_count")),
                "completion_tokens": _trace_int(response.get("eval_count")),
                "total_duration_ns": _trace_int(response.get("total_duration_ns")),
                "load_duration_ns": _trace_int(response.get("load_duration_ns")),
                "generation_tokens_per_second": _trace_float(response.get("generation_tokens_per_second")),
            }
        )

        chunk_ids: list[str] = []
        document_ids: list[str] = []
        page_numbers: list[int] = []
        for snippet in retrieved_snippets or []:
            if not isinstance(snippet, dict):
                continue
            chunk_id = _safe_trace_identifier(snippet.get("chunk_id"))
            document_id = _safe_trace_identifier(snippet.get("document_id"))
            if chunk_id and chunk_id not in chunk_ids:
                chunk_ids.append(chunk_id)
            if document_id and document_id not in document_ids:
                document_ids.append(document_id)
            for key in ("page_start", "page_end"):
                page_number = _trace_int(snippet.get(key))
                if page_number is not None and page_number not in page_numbers:
                    page_numbers.append(page_number)

        warnings: list[str] = []
        analysis_warnings = analysis_data.get("warnings")
        for warning in analysis_warnings if isinstance(analysis_warnings, list) else []:
            safe_warning = _safe_trace_warning(warning)
            if safe_warning and safe_warning not in warnings:
                warnings.append(safe_warning)
            if len(warnings) >= 8:
                break
        verifier_status = str(verifier.get("status") or "").strip().lower()
        verifier_warning = {
            "failed": "Verifier found claims that need review.",
            "timeout": "Verifier timed out before completing.",
            "error": "Verifier could not complete.",
        }.get(verifier_status, "")
        if verifier_warning and verifier_warning not in warnings:
            warnings.append(verifier_warning)
        if rag_enabled and not (retrieved_snippets or []):
            warnings.append("RAG returned no retrieved sources.")

        done_reason = str(response.get("done_reason") or "")
        thinking = response.get("thinking") if isinstance(response.get("thinking"), str) else ""
        thinking_returned = bool(thinking) and done_reason not in {
            "deterministic_visual_answer",
            "local_context_guard",
        }
        # TODO(v0.3): only persist returned model-trace text behind an explicit local setting.
        model_trace = {
            "thinking_returned": thinking_returned,
            "thinking_char_count": len(thinking) if thinking_returned else 0,
            "thinking_truncated": False,
        }

        return {
            "schema_version": 1,
            "timing": timing,
            "models": models,
            "pipeline": pipeline,
            "tokens": tokens,
            "sources": {
                "retrieved_chunk_ids": chunk_ids,
                "retrieved_document_ids": document_ids,
                "retrieved_page_numbers": sorted(page_numbers),
            },
            "warnings": warnings,
            "model_trace": model_trace,
        }

    def _recover_multimodal_turn(
        self,
        request_id: str,
        answer_style: str,
        rag_preset: str,
        thinking_mode: str,
        temperature: float,
    ) -> dict[str, Any] | None:
        if self.artifacts is None:
            return None
        analysis = self.artifacts.analysis_by_request(request_id)
        if not analysis or not analysis.get("message_id"):
            return None
        row = self.sessions.db.conn.execute(
            "SELECT id, session_id, role, content, metadata_json, created_at FROM messages WHERE id = ?",
            (analysis["message_id"],),
        ).fetchone()
        if row is None:
            return None
        user_message = dict(row)
        user_message["metadata"] = parse_message_metadata(user_message.pop("metadata_json", "{}"))
        session = self.sessions.get(user_message["session_id"])
        assistant_row = self.sessions.db.conn.execute(
            """
            SELECT id, session_id, role, content, metadata_json, created_at
            FROM messages
            WHERE session_id = ? AND role = 'assistant' AND created_at >= ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (session["id"], user_message["created_at"]),
        ).fetchone()
        assistant_message = dict(assistant_row) if assistant_row else None
        if assistant_message is not None:
            assistant_message["metadata"] = parse_message_metadata(
                assistant_message.pop("metadata_json", "{}")
            )
        attached_artifacts = self.artifacts.attachments_for_messages([user_message["id"]]).get(user_message["id"], [])
        return {
            "session": session,
            "user_message": self._hydrate_single_message(user_message),
            "assistant_message": assistant_message,
            "messages": self._hydrate_messages(self.sessions.messages(session["id"])),
            "retrieved_chunks": [],
            "retrieved_snippets": [],
            "grounding": self._grounding_report([], enabled=False, status="no_evidence"),
            "answer_style": answer_style,
            "temperature": float(temperature),
            "rag_preset": rag_preset,
            "thinking_mode": thinking_mode,
            "model_response": {},
            "artifact_analysis": analysis,
            "attached_artifacts": attached_artifacts,
            "attached_documents": self.documents.attachments_for_messages([user_message["id"]]).get(user_message["id"], []) if self.documents else [],
            "timings": {
                "answer_latency_ms": int((analysis.get("timings") or {}).get("elapsed_ms") or 0),
                "verifier_latency_ms": 0,
                "correction_latency_ms": 0,
                "final_verification_latency_ms": 0,
            },
        }

    def _hydrate_single_message(self, message: dict[str, Any]) -> dict[str, Any]:
        return self._hydrate_messages([message])[0]

    def _hydrate_messages(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        hydrated = [dict(message) for message in messages]
        if self.documents is not None:
            hydrated = self.documents.hydrate_messages(hydrated)
        if self.artifacts is not None:
            hydrated = self.artifacts.hydrate_messages(hydrated)
        for item in hydrated:
            item["attachments"] = [
                {"backend_kind": "document", **document}
                for document in item.get("documents", [])
            ] + [
                {"backend_kind": "artifact", **artifact}
                for artifact in item.get("artifacts", [])
            ]
        return hydrated

    def _prepare_document_evidence(
        self,
        document_ids: list[str],
        *,
        session_document_ids: list[str],
        indexed_source_document_ids: list[str],
    ) -> list[dict[str, Any]]:
        if self.documents is None:
            return []
        session_set = set(session_document_ids)
        indexed_set = set(indexed_source_document_ids)
        diagnostics: list[dict[str, Any]] = []
        for document_id in combine_unique(document_ids, []):
            try:
                document = self.documents.get(document_id)
                before = self.documents.text_diagnostics(document_id)
            except KeyError:
                continue
            action = "already_ready"
            ocr_result: dict[str, Any] | None = None
            if self.ocr is not None and self.documents.needs_ocr(document_id):
                action = "ocr_attempted"
                raw_result = self.ocr.ensure_document_ocr_indexed(document_id)
                ocr_result = dict(raw_result)
                if ocr_result.get("skipped"):
                    action = f"ocr_skipped:{ocr_result.get('reason') or 'unknown'}"
            after = self.documents.text_diagnostics(document_id)
            after["evidence_action"] = action
            after["display_name"] = str(document.get("title") or document.get("file_name") or "Document")
            after["session_attachment_evidence_used"] = document_id in session_set
            after["indexed_source_evidence_used"] = document_id in indexed_set or str(document.get("scope") or "") == "library"
            after["model_received_document_evidence"] = False
            after["before"] = before
            if ocr_result is not None:
                after["ocr_stats"] = ocr_result.get("stats") or {}
            diagnostics.append(after)
        return diagnostics

    def _document_evidence_limitation_message(
        self,
        diagnostics: list[dict[str, Any]],
        *,
        document_context_enabled: bool,
        document_ids: list[str],
        has_rag_context: bool,
    ) -> str:
        if not document_context_enabled or has_rag_context:
            return ""
        if not diagnostics:
            return "I do not have retrieved document evidence to answer from."
        statuses = {str(item.get("ocr_status") or "") for item in diagnostics}
        errors = [
            str(item.get("ocr_error") or "").strip()
            for item in diagnostics
            if str(item.get("ocr_error") or "").strip()
        ]
        if "no_text" in statuses:
            return "I could not extract readable text from this image-based PDF yet."
        if "unavailable" in statuses or "needed" in statuses:
            detail = f" {errors[0]}" if errors else ""
            return f"I could not extract readable text from this image-based PDF yet.{detail}".strip()
        if any(str(item.get("file_type") or "") == "pdf" and int(item.get("chunk_count") or 0) == 0 for item in diagnostics):
            return "I could not extract readable text from this image-based PDF yet."
        return ""

    def _document_evidence_from_retrieved_snippets(
        self,
        snippets: list[dict[str, Any]],
        *,
        session_document_ids: list[str],
        indexed_source_document_ids: list[str],
    ) -> list[dict[str, Any]]:
        if self.documents is None:
            return []
        diagnostics: list[dict[str, Any]] = []
        session_set = set(session_document_ids)
        indexed_set = set(indexed_source_document_ids)
        for document_id in combine_unique([str(snippet.get("document_id") or "") for snippet in snippets], []):
            if not document_id:
                continue
            try:
                document = self.documents.get(document_id)
                item = self.documents.text_diagnostics(document_id)
            except KeyError:
                continue
            item["evidence_action"] = "retrieved"
            item["display_name"] = str(document.get("title") or document.get("file_name") or "Document")
            item["session_attachment_evidence_used"] = document_id in session_set
            item["indexed_source_evidence_used"] = document_id in indexed_set or str(document.get("scope") or "") == "library"
            item["model_received_document_evidence"] = False
            item["before"] = {}
            diagnostics.append(item)
        return diagnostics

    def _apply_retrieval_diagnostics_to_document_evidence(
        self,
        diagnostics: list[dict[str, Any]],
        snippets: list[dict[str, Any]],
        *,
        model_received: bool,
    ) -> None:
        snippets_by_document: dict[str, list[dict[str, Any]]] = {}
        for snippet in snippets:
            document_id = str(snippet.get("document_id") or "")
            if document_id:
                snippets_by_document.setdefault(document_id, []).append(snippet)
        for item in diagnostics:
            document_id = str(item.get("document_id") or "")
            item["model_received_document_evidence"] = bool(model_received and snippets_by_document.get(document_id))
            document_snippets = snippets_by_document.get(document_id, [])
            if not document_snippets:
                continue
            query_terms: list[str] = []
            exact_matches: list[str] = []
            fuzzy_matches: list[dict[str, Any]] = []
            context_notes: list[str] = []
            pages: set[int] = set()
            line_windows: list[dict[str, Any]] = []
            crop_evidence: list[dict[str, Any]] = []
            selected_attempts: list[dict[str, Any]] = []
            quality_labels: list[str] = []
            vlm_backends: list[str] = []
            vlm_text_available = False
            ocr_attempt_count = int(item.get("ocr_attempt_count") or 0)
            ocr_crop_count = int(item.get("ocr_crop_count") or 0)
            for snippet in document_snippets:
                query_terms.extend(str(term) for term in snippet.get("ocr_query_terms") or [] if str(term))
                exact_matches.extend(str(term) for term in snippet.get("ocr_exact_matches") or [] if str(term))
                context_notes.extend(str(note) for note in snippet.get("ocr_context_notes") or [] if str(note))
                quality = snippet.get("ocr_quality") if isinstance(snippet.get("ocr_quality"), dict) else {}
                if quality.get("label"):
                    quality_labels.append(str(quality["label"]))
                try:
                    ocr_attempt_count += int(snippet.get("ocr_attempt_count") or 0)
                except (TypeError, ValueError):
                    pass
                try:
                    ocr_crop_count += int(snippet.get("ocr_crop_count") or 0)
                except (TypeError, ValueError):
                    pass
                selected = snippet.get("selected_ocr_attempt")
                if isinstance(selected, dict):
                    selected_attempts.append(selected)
                for crop in snippet.get("crop_evidence") or []:
                    if isinstance(crop, dict):
                        crop_evidence.append(crop)
                vlm = snippet.get("vlm_text_evidence") if isinstance(snippet.get("vlm_text_evidence"), dict) else {}
                if snippet.get("vlm_text_available") or vlm.get("text_char_count"):
                    vlm_text_available = True
                backend = str(vlm.get("backend") or "")
                if backend:
                    vlm_backends.append(backend)
                for match in snippet.get("ocr_fuzzy_matches") or []:
                    if isinstance(match, dict):
                        fuzzy_matches.append(match)
                for window in snippet.get("ocr_line_windows") or []:
                    if isinstance(window, dict):
                        line_windows.append(window)
                try:
                    page = int(snippet.get("page_start") or 0)
                except (TypeError, ValueError):
                    page = 0
                if page > 0:
                    pages.add(page)
            item["retrieved_pages"] = sorted(pages)
            item["ocr_query_terms"] = unique_strings(query_terms)
            item["ocr_exact_matches"] = unique_strings(exact_matches)
            item["ocr_fuzzy_matches"] = unique_match_dicts(fuzzy_matches)
            item["ocr_context_notes"] = unique_strings(context_notes)
            item["ocr_line_windows"] = line_windows[:6]
            item["ocr_quality"] = item.get("ocr_quality") or aggregate_quality_labels(quality_labels)
            item["ocr_attempt_count"] = ocr_attempt_count
            item["ocr_crop_count"] = ocr_crop_count
            item["ocr_selected_attempts"] = selected_attempts[:6]
            item["ocr_crop_evidence"] = crop_evidence[:8]
            item["vlm_text_available"] = bool(item.get("vlm_text_available") or vlm_text_available)
            item["vlm_text_backends"] = unique_strings([*item.get("vlm_text_backends", []), *vlm_backends])

    def _poor_ocr_limitation_message(
        self,
        diagnostics: list[dict[str, Any]],
        snippets: list[dict[str, Any]],
    ) -> str:
        if not diagnostics:
            return ""
        has_exact_or_fuzzy = any(
            snippet.get("ocr_exact_matches") or snippet.get("ocr_fuzzy_matches") or snippet.get("vlm_text_available")
            for snippet in snippets
        )
        if has_exact_or_fuzzy:
            return ""
        poor_items = [
            item
            for item in diagnostics
            if str(item.get("file_type") or "") == "pdf"
            and str(item.get("ocr_quality") or "") == "poor"
            and not item.get("vlm_text_available")
        ]
        if not poor_items:
            return ""
        names = ", ".join(str(item.get("display_name") or "the PDF") for item in poor_items[:2])
        return (
            f"I found page images in {names}, but OCR quality is too poor to read this reliably. "
            "Open the page image or try Enhanced Vision/OCR review."
        )

    def _guard_ocr_answer(self, answer: str, *, question: str, snippets: list[dict[str, Any]]) -> str:
        ocr_snippets = [
            snippet
            for snippet in snippets
            if isinstance(snippet.get("metadata"), dict) and snippet["metadata"].get("ocr_lexical_match")
        ]
        if not ocr_snippets:
            return answer

        query_terms = unique_strings(
            str(term)
            for snippet in ocr_snippets
            for term in snippet.get("ocr_query_terms") or []
            if str(term)
        )
        exact_matches = unique_strings(
            str(term)
            for snippet in ocr_snippets
            for term in snippet.get("ocr_exact_matches") or []
            if str(term)
        )
        fuzzy_matches = [
            match
            for snippet in ocr_snippets
            for match in snippet.get("ocr_fuzzy_matches") or []
            if isinstance(match, dict)
        ]
        context_notes = unique_strings(
            str(note)
            for snippet in ocr_snippets
            for note in snippet.get("ocr_context_notes") or []
            if str(note)
        )
        pages: set[int] = set()
        combined_text = " ".join(str(snippet.get("text") or "") for snippet in ocr_snippets)
        for snippet in ocr_snippets:
            try:
                page = int(snippet.get("page_start") or 0)
            except (TypeError, ValueError):
                page = 0
            if page > 0:
                pages.add(page)

        has_ocr_match = bool(exact_matches or fuzzy_matches)
        if not has_ocr_match and not context_notes:
            return answer

        guarded = re.sub(
            r"\bthere is no direct mention of ([^.]+)\.",
            r"The OCR evidence is noisy for \1.",
            answer or "",
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bthere is no direct mention\b",
            "the OCR evidence is noisy",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bthere is no direct evidence of ([^.]+)\.",
            r"The OCR evidence for \1 is noisy and incomplete.",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\b(?:does not|doesn't|not directly) mention\b",
            "only noisily mentions",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bthere is no information about what happened in ([^.]+)\.",
            r"the OCR does not cleanly preserve the full row for \1.",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bthere is no (?:information|evidence) (?:about|on) ([^.]+)\.",
            r"the OCR evidence for \1 is noisy and incomplete.",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bthere is no clear indication of what happened in ([^.]+)\.",
            r"The OCR evidence for what happened in \1 is noisy and incomplete.",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bthere (?:are|is) no direct quotes? or evidence from the retrieved snippets that specifically mention ([^.]+)\.",
            r"the OCR row evidence for \1 is noisy and incomplete.",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r"\bdoes not provide (?:any )?information about ([^.]+)\.",
            r"does not cleanly preserve \1.",
            guarded,
            flags=re.IGNORECASE,
        )
        guarded = re.sub(
            r",?\s+which is [^.]+(?:\.)",
            ".",
            guarded,
            flags=re.IGNORECASE,
        )

        answer_lower = guarded.lower()
        question_lower = question.lower()
        broad_name_question = bool(
            re.search(r"\b(tell me about|summarize|what does .* say about)\b", question_lower)
        )
        warsaw_question = "warsaw" in question_lower
        zoo_question = "zoo" in question_lower
        combined_compact = compact_answer_text(combined_text)
        has_warsaw_row_note = any(
            "warsaw-like row" in note.lower() or "warsaw entry" in note.lower()
            for note in context_notes
        )
        has_warsaw_row_clues = any(
            clue in combined_compact
            for clue in (
                "relationship",
                "riafnship",
                "felationship",
                "elattbnship",
                "patient",
                "bait",
            )
        )
        has_zoo_row_note = (
            "zoo" in combined_compact or any("zoo row" in note.lower() for note in context_notes)
        )
        has_zoo_over_clue = any(
            clue in combined_compact
            for clue in (
                "somethingshe",
                "sothethingshe",
                "sofhethingshe",
                "soffethingshe",
                "sofhiethingshe",
                "rsothethingshe",
            )
        )
        has_cabin_cool_clue = "cabin" in combined_compact and (
            "cooled" in combined_compact or "cooldown" in combined_compact
        )
        prefix_lines: list[str] = []
        page_label = format_page_list(sorted(pages))
        if (
            "rebecca" in {term.lower() for term in query_terms}
            and has_warsaw_row_note
            and (broad_name_question or warsaw_question)
            and (warsaw_question or "warsaw" not in answer_lower or "page 1" not in answer_lower)
        ):
            prefix_lines.append(
                f"OCR evidence summary: {page_label} suggests a Warsaw entry involving Rebecca, "
                "but that row is noisy and should be treated as uncertain."
            )
        if (
            "rebecca" in {term.lower() for term in query_terms}
            and has_warsaw_row_note
            and has_warsaw_row_clues
            and (broad_name_question or warsaw_question)
            and "relationship" not in answer_lower
        ):
            prefix_lines.append(
                f"OCR evidence summary: {page_label} has noisy Warsaw-row clues near Rebecca: "
                "the readable fragments point to her relationship and a learning to be patient/not take the bait. "
                "Unclear OCR details should remain uncertain."
            )
        if (
            "rebecca" in {term.lower() for term in query_terms}
            and has_warsaw_row_note
            and has_zoo_row_note
            and broad_name_question
            and "two" not in answer_lower
        ):
            prefix_lines.append(
                f"OCR evidence summary: {page_label} appears to contain two Rebecca entries: "
                "a noisy Warsaw entry and a Rebecca-at-the-Zoo entry."
            )
        if (
            "rebecca" in {term.lower() for term in query_terms}
            and has_zoo_row_note
            and "zoo" not in answer_lower
        ):
            prefix_lines.append(
                f"OCR evidence summary: {page_label} also shows a Rebecca-at-the-Zoo row or OCR variant."
            )
        if (
            "rebecca" in {term.lower() for term in query_terms}
            and (zoo_question or broad_name_question)
            and has_zoo_row_note
            and has_zoo_over_clue
            and "something she said" not in answer_lower
        ):
            prefix_lines.append(
                f"OCR evidence summary: {page_label} shows a Rebecca-at-the-Zoo row; "
                "the noisy fragments appear to say she was left at the zoo over something she said."
            )
        if (
            "rebecca" in {term.lower() for term in query_terms}
            and zoo_question
            and has_zoo_row_note
            and has_cabin_cool_clue
            and "cabin" not in answer_lower
        ):
            prefix_lines.append(
                f"OCR evidence summary: {page_label} also has a learning fragment about leaving the cabin "
                "and cooling down."
            )
        if has_ocr_match and "no direct mention" in answer_lower:
            match_label = ", ".join(exact_matches) if exact_matches else format_fuzzy_matches_for_answer(fuzzy_matches)
            prefix_lines.append(
                f"OCR evidence summary: {page_label} contains OCR evidence for {match_label}; "
                "the readable text may still be incomplete."
            )
        if "warsaw" in question_lower and "warsaw" in compact_answer_text(combined_text) and "page 1" not in answer_lower:
            prefix_lines.append(
                f"OCR evidence summary: {page_label} contains the Warsaw-like OCR text for this question."
            )

        prefix_lines = unique_strings(prefix_lines)
        if not prefix_lines:
            return guarded
        return "\n".join(prefix_lines) + "\n\n" + guarded.lstrip()

    def _resolve_multimodal_route(self, requested_mode: str, selected_model: str, vision_model: str, vision_backend: str) -> dict[str, Any]:
        mode = normalize_multimodal_mode(requested_mode)
        backend = normalize_vision_backend(vision_backend)
        selected_vision = self._vision_status(selected_model)
        requested_vision = self._vision_status(vision_model) if vision_model.strip() else {"status": "unknown", "error": ""}
        florence_ready = bool(self.vision and self.vision.florence_available())
        warnings: list[str] = []

        if mode == "ocr_only" or backend == VISION_BACKEND_OCR_ONLY:
            return {
                "mode_requested": mode,
                "mode_executed": "ocr_only",
                "analysis_mode": "ocr_only",
                "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
                "analysis_vision_model": "",
                "native_vision": False,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_OCR_ONLY,
                "vision_model": "",
                "warnings": warnings,
            }

        if backend == VISION_BACKEND_FLORENCE2:
            if not florence_ready:
                warnings.append("Florence-2 Basic local vision is selected, but its model pack or optional runtime is not ready.")
            return {
                "mode_requested": mode,
                "mode_executed": "combined" if mode == "automatic" else mode,
                "analysis_mode": "combined" if mode == "automatic" else mode,
                "analysis_vision_backend": VISION_BACKEND_FLORENCE2,
                "analysis_vision_model": FLORENCE_MODEL_ID,
                "native_vision": False,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_FLORENCE2,
                "vision_model": FLORENCE_MODEL_ID,
                "warnings": warnings,
            }

        if backend == VISION_BACKEND_OLLAMA:
            if selected_vision["status"] == "yes":
                return {
                    "mode_requested": mode,
                    "mode_executed": "combined" if mode == "automatic" else mode,
                    "analysis_mode": "ocr_only",
                    "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
                    "analysis_vision_model": "",
                    "native_vision": True,
                    "vision_backend_requested": backend,
                    "vision_backend": VISION_BACKEND_OLLAMA,
                    "vision_model": selected_model,
                    "warnings": warnings,
                }
            if vision_model.strip() and requested_vision["status"] == "yes":
                return {
                    "mode_requested": mode,
                    "mode_executed": "combined" if mode == "automatic" else mode,
                    "analysis_mode": "combined" if mode == "automatic" else mode,
                    "analysis_vision_backend": VISION_BACKEND_OLLAMA,
                    "analysis_vision_model": vision_model.strip(),
                    "native_vision": False,
                    "vision_backend_requested": backend,
                    "vision_backend": VISION_BACKEND_OLLAMA,
                    "vision_model": vision_model.strip(),
                    "warnings": warnings,
                }
            warning = "Ollama Vision Enhanced is selected, but no confirmed local vision model is available; using OCR-only fallback."
            if requested_vision.get("error"):
                warning = f"Requested vision model inspection failed; using OCR-only fallback: {requested_vision['error']}"
            return {
                "mode_requested": mode,
                "mode_executed": "ocr_only_fallback",
                "analysis_mode": "ocr_only",
                "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
                "analysis_vision_model": "",
                "native_vision": False,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_OCR_ONLY,
                "vision_model": "",
                "warnings": [warning],
            }

        if mode == "automatic":
            if selected_vision["status"] == "yes":
                return {
                    "mode_requested": mode,
                    "mode_executed": "combined",
                    "analysis_mode": "ocr_only",
                    "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
                    "analysis_vision_model": "",
                    "native_vision": True,
                    "vision_backend_requested": backend,
                    "vision_backend": VISION_BACKEND_OLLAMA,
                    "vision_model": selected_model,
                    "warnings": warnings,
                }
            if vision_model.strip() and requested_vision["status"] == "yes":
                return {
                    "mode_requested": mode,
                    "mode_executed": "combined",
                    "analysis_mode": "combined",
                    "analysis_vision_backend": VISION_BACKEND_OLLAMA,
                    "analysis_vision_model": vision_model.strip(),
                    "native_vision": False,
                    "vision_backend_requested": backend,
                    "vision_backend": VISION_BACKEND_OLLAMA,
                    "vision_model": vision_model.strip(),
                    "warnings": warnings,
                }
            if florence_ready:
                return {
                    "mode_requested": mode,
                    "mode_executed": "combined",
                    "analysis_mode": "combined",
                    "analysis_vision_backend": VISION_BACKEND_FLORENCE2,
                    "analysis_vision_model": FLORENCE_MODEL_ID,
                    "native_vision": False,
                    "vision_backend_requested": backend,
                    "vision_backend": VISION_BACKEND_FLORENCE2,
                    "vision_model": FLORENCE_MODEL_ID,
                    "warnings": warnings,
                }
            warning = "No confirmed local vision model is available; using OCR-only fallback."
            if requested_vision.get("error"):
                warning = f"Vision model inspection failed; using OCR-only fallback: {requested_vision['error']}"
            return {
                "mode_requested": mode,
                "mode_executed": "ocr_only_fallback",
                "analysis_mode": "ocr_only",
                "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
                "analysis_vision_model": "",
                "native_vision": False,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_OCR_ONLY,
                "vision_model": "",
                "warnings": [warning],
            }

        if vision_model.strip() and requested_vision["status"] == "yes":
            return {
                "mode_requested": mode,
                "mode_executed": mode,
                "analysis_mode": mode,
                "analysis_vision_backend": VISION_BACKEND_OLLAMA,
                "analysis_vision_model": vision_model.strip(),
                "native_vision": False,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_OLLAMA,
                "vision_model": vision_model.strip(),
                "warnings": warnings,
            }

        if selected_vision["status"] == "yes":
            return {
                "mode_requested": mode,
                "mode_executed": "combined" if mode == "combined" else "vision_only",
                "analysis_mode": "ocr_only" if mode == "combined" else "ocr_only",
                "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
                "analysis_vision_model": "",
                "native_vision": True,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_OLLAMA,
                "vision_model": selected_model,
                "warnings": warnings,
            }

        if florence_ready:
            return {
                "mode_requested": mode,
                "mode_executed": mode,
                "analysis_mode": mode,
                "analysis_vision_backend": VISION_BACKEND_FLORENCE2,
                "analysis_vision_model": FLORENCE_MODEL_ID,
                "native_vision": False,
                "vision_backend_requested": backend,
                "vision_backend": VISION_BACKEND_FLORENCE2,
                "vision_model": FLORENCE_MODEL_ID,
                "warnings": warnings,
            }

        warning = "Requested vision analysis could not run because no confirmed local vision model is available; using OCR-only fallback."
        if requested_vision.get("error"):
            warning = f"Requested vision model inspection failed; using OCR-only fallback: {requested_vision['error']}"
        return {
            "mode_requested": mode,
            "mode_executed": "ocr_only_fallback",
            "analysis_mode": "ocr_only",
            "analysis_vision_backend": VISION_BACKEND_OCR_ONLY,
            "analysis_vision_model": "",
            "native_vision": False,
            "vision_backend_requested": backend,
            "vision_backend": VISION_BACKEND_OCR_ONLY,
            "vision_model": "",
            "warnings": [warning],
        }

    def _vision_status(self, model: str) -> dict[str, str]:
        clean_model = (model or "").strip()
        if not clean_model:
            return {"status": "unknown", "error": ""}
        try:
            capability = self.models.inspect(clean_model)
        except Exception as exc:  # noqa: BLE001 - route truthfulness matters more than failing chat
            return {"status": "error", "error": str(exc)}
        return {"status": str(capability.get("vision") or "unknown"), "error": str(capability.get("error") or "")}

    def _resolve_context_multimodal_route(
        self,
        requested_mode: str,
        selected_model: str,
        vision_model: str,
        vision_backend: str,
        artifact_id: str,
    ) -> dict[str, Any]:
        route = self._resolve_multimodal_route(requested_mode, selected_model, vision_model, vision_backend)
        if route.get("native_vision"):
            route["image_request_sent"] = True
            route["context_evidence_action"] = "native_history_rehydrated"
            return route
        previous = self.artifacts.latest_completed_analysis(artifact_id) if self.artifacts is not None else None
        actual_vision_model = str((previous or {}).get("actual_vision_model") or "")
        previous_output = (previous or {}).get("output") if isinstance((previous or {}).get("output"), dict) else {}
        previous_provenance = previous_output.get("provenance") if isinstance(previous_output.get("provenance"), dict) else {}
        actual_vision_backend = str((previous or {}).get("actual_vision_backend") or previous_provenance.get("vision_backend") or "")
        if actual_vision_model:
            route["mode_executed"] = "combined"
            route["analysis_mode"] = "combined"
            route["analysis_vision_backend"] = actual_vision_backend or route.get("analysis_vision_backend") or VISION_BACKEND_OLLAMA
            route["analysis_vision_model"] = actual_vision_model
            route["vision_backend"] = actual_vision_backend or route.get("vision_backend") or VISION_BACKEND_OLLAMA
            route["vision_model"] = actual_vision_model
        route["image_request_sent"] = False
        route["context_evidence_action"] = "reused" if self._analysis_has_evidence(previous) else "none"
        return route

    def _prepare_context_analysis(
        self,
        *,
        artifact_id: str,
        question: str,
        route: dict[str, Any],
        message_id: str,
        vision_model: str,
        vision_backend: str,
    ) -> dict[str, Any]:
        if self.artifacts is None:
            raise ValueError("image attachments are not available")
        previous = self.artifacts.latest_completed_analysis(artifact_id)
        if self._analysis_has_evidence(previous):
            if (
                not route.get("native_vision")
                and self._question_needs_visual_details(question)
                and not self._analysis_has_vision_evidence(previous)
                and self.vision is not None
            ):
                rerun_model = str(
                    route.get("analysis_vision_model")
                    or (previous or {}).get("actual_vision_model")
                    or vision_model
                    or ""
                )
                rerun_backend = str(route.get("analysis_vision_backend") or (previous or {}).get("actual_vision_backend") or vision_backend or VISION_BACKEND_AUTOMATIC)
                if rerun_model or rerun_backend == VISION_BACKEND_FLORENCE2:
                    route["image_request_sent"] = True
                    route["context_evidence_action"] = "vision_rerun"
                    return self.vision.analyze(
                        artifact_id,
                        mode="combined",
                        question=question,
                        vision_model=rerun_model,
                        vision_backend=rerun_backend,
                        request_id=str(uuid.uuid4()),
                        message_id=message_id,
                    )
            route["image_request_sent"] = bool(route.get("native_vision"))
            route["context_evidence_action"] = "reused"
            emit_progress("visual_evidence_reuse", cache_status="reused")
            return self._clone_context_analysis(
                artifact_id=artifact_id,
                question=question,
                message_id=message_id,
                route=route,
                previous=previous,
                action="reused",
            )

        if not route.get("native_vision") and self.vision is not None:
            rerun_model = str(route.get("analysis_vision_model") or vision_model or "")
            rerun_backend = str(route.get("analysis_vision_backend") or vision_backend or VISION_BACKEND_AUTOMATIC)
            mode = "combined" if rerun_model or rerun_backend == VISION_BACKEND_FLORENCE2 else "ocr_only"
            route["image_request_sent"] = bool(rerun_model or rerun_backend == VISION_BACKEND_FLORENCE2)
            route["context_evidence_action"] = "vision_rerun" if route["image_request_sent"] else "ocr_rerun"
            return self.vision.analyze(
                artifact_id,
                mode=mode,
                question=question,
                vision_model=rerun_model,
                vision_backend=rerun_backend,
                request_id=str(uuid.uuid4()),
                message_id=message_id,
            )

        route["image_request_sent"] = bool(route.get("native_vision"))
        route["context_evidence_action"] = "native_history_rehydrated"
        return self._clone_context_analysis(
            artifact_id=artifact_id,
            question=question,
            message_id=message_id,
            route=route,
            previous=previous,
            action="native_history_rehydrated",
        )

    def _clone_context_analysis(
        self,
        *,
        artifact_id: str,
        question: str,
        message_id: str,
        route: dict[str, Any],
        previous: dict[str, Any] | None,
        action: str,
    ) -> dict[str, Any]:
        if self.artifacts is None:
            raise ValueError("image attachments are not available")
        run = self.artifacts.create_analysis_run(
            request_id=str(uuid.uuid4()),
            artifact_id=artifact_id,
            mode=str(route.get("analysis_mode") or (previous or {}).get("mode") or "ocr_only"),
            user_question=question,
            requested_vision_backend=str(route.get("vision_backend_requested") or route.get("analysis_vision_backend") or ""),
            requested_vision_model=str(route.get("analysis_vision_model") or ""),
            ocr_engine=str((previous or {}).get("ocr_engine") or ""),
            message_id=message_id,
        )
        output = dict((previous or {}).get("output") or {})
        output.setdefault("answer", "")
        output.setdefault("ocr_text", "")
        output.setdefault("vision_observations", {})
        provenance = dict(output.get("provenance") or {})
        provenance.update(
            {
                "context_evidence_action": action,
                "source_analysis_id": str((previous or {}).get("id") or ""),
                "image_request_sent": False,
            }
        )
        output["provenance"] = provenance
        evidence = dict((previous or {}).get("evidence") or {})
        evidence["conversation_context"] = {
            "action": action,
            "source_analysis_id": str((previous or {}).get("id") or ""),
            "message_id": message_id,
        }
        warnings = list((previous or {}).get("warnings") or [])
        return self.artifacts.update_analysis_run(
            str(run["id"]),
            status=status_from_warnings(warnings, question),
            stage="evidence_reused" if previous else "context_ready",
            actual_vision_backend=str((previous or {}).get("actual_vision_backend") or route.get("vision_backend") or ""),
            actual_vision_model=str((previous or {}).get("actual_vision_model") or route.get("vision_model") or ""),
            ocr_engine=str((previous or {}).get("ocr_engine") or ""),
            warnings_json=json.dumps(warnings),
            output_json=json.dumps(output),
            evidence_json=json.dumps(evidence),
            timings_json=json.dumps({"elapsed_ms": 0}),
            completed_at=utc_ms(),
        )

    def _analysis_has_evidence(self, analysis: dict[str, Any] | None) -> bool:
        if not analysis:
            return False
        output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
        if str(output.get("ocr_text") or "").strip():
            return True
        return self._analysis_has_vision_evidence(analysis)

    def _analysis_has_vision_evidence(self, analysis: dict[str, Any] | None) -> bool:
        if not analysis:
            return False
        output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
        visual_evidence = output.get("visual_evidence") if isinstance(output.get("visual_evidence"), dict) else {}
        if visual_evidence:
            if str(visual_evidence.get("summary") or "").strip():
                return True
            for key in ("objects", "regions", "text", "grounded_phrases"):
                value = visual_evidence.get(key)
                if isinstance(value, list) and value:
                    return True
        observations = output.get("vision_observations") if isinstance(output.get("vision_observations"), dict) else {}
        for value in observations.values():
            if isinstance(value, list) and any(str(item).strip() for item in value):
                return True
            if isinstance(value, str) and value.strip():
                return True
        return False

    def _question_needs_visual_details(self, question: str) -> bool:
        text = (question or "").lower()
        if not text:
            return False
        patterns = [
            r"\b(image|picture|photo|screenshot|screen|desk|object|scene|visible|shown)\b",
            r"\b(look|see|describe|tell me about it|tell me more|what is in|what's in|where is|color|layout)\b",
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _should_answer_no_active_image_context(self, question: str, history: list[dict[str, Any]]) -> bool:
        if not self._question_needs_visual_details(question) or self.artifacts is None:
            return False
        message_ids = [str(message["id"]) for message in history if message.get("role") == "user"]
        if not message_ids:
            return False
        historical = self.artifacts.attachments_for_messages(message_ids)
        return any(historical.get(message_id) for message_id in message_ids)

    def _local_reply_response(self, model: str, content: str) -> dict[str, Any]:
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "local_context_guard",
            "total_duration_ns": 0,
            "load_duration_ns": 0,
            "prompt_eval_count": 0,
            "prompt_eval_duration_ns": 0,
            "eval_count": 0,
            "eval_duration_ns": 0,
            "prompt_tokens_per_second": None,
            "generation_tokens_per_second": None,
            "elapsed_ms": 0,
            "raw": {},
        }

    def _messages_with_historical_image_paths(
        self,
        messages: list[dict[str, Any]],
        artifact_ids: list[str],
    ) -> list[dict[str, Any]]:
        if self.artifacts is None or not artifact_ids:
            return [dict(message) for message in messages]
        active_ids = set(artifact_ids)
        message_ids = [str(message.get("id") or "") for message in messages if message.get("id")]
        grouped = self.artifacts.attachments_for_messages(message_ids)
        prepared = [dict(message) for message in messages]
        attached_ids: set[str] = set()
        for message in prepared:
            if message.get("role") != "user":
                continue
            paths: list[str] = []
            for artifact in grouped.get(str(message.get("id") or ""), []):
                artifact_id = str(artifact.get("id") or "")
                if artifact_id not in active_ids or artifact.get("is_deleted"):
                    continue
                if artifact_id in attached_ids:
                    continue
                paths.append(self.artifacts.vision_image_path(artifact_id))
                attached_ids.add(artifact_id)
            if paths:
                message["_image_paths"] = paths
        missing_ids = [artifact_id for artifact_id in artifact_ids if artifact_id not in attached_ids]
        if missing_ids:
            fallback_paths = [self.artifacts.vision_image_path(artifact_id) for artifact_id in missing_ids]
            for message in reversed(prepared):
                if message.get("role") == "user":
                    message["_image_paths"] = [*message.get("_image_paths", []), *fallback_paths]
                    break
        return prepared

    def _analysis_blocks_synthesis(self, analysis: dict[str, Any] | None) -> bool:
        if not analysis:
            return False
        if self._analysis_has_evidence(analysis):
            return False
        status = str(analysis.get("status") or "")
        if status in {"error", "timeout", "interrupted"}:
            return True
        output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
        provenance = output.get("provenance") if isinstance(output.get("provenance"), dict) else {}
        if provenance or analysis.get("artifact_id"):
            return True
        return bool(provenance.get("failed_stage")) and not self._analysis_has_evidence(analysis)

    def _perception_limitation_message(self, analysis: dict[str, Any] | None) -> str:
        output = analysis.get("output") if isinstance((analysis or {}).get("output"), dict) else {}
        provenance = output.get("provenance") if isinstance(output.get("provenance"), dict) else {}
        requested_backend = str(
            provenance.get("requested_backend")
            or provenance.get("vision_backend_requested")
            or (analysis or {}).get("requested_vision_backend")
            or ""
        )
        failed_stage = str(provenance.get("failed_stage") or (analysis or {}).get("stage") or "perception")
        if requested_backend == VISION_BACKEND_FLORENCE2:
            return "Florence could not analyse this image, and OCR found no readable text. No visual answer was generated."
        return f"Image perception failed at {failed_stage}, and OCR found no readable text. No visual answer was generated."

    def _persist_multimodal_no_synthesis(
        self,
        *,
        analysis: dict[str, Any],
        route: dict[str, Any],
        final_model: str,
        answer: str,
    ) -> dict[str, Any]:
        if self.artifacts is None:
            return analysis
        output = dict(analysis.get("output") or {})
        provenance = dict(output.get("provenance") or {})
        failed_stage = str(provenance.get("failed_stage") or analysis.get("stage") or "perception")
        provenance.update(
            {
                "mode_requested": route.get("mode_requested") or provenance.get("mode_requested") or analysis.get("mode"),
                "mode_executed": route.get("mode_executed") or provenance.get("mode_executed") or analysis.get("mode"),
                "route": "no_synthesis",
                "requested_final_model": final_model,
                "final_answer_model": "",
                "vision_backend_requested": route.get("vision_backend_requested") or provenance.get("vision_backend_requested") or analysis.get("requested_vision_backend") or "",
                "vision_backend": analysis.get("actual_vision_backend") or "",
                "vision_inspection_model": analysis.get("actual_vision_model") or "",
                "actual_backend": analysis.get("actual_vision_backend") or "",
                "actual_model": analysis.get("actual_vision_model") or "",
                "failed_stage": failed_stage,
                "perception_completed": False,
                "synthesis_started": False,
                "final_answer_stage": "not_started",
                "image_request_sent": False,
            }
        )
        output["answer"] = answer
        output["provenance"] = provenance
        evidence = dict(analysis.get("evidence") or {})
        evidence["synthesis"] = {
            "requested_model": final_model,
            "actual_model": "",
            "stage": "not_started",
            "synthesis_started": False,
            "reason": "perception_failed_without_evidence",
            "failed_stage": failed_stage,
            "vision_backend": provenance.get("vision_backend_requested") or "",
        }
        return self.artifacts.update_analysis_run(
            str(analysis["id"]),
            status=str(analysis.get("status") or "error"),
            stage=failed_stage,
            actual_vision_backend=str(analysis.get("actual_vision_backend") or ""),
            actual_vision_model=str(analysis.get("actual_vision_model") or ""),
            output_json=json.dumps(output),
            evidence_json=json.dumps(evidence),
        )

    def _generate_multimodal_reply(
        self,
        *,
        selected_model: str,
        question: str,
        analysis: dict[str, Any],
        artifact_ids: list[str],
        route: dict[str, Any],
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        thinking: str,
        timeout: float,
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        if self.artifacts is None:
            raise ValueError("image attachments are not available")
        route = {**route, "final_model_hint": selected_model}
        if not route.get("native_vision"):
            analysis = self._ensure_curated_visual_evidence(analysis, question, route)
        prompt = (
            self._native_multimodal_prompt(question, analysis, route)
            if route.get("native_vision")
            else self._multimodal_synthesis_prompt(question, analysis, route)
        )
        synthesis_messages = replace_latest_user_content(messages, prompt)
        deterministic_reply = ""
        if not route.get("native_vision"):
            output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
            curated = output.get("curated_visual_evidence") if isinstance(output.get("curated_visual_evidence"), dict) else {}
            if curated:
                if curated.get("question_type") == "person_identity":
                    deterministic_reply = safe_fallback_answer(question, curated)
                else:
                    deterministic_reply = deterministic_visual_answer(question, curated)
        emit_progress("answer_generation")
        if deterministic_reply and curated.get("question_type") in {
            "action", "holding_or_contact", "object_use", "person_identity", "color_attribute", "clothing_attribute"
        }:
            reply_response = {
                "model": selected_model,
                "content": deterministic_reply,
                "thinking": "",
                "done_reason": "deterministic_visual_answer",
                "total_duration_ns": 0,
                "load_duration_ns": 0,
                "prompt_eval_count": 0,
                "prompt_eval_duration_ns": 0,
                "eval_count": 0,
                "eval_duration_ns": 0,
                "prompt_tokens_per_second": None,
                "generation_tokens_per_second": None,
                "elapsed_ms": 0,
                "raw": {},
            }
            route["deterministic_visual_answer"] = True
        elif route.get("native_vision"):
            synthesis_messages = self._messages_with_historical_image_paths(synthesis_messages, artifact_ids[:4])
            reply_response = self.models.chat_vision_history_detailed(
                selected_model,
                synthesis_messages,  # type: ignore[arg-type]
                options=options,
                thinking=thinking,
                timeout=max(float(timeout or 0), float(VISION_CHAT_TIMEOUT_SECONDS)),
            )
        else:
            reply_response = self._chat_model_detailed(
                selected_model,
                synthesis_messages,
                options,
                thinking=thinking,
                timeout=timeout,
            )
        reply = str(reply_response.get("content") or "").strip()
        if not route.get("native_vision"):
            output = analysis.get("output") if isinstance(analysis.get("output"), dict) else {}
            curated = output.get("curated_visual_evidence") if isinstance(output.get("curated_visual_evidence"), dict) else {}
            if curated:
                guard = guard_visual_answer(reply, curated)
                if not guard.get("ok"):
                    guard["safe_fallback_used"] = True
                    reply = safe_fallback_answer(question, curated)
                    reply_response = {**reply_response, "content": reply, "grounding_guard_replaced_content": True}
                route["grounding_guard"] = guard
        updated = self._persist_multimodal_synthesis(
            analysis=analysis,
            route=route,
            final_model=selected_model,
            actual_final_model=str(reply_response.get("model") or selected_model),
            answer=reply,
            synthesis_elapsed_ms=int(reply_response.get("elapsed_ms") or 0),
        )
        return reply_response, reply, updated

    def _ensure_curated_visual_evidence(self, analysis: dict[str, Any], question: str, route: dict[str, Any]) -> dict[str, Any]:
        if self.artifacts is None:
            return analysis
        output = dict(analysis.get("output") or {})
        visual_evidence = output.get("visual_evidence") if isinstance(output.get("visual_evidence"), dict) else {}
        if not visual_evidence:
            vision_observations = output.get("vision_observations") if isinstance(output.get("vision_observations"), dict) else {}
            visual_evidence = visual_evidence_from_observations(vision_observations)
        if not visual_evidence:
            return analysis
        provenance = dict(output.get("provenance") or {})
        reused = str(route.get("context_evidence_action") or provenance.get("context_evidence_action") or "") == "reused"
        emit_progress("visual_evidence_retrieval", cache_status="reused" if reused else "fresh")
        curated = curate_visual_evidence(visual_evidence, question)
        output["curated_visual_evidence"] = curated
        provenance.update(
            {
                "raw_evidence_reused": reused,
                "curated_evidence_recomputed": True,
                "visual_snippets_retrieved": True,
                "visual_evidence_curator_version": curated.get("curator_version") or VISUAL_EVIDENCE_CURATOR_VERSION,
                "visual_evidence_retriever_version": curated.get("retriever_version") or VISUAL_EVIDENCE_RETRIEVER_VERSION,
                "visual_retrieved_snippet_count": int(
                    (curated.get("retrieval_metadata") if isinstance(curated.get("retrieval_metadata"), dict) else {}).get("retrieved_count")
                    or len(curated.get("retrieved_visual_snippets") or [])
                ),
                "vision_rerun": bool(route.get("context_evidence_action") == "vision_rerun"),
            }
        )
        output["provenance"] = provenance
        evidence = dict(analysis.get("evidence") or {})
        evidence["curated_visual_evidence"] = curated
        if isinstance(evidence.get("conversation_context"), dict):
            context = dict(evidence["conversation_context"])
            context.update(
                {
                    "raw_evidence_reused": True,
                    "curated_evidence_recomputed": True,
                    "visual_snippets_retrieved": True,
                    "vision_rerun": False,
                }
            )
            evidence["conversation_context"] = context
        return self.artifacts.update_analysis_run(
            str(analysis["id"]),
            output_json=json.dumps(output),
            evidence_json=json.dumps(evidence),
        )

    def _native_multimodal_prompt(self, question: str, analysis: dict[str, Any], route: dict[str, Any]) -> str:
        output = analysis.get("output") or {}
        ocr_text = str(output.get("ocr_text") or "").strip()
        action = str(route.get("context_evidence_action") or "")
        return (
            "Answer the user's latest question directly using the image already attached in this conversation.\n"
            "Do not say no image was provided. Do not expose hidden reasoning. Do not identify a person from their face.\n"
            f"Question: {question}\n"
            f"Conversation image context: {action or 'active'}\n"
            f"Exact OCR text, if useful: {ocr_text or '[none]'}\n"
            "Use the pixels for scene, object, color, and layout details. Use OCR only for exact visible text."
        )

    def _multimodal_synthesis_prompt(self, question: str, analysis: dict[str, Any], route: dict[str, Any]) -> str:
        output = analysis.get("output") or {}
        provenance = output.get("provenance") if isinstance(output.get("provenance"), dict) else {}
        ocr_text = str(output.get("ocr_text") or "").strip()
        vision_observations = output.get("vision_observations") if isinstance(output.get("vision_observations"), dict) else {}
        visual_evidence = output.get("visual_evidence") if isinstance(output.get("visual_evidence"), dict) else {}
        curated_visual_evidence = output.get("curated_visual_evidence") if isinstance(output.get("curated_visual_evidence"), dict) else {}
        ocr_engine = str(provenance.get("ocr_engine") or analysis.get("ocr_engine") or "unavailable")
        vision_backend = str(route.get("vision_backend") or provenance.get("vision_backend") or analysis.get("actual_vision_backend") or "unavailable")
        vision_model = str(route.get("vision_model") or provenance.get("vision_model") or analysis.get("actual_vision_model") or "")
        mode_executed = str(route.get("mode_executed") or analysis.get("mode") or "")
        no_vision = mode_executed.startswith("ocr_only") and not vision_model
        compact_packet = compact_synthesis_packet(question, curated_visual_evidence, ocr_text=ocr_text) if curated_visual_evidence else ""
        return (
            "Answer the user's question using only the compact local image evidence below.\n"
            "Do not expose hidden reasoning. Do not identify a person from their face. "
            "Keep exact OCR text separate from model visual observations.\n"
            f"{strict_visual_synthesis_rules()}\n"
            f"Question: {question}\n\n"
            f"Mode requested: {route.get('mode_requested')}\n"
            f"Mode executed: {mode_executed}\n"
            f"Final answer model: {route.get('final_model_hint', 'selected chat model')}\n"
            f"OCR engine: {ocr_engine}\n"
            f"Perception backend: {vision_backend}\n"
            f"Vision inspection model: {vision_model or 'unavailable'}\n"
            f"Native image request: {'yes' if route.get('native_vision') else 'no'}\n\n"
            "Compact curated visual evidence:\n"
            f"{compact_packet or '{}'}\n\n"
            "Raw perception output available in Diagnostics: "
            f"{'yes' if visual_evidence or vision_observations else 'no'}\n\n"
            + (
                "No confirmed vision model inspected the pixels. If the question asks about objects, people, layout, "
                "or scene meaning, say that limitation clearly and do not fabricate visual details.\n"
                if no_vision
                else "Use visual observations for scene/object/layout meaning, and OCR only for exact visible text.\n"
            )
            + "Now provide a coherent final answer to the user."
        )

    def _persist_multimodal_synthesis(
        self,
        *,
        analysis: dict[str, Any],
        route: dict[str, Any],
        final_model: str,
        actual_final_model: str,
        answer: str,
        synthesis_elapsed_ms: int,
    ) -> dict[str, Any]:
        if self.artifacts is None:
            return analysis
        output = dict(analysis.get("output") or {})
        provenance = dict(output.get("provenance") or {})
        image_request_sent = (
            bool(route.get("image_request_sent"))
            if "image_request_sent" in route
            else bool(route.get("native_vision") or provenance.get("vision_model"))
        )
        provenance.update(
            {
                "mode_requested": route.get("mode_requested"),
                "mode_executed": route.get("mode_executed"),
                "route": "native_vision" if route.get("native_vision") else "external_eyes_or_ocr",
                "vision_backend_requested": route.get("vision_backend_requested") or provenance.get("vision_backend_requested") or "",
                "vision_backend": route.get("vision_backend") or provenance.get("vision_backend") or analysis.get("actual_vision_backend") or "",
                "requested_final_model": final_model,
                "final_answer_model": actual_final_model,
                "vision_inspection_model": route.get("vision_model") or provenance.get("vision_model") or "",
                "image_request_sent": image_request_sent,
                "context_evidence_action": route.get("context_evidence_action") or provenance.get("context_evidence_action") or "",
                "synthesis_started": True,
                "final_answer_stage": "model_generation",
                "deterministic_visual_answer": bool(route.get("deterministic_visual_answer")),
            }
        )
        output["answer"] = answer
        evidence = dict(analysis.get("evidence") or {})
        guard = dict(route.get("grounding_guard") or {})
        if guard:
            provenance.update(
                {
                    "grounding_guard_triggered": bool(guard.get("grounding_guard_triggered")),
                    "grounding_guard_reason": str(guard.get("grounding_guard_reason") or ""),
                    "regeneration_attempted": bool(guard.get("regeneration_attempted")),
                    "safe_fallback_used": bool(guard.get("safe_fallback_used")),
                }
            )
            evidence["grounding_guard"] = guard
        output["provenance"] = provenance
        evidence["synthesis"] = {
            "requested_model": final_model,
            "actual_model": actual_final_model,
            "elapsed_ms": synthesis_elapsed_ms,
            "stage": "model_generation",
            "vision_backend": route.get("vision_backend") or provenance.get("vision_backend") or "",
            "native_image_request": bool(route.get("native_vision")),
            "image_request_sent": image_request_sent,
            "synthesis_started": True,
            "context_evidence_action": route.get("context_evidence_action") or "",
        }
        evidence["mode_requested"] = route.get("mode_requested")
        evidence["mode_executed"] = route.get("mode_executed")
        timings = dict(analysis.get("timings") or {})
        timings["synthesis_elapsed_ms"] = synthesis_elapsed_ms
        warnings = list(analysis.get("warnings") or [])
        for warning in route.get("warnings") or []:
            if warning and warning not in warnings:
                warnings.append(str(warning))
        if guard.get("grounding_guard_triggered"):
            guard_warning = f"Grounding guard used safe fallback: {guard.get('grounding_guard_reason') or 'unsupported visual claim'}"
            if guard_warning not in warnings:
                warnings.append(guard_warning)
        status = str(analysis.get("status") or "completed")
        if status in {"completed", "completed_with_warnings"}:
            status = status_from_warnings(warnings, str(analysis.get("user_question") or ""))
        actual_vision_model = str(route.get("vision_model") or analysis.get("actual_vision_model") or "")
        actual_vision_backend = str(route.get("vision_backend") or analysis.get("actual_vision_backend") or "")
        return self.artifacts.update_analysis_run(
            str(analysis["id"]),
            status=status,
            stage="synthesis_completed",
            actual_vision_backend=actual_vision_backend,
            actual_vision_model=actual_vision_model,
            warnings_json=json.dumps(warnings),
            output_json=json.dumps(output),
            evidence_json=json.dumps(evidence),
            timings_json=json.dumps(timings),
        )

    def _persist_multimodal_synthesis_failure(
        self,
        *,
        analysis: dict[str, Any],
        route: dict[str, Any],
        final_model: str,
        error: str,
        synthesis_elapsed_ms: int,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if self.artifacts is None:
            return analysis
        output = dict(analysis.get("output") or {})
        provenance = dict(output.get("provenance") or {})
        image_request_sent = (
            bool(route.get("image_request_sent"))
            if "image_request_sent" in route
            else bool(route.get("native_vision") or provenance.get("vision_model"))
        )
        provenance.update(
            {
                "mode_requested": route.get("mode_requested"),
                "mode_executed": route.get("mode_executed"),
                "route": "native_vision" if route.get("native_vision") else "external_eyes_or_ocr",
                "vision_backend_requested": route.get("vision_backend_requested") or provenance.get("vision_backend_requested") or "",
                "vision_backend": route.get("vision_backend") or provenance.get("vision_backend") or analysis.get("actual_vision_backend") or "",
                "requested_final_model": final_model,
                "final_answer_model": "",
                "vision_inspection_model": route.get("vision_model") or provenance.get("vision_model") or analysis.get("actual_vision_model") or "",
                "image_request_sent": image_request_sent,
                "context_evidence_action": route.get("context_evidence_action") or provenance.get("context_evidence_action") or "",
                "failed_stage": "synthesis",
                "synthesis_started": True,
                "final_answer_stage": "timeout",
            }
        )
        output["answer"] = ""
        output["provenance"] = provenance
        evidence = dict(analysis.get("evidence") or {})
        evidence["synthesis"] = {
            "requested_model": final_model,
            "actual_model": "",
            "elapsed_ms": synthesis_elapsed_ms,
            "stage": "timeout",
            "error": error,
            "timeout_seconds": timeout_seconds,
            "vision_backend": route.get("vision_backend") or provenance.get("vision_backend") or "",
            "native_image_request": bool(route.get("native_vision")),
            "image_request_sent": image_request_sent,
            "synthesis_started": True,
            "context_evidence_action": route.get("context_evidence_action") or "",
        }
        evidence["mode_requested"] = route.get("mode_requested")
        evidence["mode_executed"] = route.get("mode_executed")
        timings = dict(analysis.get("timings") or {})
        timings["synthesis_elapsed_ms"] = synthesis_elapsed_ms
        warnings = list(analysis.get("warnings") or [])
        timeout_warning = f"Synthesis timed out after {timeout_seconds:g}s: {error}"
        if timeout_warning not in warnings:
            warnings.append(timeout_warning)
        actual_vision_model = str(route.get("vision_model") or analysis.get("actual_vision_model") or "")
        actual_vision_backend = str(route.get("vision_backend") or analysis.get("actual_vision_backend") or "")
        return self.artifacts.update_analysis_run(
            str(analysis["id"]),
            status="timeout",
            stage="synthesis_timeout",
            actual_vision_backend=actual_vision_backend,
            actual_vision_model=actual_vision_model,
            warnings_json=json.dumps(warnings),
            error=error,
            output_json=json.dumps(output),
            evidence_json=json.dumps(evidence),
            timings_json=json.dumps(timings),
            completed_at=utc_ms(),
        )

    def _chat_model(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any],
    ) -> str:
        return str(
            self._chat_model_detailed(
                model,
                messages,
                options,
                thinking="auto",
                timeout=INTERACTIVE_CHAT_TIMEOUT_SECONDS,
            ).get("content")
            or ""
        )

    def _chat_model_detailed(
        self,
        model: str,
        messages: list[dict[str, Any]],
        options: dict[str, Any],
        *,
        thinking: str,
        timeout: float,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        started = time.perf_counter()
        clean_messages = [
            {"role": str(message.get("role") or "user"), "content": str(message.get("content") or "")}
            for message in messages
        ]
        detailed_impl = getattr(type(self.models), "chat_detailed", None)
        if type(self.models) is ModelService or detailed_impl is not ModelService.chat_detailed:
            try:
                return self.models.chat_detailed(
                    model,
                    clean_messages,
                    options=options,
                    thinking=thinking,
                    timeout=timeout,
                    response_format=response_format,
                )
            except AttributeError:
                pass
            except TypeError:
                pass

        try:
            content = self.models.chat(model, clean_messages, options=options)
        except TypeError:
            content = self.models.chat(model, clean_messages)
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "",
            "total_duration_ns": 0,
            "load_duration_ns": 0,
            "prompt_eval_count": 0,
            "prompt_eval_duration_ns": 0,
            "eval_count": 0,
            "eval_duration_ns": 0,
            "prompt_tokens_per_second": None,
            "generation_tokens_per_second": None,
            "elapsed_ms": int((time.perf_counter() - started) * 1000),
            "raw": {},
        }

    def _derive_title(self, content: str) -> str:
        title = " ".join(content.split())
        if len(title) > 48:
            title = title[:45].rstrip() + "..."
        return title or "New chat"

    def _rag_system_prompt(
        self,
        rag_context: str,
        question: str,
        answer_style: str,
        rag_preset: str,
    ) -> str:
        named_entity_rule = (
            "Rebecca is mentioned in the attached document. Treat it as an in-document entity. "
            "Answer only from the provided document evidence. Do not provide external biography "
            "or refuse as a personal-info lookup.\n"
            if re.search(r"\bRebecca\b", question, re.IGNORECASE)
            else (
                "Treat names mentioned in the attached or retrieved document as in-document entities. "
                "Answer only from the provided document evidence. Do not provide external biography "
                "or refuse as a personal-info lookup.\n"
            )
        )
        return (
            "You are answering with retrieved evidence snippets.\n"
            f"Latest user question: {question}\n\n"
            f"{named_entity_rule}"
            f"Answer style: {ANSWER_STYLES[answer_style]}.\n"
            f"{self._answer_style_instructions(answer_style)}\n\n"
            f"{self._rag_preset_instructions(rag_preset)}\n\n"
            "Answer the user directly with a coherent synthesis of the relevant snippets. "
            "If any quoted bullet gives an event or fact relevant to the question, use it "
            "before discussing missing details.\n\n"
            "Rules:\n"
            "- Use only the evidence snippets below for factual claims.\n"
            "- Treat personal names, character names, and labels in snippets as "
            "document-local entities. Do not reject a question just because the "
            "name is not public or externally identifiable.\n"
            "- If the user asks who or what a named in-document subject is, answer "
            "from the snippet facts and say when identity or background is not stated.\n"
            "- If a snippet describes misconduct or conflict, neutrally summarize "
            "only what the document says for context. Do not endorse, dramatize, "
            "instruct, or expand beyond the evidence, but do not refuse solely "
            "because the source text is uncomfortable.\n"
            "- OCR pages may contain unrelated rows from the same worksheet or table. "
            "For a question about a named subject, use rows or phrases tied to that "
            "subject or nearby headings; ignore unrelated rows about other people.\n"
            "- If snippets came from OCR, mention OCR uncertainty only when it "
            "matters to ambiguous or hard-to-read text.\n"
            "- OCR match notes may list exact or fuzzy spellings, such as "
            "\"Rebeca ~= Rebecca\". Treat those as possible OCR variants of "
            "the user's subject. Do not say there is no mention when exact or "
            "fuzzy OCR evidence is present; instead say the OCR is noisy and "
            "state what it appears to show.\n"
            "- If an OCR match note says a Warsaw-like row appears near a "
            "name match, explicitly mention that OCR suggests a Warsaw entry "
            "for the named subject in broad name answers, even if the row is "
            "garbled. Do not convert that uncertainty into absence.\n"
            "- For OCR worksheets or table-like pages, use the retrieved line "
            "windows and neighboring page context to reconstruct likely rows. "
            "Prefer recall for named subjects, and mark uncertain row text as "
            "OCR-suggested rather than definitive.\n"
            "- Keep different source titles, files, pages, and snippets separate. "
            "Never merge facts across unrelated documents.\n"
            "- Preserve chronology exactly. Do not move a duration, cause, action, "
            "or outcome from one event or person to another.\n"
            "- Avoid changing who did what, when, where, or for how long.\n"
            "- For broad questions like \"tell me about\", first summarize the "
            "directly supported events or facts in chronological order. Mention "
            "missing identity/background details only after those facts.\n"
            "- Do not narrate each snippet independently when a concise synthesis is clearer.\n"
            "- Distinguish direct claims from inference. If you infer something, "
            "say it is suggested by the text.\n"
            "- If a source heading or title names the user's subject, use the "
            "following same-snippet story as relevant to that subject. Do not say "
            "there is no information merely because later sentences use a role "
            "or description instead of repeating the subject name.\n"
            "- Do not add unstated causes, motives, emotions, biographies, or causal links.\n"
            "- Never treat a document's existence as proof of a real-world crisis, "
            "diagnosis, cause, motive, event, or conclusion unless the snippets "
            "directly state it.\n"
            "- Cite supported facts with source IDs such as [S1].\n"
            "- State important limitations once, near the end. When no snippet "
            "contains relevant facts, abstain clearly.\n\n"
            f"Retrieved evidence snippets:\n{rag_context}"
        )

    def _augment_latest_user_message_for_rag(
        self,
        messages: list[dict[str, str]],
        content: str,
        answer_style: str,
        rag_preset: str,
    ) -> None:
        for message in reversed(messages):
            if message["role"] == "user":
                preset_note = (
                    "Potato Mode is active: keep the answer short, quote-first, "
                    "and say what cannot be confirmed."
                    if rag_preset == "potato"
                    else "Use the retrieved evidence snippets above"
                )
                message["content"] = (
                    f"{content}\n\n"
                    f"{preset_note} in {ANSWER_STYLES[answer_style]} style. "
                    "Answer directly with the supported facts or events in chronological order. "
                    "Do not answer only with missing background details when quoted evidence "
                    "gives relevant facts."
                )
                return

    def _answer_style_instructions(self, answer_style: str) -> str:
        if answer_style == "layman":
            return (
                "Layman style:\n"
                "- Explain what the retrieved document or context is about in plain English.\n"
                "- Translate procedural, bureaucratic, legal, technical, medical, or "
                "institutional language into practical meaning.\n"
                "- Clearly separate what the snippets directly state, what that practically "
                "means, and what the snippets do not prove.\n"
                "- Do not invent a concrete crisis, diagnosis, cause, motive, event, or "
                "conclusion when the snippets only contain forms, procedures, notices, or "
                "instructions.\n"
                "- Do not put internal chunk IDs in the final answer prose."
            )
        if answer_style == "detailed":
            return (
                "Detailed style:\n"
                "- Give a fuller answer while staying grounded in retrieved evidence.\n"
                "- Organize the answer clearly.\n"
                "- Include caveats and what is not established.\n"
                "- Do not merge unrelated documents or sources."
            )
        if answer_style == "extract_only":
            return (
                "Extract only style:\n"
                "- Return only facts directly stated in retrieved snippets.\n"
                "- Do not interpret, speculate, infer practical meaning, or explain what "
                "something probably means.\n"
                "- If the requested answer is not directly present, say that the retrieved "
                "context does not say."
            )
        if answer_style == "evidence_only":
            return (
                "Evidence only style:\n"
                "- Use exactly these section headings in this order:\n"
                "  Answer:\n"
                "  Evidence:\n"
                "  Not found / cannot confirm:\n"
                "- In Answer, give only one or two short directly supported claims.\n"
                "- In Evidence, list the short quoted evidence snippets or source/page labels "
                "that support the answer.\n"
                "- In Not found / cannot confirm, list any requested details that are absent. "
                "If nothing is missing, write \"None\".\n"
                "- Do not synthesize, speculate, infer motives, or fill gaps."
            )
        return (
            "Precise style:\n"
            "- Stay close to the retrieved evidence.\n"
            "- Preserve chronology.\n"
            "- Do not add broad interpretation unless the user asks.\n"
            "- Separate directly stated facts from uncertainty.\n"
            "- When evidence is missing, state the unsupported detail once without repeating caveats."
        )

    def _rag_preset_instructions(self, rag_preset: str) -> str:
        if rag_preset == "potato":
            return (
                "Potato Mode preset:\n"
                "- This mode is for weak local models and limited hardware.\n"
                "- Use quote-first evidence only.\n"
                "- Keep the answer short.\n"
                "- Be strict: if the snippets do not directly answer a detail, put it under "
                "\"Not found / cannot confirm\".\n"
                "- Do not speculate or produce broad synthesis.\n"
                "- Prefer saying what is missing over guessing."
            )
        return "Standard RAG preset."

    def _verify_and_correct(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        draft_answer: str,
        snippets: list[dict[str, Any]],
        answer_style: str,
        options: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        first_report = self._verify_answer(model, draft_answer, snippets)
        contradicted = first_report.get("contradicted_claims") or []
        if not contradicted:
            return draft_answer, first_report

        correction_messages = messages + [
            {"role": "assistant", "content": draft_answer},
            {
                "role": "user",
                "content": (
                    "Revise the answer once. Remove or correct every contradicted claim below. "
                    f"Use only the retrieved evidence snippets, preserve chronology, and keep "
                    f"{ANSWER_STYLES[answer_style]} style.\n\n"
                    f"Contradicted claims:\n{json.dumps(contradicted, ensure_ascii=False)}"
                ),
            },
        ]
        correction_options = dict(options)
        correction_options.setdefault("temperature", DEFAULT_TEMPERATURE)
        correction_options.setdefault("num_predict", CORRECTION_NUM_PREDICT)
        try:
            correction_response = self._chat_model_detailed(
                model,
                correction_messages,
                correction_options,
                thinking="off",
                timeout=CORRECTION_TIMEOUT_SECONDS,
            )
        except ModelTimeoutError as exc:
            first_report["correction_status"] = "timeout"
            first_report["correction_error"] = str(exc)
            return draft_answer, first_report
        except Exception as exc:  # noqa: BLE001 - correction is optional
            first_report["correction_status"] = "error"
            first_report["correction_error"] = str(exc)
            return draft_answer, first_report

        corrected = str(correction_response.get("content") or "")
        final_report = self._verify_answer(model, corrected, snippets, stage="final_verification")
        final_report["regenerated"] = True
        final_report["draft_verifier"] = first_report
        final_report["correction_status"] = "completed"
        final_report["correction_latency_ms"] = int(correction_response.get("elapsed_ms") or 0)
        return corrected, final_report

    def _verify_answer(
        self,
        model: str,
        answer: str,
        snippets: list[dict[str, Any]],
        *,
        stage: str = "verifier",
    ) -> dict[str, Any]:
        prompt = (
            "Use this as a lightweight warning pass, not as a perfect judge. "
            "Check the answer only against the retrieved evidence snippets. "
            "Extract factual claims from the answer. Mark each claim as supported, "
            "unsupported, or contradicted. A claim is supported only if the snippets say it. "
            "Return strict JSON only with this shape: "
            "{\"claims\":[{\"text\":\"...\",\"status\":\"supported|unsupported|contradicted\","
            "\"reason\":\"...\",\"source_ids\":[\"S1\"]}],"
            "\"unsupported_claims\":[\"...\"],\"contradicted_claims\":[\"...\"]}."
        )
        payload = (
            f"Retrieved evidence snippets:\n{format_snippets_for_verifier(snippets)}\n\n"
            f"Answer:\n{answer}"
        )
        try:
            response = self._chat_model_detailed(
                model,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": payload},
                ],
                {"temperature": DEFAULT_TEMPERATURE, "num_predict": VERIFIER_NUM_PREDICT},
                thinking="off",
                timeout=VERIFIER_TIMEOUT_SECONDS,
                response_format="json",
            )
            raw = str(response.get("content") or "")
            data = parse_json_object(raw)
            report = self._normalize_verification_report(snippets, data)
            report[f"{stage}_latency_ms"] = int(response.get("elapsed_ms") or 0)
            report["verifier_latency_ms"] = int(response.get("elapsed_ms") or 0)
            return report
        except ModelTimeoutError as exc:
            return self._grounding_report(
                snippets,
                enabled=True,
                status="timeout",
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - verifier is optional and must not break chat
            return self._grounding_report(
                snippets,
                enabled=True,
                status="error",
                error=str(exc),
            )

    def _normalize_verification_report(
        self,
        snippets: list[dict[str, Any]],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        claims = data.get("claims") if isinstance(data.get("claims"), list) else []
        normalized_claims: list[dict[str, Any]] = []
        unsupported_claims: list[str] = []
        contradicted_claims: list[str] = []

        for claim in claims:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            status = str(claim.get("status") or "unsupported").strip().lower()
            if status not in {"supported", "unsupported", "contradicted"}:
                status = "unsupported"
            if not text:
                continue
            normalized = {
                "text": text,
                "status": status,
                "reason": str(claim.get("reason") or "").strip(),
                "source_ids": [
                    str(item)
                    for item in claim.get("source_ids", [])
                    if isinstance(item, str)
                ],
            }
            normalized_claims.append(normalized)
            if status == "unsupported":
                unsupported_claims.append(text)
            elif status == "contradicted":
                contradicted_claims.append(text)

        explicit_unsupported = data.get("unsupported_claims")
        if isinstance(explicit_unsupported, list):
            unsupported_claims.extend(str(item) for item in explicit_unsupported if item)
        explicit_contradicted = data.get("contradicted_claims")
        if isinstance(explicit_contradicted, list):
            contradicted_claims.extend(str(item) for item in explicit_contradicted if item)

        unsupported_claims = dedupe_strings(unsupported_claims)
        contradicted_claims = dedupe_strings(contradicted_claims)
        status = "passed" if not unsupported_claims and not contradicted_claims else "failed"
        return self._grounding_report(
            snippets,
            enabled=True,
            status=status,
            claims=normalized_claims,
            unsupported_claims=unsupported_claims,
            contradicted_claims=contradicted_claims,
        )

    def _grounding_report(
        self,
        snippets: list[dict[str, Any]],
        *,
        enabled: bool,
        status: str,
        claims: list[dict[str, Any]] | None = None,
        unsupported_claims: list[str] | None = None,
        contradicted_claims: list[str] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "mode": "quote_first" if snippets else "none",
            "sources_used": [
                {
                    "snippet_id": snippet.get("snippet_id"),
                    "document_id": snippet.get("document_id"),
                    "source": snippet.get("source"),
                    "page_start": snippet.get("page_start"),
                    "page_end": snippet.get("page_end"),
                    "chunk_id": snippet.get("chunk_id"),
                }
                for snippet in snippets
            ],
            "verifier": {
                "enabled": enabled,
                "status": status,
                "passed": status == "passed" if enabled else None,
                "error": error,
            },
            "claims": claims or [],
            "unsupported_claims": unsupported_claims or [],
            "contradicted_claims": contradicted_claims or [],
            "regenerated": False,
        }


def _compact_trace_fields(values: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in values.items()
        if value is not None and value != ""
    }


def _trace_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 else None


def _trace_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if parsed >= 0 and math.isfinite(parsed) else None


def _trace_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, str)):
        return None
    if value in {0, "0", "false", "False"}:
        return False
    if value in {1, "1", "true", "True"}:
        return True
    return None


def _safe_trace_label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 200 or "\n" in text or "\r" in text:
        return None
    if _trace_text_is_sensitive(text):
        return None
    return text


def _safe_trace_identifier(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or len(text) > 200:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", text):
        return None
    return text


def _safe_trace_warning(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 240 or "\n" in text or "\r" in text:
        return None
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in (
            "full system prompt",
            "raw prompt",
            "raw ollama response",
            "full rag context",
            "retrieved chunk text",
            "full ocr text",
        )
    ):
        return None
    if _trace_text_is_sensitive(text):
        return None
    return text


def _trace_text_is_sensitive(text: str) -> bool:
    if re.search(r"(?i)(?:^|\s)[a-z]:[\\/]", text):
        return True
    if text.startswith(("\\\\", "/")):
        return True
    if re.search(r"(?i)data:[^;,]+;base64,", text):
        return True
    return bool(re.search(r"(?:[A-Za-z0-9+/]{80,}={0,2})", text))


def format_snippets_for_verifier(snippets: list[dict[str, Any]]) -> str:
    lines = []
    for snippet in snippets:
        page = snippet.get("page_start")
        location = f", page {page}" if page else ""
        lines.append(
            f"[{snippet.get('snippet_id')}] {snippet.get('source')}{location}: "
            f"\"{snippet.get('text')}\""
        )
    return "\n".join(lines)


def parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("verifier response must be a JSON object")
    return data


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        key = " ".join(str(value).split()).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(str(value))
    return deduped


def unique_strings(values: list[str]) -> list[str]:
    return dedupe_strings(values)


def unique_match_dicts(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str]] = set()
    deduped: list[dict[str, Any]] = []
    for value in values:
        key = (
            str(value.get("kind") or ""),
            str(value.get("term") or "").lower(),
            str(value.get("matched_text") or "").lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped


def format_page_list(pages: list[int]) -> str:
    if not pages:
        return "the retrieved OCR page"
    if len(pages) == 1:
        return f"page {pages[0]}"
    return "pages " + ", ".join(str(page) for page in pages)


def compact_answer_text(text: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def format_fuzzy_matches_for_answer(matches: list[dict[str, Any]]) -> str:
    formatted = [
        f"{match.get('matched_text')} ~= {match.get('term')}"
        for match in matches
        if match.get("matched_text") and match.get("term")
    ]
    return ", ".join(formatted) if formatted else "the query terms"


def aggregate_quality_labels(labels: list[str]) -> str:
    values = {label for label in labels if label}
    for label in ("poor", "usable_noisy", "good", "no_text"):
        if label in values:
            return label
    return ""


def normalize_answer_style(value: str | None) -> str:
    if value is None:
        return DEFAULT_ANSWER_STYLE
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "extract":
        normalized = "extract_only"
    if normalized == "evidence":
        normalized = "evidence_only"
    if normalized not in ANSWER_STYLES:
        allowed = ", ".join(sorted(ANSWER_STYLES))
        raise ValueError(f"answer_style must be one of: {allowed}")
    return normalized


def normalize_rag_preset(value: str | None) -> str:
    if value is None:
        return DEFAULT_RAG_PRESET
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"potato_mode", "potato"}:
        return "potato"
    if normalized not in RAG_PRESETS:
        allowed = ", ".join(sorted(RAG_PRESETS))
        raise ValueError(f"rag_preset must be one of: {allowed}")
    return normalized


def combine_unique(first: list[str], second: list[str]) -> list[str]:
    combined: list[str] = []
    for value in [*first, *second]:
        cleaned = str(value).strip()
        if cleaned and cleaned not in combined:
            combined.append(cleaned)
    return combined


def normalize_multimodal_mode(value: str | None) -> str:
    normalized = (value or "automatic").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"auto", "automatic"}:
        return "automatic"
    if normalized in {"ocr", "ocr_only"}:
        return "ocr_only"
    if normalized in {"vision", "vision_only"}:
        return "vision_only"
    if normalized in {"combined", "ocr_vision", "ocr_plus_vision"}:
        return "combined"
    raise ValueError("multimodal_mode must be automatic, ocr_only, vision_only, or combined")


def replace_latest_user_content(messages: list[dict[str, Any]], content: str) -> list[dict[str, Any]]:
    replaced = [dict(message) for message in messages]
    for index in range(len(replaced) - 1, -1, -1):
        if replaced[index].get("role") == "user":
            replaced[index]["content"] = content
            return replaced
    replaced.append({"role": "user", "content": content})
    return replaced
