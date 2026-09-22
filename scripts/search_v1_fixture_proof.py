from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOT = REPO_ROOT / "python"
sys.path.insert(0, str(PYTHON_ROOT))

from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService, LocalHashEmbeddingProvider
from odysseus_desktop_backend.services.local_discovery import FrontierStore, LocalDiscoveryProvider, LocalSearchIndex
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.search_service import SearchNoEvidenceError, SearchService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.services.web_extraction import ExtractedWebPage, WebExtractionError
from odysseus_desktop_backend.services.web_fetcher import FetchResponse
from odysseus_desktop_backend.storage import Database


class ProofModel:
    def __init__(self, quote: str):
        self.quote = quote
        self.calls = 0

    def chat_detailed(self, model: str, messages: list[dict[str, str]], **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        prompt = messages[-1]["content"]
        if "Generate concise public-web search formulations" in prompt:
            content = json.dumps({"queries": []})
        elif "Select only identifiers for spans" in prompt:
            sections = prompt.split("SPAN_ID=")[1:]
            match = next((section for section in sections if self.quote in section), None)
            content = json.dumps(
                {
                    "evidence": [] if match is None else [{"span_ids": [match.splitlines()[0]]}],
                    "needs_more_search": False,
                    "next_query": "",
                }
            )
        else:
            content = "Fixture answer supported by the retained observation [E1]."
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "stop",
            "prompt_eval_count": 20,
            "eval_count": 8,
            "total_duration_ns": 1_000_000,
            "load_duration_ns": 0,
            "generation_tokens_per_second": 20.0,
        }


class ProofFetcher:
    def __init__(self, bodies: dict[str, bytes]):
        self.bodies = dict(bodies)
        self.urls: list[str] = []

    def fetch(self, url: str, **_kwargs: Any) -> FetchResponse:
        self.urls.append(url)
        if url.endswith("/robots.txt"):
            body = b"User-agent: *\nAllow: /\n"
            content_type = "text/plain"
        else:
            body = self.bodies[url]
            content_type = "text/html; charset=utf-8"
        return FetchResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"content-type": content_type, "etag": "fixture-etag"},
            body=body,
            bytes_downloaded=len(body),
            redirects=0,
            elapsed_ms=1,
        )


class ProofExtractor:
    """Deterministic fixture extractor; L8 can fail one navigation URL by design."""

    def __init__(self, *, fail_urls: set[str] | None = None):
        self.fail_urls = set(fail_urls or ())

    def extract(self, body: bytes, *, content_type: str, final_url: str) -> ExtractedWebPage:
        if final_url in self.fail_urls:
            raise WebExtractionError("deliberate L8 navigation extraction failure")
        decoded = body.decode("utf-8", errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, flags=re.IGNORECASE | re.DOTALL)
        title = re.sub(r"\s+", " ", title_match.group(1)).strip() if title_match else final_url
        without_scripts = re.sub(
            r"<(script|style)\b[^>]*>.*?</\1>", " ", decoded, flags=re.IGNORECASE | re.DOTALL
        )
        text = re.sub(r"<[^>]+>", " ", without_scripts)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            raise WebExtractionError("fixture extraction produced no text")
        return ExtractedWebPage(
            title=title,
            text=text,
            metadata={"canonical_url": final_url},
            provenance_kind="exact_text",
        )


class DenseFixture:
    def __init__(self, results: list[Any]):
        self.results = results

    def search_with_audit(self, _query: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "results": self.results,
            "embedding_backend": "fixture-semantic",
            "embedding_model": "fixture-semantic-v1",
        }


def add_document(
    root: Path,
    documents: DocumentService,
    rag: RAGService,
    name: str,
    title: str,
    text: str,
    *,
    cached_url: str = "",
) -> dict[str, Any]:
    path = root / name
    path.write_text(text, encoding="utf-8")
    document = documents.import_document(str(path), title=title)
    rag.index_document(document["id"])
    if cached_url:
        documents.db.conn.execute(
            """
            UPDATE documents SET source_origin='cached_web', canonical_url=?, final_url=?,
                fetched_at=1, web_revision_current=1 WHERE id=?
            """,
            (cached_url, cached_url, document["id"]),
        )
        documents.db.conn.commit()
    return documents.get(document["id"])


def result_row(
    scenario: str,
    question: str,
    discovery_path: str,
    result: dict[str, Any] | None,
    model: ProofModel,
    fetcher: ProofFetcher,
    elapsed_ms: int,
    final_status: str,
) -> dict[str, Any]:
    metrics = dict((result or {}).get("metrics") or {})
    page_fetches = [url for url in fetcher.urls if not url.endswith("/robots.txt")]
    return {
        "scenario": scenario,
        "question": question,
        "discovery_path": discovery_path,
        "fts_candidates": int(metrics.get("local_fts_hits") or 0),
        "dense_candidates": int(metrics.get("local_dense_hits") or 0),
        "fused_candidates": int(metrics.get("local_fused_candidates") or 0),
        "frontier_candidates": int(metrics.get("frontier_candidates") or 0),
        "source_pack_matches": int(metrics.get("source_pack_hits") or 0),
        "fetches": len(page_fetches),
        "network_requests_including_robots": len(fetcher.urls),
        "cache_hits": int(metrics.get("cache_hits") or 0),
        "document_extraction_failures": int(metrics.get("extraction_failures") or 0),
        "links_discovered": int(metrics.get("links_discovered") or 0),
        "links_discovered_after_extraction_failure": int(
            metrics.get("links_discovered_after_extraction_failure") or 0
        ),
        "model_calls": model.calls,
        "verified_evidence": len((result or {}).get("citations") or []),
        "final_status": final_status,
        "wall_time_ms": elapsed_ms,
    }


def run_scenario(name: str) -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix=f"odysseus-search-v1-{name.lower()}-") as temp_dir:
        root = Path(temp_dir)
        db = Database(root / "profile")
        try:
            baseline_database_bytes = int(db.conn.execute("PRAGMA page_size").fetchone()[0]) * int(
                db.conn.execute("PRAGMA page_count").fetchone()[0]
            )
            db.set_setting("search_mode", "local")
            db.set_setting("search_local_max_new_urls", "2" if name == "L8" else "1")
            sessions = SessionService(db)
            documents = DocumentService(db)
            embeddings = EmbeddingService(db, provider=LocalHashEmbeddingProvider())
            real_rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
            frontier = FrontierStore(db)
            bodies: dict[str, bytes] = {}
            discovery_path = ""

            if name == "L1":
                question = "What is the local archive access code?"
                quote = "The local archive access code is ORBIT-441."
                add_document(root, documents, real_rag, "archive.md", "Archive", quote)
                rag: Any = real_rag
                discovery_path = "current local Source -> FTS+dense -> evidence"
            elif name == "L2":
                question = "What is the linked calibration value?"
                quote = "The decisive calibration value is 7319."
                seed_url = "https://fixture.example/seed"
                destination = "https://fixture.example/decisive"
                seed_html = f"<html><body><p>Calibration reference <a href='{destination}'>linked calibration value</a></p></body></html>"
                add_document(root, documents, real_rag, "seed.md", "Cached Seed", "Calibration reference linked calibration value", cached_url=seed_url)
                frontier.discover_links(source_url=seed_url, html_body=seed_html, source_title="Cached Seed", source_depth=0)
                bodies[destination] = f"<html><head><title>Decisive</title></head><body><article><p>{quote}</p></article></body></html>".encode()
                rag = real_rag
                discovery_path = "relevant stored anchor -> one bounded destination fetch -> only fetched destination text becomes evidence"
            elif name == "L3":
                question = "What is the PotatoCS fixture launch code?"
                quote = "The PotatoCS fixture launch code is PACK-204."
                pack_dir = db.profile_dir / "source-packs"
                pack_dir.mkdir(parents=True, exist_ok=True)
                (pack_dir / "potatocs-fixture.md").write_text(
                    "---\ntype: source-pack\ntitle: PotatoCS Fixture\ntags:\n  - potatocs\n  - fixture\n---\n\n"
                    "# Seeds\n\n- https://fixture.example/start\n",
                    encoding="utf-8",
                )
                bodies["https://fixture.example/start"] = f"<html><head><title>Fixture</title></head><body><article><p>{quote}</p></article></body></html>".encode()
                rag = real_rag
                discovery_path = "matching Source Pack -> deterministic seed -> one bounded fetch -> evidence"
            elif name == "L4":
                question = "What is the unreachable global-web answer?"
                quote = "not present"
                rag = real_rag
                discovery_path = "local corpus + frontier + Source Packs exhausted -> honest no_evidence"
            elif name == "L5":
                question = "Which build contains CVE-2026-12345?"
                quote = "CVE-2026-12345 affects only the cobalt Windows build."
                exact_doc = add_document(root, documents, real_rag, "cve.md", "Security", quote)
                decoy_doc = add_document(root, documents, real_rag, "decoy.md", "Security overview", "General release security guidance.")
                decoy = documents.chunks(decoy_doc["id"])[0]
                rag = DenseFixture([SimpleNamespace(
                    chunk_id=decoy["id"], document_id=decoy["document_id"], content=decoy["content"],
                    score=0.99, page_start=decoy["page_start"], page_end=decoy["page_end"], metadata=decoy["metadata"],
                )])
                del exact_doc
                discovery_path = "mechanism isolation: stipulated dense miss -> FTS exact-identifier rescue -> fused evidence"
            elif name == "L6":
                question = "telecommuting entitlement"
                quote = "Employees may work away from the office two days weekly."
                semantic_doc = add_document(root, documents, real_rag, "policy.md", "Flexible location policy", quote)
                chunk = documents.chunks(semantic_doc["id"])[0]
                rag = DenseFixture([SimpleNamespace(
                    chunk_id=chunk["id"], document_id=chunk["document_id"], content=chunk["content"],
                    score=0.98, page_start=chunk["page_start"], page_end=chunk["page_end"], metadata=chunk["metadata"],
                )])
                discovery_path = "mechanism isolation: stipulated FTS miss -> dense semantic rescue -> fused evidence"
            elif name == "L8":
                question = "What is the default fixture checkpoint threshold?"
                quote = "The default fixture checkpoint threshold is 1000 pages."
                navigation_url = "https://fixture.example/docs.html"
                target_url = "https://fixture.example/wal.html"
                frontier.discover(
                    navigation_url,
                    discovery_kind="manual_seed",
                    anchor_text="fixture checkpoint threshold documentation",
                )
                bodies[navigation_url] = (
                    "<html><head><title>Fixture Documentation</title></head><body>"
                    "<p>Checkpoint reference.</p>"
                    f"<a href='{target_url}'>Write-Ahead Log (WAL) Mode</a>"
                    "</body></html>"
                ).encode()
                bodies[target_url] = (
                    f"<html><head><title>WAL</title></head><body><article><p>{quote}</p>"
                    "</article></body></html>"
                ).encode()
                rag = real_rag
                discovery_path = (
                    "mechanism only: navigation fetch -> deliberate document-extraction failure -> "
                    "bounded link discovery -> target fetch/extract -> verified evidence"
                )
            else:
                raise ValueError(name)

            provider = LocalDiscoveryProvider(db)
            fetcher = ProofFetcher(bodies)
            model = ProofModel(quote)
            extractor = ProofExtractor(fail_urls={navigation_url}) if name == "L8" else ProofExtractor()
            service = SearchService(
                db, sessions, model, documents, rag, provider, fetcher=fetcher, extractor=extractor
            )
            session = sessions.create(model="fixture-model")
            started = time.perf_counter()
            result: dict[str, Any] | None = None
            status = "evidence_found"
            try:
                result = service.run(
                    question=question,
                    session_id=session["id"],
                    model="fixture-model",
                    second_round_enabled=False,
                )
            except SearchNoEvidenceError:
                status = "no_evidence"
                failed_run = db.conn.execute(
                    "SELECT metrics_json FROM search_runs ORDER BY created_at DESC, id DESC LIMIT 1"
                ).fetchone()
                if failed_run is not None:
                    result = {"metrics": json.loads(failed_run["metrics_json"]), "citations": []}
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            index = LocalSearchIndex(db)
            index.sync()
            corpus = index.metrics()
            db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            page_size = int(db.conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(db.conn.execute("PRAGMA page_count").fetchone()[0])
            try:
                fts_bytes = int(db.conn.execute("SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name LIKE 'local_fts%'").fetchone()[0])
            except sqlite3.Error:
                fts_bytes = None
            resources = {
                **corpus,
                "database_bytes": page_size * page_count,
                "database_growth_bytes": page_size * page_count - baseline_database_bytes,
                "fts_index_bytes": fts_bytes,
                "ram_bytes": None,
            }
            row = result_row(name, question, discovery_path, result, model, fetcher, elapsed_ms, status)
            if name == "L8":
                row.update(
                    {
                        "navigation_documents": db.conn.execute(
                            "SELECT COUNT(*) FROM documents WHERE canonical_url=?", (navigation_url,)
                        ).fetchone()[0],
                        "navigation_evidence": db.conn.execute(
                            """
                            SELECT COUNT(*) FROM search_evidence e
                            JOIN documents d ON d.id=e.source_document_id
                            WHERE d.canonical_url=?
                            """,
                            (navigation_url,),
                        ).fetchone()[0],
                        "target_documents": db.conn.execute(
                            "SELECT COUNT(*) FROM documents WHERE canonical_url=?", (target_url,)
                        ).fetchone()[0],
                        "proof_scope": "mechanism only; not general web navigation quality",
                    }
                )
            return row, resources
        finally:
            db.close()


def run_accumulation_scenario() -> tuple[dict[str, Any], dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="odysseus-search-v1-l7-") as temp_dir:
        root = Path(temp_dir)
        db = Database(root / "profile")
        try:
            db.set_setting("search_mode", "local")
            db.set_setting("search_local_max_new_urls", "1")
            sessions = SessionService(db)
            documents = DocumentService(db)
            embeddings = EmbeddingService(db, provider=LocalHashEmbeddingProvider())
            rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
            provider = LocalDiscoveryProvider(db)
            page_a = "https://fixture.example/trail"
            page_b = "https://fixture.example/recovery"
            provider.frontier.discover(
                page_a,
                discovery_kind="manual_seed",
                anchor_text="preliminary trail page",
            )
            bodies = {
                page_a: (
                    f"<html><head><title>Preliminary trail</title></head><body><article>"
                    f"<p>This page records a preliminary trail but contains no recovery code.</p>"
                    f"<p><a href='{page_b}'>linked recovery code</a></p></article></body></html>"
                ).encode(),
                page_b: b"<html><head><title>Recovery</title></head><body><article><p>The linked recovery code is TRAIL-882.</p></article></body></html>",
            }
            fetcher = ProofFetcher(bodies)

            first_model = ProofModel("a quote that is deliberately absent")
            first_service = SearchService(
                db, sessions, first_model, documents, rag, provider, fetcher=fetcher, extractor=ProofExtractor()
            )
            first_session = sessions.create(model="fixture-model")
            started = time.perf_counter()
            first_status = "evidence_found"
            try:
                first_service.run(
                    question="Which page describes the preliminary trail?",
                    session_id=first_session["id"],
                    model="fixture-model",
                    second_round_enabled=False,
                )
            except SearchNoEvidenceError:
                first_status = "no_evidence"
            first_elapsed = int((time.perf_counter() - started) * 1000)
            first_request_count = len(fetcher.urls)
            retained = db.conn.execute(
                "SELECT is_staging, web_revision_current FROM documents WHERE canonical_url = ?", (page_a,)
            ).fetchone()
            frontier_a = db.conn.execute(
                "SELECT status FROM crawl_frontier WHERE canonical_url = ?", (page_a,)
            ).fetchone()

            second_model = ProofModel("The linked recovery code is TRAIL-882.")
            second_service = SearchService(
                db, sessions, second_model, documents, rag, provider, fetcher=fetcher, extractor=ProofExtractor()
            )
            second_session = sessions.create(model="fixture-model")
            started = time.perf_counter()
            second_result = second_service.run(
                question="What is the linked recovery code?",
                session_id=second_session["id"],
                model="fixture-model",
                second_round_enabled=False,
            )
            second_elapsed = int((time.perf_counter() - started) * 1000)
            index = LocalSearchIndex(db)
            index.sync()
            corpus = index.metrics()
            return {
                "scenario": "L7",
                "question": "Question A no_evidence, then Question B follows the retained page-A link",
                "discovery_path": "page A fetch -> no_evidence -> retained cached observation/frontier fetched -> page-A link -> page B fetch -> verified evidence",
                "first_status": first_status,
                "first_page_retained": bool(retained and not retained["is_staging"] and retained["web_revision_current"]),
                "first_frontier_status": str(frontier_a["status"] if frontier_a else "missing"),
                "page_a_fetches_total": fetcher.urls.count(page_a),
                "first_network_requests": first_request_count,
                "second_additional_network_requests": len(fetcher.urls) - first_request_count,
                "second_verified_evidence": len(second_result["citations"]),
                "second_status": "evidence_found",
                "wall_time_ms": first_elapsed + second_elapsed,
            }, corpus
        finally:
            db.close()


def main() -> int:
    scenarios: list[dict[str, Any]] = []
    resources: dict[str, dict[str, Any]] = {}
    for name in ("L1", "L2", "L3", "L4", "L5", "L6"):
        scenario, scenario_resources = run_scenario(name)
        scenarios.append(scenario)
        resources[name] = scenario_resources
    accumulation, accumulation_resources = run_accumulation_scenario()
    scenarios.append(accumulation)
    resources["L7"] = accumulation_resources
    l8, l8_resources = run_scenario("L8")
    scenarios.append(l8)
    resources["L8"] = l8_resources
    print(json.dumps({"scenarios": scenarios, "resource_observations": resources}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
