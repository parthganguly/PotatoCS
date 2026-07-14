"""Issue #16 OCR guardrails, cancellable processes, and atomic routing."""

from __future__ import annotations

import math
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

import odysseus_desktop_backend.services.ocr_service as ocr_module
from odysseus_desktop_backend.cancellation import (
    JobCancelledError,
    cancellation_requested,
    cancellation_scope,
)
from odysseus_desktop_backend.services.document_service import OCRPage
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import LocalHashEmbeddingProvider
from odysseus_desktop_backend.services.job_service import DocumentJobExecutor, JobService
from odysseus_desktop_backend.storage import Database
from odysseus_desktop_backend.services.ocr_service import (
    OCR_CANCEL_POLL_SECONDS,
    OCR_GUARDRAIL_MESSAGES,
    OCR_MAX_PAGES,
    OCR_MAX_RENDER_PIXELS,
    OCR_MIN_RENDER_DPI,
    OCR_PDF_RENDER_DPI,
    OCR_SUBPROCESS_TIMEOUT_SECONDS,
    OCR_TERMINATE_GRACE_SECONDS,
    OCRExecutionError,
    OCRGuardrailError,
    OCRImageResult,
    OCREngineStatus,
    OCRService,
    OCRTimeoutError,
    TesseractPdfEngine,
    choose_render_dpi,
    preflight_pdf_pages,
)


PAGE_COPY = OCR_GUARDRAIL_MESSAGES["ocr_page_too_large"]
COUNT_COPY = OCR_GUARDRAIL_MESSAGES["ocr_too_many_pages"]
PRIVATE_SENTINEL = "HOSTILE_PRIVATE_SENTINEL"


def write_pdf(path: Path, sizes: list[tuple[float, float]]) -> Path:
    writer = PdfWriter()
    for width, height in sizes:
        writer.add_blank_page(width=width, height=height)
    with path.open("wb") as stream:
        writer.write(stream)
    return path


def assert_guardrail(call, code: str = "ocr_page_too_large") -> OCRGuardrailError:
    with pytest.raises(OCRGuardrailError) as raised:
        call()
    assert raised.value.code == code
    assert str(raised.value) == OCR_GUARDRAIL_MESSAGES[code]
    return raised.value


def test_structural_constants_are_exact():
    assert OCR_MAX_RENDER_PIXELS == 40_000_000
    assert OCR_MIN_RENDER_DPI == 120
    assert OCR_MAX_PAGES == 400
    assert OCR_PDF_RENDER_DPI == 400
    assert OCR_SUBPROCESS_TIMEOUT_SECONDS == 60
    assert OCR_CANCEL_POLL_SECONDS == 0.2
    assert OCR_TERMINATE_GRACE_SECONDS == 2.0


def test_ordinary_page_keeps_400_dpi(tmp_path: Path):
    pdf = write_pdf(tmp_path / "ordinary.pdf", [(612, 792)])
    assert preflight_pdf_pages(pdf) == [(1, 400)]


def test_large_page_adaptively_downscales_to_exact_safe_dpi(tmp_path: Path):
    pdf = write_pdf(tmp_path / "large.pdf", [(3600, 3600)])
    plan = preflight_pdf_pages(pdf)
    assert plan == [(1, 126)]
    dpi = plan[0][1]
    pixels = math.ceil(50 * dpi) * math.ceil(50 * dpi)
    assert pixels <= OCR_MAX_RENDER_PIXELS
    assert math.ceil(50 * (dpi + 1)) ** 2 > OCR_MAX_RENDER_PIXELS


def test_page_too_large_even_at_minimum_dpi_is_rejected(tmp_path: Path):
    pdf = write_pdf(tmp_path / "too-large.pdf", [(4320, 4320)])
    assert_guardrail(lambda: preflight_pdf_pages(pdf))


@pytest.mark.parametrize(
    ("width", "height"),
    [
        (float("nan"), 1.0),
        (float("inf"), 1.0),
        (float("-inf"), 1.0),
        (0.0, 1.0),
        (-1.0, 1.0),
        (10_001 / 72, 1.0),
        (1e300, 1e300),
    ],
)
def test_choose_render_dpi_rejects_invalid_or_absurd_dimensions_without_overflow(width, height):
    assert_guardrail(lambda: choose_render_dpi(width, height))


class FakeBox:
    def __init__(self, width=612, height=792):
        self.width = width
        self.height = height


class FakePage:
    def __init__(self, box=FakeBox(), values=None):
        self.mediabox = box
        self.values = values or {}

    def get(self, key, default=None):
        return self.values.get(key, default)


class FakeReader:
    def __init__(self, pages):
        self.pages = pages


@pytest.mark.parametrize(
    "page",
    [
        FakePage(box=None),
        FakePage(FakeBox(float("nan"), 792)),
        FakePage(FakeBox(float("inf"), 792)),
        FakePage(FakeBox(0, 792)),
        FakePage(FakeBox(-1, 792)),
        FakePage(FakeBox(10_001, 792)),
        FakePage(values={"/UserUnit": "garbage"}),
        FakePage(values={"/UserUnit": float("nan")}),
        FakePage(values={"/Rotate": "garbage"}),
        FakePage(values={"/Rotate": float("inf")}),
    ],
)
def test_malformed_metadata_is_always_mapped_to_guardrail(monkeypatch, page):
    monkeypatch.setattr(ocr_module, "PdfReader", lambda _path: FakeReader([page]))
    assert_guardrail(lambda: preflight_pdf_pages("synthetic.pdf"))


@pytest.mark.parametrize("failure", [FileNotFoundError(), PermissionError(), OSError()])
def test_file_level_open_failures_remain_generic_execution_errors(monkeypatch, failure):
    def fail_open(_path):
        raise failure

    monkeypatch.setattr(ocr_module, "PdfReader", fail_open)
    with pytest.raises(OCRExecutionError, match="could not be opened") as raised:
        preflight_pdf_pages("unavailable.pdf")
    assert not isinstance(raised.value, OCRGuardrailError)


def test_page_count_over_limit_rejects_without_partial_plan(tmp_path: Path):
    pdf = write_pdf(tmp_path / "many.pdf", [(72, 72)] * (OCR_MAX_PAGES + 1))
    assert_guardrail(lambda: preflight_pdf_pages(pdf), "ocr_too_many_pages")


class RenderCommandEngine(TesseractPdfEngine):
    def __init__(self):
        super().__init__()
        self.commands: list[list[str]] = []

    def _run_ocr_subprocess(self, command: list[str], label: str):
        self.commands.append(command)
        if label == "pdftoppm":
            output = Path(command[-1]).with_suffix(".png")
        else:
            output = Path(command[command.index("-o") + 1])
        Image.new("L", (20, 20), 255).save(output)
        return subprocess.CompletedProcess(command, 0, "", "")


def test_pdftoppm_renders_exactly_one_planned_page(tmp_path: Path):
    engine = RenderCommandEngine()
    engine.pdftoppm = "pdftoppm"
    image = engine._render_pdf(tmp_path / "input.pdf", tmp_path, "pdftoppm", 7, 233)
    command = engine.commands[0]
    assert command[command.index("-f") + 1] == "7"
    assert command[command.index("-l") + 1] == "7"
    assert command[command.index("-r") + 1] == "233"
    assert image.is_file()


def test_mutool_renders_exactly_one_planned_page(tmp_path: Path):
    engine = RenderCommandEngine()
    engine.mutool = "mutool"
    image = engine._render_pdf(tmp_path / "input.pdf", tmp_path, "mutool", 9, 211)
    command = engine.commands[0]
    assert command[command.index("-r") + 1] == "211"
    assert command[-1] == "9"
    assert image.is_file()


class PageFlowEngine(TesseractPdfEngine):
    def __init__(self, *, fail_page=0, cancel_after_page=0):
        super().__init__()
        self.fail_page = fail_page
        self.cancel_after_page = cancel_after_page
        self.event: threading.Event | None = None
        self.rendered: list[int] = []
        self.page_dirs: list[Path] = []

    def status(self):
        return OCREngineStatus(True, self.name, "fake", "OCR is available.")

    def _render_pdf(self, _pdf, tmp, _renderer, page_number, _render_dpi):
        self.rendered.append(page_number)
        self.page_dirs.append(tmp)
        image = tmp / f"page-{page_number}.png"
        Image.new("L", (100, 100), 255).save(image)
        return image

    def ocr_image(self, image_path, *, source_id="", page_number=None):
        derived = Path(image_path).with_name("derived-private-crop.png")
        Image.new("L", (10, 10), 255).save(derived)
        if page_number == self.fail_page:
            raise OCRExecutionError("synthetic OCR failure")
        if page_number == self.cancel_after_page and self.event is not None:
            self.event.set()
        return OCRImageResult(source_id, self.name, 90.0, "text", 100, 100, 1, {})


def test_huge_page_is_never_rendered_or_allocated(tmp_path: Path):
    pdf = write_pdf(tmp_path / "huge.pdf", [(4320, 4320)])
    engine = PageFlowEngine()
    assert_guardrail(lambda: engine.ocr_pdf(str(pdf), "private-source"))
    assert engine.rendered == []
    assert list(tmp_path.rglob("*.png")) == []


def test_cancel_before_first_render(tmp_path: Path):
    pdf = write_pdf(tmp_path / "two.pdf", [(612, 792), (612, 792)])
    event = threading.Event()
    event.set()
    engine = PageFlowEngine()
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        engine.ocr_pdf(str(pdf), "private-source")
    assert engine.rendered == []


def test_cancel_between_pages_and_per_page_files_are_deleted(tmp_path: Path):
    pdf = write_pdf(tmp_path / "two.pdf", [(612, 792), (612, 792)])
    event = threading.Event()
    engine = PageFlowEngine(cancel_after_page=1)
    engine.event = event
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        engine.ocr_pdf(str(pdf), "private-source")
    assert engine.rendered == [1]
    assert all(not directory.exists() for directory in engine.page_dirs)


@pytest.mark.parametrize("fail_page", [0, 2])
def test_per_page_temp_files_deleted_after_success_and_failure(tmp_path: Path, fail_page: int):
    pdf = write_pdf(tmp_path / "two.pdf", [(612, 792), (612, 792)])
    engine = PageFlowEngine(fail_page=fail_page)
    if fail_page:
        with pytest.raises(OCRExecutionError):
            engine.ocr_pdf(str(pdf), "private-source")
    else:
        pages = engine.ocr_pdf(str(pdf), "private-source")
        assert [page.metadata["render_dpi"] for page in pages] == [400, 400]
    assert all(not directory.exists() for directory in engine.page_dirs)


def test_rendered_output_dimension_backstop_rejects_without_decode(tmp_path: Path):
    image = tmp_path / "oversized.png"
    Image.new("1", (7000, 7000)).save(image)
    assert_guardrail(lambda: TesseractPdfEngine()._verify_rendered_image(image))


def test_cancel_during_tesseract_passes():
    event = threading.Event()

    class PassEngine(TesseractPdfEngine):
        def _preprocess_variants(self, image):
            return [("rendered", image, (1, 2, 3))]

        def _run_tesseract_tsv(self, image, psm):
            event.set()
            return "text", 90.0, {"lines": []}

    with cancellation_scope(event), pytest.raises(JobCancelledError):
        PassEngine()._run_tesseract(Path("page.png"))


class LongRunningProcess:
    def __init__(self, event: threading.Event | None = None, *, force_kill=False):
        self.event = event
        self.force_kill = force_kill
        self.returncode = None
        self.terminated = 0
        self.killed = 0
        self.waits: list[float | None] = []
        self.communicates = 0

    def poll(self):
        return self.returncode

    def communicate(self, timeout=None):
        self.communicates += 1
        if self.event is not None:
            self.event.set()
        raise subprocess.TimeoutExpired("synthetic", timeout)

    def terminate(self):
        self.terminated += 1

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if self.force_kill and not self.killed:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.returncode = -9 if self.killed else -15
        return self.returncode

    def kill(self):
        self.killed += 1


def test_cancel_during_renderer_terminates_kills_and_reaps_exact_handle(monkeypatch):
    event = threading.Event()
    handle = LongRunningProcess(event, force_kill=True)
    monkeypatch.setattr(ocr_module.subprocess, "Popen", lambda *_a, **_k: handle)
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        TesseractPdfEngine()._run_ocr_subprocess(["renderer", "private-input"], "renderer")
    assert handle.terminated == 1
    assert handle.killed == 1
    assert handle.waits == [OCR_TERMINATE_GRACE_SECONDS, None]


def test_timeout_terminates_and_reaps_handle(monkeypatch):
    handle = LongRunningProcess()
    monkeypatch.setattr(ocr_module.subprocess, "Popen", lambda *_a, **_k: handle)
    monkeypatch.setattr(ocr_module, "OCR_SUBPROCESS_TIMEOUT_SECONDS", 0)
    with pytest.raises(OCRTimeoutError, match="timed out after 0s"):
        TesseractPdfEngine()._run_ocr_subprocess(["renderer"], "renderer")
    assert handle.terminated == 1
    assert handle.killed == 0
    assert handle.waits == [OCR_TERMINATE_GRACE_SECONDS]


def test_runner_preserves_none_safe_utf8_capture(monkeypatch):
    class Finished:
        returncode = 0

        def poll(self):
            return 0

        def communicate(self, timeout=None):
            return None, None

    monkeypatch.setattr(ocr_module.subprocess, "Popen", lambda *_a, **_k: Finished())
    result = TesseractPdfEngine()._run_ocr_subprocess(["tesseract"], "Tesseract")
    assert result.stdout is None and result.stderr is None


class GuardrailEngine:
    name = "synthetic-guardrail"

    def status(self):
        return OCREngineStatus(True, self.name, "synthetic", "OCR is available.")

    def ocr_pdf(self, _stored_path, _source_path):
        raise OCRGuardrailError("ocr_page_too_large")


class BlockingEngine(GuardrailEngine):
    def __init__(self, entered: threading.Event):
        self.entered = entered

    def ocr_pdf(self, _stored_path, _source_path):
        from odysseus_desktop_backend.cancellation import check_cancelled

        self.entered.set()
        while True:
            check_cancelled()
            time.sleep(0.005)


def wait_terminal(service: JobService, job_id: str, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = service.get(job_id)
        if snapshot["state"] in {"completed", "cancelled", "failed"}:
            return snapshot
        time.sleep(0.005)
    raise AssertionError("job did not reach a terminal state")


def executor_factory(profile: Path, engine, configure=None):
    def build():
        executor = DocumentJobExecutor(profile)
        executor.services.embeddings.forced_provider = LocalHashEmbeddingProvider()
        executor.services.ocr.engine = engine
        if configure is not None:
            configure(executor)
        return executor

    return build


def test_explicit_guardrail_job_fails_with_code_and_restores_prior_status(tmp_path: Path):
    profile = tmp_path / "profile"
    pdf = write_pdf(tmp_path / f"{PRIVATE_SENTINEL}.pdf", [(612, 792)])
    seed_db = Database(profile)
    documents = DocumentService(seed_db)
    document = documents.import_document(str(pdf))
    prior = {
        key: document[key]
        for key in ("status", "index_status", "ocr_status", "ocr_engine", "ocr_error", "error")
    }
    seed_db.close()

    jobs = JobService(profile, executor_factory(profile, GuardrailEngine()))
    try:
        submitted = jobs.submit_ocr(document["id"])
        final = wait_terminal(jobs, submitted["job_id"])
        assert final["state"] == "failed"
        assert final["message_code"] == "ocr_page_too_large"
        rendered_snapshot = repr(final)
        assert PRIVATE_SENTINEL not in rendered_snapshot
        assert PAGE_COPY not in rendered_snapshot
    finally:
        jobs.shutdown()

    check_db = Database(profile)
    try:
        restored = DocumentService(check_db).get(document["id"])
        assert {key: restored[key] for key in prior} == prior
    finally:
        check_db.close()


def test_import_guardrail_completes_low_text_with_only_fixed_copy(tmp_path: Path):
    profile = tmp_path / "profile"
    pdf = write_pdf(tmp_path / f"{PRIVATE_SENTINEL}.pdf", [(612, 792)])
    jobs = JobService(profile, executor_factory(profile, GuardrailEngine()))
    try:
        submitted = jobs.submit_import([str(pdf)])[0]
        final = wait_terminal(jobs, submitted["job_id"])
        assert final["state"] == "completed"
        assert final["message_code"] == ""
        assert PRIVATE_SENTINEL not in repr(final)
    finally:
        jobs.shutdown()

    check_db = Database(profile)
    try:
        documents = DocumentService(check_db).list()
        assert len(documents) == 1
        document = documents[0]
        assert document["index_status"] == "low_text"
        assert document["ocr_status"] == "unavailable"
        assert document["ocr_error"] == PAGE_COPY
        assert PRIVATE_SENTINEL not in document["ocr_error"]
        assert str(tmp_path) not in document["ocr_error"]
    finally:
        check_db.close()


def test_ocr_heavy_staged_import_stays_invisible_then_cancel_purges_rows_and_copy(tmp_path: Path):
    from odysseus_desktop_backend.services.artifact_service import ArtifactService
    from odysseus_desktop_backend.services.embedding_service import EmbeddingService
    from odysseus_desktop_backend.services.rag_service import RAGService
    from odysseus_desktop_backend.services.source_service import SourceService
    from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
    profile = tmp_path / "profile"
    pdf = write_pdf(tmp_path / "blocking.pdf", [(612, 792)])
    entered = threading.Event()
    jobs = JobService(profile, executor_factory(profile, BlockingEngine(entered)))
    try:
        submitted = jobs.submit_import([str(pdf)])[0]
        assert entered.wait(3)
        observer = Database(profile)
        try:
            documents = DocumentService(observer)
            embeddings = EmbeddingService(observer, provider=LocalHashEmbeddingProvider())
            rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(observer))
            sources = SourceService(documents, ArtifactService(observer, documents, rag), rag)
            assert documents.list() == []
            assert sources.list(scope=None, include_session=True) == []
            assert rag.search("anything") == []
            staged = observer.conn.execute(
                "SELECT stored_path FROM documents WHERE is_staging = 1"
            ).fetchone()
            assert staged is not None
            copied_path = Path(staged["stored_path"])
            assert copied_path.exists()
        finally:
            observer.close()

        cancelling = jobs.cancel(submitted["job_id"])
        assert cancelling["state"] == "cancel_requested"
        final = wait_terminal(jobs, submitted["job_id"])
        assert final["state"] == "cancelled"
        assert final["message_code"] == "cancelled_by_user"
        assert final["message_code"] != "job_failed"
        assert not copied_path.exists()
        verify = Database(profile)
        try:
            assert verify.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        finally:
            verify.close()
        assert pdf.exists()
    finally:
        jobs.shutdown()


def test_cancel_at_commit_staging_boundary_resolves_completed_and_visible(tmp_path: Path):
    profile = tmp_path / "profile"
    fixture = tmp_path / "commit-boundary.txt"
    fixture.write_text("commit boundary remains visible after late cancellation", encoding="utf-8")
    entered = threading.Event()
    release = threading.Event()

    def configure(executor):
        original = executor.services.documents.commit_staging

        def gated(document_id):
            entered.set()
            assert release.wait(3)
            return original(document_id)

        executor.services.documents.commit_staging = gated

    jobs = JobService(
        profile,
        executor_factory(profile, GuardrailEngine(), configure=configure),
    )
    try:
        submitted = jobs.submit_import([str(fixture)])[0]
        assert entered.wait(3)
        assert jobs.cancel(submitted["job_id"])["state"] == "cancel_requested"
        release.set()
        final = wait_terminal(jobs, submitted["job_id"])
        assert final["state"] == "completed"
        assert final["message_code"] == ""
    finally:
        release.set()
        jobs.shutdown()

    db = Database(profile)
    try:
        visible = DocumentService(db).list()
        assert len(visible) == 1
        assert visible[0]["is_staging"] is False
    finally:
        db.close()


class ShieldDocuments:
    def __init__(self):
        self.calls: list[str] = []
        self.document = {"id": "doc", "file_type": "pdf", "stored_path": "stored", "source_path": "source"}
        self.pages: list[dict[str, object]] = []

    def get(self, _document_id):
        return dict(self.document)

    def mark_ocr_running(self, *_args):
        self.calls.append("running")

    def mark_ocr_ready(self, *_args):
        self.calls.append("ready")

    def replace_pages_from_ocr(self, *_args):
        assert cancellation_requested() is False
        self.calls.append("replace_pages")

    def mark_ocr_indexed(self, *_args):
        assert cancellation_requested() is False
        self.calls.append("indexed")

    def link_ocr_chunks(self, *_args):
        assert cancellation_requested() is False
        self.calls.append("link")

    def replace_ocr_pages(self, _document_id, pages, index_status="pending"):
        assert cancellation_requested() is False
        self.calls.append("replace_no_text")
        self.pages = [{"text": page.text} for page in pages]

    def mark_ocr_no_text(self, *_args):
        assert cancellation_requested() is False
        self.calls.append("no_text")

    def ocr_pages(self, _document_id):
        return list(self.pages)


class ShieldRag:
    def __init__(self):
        self.calls: list[str] = []

    def index_document(self, _document_id):
        assert cancellation_requested() is False
        self.calls.append("rag")
        return {"chunks": [], "embedded": 0, "cached": 0}


class CancellingResultEngine:
    name = "synthetic"

    def __init__(self, event: threading.Event, text: str):
        self.event = event
        self.text = text

    def status(self):
        return OCREngineStatus(True, self.name, "synthetic", "OCR is available.")

    def ocr_pdf(self, _stored, source):
        self.event.set()
        return [OCRPage(source, 1, self.name, 90.0, self.text)]


def test_success_commit_sequence_is_shielded_from_late_cancel():
    event = threading.Event()
    documents = ShieldDocuments()
    rag = ShieldRag()
    service = OCRService(documents, rag, CancellingResultEngine(event, "useful OCR text"))
    with cancellation_scope(event):
        result = service.run_document_ocr("doc")
    assert result["index"] is not None
    assert documents.calls == ["running", "ready", "replace_pages", "indexed", "link"]
    assert rag.calls == ["rag"]


def test_no_text_terminal_writes_are_shielded_from_late_cancel():
    event = threading.Event()
    documents = ShieldDocuments()
    service = OCRService(documents, ShieldRag(), CancellingResultEngine(event, ""))
    with cancellation_scope(event):
        result = service.run_document_ocr("doc")
    assert result["index"] is None
    assert documents.calls == ["running", "replace_no_text", "no_text"]
