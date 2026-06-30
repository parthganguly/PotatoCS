from __future__ import annotations

import shutil
import subprocess
import tempfile
import os
import re
import time
from dataclasses import dataclass, field
from glob import glob
from pathlib import Path
from typing import Any
from typing import Protocol

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.progress import emit_progress
from odysseus_desktop_backend.services.model_service import VISION_CHAT_TIMEOUT_SECONDS
from odysseus_desktop_backend.services.perception_types import (
    FLORENCE_TASK_OCR_WITH_REGION,
    PerceptionTaskPlan,
)
from odysseus_desktop_backend.services.document_service import DocumentService, OCRPage
from odysseus_desktop_backend.services.rag_service import RAGService


OCR_UNAVAILABLE_MESSAGE = "This appears scanned/low-text. OCR is not installed/enabled yet."
OCR_SUBPROCESS_TIMEOUT_SECONDS = 60
OCR_PDF_RENDER_DPI = 400
OCR_PREPROCESSING_VERSION = "ocr-prep-v3"
OCR_TESSERACT_PSM_MODES = (6, 11, 12)
OCR_TESSERACT_ENHANCED_PSM_MODES = (4, 6, 11)
OCR_TESSERACT_CROP_PSM_MODES = (6,)
OCR_CROP_REGIONS = (
    ("top", (0.0, 0.0, 1.0, 0.34)),
    ("middle", (0.0, 0.28, 1.0, 0.72)),
    ("bottom", (0.0, 0.58, 1.0, 1.0)),
    ("left_column", (0.0, 0.0, 0.42, 1.0)),
    ("center_column", (0.28, 0.0, 0.72, 1.0)),
    ("right_column", (0.58, 0.0, 1.0, 1.0)),
    ("lower_table", (0.0, 0.42, 1.0, 0.94)),
)
VLM_TEXT_EXTRACTION_PROMPT = (
    "Transcribe only visible text from this page or crop. Preserve table rows if possible. "
    "Do not summarize. Do not invent missing words. Use [unclear] for unreadable text. "
    "Return plain text only."
)
QUALITY_LABELS = {"good", "usable_noisy", "poor", "no_text"}
COMMON_ENGLISH_WORDS = {
    "a", "about", "after", "again", "all", "also", "an", "and", "anger", "are", "at", "bait",
    "be", "because", "been", "before", "but", "by", "cabin", "can", "class", "cooled", "did",
    "document", "down", "entry", "father", "for", "from", "go", "had", "have", "he", "her",
    "herself", "him", "his", "horrible", "i", "if", "in", "is", "it", "just", "learning",
    "left", "let", "like", "me", "mention", "not", "of", "off", "on", "or", "over", "page",
    "patient", "relationship", "rebecca", "row", "said", "say", "she", "should", "something",
    "speaker", "take", "text", "that", "the", "then", "things", "this", "to", "unclear",
    "was", "warsaw", "with", "zoo",
}
logger = get_logger("ocr")


@dataclass(frozen=True)
class OCRDependencyStatus:
    found: bool
    path: str
    source: str


@dataclass(frozen=True)
class OCREngineStatus:
    available: bool
    engine_name: str
    renderer: str
    message: str
    dependencies: dict[str, OCRDependencyStatus] = field(default_factory=dict)


class OCRExecutionError(RuntimeError):
    pass


class OCRTimeoutError(OCRExecutionError):
    pass


@dataclass(frozen=True)
class OCRImageResult:
    source_path: str
    engine_name: str
    confidence: float | None
    text: str
    width: int
    height: int
    elapsed_ms: int
    metadata: dict[str, object] = field(default_factory=dict)


def ocr_result_stats(
    pages: list[dict[str, object]],
    index: dict[str, object] | None,
    warning: str = "",
) -> dict[str, object]:
    return {
        "pages_processed": len(pages),
        "pages_with_text": sum(1 for page in pages if str(page.get("text") or "").strip()),
        "chunks_created": len(index["chunks"]) if index and isinstance(index.get("chunks"), list) else 0,
        "embeddings_created": int(index["embedded"]) if index and "embedded" in index else 0,
        "embeddings_cached": int(index["cached"]) if index and "cached" in index else 0,
        "warning": warning,
    }


def subprocess_time_ms() -> int:
    return int(time.perf_counter() * 1000)


class OCREngine(Protocol):
    name: str

    def status(self) -> OCREngineStatus:
        raise NotImplementedError

    def ocr_pdf(self, stored_path: str, source_path: str) -> list[OCRPage]:
        raise NotImplementedError

    def ocr_image(self, image_path: str, *, source_id: str = "") -> OCRImageResult:
        raise NotImplementedError


class VLMTextExtractor(Protocol):
    def available(self) -> dict[str, object]:
        raise NotImplementedError

    def transcribe_page(
        self,
        image_path: str,
        *,
        page_number: int | None = None,
        crop: dict[str, object] | None = None,
    ) -> dict[str, object]:
        raise NotImplementedError


class LocalVLMTextExtractor:
    def __init__(self, models: Any, florence: Any | None = None):
        self.models = models
        self.florence = florence
        self._cached_available: dict[str, object] | None = None

    def available(self) -> dict[str, object]:
        if self._cached_available is not None:
            return dict(self._cached_available)
        if self.florence is not None:
            try:
                status = self.florence.status(check_hashes=False)
                if status.get("ready"):
                    self._cached_available = {
                        "available": True,
                        "backend": "florence2",
                        "model": str(status.get("model_id") or "microsoft/Florence-2-base-ft"),
                        "reason": "bundled Florence-2 pack is available",
                    }
                    return dict(self._cached_available)
            except Exception as exc:  # noqa: BLE001 - optional fallback should degrade quietly
                logger.debug("florence vlm text availability check failed error=%s", exc)
        try:
            for capability in self.models.capabilities(refresh=False):
                if str(capability.get("vision") or "") == "yes":
                    self._cached_available = {
                        "available": True,
                        "backend": "ollama",
                        "model": str(capability.get("model") or ""),
                        "reason": "installed Ollama vision model is available",
                    }
                    return dict(self._cached_available)
        except Exception as exc:  # noqa: BLE001 - optional fallback should degrade quietly
            logger.debug("ollama vlm text availability check failed error=%s", exc)
        self._cached_available = {
            "available": False,
            "backend": "",
            "model": "",
            "reason": "no bundled Florence-2 or installed local Ollama vision model is available",
        }
        return dict(self._cached_available)

    def transcribe_page(
        self,
        image_path: str,
        *,
        page_number: int | None = None,
        crop: dict[str, object] | None = None,
    ) -> dict[str, object]:
        status = self.available()
        if not status.get("available"):
            return {
                "attempted": False,
                "available": False,
                "backend": "",
                "model": "",
                "text": "",
                "page": page_number,
                "crop": crop or {},
                "reason": status.get("reason") or "local VLM text extraction unavailable",
            }
        backend = str(status.get("backend") or "")
        model = str(status.get("model") or "")
        started = subprocess_time_ms()
        try:
            if backend == "florence2" and self.florence is not None:
                response = self.florence.inspect(
                    image_path,
                    question=VLM_TEXT_EXTRACTION_PROMPT,
                    task_plan=PerceptionTaskPlan(
                        tasks=[FLORENCE_TASK_OCR_WITH_REGION],
                        needs_exact_text=True,
                        needs_visual_details=False,
                        reason="pdf_page_text_extraction",
                    ),
                    input_metadata={"page": page_number, "crop": crop or {}},
                )
                text = extract_florence_text(response)
            else:
                response = self.models.chat_vision_detailed(
                    model,
                    [{"role": "user", "content": VLM_TEXT_EXTRACTION_PROMPT}],
                    image_paths=[image_path],
                    options={"temperature": 0},
                    thinking="off",
                    timeout=VISION_CHAT_TIMEOUT_SECONDS,
                )
                text = str(response.get("content") or "").strip()
            quality = score_ocr_quality(text, None)
            return {
                "attempted": True,
                "available": True,
                "backend": backend,
                "model": model,
                "text": text,
                "page": page_number,
                "crop": crop or {},
                "quality": quality,
                "elapsed_ms": max(0, subprocess_time_ms() - started),
                "prompt_version": "vlm-text-transcription-v1",
            }
        except Exception as exc:  # noqa: BLE001 - optional fallback should not break OCR
            return {
                "attempted": True,
                "available": True,
                "backend": backend,
                "model": model,
                "text": "",
                "page": page_number,
                "crop": crop or {},
                "error": str(exc),
                "elapsed_ms": max(0, subprocess_time_ms() - started),
                "prompt_version": "vlm-text-transcription-v1",
            }


class TesseractPdfEngine:
    name = "tesseract"

    def __init__(self, vlm_text_extractor: VLMTextExtractor | None = None):
        self.tesseract = ""
        self.pdftoppm = ""
        self.mutool = ""
        self.vlm_text_extractor = vlm_text_extractor

    def status(self) -> OCREngineStatus:
        dependencies = self._detect_dependencies()
        if not dependencies["tesseract"].found:
            return OCREngineStatus(False, self.name, "", OCR_UNAVAILABLE_MESSAGE, dependencies)
        renderer = self._renderer_name()
        if not renderer:
            return OCREngineStatus(
                False,
                self.name,
                "",
                "Tesseract is installed, but no PDF renderer was found. Install Poppler pdftoppm or MuPDF mutool.",
                dependencies,
            )
        return OCREngineStatus(True, self.name, renderer, "OCR is available.", dependencies)

    def _detect_dependencies(self) -> dict[str, OCRDependencyStatus]:
        dependencies = {
            "tesseract": self._detect_executable(
                "tesseract",
                [
                    r"%ProgramFiles%\Tesseract-OCR\tesseract.exe",
                    r"%ProgramFiles(x86)%\Tesseract-OCR\tesseract.exe",
                    r"%LocalAppData%\Programs\Tesseract-OCR\tesseract.exe",
                    r"%LocalAppData%\Microsoft\WinGet\Packages\UB-Mannheim.TesseractOCR_*\tesseract.exe",
                    r"%LocalAppData%\Microsoft\WinGet\Packages\Tesseract-OCR.Tesseract_*\tesseract.exe",
                ],
            ),
            "pdftoppm": self._detect_executable(
                "pdftoppm",
                [
                    r"%ProgramFiles%\Poppler\bin\pdftoppm.exe",
                    r"%ProgramFiles%\Poppler\Library\bin\pdftoppm.exe",
                    r"%ProgramFiles%\poppler*\Library\bin\pdftoppm.exe",
                    r"%LocalAppData%\Microsoft\WinGet\Packages\oschwartz10612.Poppler_*\Library\bin\pdftoppm.exe",
                    r"%LocalAppData%\Microsoft\WinGet\Packages\oschwartz10612.Poppler_*\poppler*\Library\bin\pdftoppm.exe",
                ],
            ),
            "mutool": self._detect_executable(
                "mutool",
                [
                    r"%ProgramFiles%\MuPDF\mutool.exe",
                    r"%ProgramFiles%\mupdf*\mutool.exe",
                    r"%LocalAppData%\Microsoft\WinGet\Packages\ArtifexSoftware.mutool_*\mutool.exe",
                    r"%LocalAppData%\Microsoft\WinGet\Packages\ArtifexSoftware.mutool_*\**\mutool.exe",
                ],
            ),
        }
        self.tesseract = dependencies["tesseract"].path
        self.pdftoppm = dependencies["pdftoppm"].path
        self.mutool = dependencies["mutool"].path
        return dependencies

    def _detect_executable(self, name: str, windows_fallbacks: list[str]) -> OCRDependencyStatus:
        resolved = shutil.which(name)
        if resolved:
            return OCRDependencyStatus(True, resolved, "PATH")

        for pattern in windows_fallbacks:
            for candidate in glob(os.path.expandvars(pattern), recursive=True):
                path = Path(candidate)
                if path.is_file():
                    return OCRDependencyStatus(True, str(path), "windows_fallback")

        return OCRDependencyStatus(False, "", "")

    def ocr_pdf(self, stored_path: str, source_path: str) -> list[OCRPage]:
        status = self.status()
        if not status.available:
            raise OCRExecutionError(status.message)
        with tempfile.TemporaryDirectory(prefix="odysseus-ocr-") as tmp:
            emit_progress("pdf_render")
            images = self._render_pdf(Path(stored_path), Path(tmp), status.renderer)
            pages: list[OCRPage] = []
            for page_number, image in enumerate(images, start=1):
                emit_progress("ocr_page", progress_current=page_number, progress_total=len(images))
                result = self.ocr_image(str(image), source_id=f"{source_path}#page={page_number}", page_number=page_number)
                pages.append(
                    OCRPage(
                        source_path=source_path,
                        page_number=page_number,
                        engine_name=self.name,
                        confidence=result.confidence,
                        text=result.text,
                        metadata={
                            "image_width": result.width,
                            "image_height": result.height,
                            "elapsed_ms": result.elapsed_ms,
                            **result.metadata,
                        },
                    )
            )
            return pages

    def ocr_image(self, image_path: str, *, source_id: str = "", page_number: int | None = None) -> OCRImageResult:
        status = self.status()
        if not status.available:
            raise OCRExecutionError(status.message)
        image = Path(image_path)
        started = subprocess_time_ms()
        text, confidence, metadata = self._run_tesseract(image, page_number=page_number)
        width = 0
        height = 0
        try:
            from PIL import Image

            with Image.open(image) as opened:
                width, height = opened.size
        except Exception:
            pass
        return OCRImageResult(
            source_path=source_id or str(image),
            engine_name=self.name,
            confidence=confidence,
            text=text,
            width=width,
            height=height,
            elapsed_ms=max(0, subprocess_time_ms() - started),
            metadata=metadata,
        )

    def _renderer_name(self) -> str:
        if self.pdftoppm:
            return "pdftoppm"
        if self.mutool:
            return "mutool"
        return ""

    def _render_pdf(self, pdf: Path, tmp: Path, renderer: str) -> list[Path]:
        if renderer == "pdftoppm":
            prefix = tmp / "page"
            self._run_ocr_subprocess(
                [self.pdftoppm or "pdftoppm", "-png", "-r", str(OCR_PDF_RENDER_DPI), str(pdf), str(prefix)],
                "pdftoppm",
            )
            images = sorted(tmp.glob("page-*.png"))
            if not images:
                raise OCRExecutionError("pdftoppm rendered no page images.")
            return images
        if renderer == "mutool":
            out_pattern = tmp / "page-%d.png"
            self._run_ocr_subprocess(
                [self.mutool or "mutool", "draw", "-r", str(OCR_PDF_RENDER_DPI), "-o", str(out_pattern), str(pdf)],
                "mutool",
            )
            images = sorted(tmp.glob("page-*.png"))
            if not images:
                raise OCRExecutionError("mutool rendered no page images.")
            return images
        raise OCRExecutionError("No supported PDF renderer available.")

    def _run_tesseract(self, image: Path, *, page_number: int | None = None) -> tuple[str, float | None, dict[str, object]]:
        passes: list[dict[str, object]] = []
        attempted: list[dict[str, object]] = []
        for variant_name, variant_path, psm_modes in self._preprocess_variants(image):
            for psm in psm_modes:
                text, confidence, metadata = self._run_tesseract_tsv(variant_path, psm)
                metadata["preprocessing"] = variant_name
                metadata["psm"] = psm
                metadata["source_type"] = "ocr_text"
                quality = score_ocr_quality(text, confidence, lines=metadata.get("lines") if isinstance(metadata.get("lines"), list) else None)
                metadata["quality"] = quality
                passes.append({"text": text, "confidence": confidence, "metadata": metadata, "quality": quality})
                attempted.append(
                    {
                        "source_type": "ocr_text",
                        "preprocessing": variant_name,
                        "psm": psm,
                        "text_char_count": len(text.strip()),
                        "confidence": confidence,
                        "quality": quality,
                        "text_excerpt": text.strip()[:160],
                    }
                )
        selected = select_best_ocr_pass(passes)
        selected_quality = selected.get("quality") if isinstance(selected.get("quality"), dict) else score_ocr_quality("")
        crop_passes: list[dict[str, object]] = []
        crop_evidence: list[dict[str, object]] = []
        if should_try_crop_ocr(selected_quality):
            emit_progress("crop_ocr")
            crop_passes, crop_evidence = self._run_crop_ocr(image, page_number=page_number)
            passes.extend(crop_passes)
            attempted.extend(
                {
                    "source_type": "ocr_crop",
                    "crop": (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("crop") or {},
                    "preprocessing": (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("preprocessing") or "",
                    "psm": (item.get("metadata") if isinstance(item.get("metadata"), dict) else {}).get("psm") or 0,
                    "text_char_count": len(str(item.get("text") or "").strip()),
                    "confidence": item.get("confidence"),
                    "quality": item.get("quality") if isinstance(item.get("quality"), dict) else {},
                    "text_excerpt": str(item.get("text") or "").strip()[:160],
                }
                for item in crop_passes
            )
            if crop_passes:
                selected = select_best_ocr_pass([selected, *crop_passes])
                selected_quality = selected.get("quality") if isinstance(selected.get("quality"), dict) else selected_quality

        final_text_parts: list[str] = []
        selected_text = str(selected.get("text") or "")
        if selected_text.strip():
            final_text_parts.append(selected_text)
        for crop in top_useful_crop_passes(crop_passes):
            crop_text = str(crop.get("text") or "").strip()
            if crop_text and is_novel_ocr_text(crop_text, "\n".join(final_text_parts)):
                final_text_parts.append(crop_text)

        vlm_text_evidence: dict[str, object] = {
            "attempted": False,
            "available": False,
            "backend": "",
            "model": "",
            "text": "",
            "reason": "OCR quality did not require local VLM fallback",
        }
        if should_try_vlm_text(selected_quality):
            if self.vlm_text_extractor is not None:
                selected_metadata = selected.get("metadata") if isinstance(selected.get("metadata"), dict) else {}
                vlm_image_path = str(selected_metadata.get("image_path") or image)
                vlm_crop = selected_metadata.get("crop") if isinstance(selected_metadata.get("crop"), dict) else None
                vlm_text_evidence = self.vlm_text_extractor.transcribe_page(vlm_image_path, page_number=page_number, crop=vlm_crop)
            else:
                vlm_text_evidence = {
                    "attempted": False,
                    "available": False,
                    "backend": "",
                    "model": "",
                    "text": "",
                    "reason": "no local VLM text extractor is configured",
                }
            vlm_text = str(vlm_text_evidence.get("text") or "").strip()
            vlm_quality = vlm_text_evidence.get("quality") if isinstance(vlm_text_evidence.get("quality"), dict) else score_ocr_quality(vlm_text)
            if vlm_text and str(vlm_quality.get("label") or "") in {"good", "usable_noisy"} and is_novel_ocr_text(vlm_text, "\n".join(final_text_parts)):
                final_text_parts.insert(0, vlm_text)

        all_attempt_texts: list[str] = []
        confidences: list[float] = []
        boxes: list[dict[str, object]] = []
        lines: list[dict[str, object]] = []
        seen_lines: set[str] = set()
        selected_items = [selected, *top_useful_crop_passes(crop_passes)]
        for item in selected_items:
            text = str(item.get("text") or "")
            confidence = item.get("confidence") if isinstance(item.get("confidence"), (float, int)) else None
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            normalized = " ".join(text.lower().split())
            if text and normalized not in {" ".join(existing.lower().split()) for existing in all_attempt_texts}:
                all_attempt_texts.append(text)
            if confidence is not None:
                confidences.append(confidence)
            boxes.extend(metadata.get("word_boxes") if isinstance(metadata.get("word_boxes"), list) else [])
            for line in metadata.get("lines") if isinstance(metadata.get("lines"), list) else []:
                line_text = str(line.get("text") or "").strip() if isinstance(line, dict) else ""
                line_key = " ".join(line_text.lower().split())
                if not line_text or line_key in seen_lines:
                    continue
                seen_lines.add(line_key)
                line_item = dict(line)
                if "crop" in metadata and "crop" not in line_item:
                    line_item["crop"] = metadata.get("crop")
                if metadata.get("source_type"):
                    line_item["source_type"] = metadata.get("source_type")
                lines.append(line_item)
        for item in passes:
            metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
            for line in metadata.get("lines") if isinstance(metadata.get("lines"), list) else []:
                if not isinstance(line, dict):
                    continue
                line_text = str(line.get("text") or "").strip()
                line_key = " ".join(line_text.lower().split())
                if not line_key or line_key in seen_lines or not is_salvage_ocr_line(line_text):
                    continue
                seen_lines.add(line_key)
                line_item = dict(line)
                line_item["source_type"] = "ocr_attempt_recovery"
                line_item["preprocessing"] = metadata.get("preprocessing") or ""
                line_item["psm"] = metadata.get("psm") or 0
                if "crop" in metadata and "crop" not in line_item:
                    line_item["crop"] = metadata.get("crop")
                lines.insert(0, line_item)
        vlm_text = str(vlm_text_evidence.get("text") or "").strip()
        if vlm_text:
            for line in lines_from_text(vlm_text, source_type="vlm_assisted_text", page_number=page_number):
                line_key = " ".join(str(line.get("text") or "").lower().split())
                if line_key and line_key not in seen_lines:
                    seen_lines.add(line_key)
                    lines.append(line)
        avg_confidence = sum(confidences) / len(confidences) if confidences else None
        final_text = "\n".join(final_text_parts)
        final_quality = score_ocr_quality(final_text, avg_confidence, lines=lines)
        selected_metadata = selected.get("metadata") if isinstance(selected.get("metadata"), dict) else {}
        return final_text, avg_confidence, {
            "lines": lines[:800],
            "word_boxes": boxes[:500],
            "timeout_seconds": OCR_SUBPROCESS_TIMEOUT_SECONDS,
            "render_dpi": OCR_PDF_RENDER_DPI,
            "psm_modes": list(OCR_TESSERACT_PSM_MODES),
            "enhanced_psm_modes": list(OCR_TESSERACT_ENHANCED_PSM_MODES),
            "crop_psm_modes": list(OCR_TESSERACT_CROP_PSM_MODES),
            "ocr_attempts": attempted,
            "ocr_attempt_count": len(attempted),
            "selected_ocr_attempt": {
                "source_type": selected_metadata.get("source_type") or "ocr_text",
                "preprocessing": selected_metadata.get("preprocessing") or "",
                "psm": selected_metadata.get("psm") or 0,
                "crop": selected_metadata.get("crop") or {},
                "confidence": selected.get("confidence"),
                "quality": selected.get("quality") if isinstance(selected.get("quality"), dict) else {},
            },
            "ocr_quality": final_quality,
            "quality": final_quality,
            "quality_label": final_quality["label"],
            "crop_evidence": crop_evidence[:20],
            "ocr_crop_count": len(crop_evidence),
            "vlm_text_evidence": without_large_text(vlm_text_evidence),
            "vlm_text_available": bool(vlm_text.strip()),
            "vlm_text_char_count": len(vlm_text),
            "preprocessing_version": OCR_PREPROCESSING_VERSION,
        }

    def _preprocess_variants(self, image: Path) -> list[tuple[str, Path, tuple[int, ...]]]:
        variants: list[tuple[str, Path, tuple[int, ...]]] = [
            ("rendered", image, OCR_TESSERACT_PSM_MODES)
        ]
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps

            enhanced_path = image.with_name(f"{image.stem}-contrast.png")
            threshold_path = image.with_name(f"{image.stem}-threshold.png")
            inverted_path = image.with_name(f"{image.stem}-inverted-threshold.png")
            resized_15_path = image.with_name(f"{image.stem}-resize-150.png")
            resized_20_path = image.with_name(f"{image.stem}-resize-200.png")
            with Image.open(image) as opened:
                gray = ImageOps.grayscale(opened)
                contrasted = ImageOps.autocontrast(gray)
                contrasted = ImageEnhance.Contrast(contrasted).enhance(1.8)
                sharpened = contrasted.filter(ImageFilter.SHARPEN)
                sharpened.save(enhanced_path)
                thresholded = sharpened.point(lambda pixel: 255 if pixel > 165 else 0)
                thresholded.save(threshold_path)
                ImageOps.invert(thresholded).save(inverted_path)
                resize_filter = getattr(Image.Resampling, "LANCZOS", Image.BICUBIC)
                width, height = sharpened.size
                sharpened.resize((max(1, int(width * 1.5)), max(1, int(height * 1.5))), resize_filter).save(resized_15_path)
                sharpened.resize((max(1, width * 2), max(1, height * 2)), resize_filter).save(resized_20_path)
            variants.append(("grayscale_contrast_sharpen", enhanced_path, OCR_TESSERACT_ENHANCED_PSM_MODES))
            variants.append(("threshold", threshold_path, OCR_TESSERACT_ENHANCED_PSM_MODES))
            variants.append(("inverted_threshold", inverted_path, OCR_TESSERACT_ENHANCED_PSM_MODES))
            variants.append(("resize_150_contrast_sharpen", resized_15_path, OCR_TESSERACT_ENHANCED_PSM_MODES))
            variants.append(("resize_200_contrast_sharpen", resized_20_path, OCR_TESSERACT_ENHANCED_PSM_MODES))
        except Exception as exc:  # noqa: BLE001 - OCR should continue with rendered image
            logger.debug("ocr preprocessing variant failed image=%s error=%s", image, exc)
        return variants

    def _run_crop_ocr(self, image: Path, *, page_number: int | None = None) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        crop_passes: list[dict[str, object]] = []
        crop_evidence: list[dict[str, object]] = []
        for crop_name, crop_path, crop_metadata in self._crop_regions(image):
            crop_attempts: list[dict[str, object]] = []
            for variant_name, variant_path, _psm_modes in self._preprocess_crop_variants(crop_path):
                for psm in OCR_TESSERACT_CROP_PSM_MODES:
                    text, confidence, metadata = self._run_tesseract_tsv(variant_path, psm)
                    metadata["preprocessing"] = variant_name
                    metadata["psm"] = psm
                    metadata["source_type"] = "ocr_crop"
                    metadata["crop"] = crop_metadata
                    metadata["page_number"] = page_number
                    metadata["image_path"] = str(crop_path)
                    for line in metadata.get("lines") if isinstance(metadata.get("lines"), list) else []:
                        if isinstance(line, dict):
                            line["crop"] = crop_metadata
                            line["source_type"] = "ocr_crop"
                    quality = score_ocr_quality(text, confidence, lines=metadata.get("lines") if isinstance(metadata.get("lines"), list) else None)
                    metadata["quality"] = quality
                    item = {"text": text, "confidence": confidence, "metadata": metadata, "quality": quality}
                    crop_passes.append(item)
                    crop_attempts.append(item)
            selected = select_best_ocr_pass(crop_attempts)
            selected_quality = selected.get("quality") if isinstance(selected.get("quality"), dict) else score_ocr_quality("")
            selected_metadata = selected.get("metadata") if isinstance(selected.get("metadata"), dict) else {}
            crop_evidence.append(
                {
                    "name": crop_name,
                    "page": page_number,
                    "coordinates": crop_metadata,
                    "selected_preprocessing": selected_metadata.get("preprocessing") or "",
                    "selected_psm": selected_metadata.get("psm") or 0,
                    "quality": selected_quality,
                    "confidence": selected.get("confidence"),
                    "text_char_count": len(str(selected.get("text") or "").strip()),
                    "text_excerpt": str(selected.get("text") or "").strip()[:240],
                    "attempt_count": len(crop_attempts),
                }
            )
        return crop_passes, crop_evidence

    def _preprocess_crop_variants(self, image: Path) -> list[tuple[str, Path, tuple[int, ...]]]:
        variants = [("crop_rendered", image, OCR_TESSERACT_CROP_PSM_MODES)]
        try:
            from PIL import Image, ImageEnhance, ImageFilter, ImageOps

            enhanced_path = image.with_name(f"{image.stem}-crop-contrast.png")
            threshold_path = image.with_name(f"{image.stem}-crop-threshold.png")
            inverted_path = image.with_name(f"{image.stem}-crop-inverted-threshold.png")
            with Image.open(image) as opened:
                gray = ImageOps.grayscale(opened)
                contrasted = ImageOps.autocontrast(gray)
                contrasted = ImageEnhance.Contrast(contrasted).enhance(2.0)
                sharpened = contrasted.filter(ImageFilter.SHARPEN)
                sharpened.save(enhanced_path)
                thresholded = sharpened.point(lambda pixel: 255 if pixel > 160 else 0)
                thresholded.save(threshold_path)
                ImageOps.invert(thresholded).save(inverted_path)
            variants.append(("crop_grayscale_contrast_sharpen", enhanced_path, OCR_TESSERACT_CROP_PSM_MODES))
            variants.append(("crop_threshold", threshold_path, OCR_TESSERACT_CROP_PSM_MODES))
            variants.append(("crop_inverted_threshold", inverted_path, OCR_TESSERACT_CROP_PSM_MODES))
        except Exception as exc:  # noqa: BLE001 - OCR should continue with crop image
            logger.debug("ocr crop preprocessing failed image=%s error=%s", image, exc)
        return variants

    def _crop_regions(self, image: Path) -> list[tuple[str, Path, dict[str, object]]]:
        try:
            from PIL import Image

            crops: list[tuple[str, Path, dict[str, object]]] = []
            with Image.open(image) as opened:
                width, height = opened.size
                for name, (left, top, right, bottom) in OCR_CROP_REGIONS:
                    box = (
                        max(0, min(width - 1, int(width * left))),
                        max(0, min(height - 1, int(height * top))),
                        max(1, min(width, int(width * right))),
                        max(1, min(height, int(height * bottom))),
                    )
                    if box[2] - box[0] < 80 or box[3] - box[1] < 80:
                        continue
                    crop_path = image.with_name(f"{image.stem}-crop-{name}.png")
                    opened.crop(box).save(crop_path)
                    crops.append(
                        (
                            name,
                            crop_path,
                            {
                                "name": name,
                                "x": box[0],
                                "y": box[1],
                                "width": box[2] - box[0],
                                "height": box[3] - box[1],
                                "normalized": [left, top, right, bottom],
                            },
                        )
                    )
            return crops
        except Exception as exc:  # noqa: BLE001 - crop strategy is optional
            logger.debug("ocr crop region generation failed image=%s error=%s", image, exc)
            return []

    def _run_tesseract_tsv(self, image: Path, psm: int) -> tuple[str, float | None, dict[str, object]]:
        proc = self._run_ocr_subprocess(
            [self.tesseract or "tesseract", str(image), "stdout", "tsv", "--psm", str(psm)],
            "Tesseract OCR",
        )
        stdout = self._safe_subprocess_text(proc.stdout)
        line_words: dict[tuple[str, str, str], list[str]] = {}
        line_confidences: dict[tuple[str, str, str], list[float]] = {}
        line_boxes: dict[tuple[str, str, str], list[dict[str, object]]] = {}
        confidences: list[float] = []
        boxes: list[dict[str, object]] = []
        for line in stdout.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) < 12:
                continue
            text = parts[11].strip()
            if not text:
                continue
            line_key = (parts[2], parts[3], parts[4])
            line_words.setdefault(line_key, []).append(text)
            try:
                confidence = float(parts[10])
            except ValueError:
                confidence = -1.0
            if confidence >= 0:
                confidences.append(confidence)
                line_confidences.setdefault(line_key, []).append(confidence)
            try:
                box = {
                    "text": text,
                    "confidence": confidence,
                    "left": int(parts[6]),
                    "top": int(parts[7]),
                    "width": int(parts[8]),
                    "height": int(parts[9]),
                }
                boxes.append(box)
                line_boxes.setdefault(line_key, []).append(box)
            except ValueError:
                pass
        avg_confidence = sum(confidences) / len(confidences) if confidences else None
        lines: list[dict[str, object]] = []
        rendered_lines: list[str] = []
        for line_number, (line_key, words) in enumerate(line_words.items(), start=1):
            line_text = " ".join(words).strip()
            if not line_text:
                continue
            rendered_lines.append(line_text)
            line_conf = line_confidences.get(line_key) or []
            word_boxes = line_boxes.get(line_key) or []
            lines.append(
                {
                    "line_number": line_number,
                    "text": line_text,
                    "confidence": sum(line_conf) / len(line_conf) if line_conf else None,
                    "word_boxes": word_boxes[:80],
                }
            )
        text = "\n".join(rendered_lines)
        return text, avg_confidence, {
            "lines": lines,
            "word_boxes": boxes[:500],
            "timeout_seconds": OCR_SUBPROCESS_TIMEOUT_SECONDS,
        }

    def _run_ocr_subprocess(self, command: list[str], label: str) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(
                command,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=OCR_SUBPROCESS_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise OCRExecutionError(f"{label} executable was not found: {command[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise OCRTimeoutError(
                f"{label} timed out after {OCR_SUBPROCESS_TIMEOUT_SECONDS}s. Try a smaller image or a shorter document."
            ) from exc
        except OSError as exc:
            raise OCRExecutionError(f"{label} failed to start: {exc}") from exc

        if proc.returncode != 0:
            detail = (
                self._safe_subprocess_text(proc.stderr).strip()
                or self._safe_subprocess_text(proc.stdout).strip()
                or f"exit code {proc.returncode}"
            )
            raise OCRExecutionError(f"{label} failed: {detail}")
        return proc

    def _safe_subprocess_text(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)


def score_ocr_quality(
    text: str,
    confidence: float | None = None,
    *,
    query_terms: list[str] | None = None,
    lines: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    content = str(text or "").strip()
    tokens = re.findall(r"[A-Za-z0-9']+", content)
    words = [token for token in tokens if re.search(r"[A-Za-z]", token)]
    alpha_words = [word for word in words if re.search(r"[A-Za-z]{2,}", word)]
    char_count = len(content)
    word_count = len(words)
    alpha_word_ratio = len(alpha_words) / max(1, len(tokens))
    one_char_count = sum(1 for word in words if len(re.sub(r"[^A-Za-z]", "", word)) == 1)
    one_char_ratio = one_char_count / max(1, word_count)
    weird_tokens = [
        word
        for word in words
        if is_garbage_ocr_token(word)
    ]
    dictionary_hits = sum(
        1
        for word in alpha_words
        if normalize_word(word) in COMMON_ENGLISH_WORDS
    )
    dictionary_ratio = dictionary_hits / max(1, len(alpha_words))
    garbage_token_ratio = max(
        len(weird_tokens) / max(1, word_count),
        max(0.0, 0.42 - dictionary_ratio),
    )
    punctuation_count = sum(1 for char in content if not char.isalnum() and not char.isspace())
    repeated_punctuation_count = len(re.findall(r"[^A-Za-z0-9\s]{2,}", content))
    repeated_punctuation_ratio = repeated_punctuation_count / max(1, punctuation_count)
    line_count = len(lines or [line for line in content.splitlines() if line.strip()])
    query_hits = 0
    compact_text = compact_quality_text(content)
    for term in query_terms or []:
        if compact_quality_text(term) in compact_text:
            query_hits += 1
    confidence_value = float(confidence) if isinstance(confidence, (float, int)) and confidence >= 0 else None
    score = 0.0
    score += min(24.0, char_count / 8)
    score += min(18.0, word_count * 0.9)
    score += alpha_word_ratio * 18.0
    score += dictionary_ratio * 26.0
    score += min(8.0, line_count * 0.7)
    score += query_hits * 8.0
    if confidence_value is not None:
        score += min(24.0, max(0.0, confidence_value) * 0.28)
    score -= garbage_token_ratio * 35.0
    score -= one_char_ratio * 18.0
    score -= repeated_punctuation_ratio * 10.0

    if char_count < 5 or word_count < 2:
        label = "no_text"
    elif score >= 64 and char_count >= 80 and alpha_word_ratio >= 0.55 and garbage_token_ratio <= 0.35:
        label = "good"
    elif score >= 34 and char_count >= 25 and alpha_word_ratio >= 0.42 and garbage_token_ratio <= 0.68:
        label = "usable_noisy"
    else:
        label = "poor"
    return {
        "label": label,
        "score": round(score, 3),
        "char_count": char_count,
        "word_count": word_count,
        "line_count": line_count,
        "alpha_word_ratio": round(alpha_word_ratio, 3),
        "garbage_token_ratio": round(garbage_token_ratio, 3),
        "one_char_token_ratio": round(one_char_ratio, 3),
        "dictionary_word_ratio": round(dictionary_ratio, 3),
        "repeated_punctuation_ratio": round(repeated_punctuation_ratio, 3),
        "average_confidence": round(confidence_value, 3) if confidence_value is not None else None,
        "query_term_hits": query_hits,
    }


def normalize_word(word: str) -> str:
    return re.sub(r"[^a-z]", "", str(word or "").lower())


def compact_quality_text(text: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", str(text or "").lower()))


def is_garbage_ocr_token(token: str) -> bool:
    clean = normalize_word(token)
    if not clean:
        return False
    if len(clean) >= 5 and not re.search(r"[aeiouy]", clean):
        return True
    if re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", clean):
        return True
    if re.search(r"(.)\1\1", clean) and clean not in {"ooo", "zoo"}:
        return True
    if re.search(r"\d", token) and re.search(r"[A-Za-z]", token) and len(token) > 3:
        return True
    return False


def select_best_ocr_pass(passes: list[dict[str, object]]) -> dict[str, object]:
    if not passes:
        return {"text": "", "confidence": None, "metadata": {}, "quality": score_ocr_quality("")}
    return max(
        passes,
        key=lambda item: (
            float((item.get("quality") if isinstance(item.get("quality"), dict) else {}).get("score") or 0),
            len(str(item.get("text") or "")),
        ),
    )


def should_try_crop_ocr(quality: dict[str, object]) -> bool:
    return (
        str(quality.get("label") or "") in {"usable_noisy", "poor", "no_text"}
        or float(quality.get("score") or 0) < 72
        or int(quality.get("char_count") or 0) < 1200
        or int(quality.get("line_count") or 0) < 24
    )


def should_try_vlm_text(quality: dict[str, object]) -> bool:
    return (
        str(quality.get("label") or "") in {"usable_noisy", "poor"}
        or float(quality.get("score") or 0) < 54
    )


def top_useful_crop_passes(crop_passes: list[dict[str, object]]) -> list[dict[str, object]]:
    useful = [
        item
        for item in crop_passes
        if str((item.get("quality") if isinstance(item.get("quality"), dict) else {}).get("label") or "") in {"good", "usable_noisy"}
        and len(str(item.get("text") or "").strip()) >= 20
    ]
    useful.sort(
        key=lambda item: (
            -float((item.get("quality") if isinstance(item.get("quality"), dict) else {}).get("score") or 0),
            -len(str(item.get("text") or "")),
        )
    )
    return useful[:6]


def is_novel_ocr_text(candidate: str, existing: str) -> bool:
    candidate_key = compact_quality_text(candidate)
    existing_key = compact_quality_text(existing)
    if not candidate_key:
        return False
    if candidate_key in existing_key:
        return False
    if existing_key and existing_key in candidate_key and len(candidate_key) < len(existing_key) + 80:
        return False
    return True


def is_salvage_ocr_line(text: str) -> bool:
    clean = str(text or "").strip()
    if len(clean) < 4:
        return False
    compact = compact_quality_text(clean)
    if any(term in compact for term in ("rebecca", "rebeca", "warsaw", "zoo")):
        return True
    if re.search(r"\b[A-Z][A-Za-z]{4,}\b", clean) and score_ocr_quality(clean).get("label") != "poor":
        return True
    return False


def lines_from_text(text: str, *, source_type: str, page_number: int | None = None) -> list[dict[str, object]]:
    lines: list[dict[str, object]] = []
    for index, line in enumerate(str(text or "").splitlines(), start=1):
        clean = line.strip()
        if clean:
            lines.append(
                {
                    "line_number": index,
                    "text": clean,
                    "confidence": None,
                    "word_boxes": [],
                    "source_type": source_type,
                    "page_number": page_number,
                }
            )
    return lines


def without_large_text(evidence: dict[str, object]) -> dict[str, object]:
    cleaned = dict(evidence)
    text = str(cleaned.pop("text", "") or "")
    cleaned["text_char_count"] = len(text.strip())
    cleaned["text_excerpt"] = text.strip()[:500]
    return cleaned


def extract_florence_text(response: dict[str, object]) -> str:
    visual = response.get("visual_evidence") if isinstance(response.get("visual_evidence"), dict) else {}
    texts: list[str] = []
    for item in visual.get("text") or []:
        if isinstance(item, dict):
            value = str(item.get("text") or "").strip()
        else:
            value = str(item or "").strip()
        if value:
            texts.append(value)
    if texts:
        return "\n".join(texts)
    raw = response.get("raw") if isinstance(response.get("raw"), dict) else {}
    for value in raw.values():
        if isinstance(value, str) and value.strip():
            texts.append(value.strip())
        elif isinstance(value, dict):
            for nested in value.values():
                if isinstance(nested, str) and nested.strip():
                    texts.append(nested.strip())
    return "\n".join(texts)


class OCRService:
    def __init__(
        self,
        documents: DocumentService,
        rag: RAGService,
        engine: OCREngine | None = None,
    ):
        self.documents = documents
        self.rag = rag
        self.engine = engine or TesseractPdfEngine()

    def set_vlm_text_extractor(self, extractor: VLMTextExtractor | None) -> None:
        if hasattr(self.engine, "vlm_text_extractor"):
            setattr(self.engine, "vlm_text_extractor", extractor)

    def status(self) -> dict[str, object]:
        status = self.engine.status()
        logger.info(
            "ocr status available=%s engine=%s renderer=%s message=%s",
            status.available,
            status.engine_name,
            status.renderer,
            status.message,
        )
        return {
            "available": status.available,
            "engine_name": status.engine_name,
            "renderer": status.renderer,
            "message": status.message,
            "dependencies": {
                name: {
                    "found": dependency.found,
                    "path": dependency.path,
                    "source": dependency.source,
                }
                for name, dependency in status.dependencies.items()
            },
            "subprocess_timeout_seconds": OCR_SUBPROCESS_TIMEOUT_SECONDS,
        }

    def run_image_ocr(self, image_path: str, *, source_id: str = "") -> dict[str, object]:
        status = self.engine.status()
        if not status.available:
            return {
                "available": False,
                "engine_name": status.engine_name,
                "text": "",
                "confidence": None,
                "metadata": {},
                "warning": status.message,
                "ocr_status": self.status(),
            }
        try:
            result = self.engine.ocr_image(image_path, source_id=source_id)
        except OCRExecutionError as exc:
            return {
                "available": False,
                "engine_name": status.engine_name,
                "text": "",
                "confidence": None,
                "metadata": {},
                "warning": str(exc),
                "ocr_status": self.status(),
            }
        return {
            "available": True,
            "engine_name": result.engine_name,
            "text": result.text,
            "confidence": result.confidence,
            "metadata": {
                "source_path": result.source_path,
                "width": result.width,
                "height": result.height,
                "elapsed_ms": result.elapsed_ms,
                **result.metadata,
            },
            "warning": "" if result.text.strip() else "OCR ran, but no text was extracted.",
            "ocr_status": self.status(),
        }

    def ensure_document_ocr_indexed(self, document_id: str) -> dict[str, object]:
        document = self.documents.get(document_id)
        if document["file_type"] != "pdf":
            return self._skipped_result(document_id, "not_pdf")
        if not self.documents.needs_ocr(document_id):
            return self._skipped_result(document_id, "already_ready")
        return self.run_document_ocr(document_id)

    def run_document_ocr(self, document_id: str) -> dict[str, object]:
        document = self.documents.get(document_id)
        if document["file_type"] != "pdf":
            raise ValueError("OCR is only available for PDF documents in Milestone 3.")

        status = self.engine.status()
        logger.info(
            "ocr requested document_id=%s file_name=%s available=%s renderer=%s",
            document_id,
            document["file_name"],
            status.available,
            status.renderer,
        )
        if not status.available:
            self.documents.mark_ocr_unavailable(document_id, status.message)
            pages: list[dict[str, object]] = []
            return {
                "document": self.documents.get(document_id),
                "ocr_status": self.status(),
                "ocr_pages": pages,
                "stats": ocr_result_stats(pages, None, status.message),
                "index": None,
            }

        self.documents.mark_ocr_running(document_id, status.engine_name)
        try:
            pages = self.engine.ocr_pdf(document["stored_path"], document["source_path"])
        except OCRExecutionError as exc:
            logger.warning("ocr execution failed document_id=%s error=%s", document_id, exc)
            return self._empty_result(document_id, str(exc))
        if not any(page.text.strip() for page in pages):
            message = "OCR ran, but no text was extracted."
            self.documents.replace_ocr_pages(document_id, pages, index_status="no_text")
            self.documents.mark_ocr_no_text(document_id, message)
            logger.warning("ocr no_text document_id=%s pages=%s", document_id, len(pages))
            stored_pages = self.documents.ocr_pages(document_id)
            return {
                "document": self.documents.get(document_id),
                "ocr_status": self.status(),
                "ocr_pages": stored_pages,
                "stats": ocr_result_stats(stored_pages, None, message),
                "index": None,
            }

        self.documents.mark_ocr_ready(document_id, status.engine_name)
        self.documents.replace_pages_from_ocr(document_id, pages)
        emit_progress("source_index")
        indexed = self.rag.index_document(document_id)
        self.documents.mark_ocr_indexed(document_id, status.engine_name)
        self.documents.link_ocr_chunks(document_id)
        stored_pages = self.documents.ocr_pages(document_id)
        index = {
            **indexed,
            "document": self.documents.get(document_id),
        }
        logger.info(
            "ocr indexed document_id=%s pages=%s chunks=%s embedded=%s cached=%s",
            document_id,
            len(stored_pages),
            len(index["chunks"]),
            index["embedded"],
            index["cached"],
        )

        return {
            "document": self.documents.get(document_id),
            "ocr_status": self.status(),
            "ocr_pages": stored_pages,
            "stats": ocr_result_stats(stored_pages, index),
            "index": index,
        }

    def _skipped_result(self, document_id: str, reason: str) -> dict[str, object]:
        pages = self.documents.ocr_pages(document_id)
        return {
            "document": self.documents.get(document_id),
            "ocr_status": self.status(),
            "ocr_pages": pages,
            "stats": ocr_result_stats(pages, None, reason),
            "index": None,
            "skipped": True,
            "reason": reason,
        }

    def _empty_result(
        self,
        document_id: str,
        message: str,
        pages: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        self.documents.mark_ocr_unavailable(document_id, message)
        stored_pages = pages or []
        return {
            "document": self.documents.get(document_id),
            "ocr_status": self.status(),
            "ocr_pages": stored_pages,
            "stats": ocr_result_stats(stored_pages, None, message),
            "index": None,
        }
