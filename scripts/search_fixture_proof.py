from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = REPO_ROOT / "python"
sys.path.insert(0, str(PYTHON_ROOT))

from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService, LocalHashEmbeddingProvider
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.search_provider import FixtureSearchProvider, ProviderSearchResult
from odysseus_desktop_backend.services.search_service import SearchService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.services.web_fetcher import FetchResponse
from odysseus_desktop_backend.storage import Database


FIXTURES = PYTHON_ROOT / "tests" / "fixtures" / "search"
QUESTION = "Municipal Heat Pump Program"
VARIANT = "municipal heat pump rebate count"
REPAIR_QUERY = "heat pump application closing date"
FIRST_URL = "https://example.com/heat-pumps"
SECOND_URL = "https://example.org/deadline"


class ProofFetcher:
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = bodies
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.urls.append(url)
        body = self.bodies[url]
        return FetchResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"content-type": "text/html; charset=utf-8", "etag": "fixture-etag"},
            body=body,
            bytes_downloaded=len(body),
            redirects=0,
            elapsed_ms=3,
        )


class ProofModel:
    def __init__(self, use_second_round: bool):
        self.use_second_round = use_second_round
        self.selection_calls = 0

    def chat_detailed(self, model: str, messages: list[dict[str, str]], **kwargs: Any) -> dict[str, Any]:
        del kwargs
        prompt = messages[-1]["content"]
        if "Generate concise public-web search formulations" in prompt:
            content = json.dumps({"queries": [VARIANT]})
        elif "Propose one concise public-web repair query" in prompt:
            content = json.dumps({"query": REPAIR_QUERY})
        elif "Select only exact copied quotes" in prompt:
            self.selection_calls += 1
            if self.selection_calls == 1:
                passage_id = prompt.split("PASSAGE_ID=", 1)[1].splitlines()[0]
                content = json.dumps(
                    {
                        "evidence": [
                            {
                                "passage_id": passage_id,
                                "quote": "The city approved 240 heat-pump rebates for owner-occupied homes in 2026.",
                            }
                        ],
                        "needs_more_search": self.use_second_round,
                        "next_query": REPAIR_QUERY,
                    }
                )
            else:
                quote = "Applications close on October 1, 2026."
                matching = next(section for section in prompt.split("PASSAGE_ID=")[1:] if quote in section)
                content = json.dumps(
                    {
                        "evidence": [{"passage_id": matching.splitlines()[0], "quote": quote}],
                        "needs_more_search": False,
                        "next_query": "",
                    }
                )
        else:
            content = "The program approved 240 rebates [E1]."
            if self.use_second_round:
                content += " Applications close October 1 [E2]."
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "stop",
            "prompt_eval_count": 25,
            "eval_count": 12,
            "total_duration_ns": 1_000_000,
            "load_duration_ns": 0,
            "generation_tokens_per_second": 20.0,
        }


def run_proof(mode: str) -> dict[str, Any]:
    use_second_round = mode == "S"
    provider = FixtureSearchProvider(
        {
            QUESTION: [ProviderSearchResult(FIRST_URL, "Program", "240 rebates", 1, "fixture", QUESTION)],
            VARIANT: [],
            REPAIR_QUERY: [
                ProviderSearchResult(SECOND_URL, "Deadline", "Applications close", 1, "fixture", REPAIR_QUERY)
            ],
        }
    )
    second_body = (
        b"<html><head><title>Deadline</title></head><body><article><h1>Deadline</h1>"
        b"<p>Applications close on October 1, 2026.</p>"
        b"<p>Late applications are not accepted.</p></article></body></html>"
    )
    fetcher = ProofFetcher(
        {
            FIRST_URL: (FIXTURES / "clean_article.html").read_bytes(),
            SECOND_URL: second_body,
        }
    )
    with tempfile.TemporaryDirectory(prefix="odysseus-search-proof-") as temp_dir:
        db = Database(Path(temp_dir))
        try:
            sessions = SessionService(db)
            documents = DocumentService(db)
            embeddings = EmbeddingService(db, provider=LocalHashEmbeddingProvider())
            rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
            service = SearchService(db, sessions, ProofModel(use_second_round), documents, rag, provider, fetcher=fetcher)
            session = sessions.create(model="fixture-model")
            result = service.run(
                question=QUESTION,
                session_id=session["id"],
                model="fixture-model",
                second_round_enabled=use_second_round,
            )
            metrics = result["metrics"]
            return {
                "mode": mode,
                "queries": result["queries"],
                "urls_fetched": fetcher.urls,
                "metrics": metrics,
                "evidence_count": len(result["citations"]),
                "citations": [
                    {
                        "number": item["citation_number"],
                        "source_origin": item["source_origin"],
                        "canonical_url": item["canonical_url"],
                        "verification_status": item["verification_status"],
                        "exact_quote": item["exact_quote"],
                    }
                    for item in result["citations"]
                ],
            }
        finally:
            db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Odysseus Search v0 fixture proof.")
    parser.add_argument("--mode", choices=("T", "S", "all"), default="all")
    args = parser.parse_args()
    modes = ("T", "S") if args.mode == "all" else (args.mode,)
    print(json.dumps([run_proof(mode) for mode in modes], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
