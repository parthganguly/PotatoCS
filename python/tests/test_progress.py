from __future__ import annotations

import json
from pathlib import Path

from odysseus_desktop_backend.progress import ProgressEmitter, emit_progress, progress_operation
from odysseus_desktop_backend.services.ocr_service import (
    OCREngineStatus,
    OCRImageResult,
    TesseractPdfEngine,
)
from test_v020_image_understanding import (
    CookingFlorenceBackend,
    EmptyImageOcrEngine,
    build_image_stack,
    write_png,
)


def progress_lines(captured: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in captured.splitlines() if "__odysseus_progress__" in line]


def test_progress_emitter_writes_valid_discriminated_json(capsys):
    emitter = ProgressEmitter(operation_id="chat-image-123", session_id="session-1")
    payload = emitter.emit("florence_load", cache_status="miss")
    events = progress_lines(capsys.readouterr().err)
    assert payload is not None
    assert events == [payload]
    assert events[0]["__odysseus_progress__"] is True
    assert events[0]["label"] == "Loading Florence..."
    assert events[0]["detail"] is None


def test_progress_labels_and_detail_cannot_leak_paths_or_private_text(capsys):
    secret_path = r"C:\Users\Private\Downloads\invoice.png"
    secret_ocr = "Found text: account 12345"
    emitter = ProgressEmitter(operation_id=secret_path, session_id=secret_path, artifact_id="artifact-1")
    emitter.emit("ocr_page", progress_current=2, progress_total=5, detail=secret_ocr)
    raw = capsys.readouterr().err
    event = progress_lines(raw)[0]
    assert event["label"] == "Running OCR page 2/5..."
    assert event["session_id"] is None
    assert secret_path not in raw
    assert secret_ocr not in raw


def test_progress_dynamic_retrieval_label_is_numeric_only(capsys):
    ProgressEmitter(operation_id="rag-1").emit("rag_retrieved", progress_current=3)
    event = progress_lines(capsys.readouterr().err)[0]
    assert event["label"] == "Retrieved 3 snippets..."
    assert event["progress_current"] == 3


def test_progress_emitter_failure_never_crashes_caller(monkeypatch):
    class BrokenStderr:
        def write(self, _value):
            raise OSError("closed")

        def flush(self):
            raise OSError("closed")

    monkeypatch.setattr("odysseus_desktop_backend.progress.sys.stderr", BrokenStderr())
    assert ProgressEmitter(operation_id="safe-operation").emit("answer_generation") is None


def test_progress_operation_emits_done(capsys):
    with progress_operation(operation_id="chat-1") as emitter:
        emitter.emit("answer_generation")
    events = progress_lines(capsys.readouterr().err)
    assert [event["stage"] for event in events] == ["answer_generation", "done"]
    assert events[-1]["status"] == "completed"


def test_visual_first_turn_and_followup_emit_expected_sequences(tmp_path: Path, capsys):
    db, _documents, _rag, artifacts, _ocr, _models, vision, chat = build_image_stack(
        tmp_path / "profile",
        vision={"text:local": "no"},
        image_ocr_engine=EmptyImageOcrEngine(),
    )
    vision.florence = CookingFlorenceBackend()
    source = write_png(tmp_path / "private-image-name.png")
    try:
        artifact = artifacts.import_path(str(source), scope="session")
        with progress_operation(operation_id="visual-first", artifact_id=artifact["id"]):
            first = chat.send(
                "What is the man doing?",
                model="text:local",
                artifact_ids=[artifact["id"]],
                multimodal_mode="automatic",
                vision_backend="florence2",
                analysis_request_id="visual-first",
            )
        first_raw = capsys.readouterr().err
        first_stages = [event["stage"] for event in progress_lines(first_raw)]
        assert first_stages == [
            "model_capability_check",
            "image_prepare",
            "ocr_run",
            "visual_evidence_build",
            "visual_evidence_retrieval",
            "answer_generation",
            "done",
        ]
        assert str(source) not in first_raw
        assert "What is the man doing?" not in first_raw

        with progress_operation(operation_id="visual-followup", session_id=first["session"]["id"]):
            chat.send("What is the woman doing?", session_id=first["session"]["id"], model="text:local")
        followup_stages = [event["stage"] for event in progress_lines(capsys.readouterr().err)]
        assert followup_stages == [
            "model_capability_check",
            "visual_evidence_reuse",
            "visual_evidence_retrieval",
            "answer_generation",
            "done",
        ]
        assert len(vision.florence.calls) == 1
    finally:
        db.close()


def test_pdf_ocr_progress_uses_page_counts_without_paths(capsys, tmp_path: Path):
    class FakePdfEngine(TesseractPdfEngine):
        def status(self):
            return OCREngineStatus(True, self.name, "fake-renderer", "OCR is available.")

        def _render_pdf(self, _pdf: Path, tmp: Path, _renderer: str):
            return [tmp / "page-1.png", tmp / "page-2.png", tmp / "page-3.png"]

        def ocr_image(self, _image_path: str, *, source_id: str = "", page_number: int | None = None):
            return OCRImageResult(
                source_path=source_id,
                engine_name=self.name,
                confidence=90.0,
                text=f"page {page_number}",
                width=100,
                height=100,
                elapsed_ms=1,
                metadata={},
            )

    private_pdf = tmp_path / "private-document-name.pdf"
    with progress_operation(operation_id="ocr-pdf"):
        FakePdfEngine().ocr_pdf(str(private_pdf), str(private_pdf))
        emit_progress("crop_ocr")
    raw = capsys.readouterr().err
    events = progress_lines(raw)
    assert [event["label"] for event in events] == [
        "Rendering PDF pages...",
        "Running OCR page 1/3...",
        "Running OCR page 2/3...",
        "Running OCR page 3/3...",
        "Running crop OCR...",
        "Done",
    ]
    assert str(private_pdf) not in raw
