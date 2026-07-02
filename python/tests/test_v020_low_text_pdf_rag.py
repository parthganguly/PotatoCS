from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from odysseus_desktop_backend.services.artifact_service import ArtifactService
from odysseus_desktop_backend.services.chat_service import ChatService, PLAIN_CHAT_SYSTEM_PROMPT
from odysseus_desktop_backend.services.document_service import DocumentService, OCRPage
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.ocr_service import OCREngineStatus, OCRService, TesseractPdfEngine, score_ocr_quality
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.source_service import SourceService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database
from rpc_server import SidecarApp


class RebeccaPdfOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=94.0,
                text=(
                    "Mean Vicky Homework. In Warsaw, Rebecca visits the zoo, "
                    "writes about a desk, and asks why Vicky said reactionarily mean things."
                ),
                metadata={"renderer": "mock-renderer"},
            )
        ]


class MixedPdfOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=80.0,
                text="OCR fallback text that should not replace the selectable first page.",
            ),
            OCRPage(
                source_path=source_path,
                page_number=2,
                engine_name=self.name,
                confidence=91.0,
                text="The image-only second page says Rebecca checked a desk near the Warsaw zoo.",
            ),
        ]


class NoTextPdfOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=None,
                text="",
            )
        ]


class NoisyRebeccaPdfOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        lines = [
            "Reactionarily mean rows",
            "Unrelated Jenny in Kaliningrad",
            "In Warsaw with Rebecca",
            "said horrible things about her relationship and father",
            "Learning: let it go, be patient, and do not take the bait",
            "Another unrelated desk row",
            "Rebeca at the ZOO",
            "left her at the zoo over something she said",
            "Learning: should have left the cabin and cooled down",
        ]
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=62.0,
                text="\n".join(lines),
                metadata={
                    "renderer": "mock-renderer",
                    "render_dpi": 400,
                    "preprocessing_version": "test-noisy-ocr",
                    "lines": [
                        {
                            "line_number": index,
                            "text": text,
                            "confidence": 62.0,
                            "word_boxes": [],
                        }
                        for index, text in enumerate(lines, start=1)
                    ],
                },
            )
        ]


class PoorGibberishPdfOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        text = "Tttwas Wrongyl liever# ai Classi Soi <a ||| 7 r sothetning she X G 3 t"
        quality = score_ocr_quality(text, 13.0)
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=13.0,
                text=text,
                metadata={
                    "renderer": "mock-renderer",
                    "ocr_quality": quality,
                    "quality": quality,
                    "ocr_attempt_count": 1,
                    "ocr_crop_count": 0,
                    "selected_ocr_attempt": {"source_type": "ocr_text", "quality": quality},
                    "lines": [{"line_number": 1, "text": text, "confidence": 13.0, "word_boxes": []}],
                },
            )
        ]


class CropEvidencePdfOcrEngine:
    name = "mock-ocr"

    def status(self):
        return OCREngineStatus(True, self.name, "mock-renderer", "OCR is available.")

    def ocr_pdf(self, _stored_path: str, source_path: str):
        text = "In Warsaw with Rebecca\nRebecca at the Zoo\nleft her at the zoo over something she said"
        quality = score_ocr_quality(text, 71.0)
        crop = {
            "name": "lower_table",
            "x": 0,
            "y": 420,
            "width": 1200,
            "height": 520,
            "normalized": [0.0, 0.42, 1.0, 0.94],
        }
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=71.0,
                text=text,
                metadata={
                    "renderer": "mock-renderer",
                    "ocr_quality": quality,
                    "quality": quality,
                    "ocr_attempt_count": 6,
                    "ocr_crop_count": 1,
                    "selected_ocr_attempt": {"source_type": "ocr_crop", "crop": crop, "quality": quality},
                    "crop_evidence": [{"name": "lower_table", "coordinates": crop, "quality": quality, "text_char_count": len(text)}],
                    "lines": [
                        {"line_number": index, "text": line, "confidence": 71.0, "word_boxes": [], "crop": crop, "source_type": "ocr_crop"}
                        for index, line in enumerate(text.splitlines(), start=1)
                    ],
                },
            )
        ]


class FakeVLMTextExtractor:
    def __init__(self, text: str = ""):
        self.text = text
        self.calls = 0

    def available(self):
        return {"available": bool(self.text), "backend": "fake_vlm", "model": "fake-vlm"}

    def transcribe_page(self, _image_path: str, *, page_number=None, crop=None):
        self.calls += 1
        quality = score_ocr_quality(self.text, None)
        return {
            "attempted": True,
            "available": bool(self.text),
            "backend": "fake_vlm",
            "model": "fake-vlm",
            "text": self.text,
            "page": page_number,
            "crop": crop or {},
            "quality": quality,
            "text_char_count": len(self.text),
        }


class VLMAssistedPdfOcrEngine(PoorGibberishPdfOcrEngine):
    def __init__(self):
        self.vlm_text_extractor = None

    def ocr_pdf(self, _stored_path: str, source_path: str):
        bad_text = "Tttwas Wrongyl liever# ai Classi Soi <a ||| 7"
        vlm = (
            self.vlm_text_extractor.transcribe_page("fake-page.png", page_number=1)
            if self.vlm_text_extractor is not None
            else {"attempted": False, "available": False, "text": "", "backend": "", "model": ""}
        )
        text = str(vlm.get("text") or bad_text)
        quality = score_ocr_quality(text, 18.0)
        return [
            OCRPage(
                source_path=source_path,
                page_number=1,
                engine_name=self.name,
                confidence=18.0,
                text=text,
                metadata={
                    "renderer": "mock-renderer",
                    "ocr_quality": quality,
                    "quality": quality,
                    "ocr_attempt_count": 2,
                    "ocr_crop_count": 0,
                    "selected_ocr_attempt": {"source_type": "vlm_assisted_text", "quality": quality},
                    "vlm_text_evidence": {key: value for key, value in vlm.items() if key != "text"},
                    "vlm_text_available": bool(vlm.get("text")),
                    "vlm_text_char_count": len(str(vlm.get("text") or "")),
                    "lines": [
                        {"line_number": index, "text": line, "confidence": None, "word_boxes": [], "source_type": "vlm_assisted_text"}
                        for index, line in enumerate(text.splitlines(), start=1)
                    ],
                },
            )
        ]


class EvidenceAwareModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[dict] = []

    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        **_kwargs,
    ) -> dict:
        self.calls.append({"model": model, "messages": messages})
        joined = "\n".join(str(message.get("content") or "") for message in messages)
        if "Fuzzy OCR matches: Rebeca ~= Rebecca" in joined:
            content = (
                "Rebecca appears on page 1 in a Warsaw entry and a zoo entry. "
                "The OCR suggests the Warsaw entry says horrible things were said about her relationship and father, "
                "with a learning to let it go, be patient, and not take the bait. "
                "The zoo entry says she was left at the zoo over something she said, and the learning was to leave the cabin and cool down [S1]."
            )
        elif "Rebecca visits the zoo" in joined or "Rebecca checked a desk" in joined:
            content = "Rebecca is described in the document evidence as connected to Warsaw, a zoo, and a desk [S1]."
        elif "Mars mission planning requires rover batteries" in joined:
            content = "Mars mission planning requires rover batteries, sample caches, and relay orbiters [S1]."
        else:
            content = "I do not have document evidence to answer from."
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "stop",
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

    def chat(self, model: str, messages: list[dict[str, str]], options=None) -> str:
        return self.chat_detailed(model, messages)["content"]


class OcrIgnoringModelService(EvidenceAwareModelService):
    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        **_kwargs,
    ) -> dict:
        result = super().chat_detailed(model, messages, **_kwargs)
        result["content"] = (
            "There is no direct mention of Rebecca and Warsaw in the snippets. "
            "Rebecca is at the Zoo [S1]. Kaliningrad is mentioned, which is a city in Poland near Warsaw."
        )
        return result


class WarsawAbsenceModelService(EvidenceAwareModelService):
    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        **_kwargs,
    ) -> dict:
        result = super().chat_detailed(model, messages, **_kwargs)
        result["content"] = (
            "There is no direct evidence of an event involving Rebecca in Warsaw. "
            "The snippets mention Rebecca at the Zoo, but there is no information about what happened in Warsaw. "
            "There is no clear indication of what happened in Warsaw with Rebecca. "
            "There are no direct quotes or evidence from the retrieved snippets that specifically mention Warsaw and Rebecca together."
        )
        return result


class ZooMisreadModelService(EvidenceAwareModelService):
    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        **_kwargs,
    ) -> dict:
        result = super().chat_detailed(model, messages, **_kwargs)
        result["content"] = "The snippets suggest Rebecca left something at the zoo."
        return result


def build_stack(profile_dir: Path, engine=None, model_cls=EvidenceAwareModelService):
    db = Database(profile_dir)
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
    ocr = OCRService(documents, rag, engine=engine or RebeccaPdfOcrEngine())
    artifacts = ArtifactService(db, documents, rag)
    sources = SourceService(documents, artifacts, rag, ocr=ocr)
    settings = SettingsService(db)
    sessions = SessionService(db)
    models = model_cls(db)
    chat = ChatService(
        sessions,
        settings,
        models,
        rag=rag,
        artifacts=artifacts,
        documents=documents,
        sources=sources,
        ocr=ocr,
    )
    return db, documents, rag, ocr, sources, models, chat


def write_image_only_pdf(path: Path, *, text: str = "Rebecca at the Warsaw zoo") -> Path:
    image_path = path.with_suffix(".png")
    image = Image.new("RGB", (900, 500), "white")
    draw = ImageDraw.Draw(image)
    draw.text((40, 80), text, fill="black")
    image.save(image_path, format="PNG")

    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawImage(ImageReader(str(image_path)), 72, 360, width=420, height=240)
    pdf.save()
    return path


def write_text_pdf(path: Path) -> Path:
    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawString(
        72,
        720,
        "This selectable PDF text explains sample labels, bottle handling, form checks, and reporting duties.",
    )
    pdf.drawString(
        72,
        700,
        "It has enough embedded text to be indexed without OCR and should not be marked low text.",
    )
    pdf.save()
    return path


def write_mixed_pdf(path: Path) -> Path:
    image_path = path.with_suffix(".png")
    image = Image.new("RGB", (900, 500), "white")
    ImageDraw.Draw(image).text((40, 80), "Rebecca checked a desk near the Warsaw zoo.", fill="black")
    image.save(image_path, format="PNG")

    pdf = canvas.Canvas(str(path), pagesize=letter)
    pdf.drawString(
        72,
        720,
        "Selectable first page text about a classroom policy, homework review, and archive notes.",
    )
    pdf.drawString(72, 700, "This text must remain after OCR processes the image-only second page.")
    pdf.showPage()
    pdf.drawImage(ImageReader(str(image_path)), 72, 360, width=420, height=240)
    pdf.save()
    return path


def test_image_only_pdf_direct_attachment_uses_ocr_page_evidence(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, models, chat = build_stack(tmp_path / "profile")
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send("Tell me about Rebecca?", attachment_document_ids=[document["id"]])

    assert result["retrieved_snippets"]
    assert "Rebecca is described" in result["assistant_message"]["content"]
    assert result["document_evidence"][0]["evidence_action"] == "ocr_attempted"
    assert result["document_evidence"][0]["ocr_status"] == "indexed"
    assert result["document_evidence"][0]["session_attachment_evidence_used"] is True
    assert result["document_evidence"][0]["model_received_document_evidence"] is True
    system_prompt = models.calls[0]["messages"][0]["content"]
    assert "Rebecca is mentioned in the attached document" in system_prompt
    assert "document-local entities" in system_prompt
    assert "Rebecca visits the zoo" in system_prompt
    assert "Warsaw" in system_prompt
    db.close()


def test_image_only_pdf_source_import_indexes_ocr_chunks(tmp_path: Path):
    db, _documents, rag, _ocr, sources, _models, _chat = build_stack(tmp_path / "profile")
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")

    result = sources.import_path(str(pdf), scope="library", index=True)

    document = result["document"]
    assert document["ocr_status"] == "indexed"
    assert result["index"]["chunks"]
    assert result["source"]["ocr_text_char_count"] > 0
    assert "OCR extracted text from page images" in result["source"]["warning"]
    hits = rag.search("Rebecca Warsaw zoo", document_ids=[document["id"]])
    assert hits
    assert hits[0]["page_start"] == 1
    db.close()


def test_noisy_ocr_name_lookup_retrieves_exact_and_fuzzy_rows_without_semantic_hits(tmp_path: Path):
    db, documents, rag, ocr, _sources, _models, _chat = build_stack(
        tmp_path / "profile",
        engine=NoisyRebeccaPdfOcrEngine(),
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")
    ocr.ensure_document_ocr_indexed(document["id"])

    rag.vector_store.similarity_search = lambda *_args, **_kwargs: []
    context, chunks, snippets = rag.build_quote_context(
        "Tell me about Rebecca in this document.",
        document_ids=[document["id"]],
    )

    assert chunks
    assert snippets
    joined = "\n".join(snippet["text"] for snippet in snippets)
    assert "In Warsaw with Rebecca" in joined
    assert "Rebeca at the ZOO" in joined
    assert "left her at the zoo over something she said" in joined
    assert snippets[0]["page_start"] == 1
    assert snippets[0]["ocr_exact_matches"] == ["Rebecca"]
    assert any(match["matched_text"] == "Rebeca" for match in snippets[0]["ocr_fuzzy_matches"])
    assert snippets[0]["ocr_line_windows"]
    assert "Fuzzy OCR matches: Rebeca ~= Rebecca" in context
    assert "Warsaw-like row" in context
    assert "Do not say there is no mention" in context
    db.close()


def test_noisy_ocr_chat_prompt_uses_fuzzy_notice_and_answer_avoids_no_mention(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, models, chat = build_stack(
        tmp_path / "profile",
        engine=NoisyRebeccaPdfOcrEngine(),
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send(
        "Tell me about Rebecca in this document.",
        attachment_document_ids=[document["id"]],
    )

    answer = result["assistant_message"]["content"]
    assert "no direct mention" not in answer.lower()
    assert "Warsaw entry" in answer
    assert "zoo entry" in answer
    evidence = result["document_evidence"][0]
    assert evidence["retrieved_pages"] == [1]
    assert evidence["ocr_exact_matches"] == ["Rebecca"]
    assert any(match["matched_text"] == "Rebeca" for match in evidence["ocr_fuzzy_matches"])
    assert evidence["ocr_line_windows"]
    system_prompt = models.calls[0]["messages"][0]["content"]
    assert "OCR text from an image-based PDF" in system_prompt
    assert "Rebeca ~= Rebecca" in system_prompt
    assert "Do not say there is no mention" in system_prompt
    db.close()


def test_noisy_ocr_answer_guard_restores_page_and_warsaw_summary(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, _models, chat = build_stack(
        tmp_path / "profile",
        engine=NoisyRebeccaPdfOcrEngine(),
        model_cls=OcrIgnoringModelService,
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send(
        "Tell me about Rebecca in this document.",
        attachment_document_ids=[document["id"]],
    )

    answer = result["assistant_message"]["content"]
    assert "no direct mention" not in answer.lower()
    assert "page 1" in answer
    assert "Warsaw entry involving Rebecca" in answer
    assert "Zoo" in answer
    assert "city in Poland" not in answer
    assert result["document_evidence"][0]["ocr_context_notes"]
    db.close()


def test_noisy_ocr_warsaw_guard_uses_relationship_learning_clues(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, _models, chat = build_stack(
        tmp_path / "profile",
        engine=NoisyRebeccaPdfOcrEngine(),
        model_cls=WarsawAbsenceModelService,
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send(
        "What happened in Warsaw with Rebecca?",
        attachment_document_ids=[document["id"]],
    )

    answer = result["assistant_message"]["content"]
    assert "no direct evidence of an event involving Rebecca in Warsaw" not in answer
    assert "no information about what happened in Warsaw" not in answer
    assert "no clear indication of what happened in Warsaw" not in answer
    assert "no direct quotes or evidence" not in answer
    assert "page 1" in answer
    assert "Warsaw entry involving Rebecca" in answer
    assert "relationship" in answer
    assert "patient/not take the bait" in answer
    assert result["document_evidence"][0]["ocr_context_notes"]
    db.close()


def test_noisy_ocr_zoo_guard_restores_left_her_row(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, _models, chat = build_stack(
        tmp_path / "profile",
        engine=NoisyRebeccaPdfOcrEngine(),
        model_cls=ZooMisreadModelService,
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send(
        "What happened with Rebecca at the Zoo?",
        attachment_document_ids=[document["id"]],
    )

    answer = result["assistant_message"]["content"]
    assert "left at the zoo over something she said" in answer
    assert "cabin" in answer
    assert result["document_evidence"][0]["ocr_context_notes"]
    db.close()


def test_ocr_quality_scoring_classifies_gibberish_and_usable_text():
    poor = score_ocr_quality("Tttwas Wrongyl liever# ai Classi Soi <a ||| 7 r sothetning she X G 3 t", 11.0)
    usable = score_ocr_quality(
        "In Warsaw with Rebecca\nsaid horrible things about her relationship and father\n"
        "Rebecca at the Zoo\nleft her at the zoo over something she said",
        68.0,
    )

    assert poor["label"] == "poor"
    assert usable["label"] in {"usable_noisy", "good"}
    assert float(usable["score"]) > float(poor["score"])


def test_inverted_threshold_preprocessing_variant_is_available_for_white_on_dark(tmp_path: Path):
    image = Image.new("RGB", (360, 160), "black")
    draw = ImageDraw.Draw(image)
    draw.text((24, 60), "Rebecca at the Zoo", fill="white")
    path = tmp_path / "white-on-dark.png"
    image.save(path)

    engine = TesseractPdfEngine()
    variants = engine._preprocess_variants(path)
    names = [name for name, _variant_path, _psm in variants]

    assert "inverted_threshold" in names
    assert any(variant_path.exists() for name, variant_path, _psm in variants if name == "inverted_threshold")


def test_crop_ocr_evidence_is_preserved_with_coordinates(tmp_path: Path):
    db, documents, rag, ocr, _sources, _models, _chat = build_stack(
        tmp_path / "profile",
        engine=CropEvidencePdfOcrEngine(),
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")
    ocr.ensure_document_ocr_indexed(document["id"])

    diagnostics = documents.text_diagnostics(document["id"])
    assert diagnostics["ocr_quality"] in {"usable_noisy", "good"}
    assert diagnostics["ocr_crop_count"] == 1
    pages = documents.ocr_pages(document["id"])
    crop_evidence = pages[0]["metadata"]["crop_evidence"][0]
    assert crop_evidence["coordinates"]["name"] == "lower_table"
    context, _chunks, snippets = rag.build_quote_context("Tell me about Rebecca", document_ids=[document["id"]])
    assert "OCR quality:" in context
    assert "Crop OCR evidence" in context
    assert snippets[0]["crop_evidence"][0]["name"] == "lower_table"
    db.close()


def test_vlm_assisted_text_evidence_is_labeled_and_retrievable(tmp_path: Path):
    engine = VLMAssistedPdfOcrEngine()
    db, documents, rag, ocr, _sources, _models, chat = build_stack(
        tmp_path / "profile",
        engine=engine,
    )
    fake_vlm = FakeVLMTextExtractor(
        "In Warsaw with Rebecca\nRebecca at the Zoo\nleft her at the zoo over something she said"
    )
    ocr.set_vlm_text_extractor(fake_vlm)
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send("Tell me about Rebecca in this document.", attachment_document_ids=[document["id"]])

    assert fake_vlm.calls == 1
    assert result["document_evidence"][0]["vlm_text_available"] is True
    assert result["document_evidence"][0]["vlm_text_backends"] == ["fake_vlm"]
    context, _chunks, snippets = rag.build_quote_context("Rebecca Warsaw zoo", document_ids=[document["id"]])
    assert "VLM-assisted text extraction: available via fake_vlm fake-vlm" in context
    assert snippets[0]["vlm_text_available"] is True
    assert "no direct mention" not in result["assistant_message"]["content"].lower()
    db.close()


def test_poor_ocr_without_local_vlm_returns_graceful_limitation(tmp_path: Path):
    db, documents, _rag, ocr, _sources, _models, chat = build_stack(
        tmp_path / "profile",
        engine=PoorGibberishPdfOcrEngine(),
    )
    pdf = write_image_only_pdf(tmp_path / "Mean Vicky Homework.pdf")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send("Tell me about Rebecca in this document.", attachment_document_ids=[document["id"]])

    evidence = result["document_evidence"][0]
    assert evidence["ocr_quality"] == "poor"
    assert evidence["vlm_text_available"] is False
    assert "OCR quality is too poor to read this reliably" in result["assistant_message"]["content"]
    assert ocr.engine.status().available is True
    db.close()


def test_low_text_detection_and_text_pdf_not_low_text(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, _models, _chat = build_stack(tmp_path / "profile")
    image_pdf = write_image_only_pdf(tmp_path / "scan.pdf")
    text_pdf = write_text_pdf(tmp_path / "text.pdf")

    scanned = documents.import_document(str(image_pdf))
    embedded = documents.import_document(str(text_pdf))

    assert scanned["is_low_text"] is True
    assert scanned["ocr_status"] == "needed"
    assert documents.needs_ocr(scanned["id"]) is True
    assert embedded["is_low_text"] is False
    assert documents.needs_ocr(embedded["id"]) is False
    db.close()


def test_mixed_pdf_preserves_selectable_text_and_adds_ocr_page(tmp_path: Path):
    db, documents, rag, ocr, _sources, _models, _chat = build_stack(tmp_path / "profile", engine=MixedPdfOcrEngine())
    pdf = write_mixed_pdf(tmp_path / "mixed.pdf")
    document = documents.import_document(str(pdf))

    result = ocr.ensure_document_ocr_indexed(document["id"])
    pages = documents.pages(document["id"])

    assert result["index"]["chunks"]
    assert pages[0]["extraction_method"] == "pdf_text"
    assert "Selectable first page text" in pages[0]["text"]
    assert pages[1]["extraction_method"] == "ocr:mock-ocr"
    assert "Rebecca checked a desk" in pages[1]["text"]
    assert rag.search("classroom policy", document_ids=[document["id"]])
    assert rag.search("Rebecca desk Warsaw", document_ids=[document["id"]])
    db.close()


def test_ocr_no_text_direct_attachment_returns_honest_limitation(tmp_path: Path):
    db, documents, _rag, _ocr, _sources, models, chat = build_stack(tmp_path / "profile", engine=NoTextPdfOcrEngine())
    pdf = write_image_only_pdf(tmp_path / "blank-scan.pdf", text="")
    document = documents.import_document(str(pdf), scope="session")

    result = chat.send("Tell me about Rebecca?", attachment_document_ids=[document["id"]])

    assert result["retrieved_snippets"] == []
    assert result["assistant_message"]["content"] == "I could not extract readable text from this image-based PDF yet."
    assert result["model_response"]["done_reason"] == "local_context_guard"
    assert models.calls == []
    assert result["document_evidence"][0]["ocr_status"] == "no_text"
    assert result["document_evidence"][0]["model_received_document_evidence"] is False
    db.close()


def test_stale_no_text_attachment_retries_and_indexes_when_ocr_improves(tmp_path: Path):
    db, documents, _rag, ocr, _sources, models, chat = build_stack(tmp_path / "profile", engine=NoTextPdfOcrEngine())
    pdf = write_image_only_pdf(tmp_path / "retry-scan.pdf")
    document = documents.import_document(str(pdf), scope="session")

    first = chat.send("Tell me about Rebecca?", attachment_document_ids=[document["id"]])
    assert first["document_evidence"][0]["ocr_status"] == "no_text"
    assert models.calls == []

    ocr.engine = RebeccaPdfOcrEngine()
    second = chat.send("Tell me about Rebecca?", attachment_document_ids=[document["id"]])

    assert second["retrieved_snippets"]
    assert second["document_evidence"][0]["before"]["ocr_status"] == "no_text"
    assert second["document_evidence"][0]["ocr_status"] == "indexed"
    assert second["document_evidence"][0]["model_received_document_evidence"] is True
    assert "Rebecca is described" in second["assistant_message"]["content"]
    db.close()


def test_rpc_document_import_and_reindex_use_ocr_fallback(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        app.ocr.engine = RebeccaPdfOcrEngine()
        pdf = write_image_only_pdf(tmp_path / "rpc-scan.pdf")

        imported = app.dispatch("documents.import", {"path": str(pdf), "scope": "session"})
        document_id = imported["document"]["id"]

        assert imported["document"]["ocr_status"] == "indexed"
        assert imported["index"]["chunks"]
        results = app.dispatch("rag.search", {"query": "Rebecca Warsaw zoo", "document_ids": [document_id]})
        assert results

        reindexed = app.dispatch("documents.reindex", {"document_id": document_id})
        assert reindexed["document"]["ocr_status"] == "indexed"
        assert reindexed["chunks"]
    finally:
        app.close()


def test_no_document_evidence_does_not_inject_rag_context(tmp_path: Path):
    db, _documents, _rag, _ocr, _sources, models, chat = build_stack(tmp_path / "profile")

    result = chat.send("Tell me about Rebecca?")

    assert result["retrieved_snippets"] == []
    assert result["assistant_message"]["content"] == "I do not have document evidence to answer from."
    assert models.calls[0]["messages"] == [
        {"role": "system", "content": PLAIN_CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": "Tell me about Rebecca?"},
    ]
    db.close()


def test_existing_txt_rag_still_works(tmp_path: Path):
    db, documents, rag, _ocr, _sources, _models, chat = build_stack(tmp_path / "profile")
    source = tmp_path / "mission.txt"
    source.write_text(
        "Mars mission planning requires rover batteries, sample caches, and relay orbiters.",
        encoding="utf-8",
    )
    document = documents.import_document(str(source))
    rag.index_document(document["id"])

    result = chat.send("What does mars mission planning require?", use_rag=True, document_ids=[document["id"]])

    assert result["retrieved_snippets"]
    assert "rover batteries" in result["assistant_message"]["content"]
    assert result["document_evidence"][0]["file_type"] == "txt"
    db.close()
