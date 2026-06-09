from __future__ import annotations

import json
from pathlib import Path

from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database


REPO_ROOT = Path(__file__).resolve().parents[2]


def build_services(profile_dir: Path, model_service: ModelService):
    db = model_service.db
    documents = DocumentService(db)
    embeddings = EmbeddingService(db)
    vector_store = SQLiteNumPyVectorStore(db)
    rag = RAGService(documents, embeddings, vector_store)
    settings = SettingsService(db)
    sessions = SessionService(db)
    chat = ChatService(sessions, settings, model_service, rag=rag)
    return db, documents, rag, chat


class CapturingModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[dict] = []

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"model": model, "messages": messages})
        return "Grounded answer"


def test_quote_first_rag_uses_short_evidence_snippets_not_full_noisy_chunks(tmp_path: Path):
    db = Database(tmp_path / "profile")
    models = CapturingModelService(db)
    _db, documents, rag, chat = build_services(tmp_path / "profile", models)
    source = tmp_path / "Frame 10.txt"
    source.write_text(
        "Tribute to my Grandfather. He eventually came out of the hole. "
        "He had tea with comrades. He told the story for the next 60-70 years. "
        + ("NOISY_FILLER unrelated background. " * 35),
        encoding="utf-8",
    )

    document = documents.import_document(str(source))
    rag.index_document(document["id"])
    result = chat.send(
        "Tell me about the grandfather?",
        use_rag=True,
        document_ids=[document["id"]],
    )

    system_prompt = models.calls[0]["messages"][0]["content"]
    assert "Retrieved evidence snippets" in system_prompt
    assert "He eventually came out of the hole" in system_prompt
    assert "He had tea with comrades" in system_prompt
    assert system_prompt.count("NOISY_FILLER") <= 1
    assert result["retrieved_snippets"]
    assert result["grounding"]["mode"] == "quote_first"
    db.close()


class VerifyingModelService(ModelService):
    def __init__(self, db: Database):
        super().__init__(db)
        self.calls: list[dict] = []

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        self.calls.append({"model": model, "messages": messages})
        system = messages[0]["content"]
        if "Check the answer only against the retrieved evidence snippets" in system:
            answer = messages[-1]["content"]
            if "stayed in the hole for 60-70 years" in answer:
                return json.dumps(
                    {
                        "claims": [
                            {
                                "text": "He stayed in the hole for 60-70 years.",
                                "status": "contradicted",
                                "reason": "The evidence says he told the story for 60-70 years.",
                                "source_ids": ["S1"],
                            }
                        ],
                        "unsupported_claims": [],
                        "contradicted_claims": ["He stayed in the hole for 60-70 years."],
                    }
                )
            return json.dumps(
                {
                    "claims": [
                        {
                            "text": "He eventually came out and had tea with comrades.",
                            "status": "supported",
                            "reason": "The evidence says this directly.",
                            "source_ids": ["S1"],
                        }
                    ],
                    "unsupported_claims": [],
                    "contradicted_claims": [],
                }
            )
        if messages[-1]["content"].startswith("Revise the answer once"):
            return (
                "The retrieved context says he eventually came out of the hole, "
                "had tea with comrades, and told the story for the next 60-70 years."
            )
        return "He stayed in the hole for 60-70 years."


def test_optional_verifier_regenerates_once_on_contradicted_claim(tmp_path: Path):
    db = Database(tmp_path / "profile")
    models = VerifyingModelService(db)
    _db, documents, rag, chat = build_services(tmp_path / "profile", models)
    source = tmp_path / "Frame 10.txt"
    source.write_text(
        "Tribute to my Grandfather. He eventually came out of the hole, "
        "had tea with comrades, and told the story for the next 60-70 years.",
        encoding="utf-8",
    )

    document = documents.import_document(str(source))
    rag.index_document(document["id"])
    result = chat.send(
        "Tell me about the grandfather?",
        use_rag=True,
        document_ids=[document["id"]],
        verify_rag=True,
    )

    answer = result["assistant_message"]["content"]
    grounding = result["grounding"]
    assert "stayed in the hole for 60-70 years" not in answer
    assert "eventually came out of the hole" in answer
    assert grounding["regenerated"] is True
    assert grounding["verifier"]["status"] == "passed"
    assert grounding["draft_verifier"]["verifier"]["status"] == "failed"
    assert len(models.calls) == 4
    db.close()


def test_rag_eval_fixtures_have_required_reliability_fields():
    cases_dir = REPO_ROOT / "evals" / "rag_cases"
    case_paths = sorted(cases_dir.glob("*.json"))
    assert case_paths
    ids = set()
    for path in case_paths:
        case = json.loads(path.read_text(encoding="utf-8"))
        ids.add(case["id"])
        assert case["documents"]
        assert case["question"]
        assert case["expected_facts"]
        assert case["forbidden_claims"]
        assert case["required_source_document"]
        for document in case["documents"]:
            fixture_path = (path.parent / document["path"]).resolve()
            assert fixture_path.exists()
    assert "grandfather_chronology" in ids
    assert "grandfather_no_water_contamination" in ids
    assert "water_no_grandfather_contamination" in ids
