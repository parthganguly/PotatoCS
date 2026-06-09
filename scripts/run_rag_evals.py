from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = ROOT / "python"
if str(PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(PYTHON_ROOT))

from odysseus_desktop_backend.services.chat_service import ChatService
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService
from odysseus_desktop_backend.services.model_service import OLLAMA_ENDPOINT, ModelService
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.storage import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="Run local RAG evals against installed Ollama models.")
    parser.add_argument("--cases", type=Path, default=ROOT / "evals" / "rag_cases")
    parser.add_argument("--models", nargs="*", help="Specific Ollama model names. Defaults to all installed models.")
    parser.add_argument("--verify", action="store_true", help="Enable the optional verifier pass during evals.")
    parser.add_argument("--show-answers", action="store_true", help="Print model answers for debugging failures.")
    parser.add_argument("--temperature", type=float, default=0.0, help="Ollama generation temperature for eval runs.")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if not cases:
        print(f"No eval cases found in {args.cases}", file=sys.stderr)
        return 2

    model_names = args.models or installed_ollama_models()
    if not model_names:
        print("No installed Ollama models found at 127.0.0.1:11434.", file=sys.stderr)
        return 2

    failures = 0
    print(f"Running {len(cases)} RAG eval case(s) against {len(model_names)} model(s).")
    for model in model_names:
        model_failures = run_model_cases(
            model,
            cases,
            verify=args.verify,
            show_answers=args.show_answers,
            temperature=args.temperature,
        )
        failures += model_failures
    return 1 if failures else 0


def load_cases(cases_dir: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in sorted(cases_dir.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        data["_path"] = str(path)
        cases.append(data)
    return cases


def installed_ollama_models() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="odysseus-rag-eval-models-") as temp:
        db = Database(Path(temp))
        try:
            status = ModelService(db).detect_ollama()
            if not status.get("reachable"):
                return []
            return [str(model) for model in status.get("models", []) if model]
        finally:
            db.close()


def run_model_cases(
    model: str,
    cases: list[dict[str, Any]],
    *,
    verify: bool,
    show_answers: bool,
    temperature: float,
) -> int:
    failures = 0
    with tempfile.TemporaryDirectory(prefix=f"odysseus-rag-eval-{safe_name(model)}-") as temp:
        db = Database(Path(temp))
        try:
            documents = DocumentService(db)
            embeddings = EmbeddingService(db)
            vector_store = SQLiteNumPyVectorStore(db)
            rag = RAGService(documents, embeddings, vector_store)
            settings = SettingsService(db)
            sessions = SessionService(db)
            models = EvalModelService(db, temperature=temperature)
            chat = ChatService(sessions, settings, models, rag=rag)
            document_ids = index_case_documents(cases, documents, rag)

            print(f"\nmodel={model}")
            for case in cases:
                started = time.perf_counter()
                required_source = str(case["required_source_document"])
                result = chat.send(
                    str(case["question"]),
                    model=model,
                    use_rag=True,
                    document_ids=[document_ids[required_source]],
                    verify_rag=verify,
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)
                outcome = evaluate_case(case, result, document_ids[required_source])
                if not outcome["passed"]:
                    failures += 1
                status = "PASS" if outcome["passed"] else "FAIL"
                print(
                    f"  {status} {case['id']} latency_ms={elapsed_ms} "
                    f"expected={outcome['expected_passed']} forbidden={outcome['forbidden_passed']} "
                    f"source={outcome['source_passed']}"
                )
                for reason in outcome["reasons"]:
                    print(f"    - {reason}")
                if show_answers:
                    answer = result["assistant_message"]["content"].replace("\n", "\n      ")
                    print(f"    answer: {answer}")
        finally:
            db.close()
    return failures


def index_case_documents(
    cases: list[dict[str, Any]],
    documents: DocumentService,
    rag: RAGService,
) -> dict[str, str]:
    indexed: dict[str, str] = {}
    for case in cases:
        case_path = Path(str(case["_path"]))
        for document in case.get("documents", []):
            fixture_id = str(document["id"])
            if fixture_id in indexed:
                continue
            path = (case_path.parent / str(document["path"])).resolve()
            imported = documents.import_document(str(path))
            rag.index_document(imported["id"])
            indexed[fixture_id] = imported["id"]
    return indexed


class EvalModelService(ModelService):
    def __init__(self, db: Database, *, temperature: float):
        super().__init__(db)
        self.temperature = temperature

    def chat(self, model: str, messages: list[dict[str, str]]) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        data = self._post_json(f"{OLLAMA_ENDPOINT}/api/chat", payload, timeout=120)
        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            if isinstance(content, str):
                return content
        raise RuntimeError("Ollama returned no assistant message")


def evaluate_case(case: dict[str, Any], result: dict[str, Any], required_document_id: str) -> dict[str, Any]:
    answer = normalize(result["assistant_message"]["content"])
    reasons: list[str] = []

    expected_passed = True
    for expected in case.get("expected_facts", []):
        phrases = [normalize(phrase) for phrase in expected.get("any", [])]
        if not any(phrase and phrase in answer for phrase in phrases):
            expected_passed = False
            reasons.append(f"missing expected fact: {expected.get('label')}")

    forbidden_passed = True
    for forbidden in case.get("forbidden_claims", []):
        phrases = [normalize(phrase) for phrase in forbidden.get("any", [])]
        matched = [phrase for phrase in phrases if phrase and phrase in answer]
        if matched:
            forbidden_passed = False
            reasons.append(f"forbidden claim present: {forbidden.get('label')}")

    retrieved = result.get("retrieved_snippets") or result.get("retrieved_chunks") or []
    source_passed = bool(retrieved) and all(
        item.get("document_id") == required_document_id for item in retrieved
    )
    if not source_passed:
        reasons.append("retrieved evidence did not stay within required source document")

    return {
        "passed": expected_passed and forbidden_passed and source_passed,
        "expected_passed": expected_passed,
        "forbidden_passed": forbidden_passed,
        "source_passed": source_passed,
        "reasons": reasons,
    }


def normalize(text: str) -> str:
    return " ".join((text or "").lower().replace("–", "-").split())


def safe_name(value: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in value).strip("-") or "model"


if __name__ == "__main__":
    raise SystemExit(main())
