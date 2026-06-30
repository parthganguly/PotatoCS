from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.progress import emit_progress
from odysseus_desktop_backend.services.artifact_service import ARTIFACT_PROMPT_VERSION, ArtifactService
from odysseus_desktop_backend.services.florence2_service import FLORENCE_MODEL_ID, Florence2Backend, Florence2Error
from odysseus_desktop_backend.services.model_service import (
    ModelService,
    ModelServiceError,
    ModelTimeoutError,
    VISION_CHAT_TIMEOUT_SECONDS,
)
from odysseus_desktop_backend.services.ocr_service import OCRService
from odysseus_desktop_backend.services.perception_types import (
    VISION_BACKEND_AUTOMATIC,
    VISION_BACKEND_FLORENCE2,
    VISION_BACKEND_OCR_ONLY,
    VISION_BACKEND_OLLAMA,
    normalize_vision_backend,
    plan_perception_tasks,
    visual_evidence_to_legacy_observations,
)
from odysseus_desktop_backend.services.visual_evidence_curator import (
    curate_visual_evidence,
    status_from_warnings,
    warning_affects_status,
)
from odysseus_desktop_backend.storage import utc_ms


ANALYSIS_MODES = {"automatic", "ocr_only", "vision_only", "combined"}
VISION_OBSERVATION_SCHEMA = {
    "summary": "",
    "visible_objects": [],
    "spatial_relations": [],
    "interface_elements": [],
    "uncertain_observations": [],
    "not_visible_or_not_determinable": [],
    "model_visible_text": [],
}
logger = get_logger("vision")


class VisionService:
    def __init__(
        self,
        artifacts: ArtifactService,
        ocr: OCRService,
        models: ModelService,
        florence: Florence2Backend | None = None,
    ):
        self.artifacts = artifacts
        self.ocr = ocr
        self.models = models
        self.florence = florence

    def analyze(
        self,
        artifact_id: str,
        *,
        mode: str,
        question: str = "",
        vision_model: str = "",
        vision_backend: str = VISION_BACKEND_AUTOMATIC,
        request_id: str = "",
        crop_derivation_id: str = "",
        message_id: str = "",
    ) -> dict[str, Any]:
        requested_mode = normalize_mode(mode)
        requested_backend = normalize_vision_backend(vision_backend)
        clean_mode = requested_mode
        selected_backend = VISION_BACKEND_OCR_ONLY
        warnings: list[str] = []
        if requested_mode == "ocr_only" or requested_backend == VISION_BACKEND_OCR_ONLY:
            clean_mode = "ocr_only"
            selected_backend = VISION_BACKEND_OCR_ONLY
        elif requested_backend == VISION_BACKEND_FLORENCE2:
            clean_mode = "combined" if requested_mode == "automatic" else requested_mode
            selected_backend = VISION_BACKEND_FLORENCE2
        elif requested_backend == VISION_BACKEND_OLLAMA:
            clean_mode = requested_mode
            selected_backend = VISION_BACKEND_OLLAMA
            if requested_mode == "automatic":
                clean_mode = "combined"
        else:
            clean_mode = "ocr_only"
            selected_backend = VISION_BACKEND_OCR_ONLY
            if vision_model:
                try:
                    capability = self.models.inspect(vision_model)
                    if capability.get("vision") == "yes":
                        clean_mode = "combined"
                        selected_backend = VISION_BACKEND_OLLAMA
                    else:
                        warnings.append(f"{vision_model} is not confirmed vision-capable; using OCR-only fallback.")
                except Exception as exc:  # noqa: BLE001 - automatic mode should fall back truthfully
                    warnings.append(f"Vision capability inspection failed; using OCR-only fallback: {exc}")
            if selected_backend == VISION_BACKEND_OCR_ONLY and self._florence_ready():
                clean_mode = "combined"
                selected_backend = VISION_BACKEND_FLORENCE2
                warnings = [warning.replace("using OCR-only fallback", "using Florence-2 Basic fallback") for warning in warnings]
        clean_request_id = request_id.strip() or str(uuid.uuid4())
        existing = self.artifacts.analysis_by_request(clean_request_id)
        if existing and existing["status"] not in {"running", "pending"}:
            return existing
        artifact = self.artifacts.get(artifact_id)
        if artifact["is_deleted"]:
            raise ValueError("cannot analyze a deleted artifact")
        run = existing or self.artifacts.create_analysis_run(
            request_id=clean_request_id,
            artifact_id=artifact_id,
            mode=clean_mode,
            user_question=question,
            requested_vision_backend=requested_backend,
            requested_vision_model=vision_model,
            ocr_engine="",
            message_id=message_id,
        )
        run_id = run["id"]
        evidence: dict[str, Any] = {
            "artifact": artifact_summary(artifact),
            "mode_requested": requested_mode,
            "mode_executed": clean_mode,
            "vision_backend_requested": requested_backend,
            "vision_backend_selected": selected_backend,
            "vision_model_requested": vision_model,
            "perception_completed": False,
            "tasks_requested": [],
            "tasks_completed": [],
            "ocr": None,
            "vision": None,
            "crop_derivation_id": crop_derivation_id,
        }
        output: dict[str, Any] = {
            "answer": "",
            "ocr_text": "",
            "vision_observations": dict(VISION_OBSERVATION_SCHEMA),
            "visual_evidence": {},
            "provenance": {},
        }
        started = time.perf_counter()
        try:
            emit_progress("image_prepare")
            preprocessing_started = time.perf_counter()
            image_paths = self._image_paths_for_analysis(artifact_id, crop_derivation_id)
            preprocessing_ms = int((time.perf_counter() - preprocessing_started) * 1000)
            derivative_details = self.artifacts.derivative_details(artifact_id) if not crop_derivation_id else {}
            evidence["preprocessing"] = {
                "original": derivative_details.get("original") or artifact_summary(artifact),
                "vision_input": derivative_details.get("vision_input") or {},
                "ocr_input": derivative_details.get("ocr_input") or {},
                "preprocessing_version": (derivative_details.get("vision_input") or {}).get("preprocessing_version") or "",
                "elapsed_ms": preprocessing_ms,
                "crop_derivation_id": crop_derivation_id,
            }
            ocr_text = ""
            ocr_engine = ""
            if clean_mode in {"ocr_only", "combined"}:
                emit_progress("ocr_run")
                self.artifacts.update_analysis_run(run_id, stage="ocr")
                ocr_result = self.ocr.run_image_ocr(image_paths["ocr"], source_id=artifact_id)
                ocr_engine = str(ocr_result.get("engine_name") or "")
                ocr_text = str(ocr_result.get("text") or "")
                ocr_warning = str(ocr_result.get("warning") or "")
                if ocr_warning and warning_affects_status(ocr_warning, question):
                    warnings.append(ocr_warning)
                evidence["ocr"] = {
                    "available": bool(ocr_result.get("available")),
                    "engine_name": ocr_engine,
                    "confidence": ocr_result.get("confidence"),
                    "warning": ocr_warning,
                    "metadata": ocr_result.get("metadata") or {},
                }
                if ocr_text:
                    self.artifacts.insert_text_derivation(
                        artifact_id,
                        "ocr_text",
                        ocr_text,
                        producer_type="ocr",
                        producer_name=ocr_engine,
                        confidence=ocr_result.get("confidence") if isinstance(ocr_result.get("confidence"), (int, float)) else None,
                        metadata={
                            **(ocr_result.get("metadata") or {}),
                            "input": evidence["preprocessing"].get("ocr_input") or {},
                        },
                    )
                output["ocr_text"] = ocr_text

            vision_payload: dict[str, Any] = dict(VISION_OBSERVATION_SCHEMA)
            actual_vision_model = ""
            actual_vision_backend = VISION_BACKEND_OCR_ONLY if clean_mode == "ocr_only" else ""
            if clean_mode in {"vision_only", "combined"}:
                self.artifacts.update_analysis_run(run_id, stage="vision")
                if selected_backend == VISION_BACKEND_FLORENCE2:
                    if self.florence is None:
                        raise ValueError("Florence-2 Basic local vision is unavailable in this runtime")
                    task_plan = plan_perception_tasks(question, clean_mode)
                    evidence["tasks_requested"] = task_plan.to_dict().get("tasks") or []
                    response = self.florence.inspect(
                        image_paths["vision"],
                        question=question,
                        task_plan=task_plan,
                        request_id=clean_request_id,
                        input_metadata=evidence["preprocessing"].get("vision_input") or {},
                    )
                    actual_vision_model = str(response.get("model") or FLORENCE_MODEL_ID)
                    actual_vision_backend = VISION_BACKEND_FLORENCE2
                    visual_evidence = response.get("visual_evidence") if isinstance(response.get("visual_evidence"), dict) else {}
                    emit_progress("visual_evidence_build")
                    curated_visual_evidence = curate_visual_evidence(visual_evidence, question) if visual_evidence else {}
                    task_warnings = [str(item) for item in response.get("warnings") or [] if str(item)]
                    warnings.extend(task_warnings)
                    vision_payload = visual_evidence_to_legacy_observations(visual_evidence)
                    tasks_completed = [
                        str(item)
                        for item in response.get("tasks_completed")
                        or visual_evidence.get("tasks_completed")
                        or visual_evidence.get("tasks")
                        or []
                    ]
                    tasks_failed = response.get("tasks_failed") if isinstance(response.get("tasks_failed"), list) else visual_evidence.get("tasks_failed") or []
                    evidence["tasks_completed"] = tasks_completed
                    evidence["tasks_failed"] = tasks_failed
                    evidence["perception_completed"] = bool(tasks_completed)
                    evidence["vision"] = {
                        "backend": VISION_BACKEND_FLORENCE2,
                        "model": actual_vision_model,
                        "requested_model": FLORENCE_MODEL_ID,
                        "elapsed_ms": int(response.get("elapsed_ms") or 0),
                        "tasks": task_plan.to_dict(),
                        "tasks_completed": tasks_completed,
                        "tasks_failed": tasks_failed,
                        "done_reason": "completed",
                    }
                    evidence["visual_evidence"] = visual_evidence
                    evidence["curated_visual_evidence"] = curated_visual_evidence
                    self.artifacts.insert_text_derivation(
                        artifact_id,
                        "vision_observations",
                        json.dumps(vision_payload, ensure_ascii=False, indent=2),
                        producer_type="vision_model",
                        producer_name="florence2",
                        producer_model=actual_vision_model,
                        prompt_version=ARTIFACT_PROMPT_VERSION,
                        metadata={
                            "backend": VISION_BACKEND_FLORENCE2,
                            "mode": clean_mode,
                            "input": evidence["preprocessing"].get("vision_input") or {},
                            "tasks": task_plan.to_dict(),
                            "tasks_completed": tasks_completed,
                            "tasks_failed": tasks_failed,
                        },
                    )
                    self.artifacts.insert_text_derivation(
                        artifact_id,
                        "visual_evidence",
                        json.dumps(visual_evidence, ensure_ascii=False, indent=2),
                        producer_type="vision_model",
                        producer_name="florence2",
                        producer_model=actual_vision_model,
                        prompt_version=ARTIFACT_PROMPT_VERSION,
                        metadata={"backend": VISION_BACKEND_FLORENCE2, "mode": clean_mode},
                    )
                    if curated_visual_evidence:
                        self.artifacts.insert_text_derivation(
                            artifact_id,
                            "curated_visual_evidence",
                            json.dumps(curated_visual_evidence, ensure_ascii=False, indent=2),
                            producer_type="analysis",
                            producer_name="odysseus",
                            producer_model=actual_vision_model,
                            prompt_version=ARTIFACT_PROMPT_VERSION,
                            metadata={"backend": VISION_BACKEND_FLORENCE2, "mode": clean_mode},
                        )
                    output["vision_observations"] = vision_payload
                    output["visual_evidence"] = visual_evidence
                    output["curated_visual_evidence"] = curated_visual_evidence
                    if self.florence and os.environ.get("ODYSSEUS_KEEP_FLORENCE_LOADED") != "1":
                        self.florence.unload()
                else:
                    if not vision_model:
                        raise ValueError("a confirmed local vision model is required for this mode")
                    capability = self.models.inspect(vision_model)
                    if capability.get("vision") != "yes":
                        raise ValueError(f"{vision_model} is not confirmed as a vision-capable local Ollama model")
                    emit_progress("local_vision")
                    response = self.models.chat_vision_detailed(
                        vision_model,
                        [{"role": "user", "content": vision_prompt(question, clean_mode, bool(ocr_text))}],
                        image_paths=[image_paths["vision"]],
                        options={"temperature": 0.0},
                        thinking="off",
                        timeout=VISION_CHAT_TIMEOUT_SECONDS,
                    )
                    actual_vision_model = str(response.get("model") or vision_model)
                    actual_vision_backend = VISION_BACKEND_OLLAMA
                    vision_payload = parse_vision_observations(str(response.get("content") or ""))
                    emit_progress("visual_evidence_build")
                    evidence["perception_completed"] = True
                    evidence["vision"] = {
                        "backend": VISION_BACKEND_OLLAMA,
                        "model": actual_vision_model,
                        "requested_model": vision_model,
                        "elapsed_ms": int(response.get("elapsed_ms") or 0),
                        "done_reason": response.get("done_reason") or "",
                    }
                    self.artifacts.insert_text_derivation(
                        artifact_id,
                        "vision_observations",
                        json.dumps(vision_payload, ensure_ascii=False, indent=2),
                        producer_type="vision_model",
                        producer_name="ollama",
                        producer_model=actual_vision_model,
                        prompt_version=ARTIFACT_PROMPT_VERSION,
                        metadata={
                            "backend": VISION_BACKEND_OLLAMA,
                            "mode": clean_mode,
                            "input": evidence["preprocessing"].get("vision_input") or {},
                        },
                    )
                    output["vision_observations"] = vision_payload

            final_answer = synthesize_answer(question, clean_mode, ocr_text, vision_payload, warnings)
            output["answer"] = final_answer
            output["provenance"] = {
                "ocr_engine": ocr_engine,
                "requested_backend": requested_backend,
                "requested_model": vision_model or (FLORENCE_MODEL_ID if selected_backend == VISION_BACKEND_FLORENCE2 else ""),
                "actual_backend": actual_vision_backend,
                "actual_model": actual_vision_model,
                "vision_backend_requested": requested_backend,
                "vision_backend": actual_vision_backend,
                "vision_model": actual_vision_model,
                "perception_completed": bool(evidence.get("perception_completed") or ocr_text.strip()),
                "failed_stage": "",
                "tasks_requested": evidence.get("tasks_requested") or [],
                "tasks_completed": evidence.get("tasks_completed") or [],
                "synthesis_started": False,
                "mode_requested": requested_mode,
                "mode_executed": clean_mode,
                "prompt_version": ARTIFACT_PROMPT_VERSION,
                "preprocessing_version": evidence["preprocessing"].get("preprocessing_version") or "",
            }
            combined_text = combined_evidence_text(ocr_text, vision_payload, final_answer)
            if combined_text.strip():
                self.artifacts.insert_text_derivation(
                    artifact_id,
                    "combined_evidence",
                    combined_text,
                    producer_type="analysis",
                    producer_name="odysseus",
                    producer_model=actual_vision_model,
                    prompt_version=ARTIFACT_PROMPT_VERSION,
                    metadata={"mode": clean_mode},
                )

            status = status_from_warnings(warnings, question)
            return self.artifacts.update_analysis_run(
                run_id,
                status=status,
                stage="completed",
                actual_vision_backend=actual_vision_backend,
                actual_vision_model=actual_vision_model,
                ocr_engine=ocr_engine,
                warnings_json=json.dumps(warnings),
                output_json=json.dumps(output),
                evidence_json=json.dumps(evidence),
                timings_json=json.dumps({
                    "elapsed_ms": int((time.perf_counter() - started) * 1000),
                    "preprocessing_elapsed_ms": preprocessing_ms,
                }),
                completed_at=utc_ms(),
            )
        except ModelTimeoutError as exc:
            return self._finalize_error(run_id, "timeout", str(exc), warnings, evidence, started, failed_stage="inference")
        except Florence2Error as exc:
            logger.warning("florence analysis failed artifact_id=%s request_id=%s stage=%s error=%s", artifact_id, clean_request_id, exc.stage, exc)
            if exc.diagnostics:
                evidence["florence_diagnostics"] = exc.diagnostics
            return self._finalize_error(run_id, "error", str(exc), warnings, evidence, started, failed_stage=exc.stage)
        except (ModelServiceError, Exception) as exc:  # noqa: BLE001 - persisted analysis is the recovery surface
            logger.warning("artifact analysis failed artifact_id=%s request_id=%s error=%s", artifact_id, clean_request_id, exc)
            return self._finalize_error(run_id, "error", str(exc), warnings, evidence, started, failed_stage="inference")

    def _finalize_error(
        self,
        run_id: str,
        status: str,
        error: str,
        warnings: list[str],
        evidence: dict[str, Any],
        started: float,
        failed_stage: str = "",
    ) -> dict[str, Any]:
        requested_backend = str(evidence.get("vision_backend_requested") or "")
        requested_model = str(evidence.get("vision_model_requested") or "")
        if requested_backend == VISION_BACKEND_FLORENCE2 and not requested_model:
            requested_model = FLORENCE_MODEL_ID
        return self.artifacts.update_analysis_run(
            run_id,
            status=status,
            stage=failed_stage or status,
            error=error,
            warnings_json=json.dumps(warnings),
            actual_vision_backend="",
            actual_vision_model="",
            output_json=json.dumps({
                "answer": "",
                "ocr_text": "",
                "vision_observations": dict(VISION_OBSERVATION_SCHEMA),
                "visual_evidence": {},
                "provenance": {
                    "requested_backend": requested_backend,
                    "requested_model": requested_model,
                    "actual_backend": "",
                    "actual_model": "",
                    "vision_backend_requested": requested_backend,
                    "vision_backend": "",
                    "vision_model": "",
                    "failed_stage": failed_stage or status,
                    "perception_completed": False,
                    "synthesis_started": False,
                    "tasks_requested": evidence.get("tasks_requested") or [],
                    "tasks_completed": [],
                },
            }),
            evidence_json=json.dumps({**evidence, "failed_stage": failed_stage or status, "perception_completed": False}),
            timings_json=json.dumps({"elapsed_ms": int((time.perf_counter() - started) * 1000)}),
            completed_at=utc_ms(),
        )

    def _image_paths_for_analysis(self, artifact_id: str, crop_derivation_id: str) -> dict[str, str]:
        if crop_derivation_id:
            derivation = self.artifacts._derivation(crop_derivation_id)  # internal provenance lookup
            if derivation["artifact_id"] != artifact_id or derivation["kind"] != "crop":
                raise ValueError("crop derivation does not belong to the artifact")
            return {"ocr": str(derivation["stored_path"]), "vision": str(derivation["stored_path"])}
        return {
            "ocr": self.artifacts.ocr_image_path(artifact_id),
            "vision": self.artifacts.vision_image_path(artifact_id),
        }

    def _florence_ready(self) -> bool:
        if self.florence is None:
            return False
        try:
            return bool(self.florence.status(check_hashes=False).get("ready"))
        except Exception:
            return False

    def florence_available(self) -> bool:
        return self._florence_ready()


def normalize_mode(value: str) -> str:
    mode = (value or "automatic").strip()
    if mode not in ANALYSIS_MODES:
        raise ValueError("multimodal mode must be automatic, ocr_only, vision_only, or combined")
    return mode


def vision_prompt(question: str, mode: str, has_ocr: bool) -> str:
    ocr_note = (
        "Exact OCR text is handled separately. Do not claim your model-read text is exact OCR."
        if has_ocr
        else "If text is visible, list it under model_visible_text and mark uncertainty when needed."
    )
    return (
        "You are analyzing a user-supplied local image in Odysseus Desktop.\n"
        "Return concise JSON with these keys only: summary, visible_objects, spatial_relations, "
        "interface_elements, uncertain_observations, not_visible_or_not_determinable, model_visible_text.\n"
        "Distinguish directly visible facts from uncertainty. Abstain when a requested fact is not visible. "
        "Do not infer hidden context, source paths, filenames, or private data.\n"
        f"Mode: {mode}. {ocr_note}\n"
        f"User question: {question or 'Describe the image with careful evidence labels.'}"
    )


def parse_vision_observations(content: str) -> dict[str, Any]:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = {"summary": content}
    result = dict(VISION_OBSERVATION_SCHEMA)
    if isinstance(parsed, dict):
        for key in result:
            value = parsed.get(key)
            if key == "summary":
                result[key] = str(value or "")
            elif isinstance(value, list):
                result[key] = [str(item) for item in value[:50]]
            elif value:
                result[key] = [str(value)]
    return result


def synthesize_answer(
    question: str,
    mode: str,
    ocr_text: str,
    vision_payload: dict[str, Any],
    warnings: list[str],
) -> str:
    if mode == "ocr_only":
        if ocr_text.strip():
            return "Exact OCR text was extracted and is available as image evidence."
        return "No OCR text could be extracted from the image."
    if mode == "vision_only":
        summary = str(vision_payload.get("summary") or "").strip()
        return summary or "The selected vision model returned no visual observations."
    summary = str(vision_payload.get("summary") or "").strip()
    parts = []
    if ocr_text.strip():
        parts.append("OCR text is available and should be treated as the exact visible text.")
    if summary:
        parts.append(f"Vision observations: {summary}")
    if warnings:
        parts.append("Warnings: " + "; ".join(warnings))
    return " ".join(parts) or "No OCR text or vision observations were available."


def combined_evidence_text(ocr_text: str, vision_payload: dict[str, Any], answer: str) -> str:
    return (
        "OCR text:\n"
        f"{ocr_text.strip() or '[none]'}\n\n"
        "Vision observations:\n"
        f"{json.dumps(vision_payload, ensure_ascii=False, indent=2)}\n\n"
        "Final answer:\n"
        f"{answer}"
    )


def artifact_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": artifact.get("id"),
        "kind": artifact.get("kind"),
        "source_kind": artifact.get("source_kind"),
        "name": artifact.get("name"),
        "mime_type": artifact.get("mime_type"),
        "width": artifact.get("width"),
        "height": artifact.get("height"),
        "size_bytes": artifact.get("size_bytes"),
        "format": artifact.get("original_format") or artifact.get("mime_type"),
        "color_mode": artifact.get("original_color_mode") or "",
        "pixels": int(artifact.get("width") or 0) * int(artifact.get("height") or 0),
        "has_alpha": bool(artifact.get("original_has_alpha") or False),
    }
