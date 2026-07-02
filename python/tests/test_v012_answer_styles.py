from __future__ import annotations

import json
from pathlib import Path

from odysseus_desktop_backend.services.chat_service import ChatService, PLAIN_CHAT_SYSTEM_PROMPT
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database
from rpc_server import SidecarApp


REPO_ROOT = Path(__file__).resolve().parents[2]


class CapturingModelService(ModelService):
    def __init__(self, db: Database, reply: str = "styled answer"):
        super().__init__(db)
        self.reply = reply
        self.calls: list[dict] = []

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"model": model, "messages": messages})
        return self.reply


def build_rag_chat(tmp_path: Path, models: ModelService):
    db = models.db
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    vector_store = SQLiteNumPyVectorStore(db)
    rag = RAGService(documents, embeddings, vector_store)
    settings = SettingsService(db)
    sessions = SessionService(db)
    chat = ChatService(sessions, settings, models, rag=rag)
    return db, documents, rag, chat


def index_source(documents: DocumentService, rag: RAGService, path: Path) -> str:
    document = documents.import_document(str(path))
    rag.index_document(document["id"])
    return document["id"]


def prompt_for_style(tmp_path: Path, answer_style: str) -> str:
    db = Database(tmp_path / answer_style)
    models = CapturingModelService(db)
    _db, documents, rag, chat = build_rag_chat(tmp_path / answer_style, models)
    source = tmp_path / f"{answer_style}.txt"
    source.write_text(
        "A procedural notice explains that sample bottles must be labeled, forms must be complete, "
        "and late shipments may be rejected.",
        encoding="utf-8",
    )
    document_id = index_source(documents, rag, source)
    chat.send(
        "What does this document mean?",
        use_rag=True,
        document_ids=[document_id],
        answer_style=answer_style,
    )
    prompt = models.calls[0]["messages"][0]["content"]
    db.close()
    return prompt


def test_answer_style_shapes_rag_prompt_generally(tmp_path: Path):
    precise = prompt_for_style(tmp_path, "precise")
    layman = prompt_for_style(tmp_path, "layman")
    detailed = prompt_for_style(tmp_path, "detailed")
    extract_only = prompt_for_style(tmp_path, "extract_only")

    assert "Precise style" in precise
    assert "Stay close to the retrieved evidence" in precise
    assert "60-70" not in precise
    assert "Layman style" in layman
    assert "plain English" in layman
    assert "what that practically means" in layman
    assert "Detailed style" in detailed
    assert "Give a fuller answer" in detailed
    assert "Extract only style" in extract_only
    assert "Do not interpret" in extract_only


def test_answer_style_is_passed_through_json_rpc(tmp_path: Path):
    app = SidecarApp(tmp_path / "profile")
    try:
        fake = FakeChat()
        app.chat = fake
        result = app.dispatch(
            "chat.send",
            {
                "message": "Explain this plainly",
                "use_rag": True,
                "answer_style": "layman",
                "verify_rag": True,
                "document_ids": ["doc-1"],
            },
        )
        assert fake.kwargs["answer_style"] == "layman"
        assert fake.kwargs["verify_rag"] is True
        assert fake.kwargs["document_ids"] == ["doc-1"]
        assert result["answer_style"] == "layman"
    finally:
        app.close()


def test_invalid_answer_style_does_not_change_non_rag_chat(tmp_path: Path):
    db = Database(tmp_path / "profile")
    models = CapturingModelService(db, reply="plain chat")
    settings = SettingsService(db)
    sessions = SessionService(db)
    chat = ChatService(sessions, settings, models)

    result = chat.send("simple chat", answer_style="not-a-style")

    assert result["assistant_message"]["content"] == "plain chat"
    assert result["retrieved_chunks"] == []
    assert models.calls[0]["messages"] == [
        {"role": "system", "content": PLAIN_CHAT_SYSTEM_PROMPT},
        {"role": "user", "content": "simple chat"},
    ]
    db.close()


def test_verifier_still_regenerates_with_styled_answers(tmp_path: Path):
    db = Database(tmp_path / "profile")
    models = StyledVerifyingModelService(db)
    _db, documents, rag, chat = build_rag_chat(tmp_path / "profile", models)
    source = tmp_path / "procedure.txt"
    source.write_text(
        "The notice states that bottles must be labeled and incomplete forms may cause samples to be rejected.",
        encoding="utf-8",
    )
    document_id = index_source(documents, rag, source)

    result = chat.send(
        "Explain the problem in plain English.",
        use_rag=True,
        document_ids=[document_id],
        verify_rag=True,
        answer_style="layman",
    )

    assert "labeled bottles" in result["assistant_message"]["content"]
    assert result["grounding"]["regenerated"] is True
    correction_call = models.calls[2]["messages"][-1]["content"]
    assert "Layman style" in correction_call
    db.close()


def test_v012_eval_fixtures_cover_general_behavior_classes():
    cases_dir = REPO_ROOT / "evals" / "rag_cases"
    cases = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(cases_dir.glob("*.json"))
    ]
    ids = {case["id"] for case in cases}
    assert "grandfather_chronology" in ids
    assert "grandfather_no_water_contamination" in ids
    assert "procedural_problem_layman" in ids
    assert "extract_only_direct_facts" in ids
    assert any(case.get("answer_style") == "layman" for case in cases)
    assert any(case.get("answer_style") == "extract_only" for case in cases)
    for case in cases:
        assert case.get("answer_style", "precise") in {
            "precise",
            "layman",
            "detailed",
            "extract_only",
        }


class FakeChat:
    def send(self, **kwargs):
        self.kwargs = kwargs
        return {
            "answer_style": kwargs.get("answer_style"),
            "retrieved_chunks": [],
            "retrieved_snippets": [],
            "grounding": {},
        }


class StyledVerifyingModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[dict] = []

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"model": model, "messages": messages})
        system = messages[0]["content"]
        if "Check the answer only against the retrieved evidence snippets" in system:
            answer = messages[-1]["content"]
            if "specific outbreak" in answer:
                return json.dumps(
                    {
                        "claims": [
                            {
                                "text": "The notice proves there is a specific outbreak.",
                                "status": "contradicted",
                                "reason": "The evidence only describes sample handling.",
                                "source_ids": ["S1"],
                            }
                        ],
                        "unsupported_claims": [],
                        "contradicted_claims": ["The notice proves there is a specific outbreak."],
                    }
                )
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": "The practical issue is labeled bottles and complete forms.",
                            "status": "supported",
                            "reason": "The evidence states both requirements.",
                            "source_ids": ["S1"],
                        }
                    ],
                    "unsupported_claims": [],
                    "contradicted_claims": [],
                }
            )
        if messages[-1]["content"].startswith("Revise the answer once"):
            return "In plain English, the practical issue is labeled bottles and complete forms."
        return "This proves there is a specific outbreak."
