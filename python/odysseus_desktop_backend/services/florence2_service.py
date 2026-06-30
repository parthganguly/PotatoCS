from __future__ import annotations

import gc
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.progress import emit_progress
from odysseus_desktop_backend.services.perception_types import (
    FLORENCE_TASK_CAPTION,
    FLORENCE_TASK_DENSE_REGION_CAPTION,
    FLORENCE_TASK_DETAILED_CAPTION,
    FLORENCE_TASK_MORE_DETAILED_CAPTION,
    FLORENCE_TASK_OBJECT_DETECTION,
    FLORENCE_TASK_OCR,
    FLORENCE_TASK_OCR_WITH_REGION,
    FLORENCE_TASK_OPEN_VOCABULARY_DETECTION,
    FLORENCE_TASK_PHRASE_GROUNDING,
    PerceptionTaskPlan,
    normalize_florence_outputs,
)


FLORENCE_PACK_ID = "florence2-base-ft"
FLORENCE_MODEL_ID = "microsoft/Florence-2-base-ft"
FLORENCE_MODEL_REVISION = "f6c1a25888ffc1d945ee8a1a77ac833c7303d46e"
FLORENCE_MODEL_LICENSE = "MIT"
FLORENCE_MANIFEST_NAME = "manifest.json"
FLORENCE_LEGACY_MANIFEST_NAMES = ("odysseus-florence2-manifest.json",)
FLORENCE_RUNTIME_PINS = {
    "torch": "2.12.0",
    "torchvision": "0.27.0",
    "transformers": "5.12.1",
    "safetensors": "0.8.0",
    "tokenizers": "0.22.2",
    "huggingface_hub": "1.19.0",
}
FLORENCE_REQUIRED_FILES = {
    "LICENSE": {"size_bytes": 1141},
    "README.md": {"size_bytes": 14820},
    "config.json": {"size_bytes": 2430},
    "model.safetensors": {"size_bytes": 463221266},
    "preprocessor_config.json": {"size_bytes": 806},
    "tokenizer.json": {"size_bytes": 1355863},
    "tokenizer_config.json": {"size_bytes": 34},
    "vocab.json": {"size_bytes": 1099884},
}
FLORENCE_ALLOWED_FILES = set(FLORENCE_REQUIRED_FILES)
FLORENCE_TASK_PROMPTS = {
    FLORENCE_TASK_CAPTION: "<CAPTION>",
    FLORENCE_TASK_DETAILED_CAPTION: "<DETAILED_CAPTION>",
    FLORENCE_TASK_MORE_DETAILED_CAPTION: "<MORE_DETAILED_CAPTION>",
    FLORENCE_TASK_OBJECT_DETECTION: "<OD>",
    FLORENCE_TASK_DENSE_REGION_CAPTION: "<DENSE_REGION_CAPTION>",
    FLORENCE_TASK_PHRASE_GROUNDING: "<CAPTION_TO_PHRASE_GROUNDING>",
    FLORENCE_TASK_OPEN_VOCABULARY_DETECTION: "<OPEN_VOCABULARY_DETECTION>",
    FLORENCE_TASK_OCR: "<OCR>",
    FLORENCE_TASK_OCR_WITH_REGION: "<OCR_WITH_REGION>",
}
FLORENCE_IMAGE_TOKEN = "<image>"
FLORENCE_LEGACY_SPECIAL_TOKENS = (
    ["<od>", "</od>", "<ocr>", "</ocr>"]
    + [f"<loc_{index}>" for index in range(1000)]
    + [
        "<cap>",
        "</cap>",
        "<ncap>",
        "</ncap>",
        "<dcap>",
        "</dcap>",
        "<grounding>",
        "</grounding>",
        "<seg>",
        "</seg>",
        "<sep>",
        "<region_cap>",
        "</region_cap>",
        "<region_to_desciption>",
        "</region_to_desciption>",
        "<proposal>",
        "</proposal>",
        "<poly>",
        "</poly>",
        "<and>",
    ]
)
FLORENCE_POST_PROCESSOR_CONFIG = {
    "ocr": {},
    "phrase_grounding": {},
    "pure_text": {},
    "description_with_bboxes": {},
    "description_with_polygons": {},
    "description_with_bboxes_or_polygons": {},
    "polygons": {},
    "bboxes": {},
}


class Florence2Error(RuntimeError):
    def __init__(self, message: str, *, stage: str = "inference", diagnostics: dict[str, Any] | None = None):
        super().__init__(message)
        self.stage = stage
        self.diagnostics = diagnostics or {}


class Florence2Backend:
    def __init__(
        self,
        profile_dir: str | Path,
        *,
        resource_dir: str | Path | None = None,
        dev_repo_root: str | Path | None = None,
        app_data_dir: str | Path | None = None,
    ):
        self.profile_dir = Path(profile_dir)
        self.resource_dir = Path(resource_dir) if resource_dir else None
        self.dev_repo_root = Path(dev_repo_root) if dev_repo_root else None
        self.app_data_dir = Path(app_data_dir) if app_data_dir else None
        self._processor: Any = None
        self._model: Any = None
        self._device = "cpu"
        self._last_load_status: dict[str, Any] = {
            "model_load": stage_status("not_started"),
            "processor_load": stage_status("not_started"),
        }

    def diagnostics(self) -> dict[str, Any]:
        return self.status(check_hashes=False)

    def status(self, *, check_hashes: bool = False) -> dict[str, Any]:
        discovery = self._discover_pack(check_hashes=check_hashes)
        selected = discovery.get("selected") if isinstance(discovery.get("selected"), dict) else None
        pack_dir = Path(str(selected["path"])) if selected else None
        deps = dependency_status()
        result: dict[str, Any] = {
            "pack_id": FLORENCE_PACK_ID,
            "model_id": FLORENCE_MODEL_ID,
            "revision": FLORENCE_MODEL_REVISION,
            "license": FLORENCE_MODEL_LICENSE,
            "pack_dir": str(pack_dir) if pack_dir else "",
            "selected_pack_path": str(pack_dir) if pack_dir else "",
            "selected_pack_source": str(selected.get("source") or "") if selected else "",
            "searched_candidates": discovery["candidates"],
            "path_context": self.path_context(),
            "python_executable": sys.executable,
            "state": "missing",
            "ready": False,
            "pack_ready": False,
            "runtime_ready": bool(deps.get("ready")),
            "message": "Florence-2 model pack is not installed.",
            "failed_stage": "pack_discovery",
            "normal_runtime_downloads": False,
            "trust_remote_code": False,
            "dependencies": deps,
            "runtime_pins": FLORENCE_RUNTIME_PINS,
            "native_class_status": native_class_status(deps),
            "required_files": list(FLORENCE_REQUIRED_FILES),
            "hashes_checked": False,
            "loaded": self._model is not None,
            "stages": {
                "pack_discovery": stage_status("failed", "Florence-2 model pack is not installed."),
                "manifest_parse": stage_status("not_started"),
                "manifest_validation": stage_status("not_started"),
                "checksum_verification": stage_status("not_started"),
                "runtime_import": stage_status("ready" if deps.get("ready") else "failed", runtime_message(deps), dependencies=deps),
                "model_load": self._last_load_status.get("model_load", stage_status("not_started")),
                "processor_load": self._last_load_status.get("processor_load", stage_status("not_started")),
                "inference": stage_status("not_started"),
                "output_parse": stage_status("not_started"),
            },
        }
        if pack_dir is None:
            failed = discovery.get("first_existing_failure")
            if isinstance(failed, dict):
                failed_stage = str(failed.get("failed_stage") or "manifest_validation")
                message = str(failed.get("rejection_reason") or "Florence-2 model pack candidate failed validation.")
                result["stages"]["pack_discovery"] = stage_status(
                    "ready",
                    "Found a Florence-2 model pack candidate, but it failed validation.",
                    path=str(failed.get("path") or ""),
                    source=str(failed.get("source") or ""),
                )
                if failed_stage in result["stages"]:
                    result["stages"][failed_stage] = stage_status("failed", message)
                result.update(
                    {
                        "state": failed_stage,
                        "ready": False,
                        "pack_ready": False,
                        "message": message,
                        "failed_stage": failed_stage,
                        "hashes_checked": bool(check_hashes),
                    }
                )
            return result
        result["stages"]["pack_discovery"] = stage_status(
            "ready",
            "Selected Florence-2 model pack path.",
            path=str(pack_dir),
            source=str(selected.get("source") or ""),
        )
        manifest_info = selected.get("manifest_info") if isinstance(selected.get("manifest_info"), dict) else {}
        result["stages"]["manifest_parse"] = stage_status("ready", "Manifest parsed.", **manifest_info)
        result["stages"]["manifest_validation"] = stage_status("ready", "Manifest metadata is valid.")
        if check_hashes:
            result["stages"]["checksum_verification"] = stage_status("ready", "Manifest checksums verified.")
        else:
            result["stages"]["checksum_verification"] = stage_status("not_checked", "Checksums are verified before model load or explicit pack verification.")
        manifest = selected.get("manifest") if isinstance(selected.get("manifest"), dict) else {}
        result.update(
            {
                "state": "ready" if deps["ready"] else "runtime_missing",
                "ready": bool(deps["ready"]),
                "pack_ready": True,
                "message": "Florence-2 local pack is ready." if deps["ready"] else "Florence-2 pack exists but optional runtime dependencies are missing.",
                "failed_stage": "" if deps["ready"] else "runtime_import",
                "manifest": {
                    "pack_id": manifest.get("pack_id"),
                    "model_id": manifest.get("model_id"),
                    "revision": manifest.get("revision"),
                    "created_at": manifest.get("created_at"),
                    "file_count": len(manifest.get("files") or {}),
                },
                "hashes_checked": bool(check_hashes),
            }
        )
        return result

    def verify_pack(self) -> dict[str, Any]:
        return self.status(check_hashes=True)

    def inspect(
        self,
        image_path: str,
        *,
        question: str,
        task_plan: PerceptionTaskPlan | dict[str, Any],
        request_id: str = "",
        input_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        status = self.status(check_hashes=True)
        if not status.get("ready"):
            raise Florence2Error(
                str(status.get("message") or "Florence-2 is unavailable"),
                stage=str(status.get("failed_stage") or "runtime_import"),
                diagnostics=status,
            )
        pack_dir = Path(str(status["pack_dir"]))
        started = time.perf_counter()
        if self._model is None or self._processor is None:
            emit_progress("florence_load", cache_status="miss")
        processor, model, device = self._load(pack_dir)
        emit_progress("local_vision")
        from PIL import Image
        import torch

        with Image.open(image_path) as image:
            rgb_image = image.convert("RGB")
            width, height = rgb_image.size
            raw_by_task: dict[str, Any] = {}
            tasks_completed: list[str] = []
            tasks_failed: list[dict[str, str]] = []
            plan_dict = task_plan.to_dict() if isinstance(task_plan, PerceptionTaskPlan) else dict(task_plan)
            for task in plan_dict.get("tasks") or []:
                prompt = FLORENCE_TASK_PROMPTS.get(str(task))
                if not prompt:
                    continue
                try:
                    prompt_text = self._prompt_for_task(prompt, question, plan_dict)
                    inputs = processor(text=prompt_text, images=rgb_image, return_tensors="pt")
                    inputs = {key: value.to(device) if hasattr(value, "to") else value for key, value in inputs.items()}
                    with torch.no_grad():
                        generated_ids = model.generate(
                            input_ids=inputs.get("input_ids"),
                            pixel_values=inputs.get("pixel_values"),
                            max_new_tokens=max_tokens_for_task(str(task)),
                            num_beams=1,
                            do_sample=False,
                        )
                    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
                    parsed = post_process_florence_generation(processor, generated_text, prompt, image_size=(width, height))
                    raw_by_task[str(task)] = parsed
                    tasks_completed.append(str(task))
                except Exception as exc:  # noqa: BLE001 - partial task success is valid evidence
                    tasks_failed.append({"task": str(task), "stage": "inference", "error": str(exc)})
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not raw_by_task:
            raise Florence2Error(
                "Florence-2 inference failed before producing visual evidence.",
                stage="inference",
                diagnostics={"tasks_failed": tasks_failed, "elapsed_ms": elapsed_ms},
            )
        evidence = normalize_florence_outputs(
            raw_by_task,
            model=FLORENCE_MODEL_ID,
            question=question,
            plan=task_plan,
            image_width=width,
            image_height=height,
            elapsed_ms=elapsed_ms,
        )
        if not visual_evidence_has_content(evidence):
            raise Florence2Error(
                "Florence-2 output parsing produced no usable visual evidence.",
                stage="output_parse",
                diagnostics={"tasks_completed": tasks_completed, "tasks_failed": tasks_failed, "elapsed_ms": elapsed_ms},
            )
        evidence["request_id"] = request_id
        evidence["input"] = input_metadata or {}
        evidence["tasks_completed"] = tasks_completed
        evidence["tasks_failed"] = tasks_failed
        return {
            "model": FLORENCE_MODEL_ID,
            "backend": "florence2",
            "elapsed_ms": elapsed_ms,
            "visual_evidence": evidence,
            "raw_task_count": len(raw_by_task),
            "tasks_requested": [str(item) for item in plan_dict.get("tasks") or []],
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "warnings": [f"{item['task']} failed: {item['error']}" for item in tasks_failed],
        }

    def unload(self) -> dict[str, Any]:
        was_loaded = self._model is not None or self._processor is not None
        self._model = None
        self._processor = None
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:
            pass
        return {"unloaded": was_loaded, "loaded": False}

    def smoke_test(self, image_path: str | None = None) -> dict[str, Any]:
        if image_path:
            target = image_path
        else:
            target = str(self.profile_dir / "florence2-smoke-test.png")
            from PIL import Image, ImageDraw

            image = Image.new("RGB", (96, 64), (245, 245, 240))
            draw = ImageDraw.Draw(image)
            draw.rectangle((12, 18, 84, 48), outline=(40, 80, 90), width=2)
            draw.text((18, 24), "TEST", fill=(20, 20, 20))
            image.save(target, format="PNG")
        plan = PerceptionTaskPlan(tasks=[FLORENCE_TASK_MORE_DETAILED_CAPTION], reason="smoke_test")
        return self.inspect(target, question="Describe this test image.", task_plan=plan)

    def _load(self, pack_dir: Path) -> tuple[Any, Any, str]:
        if self._model is not None and self._processor is not None:
            return self._processor, self._model, self._device
        try:
            import torch
            from transformers import AutoImageProcessor, AutoTokenizer, Florence2Config, Florence2ForConditionalGeneration
            from transformers.models.florence2.processing_florence2 import Florence2Processor
        except Exception as exc:  # noqa: BLE001 - exact import failure belongs in diagnostics
            self._last_load_status["runtime_import"] = stage_status("failed", str(exc))
            raise Florence2Error(f"Florence-2 runtime import failed: {exc}", stage="runtime_import") from exc

        device = "cuda" if os.environ.get("ODYSSEUS_FLORENCE_ENABLE_CUDA") == "1" and torch.cuda.is_available() else "cpu"
        dtype = torch.float16 if device == "cuda" else torch.float32
        try:
            processor_started = time.perf_counter()
            tokenizer = AutoTokenizer.from_pretrained(str(pack_dir), local_files_only=True, trust_remote_code=False)
            token_info = prepare_native_florence_tokenizer(tokenizer)
            image_processor = AutoImageProcessor.from_pretrained(str(pack_dir), local_files_only=True, trust_remote_code=False)
            processor = Florence2Processor(
                image_processor=image_processor,
                tokenizer=tokenizer,
                post_processor_config=FLORENCE_POST_PROCESSOR_CONFIG,
            )
            self._last_load_status["processor_load"] = stage_status(
                "ready",
                "Florence-2 processor loaded from local files.",
                tokenizer_length=token_info["tokenizer_length"],
                image_token_id=token_info["image_token_id"],
                legacy_tokens_added=token_info["legacy_tokens_added"],
                image_token_added=token_info["image_token_added"],
                elapsed_ms=int((time.perf_counter() - processor_started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact actionable load error
            self._last_load_status["processor_load"] = stage_status("failed", str(exc))
            raise Florence2Error(f"Florence-2 processor load failed: {exc}", stage="processor_load") from exc
        try:
            model_started = time.perf_counter()
            config = Florence2Config.from_pretrained(str(pack_dir), local_files_only=True, trust_remote_code=False)
            model = Florence2ForConditionalGeneration(config)
            checkpoint_info = load_native_florence_checkpoint(model, pack_dir, torch)
            tokenizer_length = len(processor.tokenizer)
            embedding_rows = int(model.get_input_embeddings().weight.shape[0])
            if tokenizer_length > embedding_rows:
                try:
                    model.resize_token_embeddings(tokenizer_length, mean_resizing=False)
                except TypeError:
                    model.resize_token_embeddings(tokenizer_length)
                embedding_rows = int(model.get_input_embeddings().weight.shape[0])
            model.config.image_token_id = int(processor.image_token_id)
            if getattr(model, "generation_config", None) is not None:
                model.generation_config.image_token_id = int(processor.image_token_id)
            model.to(device=device, dtype=dtype)
            model.eval()
            self._last_load_status["model_load"] = stage_status(
                "ready",
                "Florence-2 model loaded from local files.",
                device=device,
                tokenizer_length=tokenizer_length,
                embedding_rows=embedding_rows,
                image_token_id=int(processor.image_token_id),
                remapped_checkpoint_keys=checkpoint_info["loaded_keys"],
                transposed_checkpoint_keys=checkpoint_info["transposed_keys"],
                elapsed_ms=int((time.perf_counter() - model_started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 - preserve exact actionable load error
            self._last_load_status["model_load"] = stage_status("failed", str(exc))
            raise Florence2Error(f"Florence-2 model load failed: {exc}", stage="model_load") from exc
        self._processor = processor
        self._model = model
        self._device = device
        return processor, model, device

    def path_context(self) -> dict[str, str]:
        return {
            "profile_dir": str(self.profile_dir),
            "app_data_dir": str(self.app_data_dir or ""),
            "resource_dir": str(self.resource_dir or ""),
            "dev_repo_root": str(self.dev_repo_root or ""),
            "florence_model_dir_env": os.environ.get("ODYSSEUS_FLORENCE_MODEL_DIR", ""),
            "copied_backend_dir": str(Path(__file__).resolve().parent),
            "cwd": os.getcwd(),
        }

    def _candidate_paths(self) -> list[tuple[str, Path]]:
        candidates: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def add(source: str, path: Path | None) -> None:
            if path is None:
                return
            normalized = str(path.expanduser().resolve(strict=False))
            key = normalized.lower() if os.name == "nt" else normalized
            if key in seen:
                return
            seen.add(key)
            candidates.append((source, Path(normalized)))

        env_dir = os.environ.get("ODYSSEUS_FLORENCE_MODEL_DIR", "").strip()
        if env_dir:
            add("env_override", Path(env_dir))
        add("profile_models", self.profile_dir / "models" / FLORENCE_PACK_ID)
        if self.app_data_dir is not None:
            add("app_data_models", self.app_data_dir / "models" / FLORENCE_PACK_ID)
        if self.resource_dir is not None:
            add("tauri_resource", self.resource_dir / "models" / FLORENCE_PACK_ID)
        if self.dev_repo_root is not None:
            add("debug_repo_root", self.dev_repo_root / "models" / FLORENCE_PACK_ID)
        return candidates

    def _discover_pack(self, *, check_hashes: bool) -> dict[str, Any]:
        candidates: list[dict[str, Any]] = []
        selected: dict[str, Any] | None = None
        first_existing_failure: dict[str, Any] | None = None
        for source, path in self._candidate_paths():
            candidate = self._evaluate_candidate(source, path, check_hashes=check_hashes)
            candidates.append(candidate)
            if candidate.get("ready") and selected is None:
                selected = candidate
            elif candidate.get("exists") and first_existing_failure is None:
                first_existing_failure = candidate
        return {
            "selected": selected,
            "first_existing_failure": first_existing_failure if selected is None else None,
            "candidates": candidates,
        }

    def _evaluate_candidate(self, source: str, path: Path, *, check_hashes: bool) -> dict[str, Any]:
        manifest_path = resolve_manifest_path(path)
        candidate: dict[str, Any] = {
            "source": source,
            "path": str(path),
            "exists": path.exists(),
            "is_dir": path.is_dir() if path.exists() else False,
            "manifest_path": str(manifest_path),
            "manifest_present": manifest_path.exists(),
            "manifest_parsed": False,
            "manifest_valid": False,
            "files_valid": False,
            "checksums_valid": False if check_hashes else "not_checked",
            "hashes_checked": bool(check_hashes),
            "ready": False,
            "failed_stage": "pack_discovery",
            "rejection_reason": "path does not exist",
        }
        if not candidate["exists"]:
            return candidate
        if not candidate["is_dir"]:
            candidate["rejection_reason"] = "candidate path exists but is not a directory"
            return candidate
        try:
            manifest, manifest_info = load_manifest_with_info(path)
            candidate["manifest"] = manifest
            candidate["manifest_info"] = manifest_info
            candidate["manifest_parsed"] = True
        except Exception as exc:  # noqa: BLE001 - diagnostics surface exact parse failures
            candidate["failed_stage"] = getattr(exc, "stage", "manifest_parse")
            candidate["rejection_reason"] = str(exc)
            return candidate
        try:
            verify_manifest_shape(manifest)
            candidate["manifest_valid"] = True
        except Exception as exc:  # noqa: BLE001 - diagnostics surface exact validation failures
            candidate["failed_stage"] = getattr(exc, "stage", "manifest_validation")
            candidate["rejection_reason"] = str(exc)
            return candidate
        try:
            verify_pack_files(path, manifest, check_hashes=check_hashes)
            candidate["files_valid"] = True
            candidate["checksums_valid"] = True if check_hashes else "not_checked"
        except Exception as exc:  # noqa: BLE001 - diagnostics surface exact file failures
            candidate["failed_stage"] = getattr(exc, "stage", "checksum_verification")
            candidate["rejection_reason"] = str(exc)
            return candidate
        candidate["ready"] = True
        candidate["failed_stage"] = ""
        candidate["rejection_reason"] = ""
        return candidate

    def _prompt_for_task(self, prompt: str, question: str, plan: dict[str, Any]) -> str:
        if prompt in {"<OPEN_VOCABULARY_DETECTION>", "<CAPTION_TO_PHRASE_GROUNDING>"}:
            phrases = [str(item) for item in plan.get("target_phrases") or [] if str(item).strip()]
            phrase_text = "; ".join(phrases) if phrases else (question or "object")
            return f"{prompt}{phrase_text}"
        return prompt


def load_manifest(pack_dir: Path) -> dict[str, Any]:
    manifest, _info = load_manifest_with_info(pack_dir)
    return manifest


def prepare_native_florence_tokenizer(tokenizer: Any) -> dict[str, Any]:
    existing_tokens = additional_special_tokens(tokenizer)
    legacy_tokens = dedupe(existing_tokens + list(FLORENCE_LEGACY_SPECIAL_TOKENS))
    legacy_tokens_added = int(tokenizer.add_special_tokens({"additional_special_tokens": legacy_tokens}) or 0)
    tokens_with_image = dedupe(additional_special_tokens(tokenizer) + [FLORENCE_IMAGE_TOKEN])
    image_token_added = int(tokenizer.add_special_tokens({"additional_special_tokens": tokens_with_image}) or 0)
    image_token_id = tokenizer.convert_tokens_to_ids(FLORENCE_IMAGE_TOKEN)
    unk_token_id = getattr(tokenizer, "unk_token_id", None)
    if image_token_id is None or image_token_id == unk_token_id:
        raise Florence2Error("Florence-2 tokenizer could not register the native image token", stage="processor_load")
    setattr(tokenizer, "image_token", FLORENCE_IMAGE_TOKEN)
    setattr(tokenizer, "image_token_id", int(image_token_id))
    return {
        "tokenizer_length": int(len(tokenizer)),
        "image_token_id": int(image_token_id),
        "legacy_tokens_added": legacy_tokens_added,
        "image_token_added": image_token_added,
    }


def load_native_florence_checkpoint(model: Any, pack_dir: Path, torch: Any) -> dict[str, int]:
    from safetensors import safe_open

    expected = model.state_dict()
    remapped: dict[str, Any] = {}
    unexpected: list[str] = []
    shape_errors: list[str] = []
    transposed_keys = 0
    checkpoint_path = pack_dir / "model.safetensors"
    with safe_open(str(checkpoint_path), framework="pt", device="cpu") as handle:
        for checkpoint_key in handle.keys():
            mapped_key, transpose = native_florence_checkpoint_key(checkpoint_key)
            if mapped_key is None:
                continue
            if mapped_key not in expected:
                unexpected.append(f"{checkpoint_key} -> {mapped_key}")
                continue
            tensor = handle.get_tensor(checkpoint_key)
            if transpose:
                tensor = tensor.transpose(0, 1).contiguous()
                transposed_keys += 1
            if tuple(tensor.shape) != tuple(expected[mapped_key].shape):
                shape_errors.append(f"{checkpoint_key} -> {mapped_key}: {tuple(tensor.shape)} != {tuple(expected[mapped_key].shape)}")
                continue
            remapped[mapped_key] = tensor

    shared = remapped.get("model.language_model.shared.weight")
    if shared is not None:
        for tied_key in (
            "model.language_model.encoder.embed_tokens.weight",
            "model.language_model.decoder.embed_tokens.weight",
            "lm_head.weight",
        ):
            remapped.setdefault(tied_key, shared)

    missing = [key for key in expected if key not in remapped]
    if unexpected or missing or shape_errors:
        details = {
            "unexpected": unexpected[:20],
            "missing": missing[:20],
            "shape_errors": shape_errors[:20],
            "unexpected_count": len(unexpected),
            "missing_count": len(missing),
            "shape_error_count": len(shape_errors),
        }
        raise Florence2Error(f"Florence-2 native checkpoint remap failed: {details}", stage="model_load", diagnostics=details)

    model.load_state_dict(remapped, strict=True)
    return {"loaded_keys": len(remapped), "transposed_keys": transposed_keys}


def native_florence_checkpoint_key(checkpoint_key: str) -> tuple[str | None, bool]:
    if checkpoint_key == "language_model.final_logits_bias":
        return None, False
    if checkpoint_key == "image_projection":
        return "model.multi_modal_projector.image_projection.weight", True
    if checkpoint_key.startswith("image_proj_norm."):
        return "model.multi_modal_projector." + checkpoint_key, False
    if checkpoint_key.startswith("image_pos_embed."):
        return "model.multi_modal_projector.image_position_embed." + checkpoint_key[len("image_pos_embed.") :], False
    if checkpoint_key.startswith("visual_temporal_embed."):
        return "model.multi_modal_projector." + checkpoint_key, False
    if checkpoint_key.startswith("language_model.model."):
        return "model.language_model." + checkpoint_key[len("language_model.model.") :], False
    if checkpoint_key.startswith("language_model."):
        return "model." + checkpoint_key, False
    if checkpoint_key.startswith("vision_tower."):
        mapped = "model." + checkpoint_key
        mapped = mapped.replace(".proj.", ".conv.") if ".convs." in mapped else mapped
        replacements = {
            ".conv1.fn.dw.": ".conv1.",
            ".conv2.fn.dw.": ".conv2.",
            ".window_attn.fn.qkv.": ".window_attn.qkv.",
            ".window_attn.fn.proj.": ".window_attn.proj.",
            ".channel_attn.fn.qkv.": ".channel_attn.qkv.",
            ".channel_attn.fn.proj.": ".channel_attn.proj.",
            ".spatial_block.window_attn.norm.": ".spatial_block.norm1.",
            ".spatial_block.ffn.norm.": ".spatial_block.norm2.",
            ".spatial_block.ffn.fn.net.fc1.": ".spatial_block.ffn.fc1.",
            ".spatial_block.ffn.fn.net.fc2.": ".spatial_block.ffn.fc2.",
            ".channel_block.channel_attn.norm.": ".channel_block.norm1.",
            ".channel_block.ffn.norm.": ".channel_block.norm2.",
            ".channel_block.ffn.fn.net.fc1.": ".channel_block.ffn.fc1.",
            ".channel_block.ffn.fn.net.fc2.": ".channel_block.ffn.fc2.",
        }
        for old, new in replacements.items():
            mapped = mapped.replace(old, new)
        return mapped, False
    return checkpoint_key, False


def post_process_florence_generation(processor: Any, generated_text: str, prompt: str, *, image_size: tuple[int, int]) -> dict[str, Any]:
    post_processing_type = getattr(processor, "tasks_answer_post_processing_type", {}).get(prompt, "pure_text")
    if post_processing_type == "pure_text":
        text = generated_text.replace("<s>", "").replace("</s>", "").replace("<pad>", "").strip()
        return {prompt: text}
    return processor.post_process_generation(generated_text, task=prompt, image_size=image_size)


def additional_special_tokens(tokenizer: Any) -> list[str]:
    special_map = getattr(tokenizer, "special_tokens_map", None) or {}
    return [str(item) for item in special_map.get("additional_special_tokens") or []]


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def load_manifest_with_info(pack_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest_path = resolve_manifest_path(pack_dir)
    if not manifest_path.exists():
        raise Florence2Error(f"Florence-2 manifest is missing: {manifest_path}", stage="manifest_parse")
    raw = manifest_path.read_bytes()
    if not raw:
        raise Florence2Error(f"Florence-2 manifest is empty: {manifest_path}", stage="manifest_parse")
    bom_present = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise Florence2Error(f"Florence-2 manifest is not valid UTF-8: {exc}", stage="manifest_parse") from exc
    if not text.strip():
        raise Florence2Error(f"Florence-2 manifest is empty: {manifest_path}", stage="manifest_parse")
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as exc:
        raise Florence2Error(f"Florence-2 manifest JSON is malformed: {exc}", stage="manifest_parse") from exc
    if not isinstance(manifest, dict):
        raise Florence2Error("Florence-2 manifest root must be a JSON object", stage="manifest_parse")
    return manifest, {
        "path": str(manifest_path),
        "encoding": "utf-8-sig-compatible",
        "bom_present": bom_present,
        "byte_count": len(raw),
    }


def resolve_manifest_path(pack_dir: Path) -> Path:
    primary = pack_dir / FLORENCE_MANIFEST_NAME
    if primary.exists():
        return primary
    for name in FLORENCE_LEGACY_MANIFEST_NAMES:
        legacy = pack_dir / name
        if legacy.exists():
            return legacy
    return primary


def verify_manifest_shape(manifest: dict[str, Any]) -> None:
    if manifest.get("pack_id") != FLORENCE_PACK_ID:
        raise Florence2Error("Florence-2 manifest pack_id mismatch", stage="manifest_validation")
    if manifest.get("model_id") != FLORENCE_MODEL_ID:
        raise Florence2Error("Florence-2 manifest model_id mismatch", stage="manifest_validation")
    if manifest.get("revision") != FLORENCE_MODEL_REVISION:
        raise Florence2Error("Florence-2 manifest revision mismatch", stage="manifest_validation")
    files = manifest.get("files")
    if not isinstance(files, dict):
        raise Florence2Error("Florence-2 manifest files section is invalid", stage="manifest_validation")
    missing = sorted(set(FLORENCE_REQUIRED_FILES) - set(files))
    if missing:
        raise Florence2Error(f"Florence-2 manifest is missing required files: {', '.join(missing)}", stage="manifest_validation")
    disallowed = sorted(set(files) - FLORENCE_ALLOWED_FILES)
    if disallowed:
        raise Florence2Error(f"Florence-2 manifest includes files Odysseus will not load: {', '.join(disallowed)}", stage="manifest_validation")


def verify_pack_files(pack_dir: Path, manifest: dict[str, Any], *, check_hashes: bool) -> None:
    files = manifest.get("files") or {}
    for relative, expected in FLORENCE_REQUIRED_FILES.items():
        path = pack_dir / relative
        if not path.exists():
            raise Florence2Error(f"Florence-2 pack file is missing: {relative}", stage="checksum_verification")
        actual_size = path.stat().st_size
        manifest_item = files.get(relative) or {}
        expected_size = int(manifest_item.get("size_bytes") or expected.get("size_bytes") or 0)
        if expected_size and actual_size != expected_size:
            raise Florence2Error(f"Florence-2 pack file size mismatch for {relative}: expected {expected_size}, got {actual_size}", stage="checksum_verification")
        if check_hashes:
            expected_hash = str(manifest_item.get("sha256") or "")
            if not expected_hash:
                raise Florence2Error(f"Florence-2 manifest is missing sha256 for {relative}", stage="checksum_verification")
            actual_hash = file_sha256(path)
            if actual_hash.lower() != expected_hash.lower():
                raise Florence2Error(f"Florence-2 pack checksum mismatch for {relative}", stage="checksum_verification")


def dependency_status() -> dict[str, Any]:
    items: dict[str, dict[str, str]] = {}
    ready = True
    import_names = {"huggingface_hub": "huggingface_hub"}
    for package, expected in FLORENCE_RUNTIME_PINS.items():
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            actual = ""
        import_name = import_names.get(package, package)
        spec_found = importlib.util.find_spec(import_name) is not None
        state = "missing" if not actual else ("ok" if version_at_least(actual, expected) else "too_old")
        if state != "ok":
            ready = False
        items[package] = {"required": expected, "installed": actual, "state": state, "import_spec_found": str(bool(spec_found)).lower()}
    return {"ready": ready, "python_executable": sys.executable, "packages": items}


def runtime_message(deps: dict[str, Any]) -> str:
    if deps.get("ready"):
        return "Pinned Florence runtime packages are installed."
    packages = deps.get("packages") if isinstance(deps.get("packages"), dict) else {}
    missing = [name for name, item in packages.items() if isinstance(item, dict) and item.get("state") != "ok"]
    return "Missing or incompatible Florence runtime packages: " + ", ".join(missing)


def native_class_status(deps: dict[str, Any]) -> str:
    packages = deps.get("packages") if isinstance(deps, dict) else {}
    transformers = packages.get("transformers") if isinstance(packages, dict) else {}
    if not transformers or transformers.get("state") != "ok":
        return "unavailable_transformers_missing_or_old"
    return "deferred_native_import_required"


def version_at_least(actual: str, expected: str) -> bool:
    return version_tuple(actual) >= version_tuple(expected)


def version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for part in value.split("."):
        match = ""
        for char in part:
            if char.isdigit():
                match += char
            else:
                break
        parts.append(int(match or 0))
    return tuple(parts)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def max_tokens_for_task(task: str) -> int:
    if task in {FLORENCE_TASK_OCR, FLORENCE_TASK_OCR_WITH_REGION}:
        return 512
    if task in {FLORENCE_TASK_DENSE_REGION_CAPTION, FLORENCE_TASK_OBJECT_DETECTION}:
        return 768
    return 256


def stage_status(status: str, message: str = "", **extra: Any) -> dict[str, Any]:
    return {"status": status, "message": message, **extra}


def visual_evidence_has_content(evidence: dict[str, Any]) -> bool:
    if str(evidence.get("summary") or "").strip():
        return True
    for key in ("objects", "regions", "text", "grounded_phrases"):
        value = evidence.get(key)
        if isinstance(value, list) and value:
            return True
    return False
