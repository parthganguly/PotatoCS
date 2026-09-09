from __future__ import annotations

import base64
import hashlib
import inspect
import json
from pathlib import Path

import pytest
from PIL import Image

from odysseus_desktop_backend.services.image_preprocessing_service import MAX_ORIGINAL_BYTES
import odysseus_desktop_backend.services.vision_service as vision_module
from odysseus_desktop_backend.services.artifact_service import (
    ArtifactService,
    ArtifactValidationError,
)
from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.florence2_service import (
    FLORENCE_IMAGE_TOKEN,
    FLORENCE_MODEL_ID,
    Florence2Backend,
    Florence2Error,
    load_manifest,
    native_florence_checkpoint_key,
    prepare_native_florence_tokenizer,
)
from odysseus_desktop_backend.services.image_eval_service import grade_case, load_cases
from odysseus_desktop_backend.services.model_service import ModelService, ModelTimeoutError
from odysseus_desktop_backend.services.ocr_service import (
    OCRImageResult,
    OCREngineStatus,
    OCRService,
)
from odysseus_desktop_backend.services.perception_types import (
    FLORENCE_TASK_DENSE_REGION_CAPTION,
    FLORENCE_TASK_MORE_DETAILED_CAPTION,
    FLORENCE_TASK_OBJECT_DETECTION,
    FLORENCE_TASK_OCR_WITH_REGION,
    normalize_florence_outputs,
    plan_perception_tasks,
)
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.source_service import SourceService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.services.vision_service import VisionService
from odysseus_desktop_backend.services.visual_evidence_curator import OCR_NO_TEXT_WARNING
from odysseus_desktop_backend.storage import Database
from rpc_server import SidecarApp


class FakeImageOcrEngine:
    name = "fake-ocr"

    def __init__(self):
        self.image_paths: list[str] = []

    def status(self):
        return OCREngineStatus(True, self.name, "fake-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, _source_path: str):
        return []

    def ocr_image(self, image_path: str, *, source_id: str = ""):
        self.image_paths.append(image_path)
        with Image.open(image_path) as image:
            width, height = image.size
        return OCRImageResult(
            source_path=source_id or image_path,
            engine_name=self.name,
            confidence=91.5,
            text="ERROR 42: cache miss",
            width=width,
            height=height,
            elapsed_ms=12,
            metadata={"words": [{"text": "ERROR", "confidence": 91.5}]},
        )


class EmptyImageOcrEngine(FakeImageOcrEngine):
    def ocr_image(self, image_path: str, *, source_id: str = ""):
        self.image_paths.append(image_path)
        with Image.open(image_path) as image:
            width, height = image.size
        return OCRImageResult(
            source_path=source_id or image_path,
            engine_name=self.name,
            confidence=None,
            text="",
            width=width,
            height=height,
            elapsed_ms=5,
            metadata={"words": []},
        )


class FakeVisionModelService(ModelService):
    def __init__(self, db: Database, *, vision: str | dict[str, str] = "yes"):
        super().__init__(db)
        self.vision = vision
        self.calls: list[dict[str, object]] = []

    def inspect(self, model: str):
        vision = self.vision.get(model, "no") if isinstance(self.vision, dict) else self.vision
        return {
            "model": model,
            "digest": "fake",
            "size": 123,
            "family": "llava",
            "parameter_size": "tiny",
            "quantization_level": "q4",
            "context_length": 4096,
            "capabilities": ["vision"] if vision == "yes" else ["completion"],
            "text_generation": "yes",
            "vision": vision,
            "embedding": "no",
            "tools": "unknown",
            "thinking": "unknown",
            "raw": {},
            "inspected_at": 1,
            "error": "",
        }

    def chat(self, model: str, messages: list[dict[str, str]], **kwargs):
        self.calls.append({"model": model, "messages": messages, "kind": "text", "kwargs": kwargs})
        return "text reply"

    def chat_vision_detailed(self, model: str, messages: list[dict[str, object]], *, image_paths: list[str], **kwargs):
        self.calls.append({"model": model, "messages": messages, "image_paths": image_paths, "kind": "vision", "kwargs": kwargs})
        return {
            "model": model,
            "content": json.dumps(
                {
                    "summary": "A terminal shows a cache miss error.",
                    "visible_objects": ["terminal"],
                    "spatial_relations": [],
                    "interface_elements": ["prompt"],
                    "uncertain_observations": [],
                    "not_visible_or_not_determinable": [],
                    "model_visible_text": ["ERROR 42"],
                }
            ),
            "elapsed_ms": 44,
            "done_reason": "stop",
        }

    def chat_vision_history_detailed(self, model: str, messages: list[dict[str, object]], **kwargs):
        prepared: list[dict[str, object]] = []
        for message in messages:
            item: dict[str, object] = {
                "role": str(message.get("role") or "user"),
                "content": str(message.get("content") or ""),
            }
            image_paths = message.get("_image_paths")
            if isinstance(image_paths, list) and image_paths:
                item["images"] = [
                    base64.b64encode(Path(str(path)).read_bytes()).decode("ascii")
                    for path in image_paths
                ]
            prepared.append(item)
        self.calls.append({"model": model, "messages": prepared, "raw_messages": messages, "kind": "vision_history", "kwargs": kwargs})
        return {
            "model": model,
            "content": "The image shows a terminal with ERROR 42 cache miss.",
            "elapsed_ms": 45,
            "done_reason": "stop",
        }


class FakeFlorenceBackend:
    def __init__(self):
        self.calls: list[dict[str, object]] = []
        self.unload_count = 0

    def status(self, *, check_hashes: bool = False):
        return {
            "ready": True,
            "state": "ready",
            "message": "fake Florence ready",
            "hashes_checked": check_hashes,
        }

    def inspect(self, image_path: str, *, question: str, task_plan, request_id: str = "", input_metadata=None):
        self.calls.append({
            "image_path": image_path,
            "question": question,
            "task_plan": task_plan.to_dict() if hasattr(task_plan, "to_dict") else task_plan,
            "request_id": request_id,
            "input_metadata": input_metadata or {},
        })
        return {
            "model": FLORENCE_MODEL_ID,
            "backend": "florence2",
            "elapsed_ms": 17,
            "visual_evidence": {
                "schema": "odysseus.visual_evidence.v1",
                "backend": "florence2",
                "model": FLORENCE_MODEL_ID,
                "question": question,
                "tasks": ["more_detailed_caption", "object_detection"],
                "task_plan": task_plan.to_dict() if hasattr(task_plan, "to_dict") else task_plan,
                "image": {"width": 240, "height": 120},
                "summary": "A tidy desk with a monitor and keyboard.",
                "objects": [{"label": "desk", "box": [0.1, 0.2, 0.9, 0.8], "source": "object_detection"}],
                "regions": [],
                "text": [],
                "grounded_phrases": [],
                "uncertain": [],
                "not_determinable": [],
                "elapsed_ms": 17,
                "raw_task_count": 2,
            },
        }

    def unload(self):
        self.unload_count += 1
        return {"unloaded": True}


class FailingFlorenceBackend(FakeFlorenceBackend):
    def __init__(self, stage: str = "manifest_parse"):
        super().__init__()
        self.stage = stage

    def inspect(self, image_path: str, *, question: str, task_plan, request_id: str = "", input_metadata=None):
        self.calls.append({"image_path": image_path, "question": question, "request_id": request_id})
        raise Florence2Error("synthetic Florence failure", stage=self.stage)


class PartialFlorenceBackend(FakeFlorenceBackend):
    def inspect(self, image_path: str, *, question: str, task_plan, request_id: str = "", input_metadata=None):
        response = super().inspect(image_path, question=question, task_plan=task_plan, request_id=request_id, input_metadata=input_metadata)
        response["tasks_completed"] = ["more_detailed_caption"]
        response["tasks_failed"] = [{"task": "object_detection", "stage": "inference", "error": "synthetic detector failure"}]
        response["warnings"] = ["object_detection failed: synthetic detector failure"]
        response["visual_evidence"]["tasks_completed"] = ["more_detailed_caption"]
        response["visual_evidence"]["tasks_failed"] = response["tasks_failed"]
        return response


class LightingFlorenceBackend(FakeFlorenceBackend):
    def inspect(self, image_path: str, *, question: str, task_plan, request_id: str = "", input_metadata=None):
        self.calls.append({
            "image_path": image_path,
            "question": question,
            "task_plan": task_plan.to_dict() if hasattr(task_plan, "to_dict") else task_plan,
            "request_id": request_id,
            "input_metadata": input_metadata or {},
        })
        return {
            "model": FLORENCE_MODEL_ID,
            "backend": "florence2",
            "elapsed_ms": 19,
            "visual_evidence": {
                "schema": "odysseus.visual_evidence.v1",
                "backend": "florence2",
                "model": FLORENCE_MODEL_ID,
                "question": question,
                "tasks": ["more_detailed_caption", "object_detection"],
                "summary": "A lamp is visible beside the desk. A window is visible on the right. Picture frames hang on the wall.",
                "objects": [
                    {"label": "lamp", "box": [0.1, 0.45, 0.22, 0.72], "source": "object_detection"},
                    {"label": "window", "box": [0.68, 0.05, 0.96, 0.42], "source": "object_detection"},
                    {"label": "picture frame", "box": [0.3, 0.08, 0.45, 0.25], "source": "object_detection"},
                    {"label": "picture frame", "box": [0.31, 0.09, 0.46, 0.26], "source": "object_detection"},
                    {"label": "book", "box": [0.45, 0.75, 0.55, 0.82], "source": "object_detection"},
                ],
                "regions": [],
                "text": [],
                "grounded_phrases": [],
                "uncertain": [],
                "not_determinable": [],
                "elapsed_ms": 19,
                "raw_task_count": 2,
            },
        }


class ActionFlorenceBackend(FakeFlorenceBackend):
    def inspect(self, image_path: str, *, question: str, task_plan, request_id: str = "", input_metadata=None):
        self.calls.append({
            "image_path": image_path,
            "question": question,
            "task_plan": task_plan.to_dict() if hasattr(task_plan, "to_dict") else task_plan,
            "request_id": request_id,
            "input_metadata": input_metadata or {},
        })
        return {
            "model": FLORENCE_MODEL_ID,
            "backend": "florence2",
            "elapsed_ms": 21,
            "visual_evidence": {
                "schema": "odysseus.visual_evidence.v1",
                "backend": "florence2",
                "model": FLORENCE_MODEL_ID,
                "question": question,
                "tasks": ["more_detailed_caption", "object_detection"],
                "summary": "A man is sitting in a chair reading a book.",
                "objects": [
                    {"label": "person", "box": [0.32, 0.18, 0.72, 0.9], "source": "object_detection"},
                    {"label": "book", "box": [0.42, 0.48, 0.58, 0.62], "source": "object_detection"},
                    {"label": "chair", "box": [0.28, 0.3, 0.76, 0.95], "source": "object_detection"},
                    {"label": "desk", "box": [0.1, 0.62, 0.9, 0.95], "source": "object_detection"},
                    {"label": "lamp", "box": [0.72, 0.18, 0.9, 0.58], "source": "object_detection"},
                    {"label": "window", "box": [0.02, 0.05, 0.25, 0.5], "source": "object_detection"},
                ],
                "regions": [],
                "text": [],
                "grounded_phrases": [],
                "uncertain": [],
                "not_determinable": [],
                "elapsed_ms": 21,
                "raw_task_count": 2,
            },
        }


class CookingFlorenceBackend(FakeFlorenceBackend):
    def inspect(self, image_path: str, *, question: str, task_plan, request_id: str = "", input_metadata=None):
        self.calls.append({
            "image_path": image_path,
            "question": question,
            "task_plan": task_plan.to_dict() if hasattr(task_plan, "to_dict") else task_plan,
            "request_id": request_id,
            "input_metadata": input_metadata or {},
        })
        return {
            "model": FLORENCE_MODEL_ID,
            "backend": "florence2",
            "elapsed_ms": 23,
            "visual_evidence": {
                "schema": "odysseus.visual_evidence.v1",
                "backend": "florence2",
                "model": FLORENCE_MODEL_ID,
                "question": question,
                "tasks": ["more_detailed_caption", "object_detection"],
                "summary": (
                    "A man and a woman are in a kitchen. "
                    "The man is cutting vegetables on a cutting board. "
                    "The woman is standing next to the man. "
                    "A knife and cutting board are visible."
                ),
                "objects": [
                    {"label": "person", "box": [0.12, 0.12, 0.82, 0.95], "source": "object_detection"},
                    {"label": "knife", "box": [0.42, 0.58, 0.52, 0.64], "source": "object_detection"},
                    {"label": "cutting board", "box": [0.36, 0.62, 0.68, 0.78], "source": "object_detection"},
                    {"label": "vegetables", "box": [0.44, 0.6, 0.7, 0.75], "source": "object_detection"},
                ],
                "regions": [],
                "text": [],
                "grounded_phrases": [],
                "uncertain": [],
                "not_determinable": [],
                "elapsed_ms": 23,
                "raw_task_count": 2,
            },
        }


class TinyTokenizer:
    def __init__(self):
        self.special_tokens_map = {"additional_special_tokens": []}
        self.unk_token_id = 3
        self._ids = {"<unk>": self.unk_token_id}

    def add_special_tokens(self, tokens):
        added = 0
        for token in tokens.get("additional_special_tokens") or []:
            if token not in self._ids:
                self._ids[token] = len(self._ids)
                added += 1
            if token not in self.special_tokens_map["additional_special_tokens"]:
                self.special_tokens_map["additional_special_tokens"].append(token)
        return added

    def convert_tokens_to_ids(self, token):
        return self._ids.get(token, self.unk_token_id)

    def __len__(self):
        return len(self._ids)


def build_image_stack(profile_dir: Path, *, vision: str | dict[str, str] = "yes", image_ocr_engine=None):
    db = Database(profile_dir)
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
    artifacts = ArtifactService(db, documents, rag)
    ocr = OCRService(documents, rag, engine=image_ocr_engine or FakeImageOcrEngine())
    models = FakeVisionModelService(db, vision=vision)
    vision = VisionService(artifacts, ocr, models)
    settings = SettingsService(db)
    sessions = SessionService(db)
    sources = SourceService(documents, artifacts, rag)
    chat = ChatService(sessions, settings, models, rag=rag, artifacts=artifacts, documents=documents, sources=sources, vision=vision)
    return db, documents, rag, artifacts, ocr, models, vision, chat


def write_png(path: Path, *, size: tuple[int, int] = (240, 120), color=(244, 248, 245, 255)) -> Path:
    image = Image.new("RGBA", size, color)
    image.save(path, format="PNG")
    return path


def write_exif_jpeg(path: Path, *, orientation: int | None, size: tuple[int, int] = (80, 120)) -> Path:
    image = Image.new("RGB", size, (220, 230, 240))
    exif = Image.Exif()
    if orientation is not None:
        exif[274] = orientation
        image.save(path, format="JPEG", quality=95, exif=exif)
    else:
        image.save(path, format="JPEG", quality=95)
    return path


def derivative_counts(db: Database, artifact_id: str) -> dict[str, int]:
    rows = db.conn.execute(
        """
        SELECT kind, COUNT(*) AS count
        FROM artifact_derivations
        WHERE artifact_id = ? AND kind IN ('thumbnail', 'vision_input', 'ocr_input', 'normalized_image')
        GROUP BY kind
        """,
        (artifact_id,),
    ).fetchall()
    return {str(row["kind"]): int(row["count"] or 0) for row in rows}


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def test_v020_schema_is_additive_and_stamped(tmp_path: Path):
    db = Database(tmp_path / "profile")
    try:
        tables = {
            row["name"]
            for row in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert {
            "artifacts",
            "artifact_derivations",
            "message_artifacts",
            "artifact_analysis_runs",
            "artifact_rag_documents",
            "message_documents",
            "conversation_attachments",
            "model_capabilities",
            "multimodal_eval_runs",
            "multimodal_eval_case_results",
        }.issubset(tables)
        assert db.conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()["value"] == "10"
        document_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(documents)").fetchall()}
        assert {"is_internal", "source_artifact_id", "generated_source_label", "scope", "promoted_at"}.issubset(document_columns)
        artifact_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(artifacts)").fetchall()}
        assert {
            "scope",
            "promoted_at",
            "original_format",
            "original_color_mode",
            "original_pixel_count",
            "original_has_alpha",
            "original_exif_orientation",
            "preprocessing_version",
        }.issubset(artifact_columns)
        analysis_columns = {row["name"] for row in db.conn.execute("PRAGMA table_info(artifact_analysis_runs)").fetchall()}
        assert {"requested_vision_backend", "actual_vision_backend"}.issubset(analysis_columns)
        assert db.get_setting("vision_backend") == "automatic"
    finally:
        db.close()


def test_florence_pack_missing_is_diagnostic_only(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ODYSSEUS_FLORENCE_MODEL_DIR", raising=False)
    backend = Florence2Backend(
        tmp_path / "profile",
        resource_dir=tmp_path / "resource",
        dev_repo_root=tmp_path / "repo",
        app_data_dir=tmp_path / "app data",
    )
    status = backend.status()
    assert status["ready"] is False
    assert status["state"] == "missing"
    assert status["failed_stage"] == "pack_discovery"
    assert [candidate["source"] for candidate in status["searched_candidates"]] == [
        "profile_models",
        "app_data_models",
        "tauri_resource",
        "debug_repo_root",
    ]
    assert status["normal_runtime_downloads"] is False
    assert status["trust_remote_code"] is False
    assert status["license"] == "MIT"


def write_tiny_florence_pack(pack_dir: Path, manifest: dict | None = None, *, bom: bool = False) -> dict:
    import odysseus_desktop_backend.services.florence2_service as florence_module

    pack_dir.mkdir(parents=True, exist_ok=True)
    (pack_dir / "config.json").write_text("{}", encoding="utf-8")
    data = manifest or {
        "pack_id": "florence2-base-ft",
        "model_id": "microsoft/Florence-2-base-ft",
        "revision": florence_module.FLORENCE_MODEL_REVISION,
        "files": {"config.json": {"size_bytes": 2, "sha256": sha256_bytes(b"{}")}},
    }
    payload = json.dumps(data)
    if bom:
        (pack_dir / florence_module.FLORENCE_MANIFEST_NAME).write_bytes(b"\xef\xbb\xbf" + payload.encode("utf-8"))
    else:
        (pack_dir / florence_module.FLORENCE_MANIFEST_NAME).write_bytes(payload.encode("utf-8"))
    return data


def patch_tiny_florence_contract(monkeypatch):
    import odysseus_desktop_backend.services.florence2_service as florence_module

    monkeypatch.setattr(florence_module, "FLORENCE_REQUIRED_FILES", {"config.json": {"size_bytes": 2}})
    monkeypatch.setattr(florence_module, "FLORENCE_ALLOWED_FILES", {"config.json"})
    monkeypatch.setattr(florence_module, "dependency_status", lambda: {"ready": False, "packages": {"torch": {"state": "missing"}}})
    return florence_module


def test_florence_resolver_uses_explicit_debug_repo_root_in_tauri_dev(tmp_path: Path, monkeypatch):
    florence_module = patch_tiny_florence_contract(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FLORENCE_MODEL_DIR", raising=False)
    copied_backend_resource = tmp_path / "src-tauri" / "target" / "debug"
    real_repo_root = tmp_path / "repo with spaces" / "odysseus-desktop"
    pack_dir = real_repo_root / "models" / florence_module.FLORENCE_PACK_ID
    write_tiny_florence_pack(pack_dir)

    backend = Florence2Backend(
        tmp_path / "profile",
        resource_dir=copied_backend_resource,
        dev_repo_root=real_repo_root,
        app_data_dir=tmp_path / "app data",
    )
    status = backend.status()

    assert status["state"] == "runtime_missing"
    assert status["selected_pack_source"] == "debug_repo_root"
    assert Path(status["selected_pack_path"]) == pack_dir.resolve(strict=False)
    assert "target" not in Path(status["selected_pack_path"]).parts
    assert {candidate["source"] for candidate in status["searched_candidates"]} == {
        "profile_models",
        "app_data_models",
        "tauri_resource",
        "debug_repo_root",
    }


def test_florence_packaged_resource_candidate_wins_in_installed_mode(tmp_path: Path, monkeypatch):
    florence_module = patch_tiny_florence_contract(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FLORENCE_MODEL_DIR", raising=False)
    resource_dir = tmp_path / "installed app resources with spaces"
    pack_dir = resource_dir / "models" / florence_module.FLORENCE_PACK_ID
    write_tiny_florence_pack(pack_dir)

    status = Florence2Backend(
        tmp_path / "profile",
        resource_dir=resource_dir,
        app_data_dir=tmp_path / "app data",
    ).status()

    assert status["state"] == "runtime_missing"
    assert status["selected_pack_source"] == "tauri_resource"
    assert Path(status["selected_pack_path"]) == pack_dir.resolve(strict=False)
    assert not status["path_context"]["dev_repo_root"]


def test_florence_release_mode_does_not_guess_development_paths(tmp_path: Path, monkeypatch):
    florence_module = patch_tiny_florence_contract(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FLORENCE_MODEL_DIR", raising=False)
    unreferenced_repo_pack = tmp_path / "repo" / "models" / florence_module.FLORENCE_PACK_ID
    write_tiny_florence_pack(unreferenced_repo_pack)

    status = Florence2Backend(
        tmp_path / "profile",
        resource_dir=tmp_path / "installed resources",
        app_data_dir=tmp_path / "app data",
    ).status()

    assert status["state"] == "missing"
    assert status["selected_pack_source"] == ""
    assert "debug_repo_root" not in [candidate["source"] for candidate in status["searched_candidates"]]


def test_florence_resolver_precedence_prefers_env_override(tmp_path: Path, monkeypatch):
    florence_module = patch_tiny_florence_contract(monkeypatch)
    env_pack = tmp_path / "override pack with spaces"
    app_pack = tmp_path / "app data" / "models" / florence_module.FLORENCE_PACK_ID
    resource_pack = tmp_path / "resource" / "models" / florence_module.FLORENCE_PACK_ID
    repo_pack = tmp_path / "repo" / "models" / florence_module.FLORENCE_PACK_ID
    for pack_dir in (env_pack, app_pack, resource_pack, repo_pack):
        write_tiny_florence_pack(pack_dir)
    monkeypatch.setenv("ODYSSEUS_FLORENCE_MODEL_DIR", str(env_pack))

    status = Florence2Backend(
        tmp_path / "profile",
        resource_dir=tmp_path / "resource",
        dev_repo_root=tmp_path / "repo",
        app_data_dir=tmp_path / "app data",
    ).status()

    assert status["selected_pack_source"] == "env_override"
    assert Path(status["selected_pack_path"]) == env_pack.resolve(strict=False)
    assert [candidate["source"] for candidate in status["searched_candidates"]][0] == "env_override"


def test_florence_existing_bad_candidate_reports_manifest_stage_not_missing(tmp_path: Path, monkeypatch):
    florence_module = patch_tiny_florence_contract(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FLORENCE_MODEL_DIR", raising=False)
    bad_pack = tmp_path / "resource" / "models" / florence_module.FLORENCE_PACK_ID
    bad_pack.mkdir(parents=True)
    (bad_pack / florence_module.FLORENCE_MANIFEST_NAME).write_text("{nope", encoding="utf-8")

    status = Florence2Backend(
        tmp_path / "profile",
        resource_dir=tmp_path / "resource",
        dev_repo_root=tmp_path / "repo",
        app_data_dir=tmp_path / "app data",
    ).status()

    assert status["state"] == "manifest_parse"
    assert status["failed_stage"] == "manifest_parse"
    assert "not installed" not in status["message"]
    failed = next(candidate for candidate in status["searched_candidates"] if candidate["source"] == "tauri_resource")
    assert failed["exists"] is True
    assert failed["manifest_present"] is True
    assert failed["manifest_parsed"] is False


def test_florence_pack_added_after_startup_is_discovered_without_cache(tmp_path: Path, monkeypatch):
    florence_module = patch_tiny_florence_contract(monkeypatch)
    monkeypatch.delenv("ODYSSEUS_FLORENCE_MODEL_DIR", raising=False)
    app_data_dir = tmp_path / "app data"
    backend = Florence2Backend(tmp_path / "profile", app_data_dir=app_data_dir)
    assert backend.status()["state"] == "missing"

    pack_dir = app_data_dir / "models" / florence_module.FLORENCE_PACK_ID
    write_tiny_florence_pack(pack_dir)
    status = backend.status()

    assert status["state"] == "runtime_missing"
    assert status["selected_pack_source"] == "app_data_models"
    assert Path(status["selected_pack_path"]) == pack_dir.resolve(strict=False)


def test_florence_pack_manifest_verifier_uses_hashes(tmp_path: Path, monkeypatch):
    import odysseus_desktop_backend.services.florence2_service as florence_module

    pack_dir = tmp_path / "pack"
    write_tiny_florence_pack(pack_dir)
    monkeypatch.setenv("ODYSSEUS_FLORENCE_MODEL_DIR", str(pack_dir))
    monkeypatch.setattr(florence_module, "FLORENCE_REQUIRED_FILES", {"config.json": {"size_bytes": 2}})
    monkeypatch.setattr(florence_module, "FLORENCE_ALLOWED_FILES", {"config.json"})

    backend = Florence2Backend(tmp_path / "profile")
    status = backend.verify_pack()
    assert status["state"] in {"runtime_missing", "ready"}
    assert status["hashes_checked"] is True

    (pack_dir / "config.json").write_text("[]", encoding="utf-8")
    corrupted = backend.verify_pack()
    assert corrupted["state"] == "checksum_verification"
    assert "checksum mismatch" in corrupted["message"]


def test_florence_manifest_accepts_bom_free_and_bom(tmp_path: Path, monkeypatch):
    import odysseus_desktop_backend.services.florence2_service as florence_module

    monkeypatch.setattr(florence_module, "FLORENCE_REQUIRED_FILES", {"config.json": {"size_bytes": 2}})
    monkeypatch.setattr(florence_module, "FLORENCE_ALLOWED_FILES", {"config.json"})
    bom_free = tmp_path / "bom-free"
    manifest = write_tiny_florence_pack(bom_free, bom=False)
    assert not (bom_free / florence_module.FLORENCE_MANIFEST_NAME).read_bytes().startswith(b"\xef\xbb\xbf")
    assert load_manifest(bom_free)["revision"] == manifest["revision"]

    bom = tmp_path / "bom"
    write_tiny_florence_pack(bom, bom=True)
    assert (bom / florence_module.FLORENCE_MANIFEST_NAME).read_bytes().startswith(b"\xef\xbb\xbf")
    assert load_manifest(bom)["revision"] == manifest["revision"]


def test_florence_manifest_parse_and_validation_failures_are_staged(tmp_path: Path, monkeypatch):
    import odysseus_desktop_backend.services.florence2_service as florence_module

    monkeypatch.setattr(florence_module, "FLORENCE_REQUIRED_FILES", {"config.json": {"size_bytes": 2}})
    monkeypatch.setattr(florence_module, "FLORENCE_ALLOWED_FILES", {"config.json"})

    malformed = tmp_path / "malformed"
    malformed.mkdir()
    (malformed / florence_module.FLORENCE_MANIFEST_NAME).write_text("{nope", encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_FLORENCE_MODEL_DIR", str(malformed))
    assert Florence2Backend(tmp_path / "profile-a").status()["failed_stage"] == "manifest_parse"

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / florence_module.FLORENCE_MANIFEST_NAME).write_bytes(b"")
    monkeypatch.setenv("ODYSSEUS_FLORENCE_MODEL_DIR", str(empty))
    assert Florence2Backend(tmp_path / "profile-b").status()["failed_stage"] == "manifest_parse"

    wrong_revision = tmp_path / "wrong-revision"
    manifest = write_tiny_florence_pack(wrong_revision)
    manifest["revision"] = "wrong"
    (wrong_revision / florence_module.FLORENCE_MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("ODYSSEUS_FLORENCE_MODEL_DIR", str(wrong_revision))
    status = Florence2Backend(tmp_path / "profile-c").status()
    assert status["failed_stage"] == "manifest_validation"
    assert "revision mismatch" in status["message"]


def test_native_florence_tokenizer_and_checkpoint_repairs_are_deterministic():
    tokenizer = TinyTokenizer()

    info = prepare_native_florence_tokenizer(tokenizer)

    assert info["legacy_tokens_added"] == 1024
    assert info["image_token_added"] == 1
    assert tokenizer.image_token == FLORENCE_IMAGE_TOKEN
    assert tokenizer.image_token_id == tokenizer.convert_tokens_to_ids(FLORENCE_IMAGE_TOKEN)
    assert len(tokenizer) == 1026
    assert native_florence_checkpoint_key("image_projection") == (
        "model.multi_modal_projector.image_projection.weight",
        True,
    )
    assert native_florence_checkpoint_key("vision_tower.convs.0.proj.weight") == (
        "model.vision_tower.convs.0.conv.weight",
        False,
    )
    assert native_florence_checkpoint_key("vision_tower.blocks.0.0.spatial_block.ffn.fn.net.fc1.weight") == (
        "model.vision_tower.blocks.0.0.spatial_block.ffn.fc1.weight",
        False,
    )
    assert native_florence_checkpoint_key("language_model.model.encoder.layers.0.fc1.weight") == (
        "model.language_model.encoder.layers.0.fc1.weight",
        False,
    )


def test_florence_runtime_missing_is_distinct_from_pack_missing(tmp_path: Path, monkeypatch):
    import odysseus_desktop_backend.services.florence2_service as florence_module

    pack_dir = tmp_path / "pack"
    write_tiny_florence_pack(pack_dir)
    monkeypatch.setenv("ODYSSEUS_FLORENCE_MODEL_DIR", str(pack_dir))
    monkeypatch.setattr(florence_module, "FLORENCE_REQUIRED_FILES", {"config.json": {"size_bytes": 2}})
    monkeypatch.setattr(florence_module, "FLORENCE_ALLOWED_FILES", {"config.json"})
    monkeypatch.setattr(florence_module, "dependency_status", lambda: {"ready": False, "packages": {"torch": {"state": "missing"}}})
    status = Florence2Backend(tmp_path / "profile").status()
    assert status["pack_ready"] is True
    assert status["runtime_ready"] is False
    assert status["failed_stage"] == "runtime_import"


def test_florence_task_planner_and_output_normalization():
    text_plan = plan_perception_tasks("Read the serial number on the label")
    assert FLORENCE_TASK_OCR_WITH_REGION in text_plan.tasks
    scene_plan = plan_perception_tasks("Tell me about the desk layout")
    assert FLORENCE_TASK_MORE_DETAILED_CAPTION in scene_plan.tasks
    assert FLORENCE_TASK_DENSE_REGION_CAPTION in scene_plan.tasks
    assert FLORENCE_TASK_OBJECT_DETECTION in scene_plan.tasks

    evidence = normalize_florence_outputs(
        {
            "more_detailed_caption": {"<MORE_DETAILED_CAPTION>": "A desk with a monitor."},
            "object_detection": {"<OD>": {"labels": ["monitor"], "bboxes": [[20, 10, 120, 80]]}},
            "ocr_with_region": {"<OCR_WITH_REGION>": {"labels": ["HELLO"], "quad_boxes": [[0, 0, 100, 0, 100, 40, 0, 40]]}},
        },
        model=FLORENCE_MODEL_ID,
        question="What is shown?",
        plan=scene_plan,
        image_width=200,
        image_height=100,
        elapsed_ms=4,
    )
    assert evidence["summary"] == "A desk with a monitor."
    assert evidence["objects"][0]["box"] == [0.1, 0.1, 0.6, 0.8]
    assert evidence["text"][0]["text"] == "HELLO"


def test_florence_backend_analysis_persists_structured_evidence(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, vision, _chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no", "vision:local": "yes"},
    )
    fake_florence = FakeFlorenceBackend()
    vision.florence = fake_florence
    source = write_png(tmp_path / "florence-desk.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        analysis = vision.analyze(
            artifact["id"],
            mode="automatic",
            question="Tell me about the desk",
            vision_backend="florence2",
            request_id="florence-analysis-1",
        )
        assert analysis["status"] == "completed"
        assert analysis["requested_vision_backend"] == "florence2"
        assert analysis["actual_vision_backend"] == "florence2"
        assert analysis["actual_vision_model"] == FLORENCE_MODEL_ID
        assert analysis["output"]["visual_evidence"]["summary"].startswith("A tidy desk")
        assert analysis["output"]["curated_visual_evidence"]["raw_evidence_available"] is True
        assert analysis["evidence"]["curated_visual_evidence"]["direct_observations"]
        assert analysis["output"]["vision_observations"]["visible_objects"] == ["desk"]
        assert fake_florence.calls
        assert fake_florence.unload_count == 1
        assert not [call for call in models.calls if call["kind"] == "vision"]
    finally:
        db.close()


def test_successful_visual_question_with_empty_ocr_is_completed(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, _models, vision, _chat = build_image_stack(
        tmp_path / "profile",
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = FakeFlorenceBackend()
    source = write_png(tmp_path / "empty-ocr-visual-success.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        analysis = vision.analyze(
            artifact["id"],
            mode="combined",
            question="Describe this image",
            vision_backend="florence2",
            request_id="empty-ocr-visual-success",
        )
        assert analysis["status"] == "completed"
        assert OCR_NO_TEXT_WARNING not in analysis["warnings"]
        assert analysis["evidence"]["ocr"]["warning"] == OCR_NO_TEXT_WARNING
        assert analysis["output"]["curated_visual_evidence"]["direct_observations"]
    finally:
        db.close()


def test_florence_lighting_prompt_is_curated_and_guarded(tmp_path: Path, monkeypatch):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = LightingFlorenceBackend()

    def hallucinating_chat(model: str, messages: list[dict[str, str]], **kwargs):
        models.calls.append({"model": model, "messages": messages, "kind": "text", "kwargs": kwargs})
        return "The lamp, window, and picture frames illuminate the room."

    monkeypatch.setattr(models, "chat", hallucinating_chat)
    source = write_png(tmp_path / "lighting.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        result = chat.send(
            "Where is the light in the image coming from?",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="lighting-curated-guarded",
        )
        answer = result["assistant_message"]["content"]
        analysis = result["artifact_analysis"]
        curated = analysis["output"]["curated_visual_evidence"]
        prompt = "\n".join(str(message["content"]) for message in models.calls[-1]["messages"])

        assert "lamp" in answer.lower()
        assert "window" in answer.lower()
        assert "picture frame" not in answer.lower()
        assert analysis["status"] == "completed_with_warnings"
        assert analysis["output"]["provenance"]["grounding_guard_triggered"] is True
        assert analysis["output"]["provenance"]["safe_fallback_used"] is True
        assert curated["question_type"] == "source_or_cause"
        assert "picture frame" in curated["excluded_irrelevant_entities"]
        assert "book" in curated["excluded_irrelevant_entities"]
        assert prompt.count("picture frame") <= 1
        assert "Raw perception output available in Diagnostics: yes" in prompt
        assert len(vision.florence.calls) == 1
    finally:
        db.close()


def test_florence_action_evidence_preserved_and_weak_model_cannot_walk(tmp_path: Path, monkeypatch):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = ActionFlorenceBackend()

    def walking_chat(model: str, messages: list[dict[str, str]], **kwargs):
        models.calls.append({"model": model, "messages": messages, "kind": "text", "kwargs": kwargs})
        return "The man is walking."

    monkeypatch.setattr(models, "chat", walking_chat)
    source = write_png(tmp_path / "desk-action.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        result = chat.send(
            "What is the man doing?",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="action-curated-guarded",
        )
        answer = result["assistant_message"]["content"]
        analysis = result["artifact_analysis"]
        curated = analysis["output"]["curated_visual_evidence"]
        observed = [item["text"] for item in curated["direct_observations"]]
        inference = [item["text"] for item in curated["allowed_inferences"]]

        assert answer == "He appears to be sitting in a chair and reading a book."
        assert "walking" not in answer.lower()
        assert observed[0] == "A man is sitting in a chair reading a book."
        assert "He appears to be reading while seated." in inference
        assert "book" not in curated["excluded_irrelevant_entities"]
        assert "chair" not in curated["excluded_irrelevant_entities"]
        assert analysis["output"]["provenance"]["deterministic_visual_answer"] is True
        assert len(vision.florence.calls) == 1
        assert not [call for call in models.calls if call["kind"] == "text"]
    finally:
        db.close()


def test_florence_cooking_action_uses_retrieved_visual_snippet_instead_of_woman_context(tmp_path: Path, monkeypatch):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = CookingFlorenceBackend()

    def wrong_chat(model: str, messages: list[dict[str, str]], **kwargs):
        models.calls.append({"model": model, "messages": messages, "kind": "text", "kwargs": kwargs})
        return "The woman is standing next to the man."

    monkeypatch.setattr(models, "chat", wrong_chat)
    source = write_png(tmp_path / "cooking-action.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        result = chat.send(
            "What is the man doing?",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="cooking-action-curated",
        )
        answer = result["assistant_message"]["content"]
        analysis = result["artifact_analysis"]
        curated = analysis["output"]["curated_visual_evidence"]
        snippets = curated["retrieved_visual_snippets"]

        assert answer == "He appears to be cutting vegetables on a cutting board."
        assert "woman is standing" not in answer.lower()
        assert snippets[0]["text"] == "The man is cutting vegetables on a cutting board."
        assert snippets[0]["actions"] == ["cut"]
        assert curated["retrieval_metadata"]["retrieved_count"] >= 1
        assert analysis["output"]["provenance"]["deterministic_visual_answer"] is True
        assert analysis["output"]["provenance"]["visual_snippets_retrieved"] is True
        assert analysis["output"]["provenance"]["visual_retrieved_snippet_count"] >= 1
        assert len(vision.florence.calls) == 1
        assert not [call for call in models.calls if call["kind"] == "text"]
    finally:
        db.close()


def test_florence_cooking_followups_reuse_raw_evidence_and_reretrieve_snippets(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = CookingFlorenceBackend()
    source = write_png(tmp_path / "cooking-followup.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        first = chat.send(
            "What is the man doing?",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="cooking-followup-1",
        )
        assert first["assistant_message"]["content"] == "He appears to be cutting vegetables on a cutting board."
        assert len(vision.florence.calls) == 1

        models.calls.clear()
        woman = chat.send("What is the woman doing?", session_id=first["session"]["id"], model="text:local")
        woman_analysis = woman["artifact_analysis"]
        woman_curated = woman_analysis["output"]["curated_visual_evidence"]
        assert woman["assistant_message"]["content"] == "She appears to be standing near the man."
        assert woman_curated["retrieved_visual_snippets"][0]["text"] == "The woman is standing next to the man."
        assert woman_analysis["output"]["provenance"]["context_evidence_action"] == "reused"
        assert woman_analysis["output"]["provenance"]["raw_evidence_reused"] is True
        assert woman_analysis["output"]["provenance"]["curated_evidence_recomputed"] is True
        assert woman_analysis["output"]["provenance"]["vision_rerun"] is False
        assert len(vision.florence.calls) == 1
        assert not [call for call in models.calls if call["kind"] in {"vision", "vision_history", "text"}]

        fighting = chat.send("Are the man and woman fighting?", session_id=first["session"]["id"], model="text:local")
        assert "does not show them fighting" in fighting["assistant_message"]["content"].lower()
        assert "man cutting vegetables" in fighting["assistant_message"]["content"].lower()
        assert "woman standing nearby" in fighting["assistant_message"]["content"].lower()
        assert len(vision.florence.calls) == 1
    finally:
        db.close()


def test_florence_identity_question_abstains_without_text_model_guess(tmp_path: Path, monkeypatch):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = ActionFlorenceBackend()

    def descriptive_chat(model: str, messages: list[dict[str, str]], **kwargs):
        models.calls.append({"model": model, "messages": messages, "kind": "text", "kwargs": kwargs})
        return "The man is reading a book while sitting in a chair."

    monkeypatch.setattr(models, "chat", descriptive_chat)
    source = write_png(tmp_path / "identity-trap.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        result = chat.send(
            "Who is this man?",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="identity-trap-safe",
        )
        answer = result["assistant_message"]["content"]
        analysis = result["artifact_analysis"]

        assert "can't identify who he is" in answer.lower()
        assert analysis["output"]["curated_visual_evidence"]["question_type"] == "person_identity"
        assert analysis["output"]["provenance"]["deterministic_visual_answer"] is True
        assert not [call for call in models.calls if call["kind"] == "text"]
    finally:
        db.close()


def test_florence_external_eyes_followup_reuses_structured_observations(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no", "vision:local": "yes"},
    )
    fake_florence = FakeFlorenceBackend()
    vision.florence = fake_florence
    SettingsService(db).set({"vision_backend": "florence2"})
    source = write_png(tmp_path / "florence-followup.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        first = chat.send(
            "Describe this desk image",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="florence-continuity-1",
        )
        assert first["assistant_message"]["content"]
        assert len(fake_florence.calls) == 1

        models.calls.clear()
        followup = chat.send("tell me about it", session_id=first["session"]["id"], model="text:local")
        assert followup["assistant_message"]["content"]
        assert len(fake_florence.calls) == 1
        assert not [call for call in models.calls if call["kind"] in {"vision", "vision_history"}]
        text_calls = [call for call in models.calls if call["kind"] == "text"]
        assert text_calls
        prompt = "\n".join(str(message["content"]) for message in text_calls[-1]["messages"])
        assert "A tidy desk with a monitor and keyboard." in prompt
        assert "Previous assistant claims are not evidence" in prompt
        provenance = followup["artifact_analysis"]["output"]["provenance"]
        assert provenance["vision_backend"] == "florence2"
        assert provenance["context_evidence_action"] == "reused"
        assert provenance["raw_evidence_reused"] is True
        assert provenance["curated_evidence_recomputed"] is True
        assert provenance["vision_rerun"] is False
        assert followup["artifact_analysis"]["evidence"]["conversation_context"]["raw_evidence_reused"] is True
        assert followup["artifact_analysis"]["output"]["visual_evidence"]["summary"].startswith("A tidy desk")
    finally:
        db.close()


def test_florence_failure_with_empty_ocr_prevents_unsupported_synthesis(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no", "vision:local": "yes"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = FailingFlorenceBackend(stage="manifest_parse")
    source = write_png(tmp_path / "failed-florence.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        result = chat.send(
            "Tell me about the desk and its uses.",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="florence-failure-no-synthesis",
        )
        assert result["assistant_message"]["content"] == "Florence could not analyse this image, and OCR found no readable text. No visual answer was generated."
        assert not [call for call in models.calls if call["kind"] == "text"]
        analysis = result["artifact_analysis"]
        assert analysis["status"] == "error"
        assert analysis["stage"] == "manifest_parse"
        assert analysis["actual_vision_backend"] == ""
        provenance = analysis["output"]["provenance"]
        assert provenance["requested_backend"] == "florence2"
        assert provenance["actual_backend"] == ""
        assert provenance["failed_stage"] == "manifest_parse"
        assert provenance["perception_completed"] is False
        assert provenance["synthesis_started"] is False
        assert analysis["evidence"]["synthesis"]["stage"] == "not_started"
    finally:
        db.close()


def test_previous_assistant_prose_is_not_visual_evidence_after_florence_failure(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = FailingFlorenceBackend(stage="runtime_import")
    source = write_png(tmp_path / "prose-is-not-evidence.png")
    try:
        first = chat.send("Pretend there is a red desk in the image.", model="text:local")
        artifact = artifacts.import_path(str(source), scope="session")
        models.calls.clear()
        result = chat.send(
            "tell me about it",
            session_id=first["session"]["id"],
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="florence-failure-prose-not-evidence",
        )
        assert "No visual answer was generated" in result["assistant_message"]["content"]
        assert not [call for call in models.calls if call["kind"] == "text"]
        assert "red desk" not in result["assistant_message"]["content"].lower()
    finally:
        db.close()


def test_partial_florence_output_remains_usable_with_warnings(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = PartialFlorenceBackend()
    source = write_png(tmp_path / "partial-florence.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        result = chat.send(
            "Tell me about the desk",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            vision_backend="florence2",
            analysis_request_id="florence-partial-success",
        )
        analysis = result["artifact_analysis"]
        assert analysis["status"] == "completed_with_warnings"
        assert analysis["actual_vision_backend"] == "florence2"
        assert analysis["output"]["visual_evidence"]["summary"].startswith("A tidy desk")
        assert analysis["output"]["provenance"]["synthesis_started"] is True
        assert analysis["output"]["provenance"]["final_answer_stage"] == "model_generation"
        assert analysis["evidence"]["tasks_completed"] == ["more_detailed_caption"]
        assert analysis["evidence"]["tasks_failed"]
        assert [call for call in models.calls if call["kind"] == "text"]
        prompt = "\n".join(str(message["content"]) for message in models.calls[-1]["messages"])
        assert "A tidy desk with a monitor and keyboard." in prompt
    finally:
        db.close()


def test_artifact_import_derivatives_redaction_and_delete(tmp_path: Path):
    db, _documents, _rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "screen.png")
    try:
        artifact = artifacts.import_path(str(source))
        assert artifact["kind"] == "image"
        assert "stored_path" not in artifact
        assert artifact["source_path_redacted"] is True
        assert artifact["thumbnail_path"]
        assert Path(artifact["thumbnail_path"]).exists()
        kinds = {item["kind"] for item in artifact["derivations"]}
        assert {"normalized_image", "thumbnail", "vision_input", "ocr_input"}.issubset(kinds)
        assert artifact["original_format"] == "PNG"
        assert artifact["original_color_mode"] == "RGBA"
        assert artifact["original_pixel_count"] == 240 * 120
        assert artifact["original_has_alpha"] is True
        assert artifact["preprocessing_version"] == "image-preprocess-v1"

        deleted = artifacts.delete(artifact["id"])
        assert deleted["deleted"] is True
        assert not Path(artifact["thumbnail_path"]).exists()
        # Unreferenced artifacts are hard-deleted (Issue #17); a tombstone
        # survives only when chat history or analysis runs reference the row.
        assert deleted["tombstoned"] is False
        with pytest.raises(KeyError):
            artifacts.get(artifact["id"])
    finally:
        db.close()


def test_artifact_import_rejects_malformed_image(tmp_path: Path):
    db = Database(tmp_path / "profile")
    artifacts = ArtifactService(db)
    malformed = tmp_path / "bad.png"
    malformed.write_bytes(b"not actually an image")
    try:
        with pytest.raises(ArtifactValidationError):
            artifacts.import_path(str(malformed))
    finally:
        db.close()


def test_artifact_import_rejects_oversized_and_animated_inputs(tmp_path: Path):
    db = Database(tmp_path / "profile")
    artifacts = ArtifactService(db)
    oversized = tmp_path / "too-large.png"
    animated = tmp_path / "animated.webp"
    with oversized.open("wb") as handle:
        handle.seek(MAX_ORIGINAL_BYTES)
        handle.write(b"x")
    frames = [
        Image.new("RGB", (24, 24), (200, 40, 40)),
        Image.new("RGB", (24, 24), (40, 80, 200)),
    ]
    try:
        frames[0].save(animated, format="WEBP", save_all=True, append_images=frames[1:], duration=50, loop=0)
    except Exception as exc:  # pragma: no cover - depends on local Pillow WebP features
        pytest.skip(f"animated WebP save is unavailable in this Pillow build: {exc}")
    try:
        with pytest.raises(ArtifactValidationError, match="byte limit"):
            artifacts.import_path(str(oversized))
        with pytest.raises(ArtifactValidationError, match="animated"):
            artifacts.import_path(str(animated))
    finally:
        db.close()


@pytest.mark.parametrize(
    ("orientation", "expected_size", "normalized"),
    [
        (6, (120, 80), True),
        (3, (80, 120), True),
        (2, (80, 120), True),
        (None, (80, 120), False),
    ],
)
def test_exif_orientation_is_applied_to_derivatives(
    tmp_path: Path,
    orientation: int | None,
    expected_size: tuple[int, int],
    normalized: bool,
):
    db, _documents, _rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    source = write_exif_jpeg(tmp_path / f"orientation-{orientation or 'none'}.jpg", orientation=orientation)
    try:
        artifact = artifacts.import_path(str(source))
        assert (artifact["width"], artifact["height"]) == expected_size
        assert artifact["normalized_orientation"] is normalized
        assert artifact["original_exif_orientation"] == (str(orientation) if orientation is not None else "")
        with Image.open(artifacts.thumbnail_path(artifact["id"])) as thumbnail:
            assert thumbnail.size == expected_size
        with Image.open(artifacts.vision_image_path(artifact["id"])) as vision_input:
            assert vision_input.size == expected_size
    finally:
        db.close()


def test_small_jpeg_and_webp_are_not_upscaled(tmp_path: Path):
    db, _documents, _rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    jpeg = tmp_path / "small.jpg"
    webp = tmp_path / "small.webp"
    Image.new("RGB", (64, 32), (80, 120, 160)).save(jpeg, format="JPEG", quality=90)
    Image.new("RGB", (96, 48), (100, 140, 180)).save(webp, format="WEBP", quality=90)
    try:
        jpeg_artifact = artifacts.import_path(str(jpeg))
        webp_artifact = artifacts.import_path(str(webp))
        assert artifacts.derivative_details(jpeg_artifact["id"])["vision_input"]["width"] == 64
        assert artifacts.derivative_details(jpeg_artifact["id"])["ocr_input"]["height"] == 32
        assert webp_artifact["original_format"] == "WEBP"
        assert artifacts.derivative_details(webp_artifact["id"])["vision_input"]["width"] == 96
        assert artifacts.derivative_details(webp_artifact["id"])["ocr_input"]["format"] == "PNG"
    finally:
        db.close()


def test_large_rgba_png_gets_bounded_purpose_specific_derivatives(tmp_path: Path):
    db, _documents, _rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    source = tmp_path / "large-desk.png"
    noise = Image.effect_noise((1536, 2048), 90).convert("RGBA")
    alpha = Image.new("L", noise.size, 225)
    noise.putalpha(alpha)
    noise.save(source, format="PNG")
    original_bytes = source.read_bytes()
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        assert sha256_bytes(source.read_bytes()) == sha256_bytes(original_bytes)
        details = artifacts.derivative_details(artifact["id"])
        vision_input = details["vision_input"]
        ocr_input = details["ocr_input"]
        thumbnail = details["thumbnail"]
        assert details["original"]["width"] == 1536
        assert details["original"]["height"] == 2048
        assert details["original"]["has_alpha"] is True
        assert thumbnail["width"] <= 384 and thumbnail["height"] <= 384
        assert vision_input["format"] == "JPEG"
        assert max(vision_input["width"], vision_input["height"]) <= 1024
        assert vision_input["pixels"] <= 1_100_000
        assert vision_input["color_mode"] == "RGB"
        assert vision_input["alpha_flattened"] is True
        assert Path(artifacts.vision_image_path(artifact["id"])).stat().st_size < len(original_bytes)
        assert ocr_input["format"] == "PNG"
        assert max(ocr_input["width"], ocr_input["height"]) <= 2000
        assert ocr_input["color_mode"] == "RGB"
        assert ocr_input["alpha_flattened"] is True
        assert artifacts.vision_image_path(artifact["id"]) != artifacts.ocr_image_path(artifact["id"])
    finally:
        db.close()


def test_analysis_uses_ocr_derivative_and_vision_derivative_without_regenerating(tmp_path: Path):
    db, _documents, _rag, artifacts, ocr, models, vision, _chat = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "terminal-large.png", size=(1536, 2048))
    try:
        artifact = artifacts.import_path(str(source))
        vision_path = artifacts.vision_image_path(artifact["id"])
        ocr_path = artifacts.ocr_image_path(artifact["id"])
        before_counts = derivative_counts(db, artifact["id"])
        analysis = vision.analyze(
            artifact["id"],
            mode="combined",
            question="What error is visible?",
            vision_model="llava:local",
            request_id="derivative-routing-1",
        )
        after_counts = derivative_counts(db, artifact["id"])
        assert before_counts == after_counts
        assert analysis["status"] == "completed"
        assert ocr.engine.image_paths[-1] == ocr_path
        assert models.calls[0]["kind"] == "vision"
        assert models.calls[0]["image_paths"] == [vision_path]
        preprocessing = analysis["evidence"]["preprocessing"]
        assert preprocessing["vision_input"]["derivative_kind"] == "vision_input"
        assert preprocessing["ocr_input"]["derivative_kind"] == "ocr_input"
    finally:
        db.close()


def test_direct_image_ocr_and_combined_vision_analysis(tmp_path: Path):
    db, _documents, _rag, artifacts, ocr, models, vision, _chat = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "terminal.png")
    try:
        artifact = artifacts.import_path(str(source))
        ocr_result = ocr.run_image_ocr(artifacts.ocr_image_path(artifact["id"]), source_id=artifact["id"])
        assert ocr_result["available"] is True
        assert "ERROR 42" in ocr_result["text"]
        assert ocr_result["metadata"]["words"][0]["text"] == "ERROR"

        analysis = vision.analyze(
            artifact["id"],
            mode="combined",
            question="What error is visible?",
            vision_model="llava:local",
            request_id="analysis-1",
        )
        assert analysis["status"] == "completed"
        assert analysis["output"]["ocr_text"] == "ERROR 42: cache miss"
        assert analysis["output"]["vision_observations"]["summary"].startswith("A terminal")
        derivation_kinds = {item["kind"] for item in artifacts.derivations(artifact["id"])}
        assert {"ocr_text", "vision_observations", "combined_evidence"}.issubset(derivation_kinds)
        assert models.calls[0]["kind"] == "vision"
    finally:
        db.close()


def test_chat_send_links_image_attachment_and_hydrates_history(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, _models, _vision, chat = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "clip.png")
    try:
        artifact = artifacts.import_path(str(source), source_kind="clipboard")
        result = chat.send(
            "Read the visible error",
            artifact_ids=[artifact["id"]],
            multimodal_mode="ocr_only",
            analysis_request_id="chat-analysis-1",
        )
        assert result["assistant_message"]["content"] == "text reply"
        assert _models.calls[-1]["kind"] == "text"
        assert result["artifact_analysis"]["output"]["provenance"]["final_answer_stage"] == "model_generation"
        assert result["user_message"]["artifacts"][0]["id"] == artifact["id"]
        recovered = chat.send(
            "Read the visible error",
            artifact_ids=[artifact["id"]],
            multimodal_mode="ocr_only",
            analysis_request_id="chat-analysis-1",
        )
        assert recovered["user_message"]["id"] == result["user_message"]["id"]
        assert len(SessionService(db).messages(result["session"]["id"])) == 2
        hydrated = artifacts.hydrate_messages(SessionService(db).messages(result["session"]["id"]))
        assert hydrated[0]["artifacts"][0]["status_label"] == "imported"
    finally:
        db.close()


def test_native_vision_followup_rehydrates_historical_image_message(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, _vision, chat = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "desk.png")
    try:
        artifact = artifacts.import_path(str(source), source_kind="clipboard", scope="session")
        first = chat.send(
            "Describe the desk image",
            model="vision:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            analysis_request_id="native-continuity-1",
        )
        assert first["assistant_message"]["content"]

        models.calls.clear()
        followup = chat.send("tell me about it", session_id=first["session"]["id"], model="vision:local")
        assert followup["assistant_message"]["content"]
        native_calls = [call for call in models.calls if call["kind"] == "vision_history"]
        assert native_calls
        assert native_calls[-1]["kwargs"]["timeout"] >= 240
        sent_messages = native_calls[-1]["messages"]
        historical_user = next(
            message for message in sent_messages
            if message["role"] == "user" and str(message["content"]).startswith("Describe the desk image")
        )
        assert historical_user.get("images")
        assert sum(1 for message in sent_messages if message.get("images")) == 1
        latest_user = sent_messages[-1]
        assert latest_user["role"] == "user"
        assert "images" not in latest_user
    finally:
        db.close()


def test_native_vision_synthesis_timeout_is_persisted(tmp_path: Path, monkeypatch):
    db, _documents, _rag, artifacts, _ocr, models, _vision, chat = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "timeout.png")

    def timeout_history(*_args, **_kwargs):
        raise ModelTimeoutError("timeout: synthetic native vision timeout")

    monkeypatch.setattr(models, "chat_vision_history_detailed", timeout_history)
    try:
        artifact = artifacts.import_path(str(source), source_kind="clipboard", scope="session")
        result = chat.send(
            "Describe this image",
            model="vision:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            analysis_request_id="native-timeout-1",
        )
        assert result["assistant_message"] is None
        analysis = result["artifact_analysis"]
        assert analysis["status"] == "timeout"
        assert analysis["stage"] == "synthesis_timeout"
        assert "synthetic native vision timeout" in analysis["error"]
        assert analysis["output"]["provenance"]["failed_stage"] == "synthesis"
        assert analysis["output"]["provenance"]["requested_final_model"] == "vision:local"
        assert analysis["output"]["provenance"]["vision_inspection_model"] == "vision:local"
        assert analysis["evidence"]["synthesis"]["stage"] == "timeout"
        assert analysis["evidence"]["synthesis"]["native_image_request"] is True
        assert analysis["evidence"]["preprocessing"]["vision_input"]["derivative_kind"] == "vision_input"
    finally:
        db.close()


def test_external_eyes_followup_reuses_persisted_observations(tmp_path: Path):
    db, _documents, _rag, artifacts, _ocr, models, _vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no", "vision:local": "yes"},
    )
    source = write_png(tmp_path / "terminal.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        first = chat.send(
            "What error is visible?",
            model="text:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="combined",
            vision_model="vision:local",
            analysis_request_id="external-continuity-1",
        )
        assert first["artifact_analysis"]["output"]["vision_observations"]["summary"].startswith("A terminal")

        models.calls.clear()
        followup = chat.send("tell me about it", session_id=first["session"]["id"], model="text:local")
        assert not [call for call in models.calls if call["kind"] in {"vision", "vision_history"}]
        text_calls = [call for call in models.calls if call["kind"] == "text"]
        assert text_calls
        prompt = "\n".join(str(message["content"]) for message in text_calls[-1]["messages"])
        assert "A terminal shows a cache miss error." in prompt
        provenance = followup["artifact_analysis"]["output"]["provenance"]
        assert provenance["context_evidence_action"] == "reused"
        assert provenance["image_request_sent"] is False
        assert followup["artifact_analysis"]["evidence"]["conversation_context"]["action"] == "reused"
    finally:
        db.close()


def test_base64_is_not_persisted_after_native_image_followup(tmp_path: Path):
    profile = tmp_path / "profile"
    db, _documents, _rag, artifacts, _ocr, models, _vision, chat = build_image_stack(profile)
    source = write_png(tmp_path / "payload.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        first = chat.send(
            "Describe this image",
            model="vision:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            analysis_request_id="base64-continuity-1",
        )
        models.calls.clear()
        followup = chat.send("tell me about it", session_id=first["session"]["id"], model="vision:local")
        native_call = [call for call in models.calls if call["kind"] == "vision_history"][-1]
        encoded = native_call["messages"][0]["images"][0]
        assert base64.b64decode(encoded)
        serialized_result = json.dumps(followup, default=str)
        assert encoded not in serialized_result
        assert "iVBOR" not in serialized_result
        db.conn.commit()
        for db_file in [profile / "app.db", profile / "app.db-wal", profile / "app.db-shm"]:
            if db_file.exists():
                data = db_file.read_bytes()
                assert encoded.encode("ascii") not in data
                assert b"iVBOR" not in data
        hydrated = chat._hydrate_messages(SessionService(db).messages(first["session"]["id"]))
        assert "images" not in json.dumps(hydrated, default=str)
    finally:
        db.close()


def test_removing_image_from_conversation_context_blocks_followup_reuse(tmp_path: Path):
    db, documents, rag, artifacts, _ocr, models, _vision, chat = build_image_stack(tmp_path / "profile")
    sources = SourceService(documents, artifacts, rag)
    source = write_png(tmp_path / "remove.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        first = chat.send(
            "Describe this image",
            model="vision:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            analysis_request_id="remove-continuity-1",
        )
        context = sources.conversation_context(first["session"]["id"])
        assert context and context[0]["conversation_status"] == "in_conversation"
        sources.remove_from_conversation(first["session"]["id"], "artifact", artifact["id"])

        models.calls.clear()
        followup = chat.send("tell me about it", session_id=first["session"]["id"], model="vision:local")
        assert followup["assistant_message"]["content"].startswith("No image is currently in the conversation context.")
        assert not [call for call in models.calls if call["kind"] == "vision_history"]
        assert sources.conversation_context(first["session"]["id"]) == []
    finally:
        db.close()


def test_image_conversation_context_survives_restart(tmp_path: Path):
    profile = tmp_path / "profile"
    db, _documents, _rag, artifacts, _ocr, _models, _vision, chat = build_image_stack(profile)
    source = write_png(tmp_path / "restart.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        first = chat.send(
            "Describe this image",
            model="vision:local",
            artifact_ids=[artifact["id"]],
            multimodal_mode="automatic",
            analysis_request_id="restart-continuity-1",
        )
        session_id = first["session"]["id"]
    finally:
        db.close()

    db2, _documents2, _rag2, _artifacts2, _ocr2, models2, _vision2, chat2 = build_image_stack(profile)
    try:
        followup = chat2.send("tell me about it", session_id=session_id, model="vision:local")
        assert followup["assistant_message"]["content"]
        native_calls = [call for call in models2.calls if call["kind"] == "vision_history"]
        assert native_calls
        assert any("images" in message for message in native_calls[-1]["messages"])
    finally:
        db2.close()


def test_session_default_and_active_model_semantics(tmp_path: Path):
    db, documents, rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    settings = SettingsService(db)
    sessions = SessionService(db)
    sources = SourceService(documents, artifacts, rag)
    image = write_png(tmp_path / "context.png")
    try:
        settings.set({"default_model": "qwen3:8b"})
        first = sessions.create()
        assert first["model"] == "qwen3:8b"
        updated = sessions.update(first["id"], {"model": "nemotron-nano-chat:8b"})
        assert updated["model"] == "nemotron-nano-chat:8b"
        assert settings.get()["default_model"] == "qwen3:8b"

        settings.set({"default_model": "llama3.2:latest"})
        assert sessions.get(first["id"])["model"] == "nemotron-nano-chat:8b"

        preselected = sessions.create(model="qwen3-vl:2b")
        assert preselected["model"] == "qwen3-vl:2b"
        artifact = artifacts.import_path(str(image), scope="session")
        sources.add_to_conversation(first["id"], [("artifact", artifact["id"])])
        assert sources.active_artifact_ids(first["id"]) == [artifact["id"]]
        assert sources.conversation_context(preselected["id"]) == []
    finally:
        db.close()


def test_sidebar_and_header_product_semantics_are_explicit():
    root = Path(__file__).resolve().parents[2]
    app_source = (root / "src" / "App.tsx").read_text(encoding="utf-8")
    sidebar_source = (root / "src" / "features" / "shell" / "AppSidebar.tsx").read_text(encoding="utf-8")
    header_source = (root / "src" / "features" / "chat" / "ChatHeader.tsx").read_text(encoding="utf-8")

    assert "AppSidebar" in app_source
    assert "ChatHeader" in app_source
    assert "New chat" in sidebar_source
    assert "Recent chats" in sidebar_source
    assert "Default model for new chats" in sidebar_source
    assert "Ollama:" in sidebar_source
    assert "Profile:" in sidebar_source
    assert "profile_dir" not in sidebar_source
    assert "127.0.0.1:11434" not in sidebar_source
    assert "New session" not in sidebar_source
    assert "min-h-0 flex-1 overflow-auto" in sidebar_source
    assert "Conversation model" in header_source
    assert "Use this model for future messages in this conversation" in header_source
    assert "Vision:" in header_source
    assert "Default model for new chats" not in header_source
    assert "sessions.update" in app_source
    assert "setPendingSources([])" in app_source
    assert "setConversationContext([])" in app_source
    assert 'setSelectedRagDocumentId("")' in app_source


def test_sources_facade_hides_session_imports_until_promotion(tmp_path: Path):
    db, documents, rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    sources = SourceService(documents, artifacts, rag)
    note = tmp_path / "note.md"
    note.write_text("Odysseus session attachment note about source scope.", encoding="utf-8")
    image = write_png(tmp_path / "session.png")
    try:
        imported = sources.import_many([str(note), str(image)], scope="session", index=True)
        assert len(imported["sources"]) == 2
        assert sources.list(scope="library") == []
        session_sources = sources.list(scope="session")
        assert {source["backend_kind"] for source in session_sources} == {"document", "artifact"}
        document_source = next(source for source in session_sources if source["backend_kind"] == "document")
        promoted = sources.promote("document", document_source["id"], index=True)
        assert promoted["source"]["scope"] == "library"
        assert [source["id"] for source in sources.list(scope="library")] == [document_source["id"]]
    finally:
        db.close()


def test_direct_text_attachment_links_message_and_scopes_rag(tmp_path: Path):
    db, documents, rag, _artifacts, _ocr, models, _vision, chat = build_image_stack(tmp_path / "profile")
    sources = SourceService(documents, _artifacts, rag)
    attached = tmp_path / "attached.txt"
    attached.write_text("The launch code is BLUE-17 and belongs only to the attached file.", encoding="utf-8")
    other = tmp_path / "other.txt"
    other.write_text("The launch code is RED-99 in this persistent source.", encoding="utf-8")
    try:
        session_doc = sources.import_path(str(attached), scope="session", index=True)["document"]
        library_doc = sources.import_path(str(other), scope="library", index=True)["document"]
        result = chat.send(
            "What is the launch code?",
            attachment_document_ids=[session_doc["id"]],
            document_ids=[library_doc["id"]],
        )
        assert result["user_message"]["documents"][0]["id"] == session_doc["id"]
        assert result["retrieved_chunks"]
        assert {chunk["document_id"] for chunk in result["retrieved_chunks"]}.issubset({session_doc["id"], library_doc["id"]})
        assert models.calls[-1]["kind"] == "text"
        assert documents.list(scope="library")[0]["id"] == library_doc["id"]
        assert session_doc["id"] not in {document["id"] for document in documents.list(scope="library")}
    finally:
        db.close()


def test_artifact_rag_bridge_indexes_internal_document_and_hides_it(tmp_path: Path):
    db, documents, rag, artifacts, *_ = build_image_stack(tmp_path / "profile")
    source = write_png(tmp_path / "ocr.png")
    try:
        artifact = artifacts.import_path(str(source))
        derivation = artifacts.insert_text_derivation(
            artifact["id"],
            "ocr_text",
            "Visible local invoice number INV-2048",
            producer_type="ocr",
            producer_name="fake-ocr",
        )
        indexed = artifacts.index_derivation(artifact["id"], derivation["id"])
        assert indexed["document"]["is_internal"] is True
        assert documents.list() == []
        results = rag.search("invoice INV-2048", limit=1)
        assert results and results[0]["metadata"]["source_kind"] == "artifact"
        artifacts.unindex(artifact["id"])
        assert rag.search("invoice INV-2048", limit=1) == []
    finally:
        db.close()


def test_capability_registry_does_not_infer_vision_from_model_name(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "profile")
    models = ModelService(db)

    def fake_post(_url: str, payload: dict, timeout: float):
        assert payload["model"] == "llava-not-really:latest"
        return {
            "details": {"family": "llama", "parameter_size": "1b"},
            "model_info": {},
            "capabilities": ["completion"],
        }

    monkeypatch.setattr(models, "_post_json", fake_post)
    try:
        inspected = models.inspect("llava-not-really:latest")
        assert inspected["vision"] == "no"
    finally:
        db.close()


def test_ollama_vision_payload_adds_base64_only_at_request_boundary(tmp_path: Path, monkeypatch):
    db = Database(tmp_path / "profile")
    models = ModelService(db)
    image = write_png(tmp_path / "payload.png")
    captured: dict[str, object] = {}

    def fake_post(_url: str, payload: dict, timeout: float):
        captured.update(payload)
        return {"model": "vision:local", "message": {"content": "ok"}, "done_reason": "stop"}

    monkeypatch.setattr(models, "_post_json", fake_post)
    try:
        result = models.chat_vision_detailed(
            "vision:local",
            [{"role": "user", "content": "describe"}],
            image_paths=[str(image)],
            thinking="off",
        )
        images = captured["messages"][0]["images"]
        assert len(images) == 1
        assert base64.b64decode(images[0])
        assert "images" not in result
        assert result["content"] == "ok"
    finally:
        db.close()


def test_image_eval_suite_and_grader_are_separate_from_production_prompts():
    cases = load_cases()
    assert len(cases) >= 10
    case = {
        "assertions": [
            {"type": "required_object", "any": ["terminal"]},
            {"type": "forbidden_object", "any": ["dog"]},
            {"type": "abstention"},
        ]
    }
    grade = grade_case(
        case,
        {
            "output": {
                "summary": "A terminal is visible. The password is not visible and not determinable.",
                "visible_objects": ["terminal"],
            }
        },
    )
    assert grade["passed"] is True
    prompt_source = inspect.getsource(vision_module.vision_prompt)
    leaked = [case["id"] for case in cases if case["id"] in prompt_source]
    leaked += [Path(case["image"]).name for case in cases if Path(case["image"]).name in prompt_source]
    assert leaked == []


def test_rpc_artifact_and_image_eval_methods(tmp_path: Path):
    source = write_png(tmp_path / "rpc.png")
    app = SidecarApp(tmp_path / "profile")
    try:
        artifact = app.dispatch("artifacts.import", {"path": str(source)})
        assert artifact["id"]
        assert app.dispatch("artifacts.list", {})[0]["id"] == artifact["id"]
        source_rows = app.dispatch("sources.list", {})
        assert source_rows[0]["backend_kind"] == "artifact"
        session_import = app.dispatch("sources.import", {"path": str(source), "scope": "session"})
        assert session_import["source"]["scope"] == "session"
        assert app.dispatch("artifacts.derivations", {"artifact_id": artifact["id"]})
        suite = app.dispatch("image_evals.list", {})
        assert suite["suite_version"] == "v0.2.0"
        diagnostics = app.dispatch("diagnostics.get", {})
        assert diagnostics["image_vision"]["artifacts"]["artifact_count"] == 2
        assert diagnostics["image_vision"]["sources"]["library_count"] == 1
        assert diagnostics["image_vision"]["sources"]["session_count"] == 1
    finally:
        app.close()
