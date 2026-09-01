from __future__ import annotations

import json
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from odysseus_desktop_backend.cancellation import JobCancelledError, cancellation_scope
from odysseus_desktop_backend.services.document_service import DocumentService
from odysseus_desktop_backend.services.embedding_service import EmbeddingService, LocalHashEmbeddingProvider
from odysseus_desktop_backend.services.job_service import DocumentJobExecutor, JobFailure, JobRecord
from odysseus_desktop_backend.services.local_discovery import (
    DiscoveryParser,
    LocalBudgetExhausted,
    FrontierStore,
    LocalDiscoveryLimits,
    LocalDiscoveryProvider,
    LocalSearchIndex,
    SourcePackStore,
    fts_match_expression,
)
from odysseus_desktop_backend.services.rag_service import RAGService
from odysseus_desktop_backend.services.search_service import (
    SearchBudgetError,
    SearchNoEvidenceError,
    SearchService,
)
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.vector_store import SQLiteNumPyVectorStore
from odysseus_desktop_backend.services.web_extraction import (
    ExtractedWebPage,
    WebExtractionError,
)
from odysseus_desktop_backend.services.web_fetcher import (
    FetchBlockedError,
    FetchError,
    FetchLimitError,
    FetchResponse,
    SafeHttpFetcher,
)
from odysseus_desktop_backend.services.web_source_store import WebSourceStoreError
from odysseus_desktop_backend.storage import Database, SCHEMA_VERSION


FIXTURES = Path(__file__).parent / "fixtures" / "search_v1"
PUBLIC_IP = "93.184.216.34"


def stack(tmp_path: Path) -> tuple[Database, DocumentService, RAGService, LocalSearchIndex]:
    db = Database(tmp_path / "profile")
    documents = DocumentService(db)
    embeddings = EmbeddingService(db, provider=LocalHashEmbeddingProvider())
    rag = RAGService(documents, embeddings, SQLiteNumPyVectorStore(db))
    return db, documents, rag, LocalSearchIndex(db)


def add_document(tmp_path: Path, documents: DocumentService, rag: RAGService, name: str, title: str, text: str) -> dict:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    document = documents.import_document(str(path), title=title)
    rag.index_document(document["id"])
    return documents.get(document["id"])


class EmptyEvidenceModel:
    def __init__(
        self,
        *,
        request_second_round: bool = False,
        repair_query: str = "secondary calibration details",
        cancel_on_selection: threading.Event | None = None,
    ):
        self.request_second_round = request_second_round
        self.repair_query = repair_query
        self.cancel_on_selection = cancel_on_selection
        self.calls = 0
        self.selection_calls = 0

    def chat_detailed(self, model: str, messages: list[dict[str, str]], **_kwargs: object) -> dict:
        self.calls += 1
        prompt = messages[-1]["content"]
        if "Generate concise public-web search formulations" in prompt:
            content = json.dumps({"queries": []})
        elif "Propose one concise public-web repair query" in prompt:
            content = json.dumps({"query": self.repair_query})
        elif "Select only identifiers for spans" in prompt:
            self.selection_calls += 1
            if self.cancel_on_selection is not None:
                self.cancel_on_selection.set()
            content = json.dumps(
                {
                    "evidence": [],
                    "needs_more_search": self.request_second_round and self.selection_calls == 1,
                    "next_query": "",
                }
            )
        else:
            content = "This synthesis should not run."
        return {
            "model": model,
            "content": content,
            "thinking": "",
            "done_reason": "stop",
            "prompt_eval_count": 10,
            "eval_count": 4,
            "total_duration_ns": 1_000_000,
            "load_duration_ns": 0,
            "generation_tokens_per_second": 10.0,
        }


class QuoteEvidenceModel(EmptyEvidenceModel):
    def __init__(self, quote: str):
        super().__init__()
        self.quote = quote

    def chat_detailed(self, model: str, messages: list[dict[str, str]], **kwargs: object) -> dict:
        prompt = messages[-1]["content"]
        if "Select only identifiers for spans" in prompt:
            self.calls += 1
            section = next((part for part in prompt.split("SPAN_ID=")[1:] if self.quote in part), "")
            content = json.dumps(
                {
                    "evidence": [] if not section else [{"span_ids": [section.splitlines()[0]]}],
                    "needs_more_search": False,
                    "next_query": "",
                }
            )
            return {
                "model": model, "content": content, "thinking": "", "done_reason": "stop",
                "prompt_eval_count": 10, "eval_count": 4, "total_duration_ns": 1_000_000,
                "load_duration_ns": 0, "generation_tokens_per_second": 10.0,
            }
        if "Generate concise public-web search formulations" in prompt:
            return super().chat_detailed(model, messages, **kwargs)
        self.calls += 1
        return {
            "model": model, "content": "Verified fixture answer [E1].", "thinking": "", "done_reason": "stop",
            "prompt_eval_count": 10, "eval_count": 4, "total_duration_ns": 1_000_000,
            "load_duration_ns": 0, "generation_tokens_per_second": 10.0,
        }


class LocalFixtureFetcher:
    def __init__(self, outcomes: dict[str, object], *, default_robots: bytes | None = None):
        self.outcomes = dict(outcomes)
        self.default_robots = default_robots
        self.calls: list[str] = []
        self.max_response_bytes = 8 * 1024 * 1024

    def fetch(self, url: str, **_kwargs: object) -> FetchResponse:
        self.calls.append(url)
        if url.endswith("/robots.txt") and url not in self.outcomes and self.default_robots is not None:
            outcome: object = (self.default_robots, "text/plain")
        else:
            outcome = self.outcomes[url]
        if isinstance(outcome, Exception):
            raise outcome
        if isinstance(outcome, tuple):
            body, content_type = outcome
        else:
            body, content_type = outcome, "text/html"
        raw = bytes(body)
        if len(raw) > self.max_response_bytes:
            raise FetchLimitError("response size limit exceeded")
        return FetchResponse(
            requested_url=url,
            final_url=url,
            status=200,
            headers={"content-type": str(content_type)},
            body=raw,
            bytes_downloaded=len(raw),
            redirects=0,
            elapsed_ms=1,
        )


class FixtureHTMLExtractor:
    def __init__(self, *, fail_urls: set[str] | None = None):
        self.fail_urls = set(fail_urls or set())

    def extract(self, body: bytes, *, content_type: str, final_url: str) -> ExtractedWebPage:
        del content_type
        if final_url in self.fail_urls:
            raise WebExtractionError("fixture document extraction failed")
        decoded = body.decode("utf-8", errors="replace")
        title_match = re.search(r"<title[^>]*>(.*?)</title>", decoded, flags=re.IGNORECASE | re.DOTALL)
        title = " ".join((title_match.group(1) if title_match else final_url).split())
        without_blocked = re.sub(
            r"<(script|style|template|noscript)\b[^>]*>.*?</\1>",
            " ",
            decoded,
            flags=re.IGNORECASE | re.DOTALL,
        )
        text = " ".join(re.sub(r"<[^>]+>", " ", without_blocked).split())
        if not text:
            raise WebExtractionError("fixture document extraction produced no text")
        return ExtractedWebPage(
            title=title,
            text=text,
            metadata={"canonical_url": final_url},
        )


def local_search_stack(
    tmp_path: Path,
    model: object,
    fetcher: object,
    *,
    limits: LocalDiscoveryLimits | None = None,
    extractor: object | None = None,
) -> tuple[Database, SessionService, DocumentService, RAGService, LocalDiscoveryProvider, SearchService]:
    db = Database(tmp_path / "profile")
    sessions = SessionService(db)
    documents = DocumentService(db)
    rag = RAGService(
        documents,
        EmbeddingService(db, provider=LocalHashEmbeddingProvider()),
        SQLiteNumPyVectorStore(db),
    )
    provider = LocalDiscoveryProvider(db, limits=limits)
    service = SearchService(
        db,
        sessions,
        model,
        documents,
        rag,
        provider,
        fetcher=fetcher,
        extractor=extractor or FixtureHTMLExtractor(),
    )
    return db, sessions, documents, rag, provider, service


def test_fts5_available_and_v11_profile_migrates(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    legacy = sqlite3.connect(profile / "app.db")
    legacy.execute("CREATE TABLE app_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER NOT NULL)")
    legacy.execute("INSERT INTO app_meta(key, value, updated_at) VALUES ('schema_version', '11', 1)")
    legacy.commit()
    legacy.close()
    migrated = Database(profile)
    try:
        assert SCHEMA_VERSION == 14
        assert migrated.conn.execute("SELECT value FROM app_meta WHERE key='schema_version'").fetchone()[0] == "14"
        assert LocalSearchIndex(migrated).fts5_available()
    finally:
        migrated.close()


def test_fts_insert_update_delete_sync_and_exact_identifier(tmp_path: Path) -> None:
    db, documents, rag, index = stack(tmp_path)
    try:
        document = add_document(
            tmp_path,
            documents,
            rag,
            "security.md",
            "Incident catalog",
            "The affected release is CVE-2026-12345. Nearby qualifier: only Windows builds are affected.",
        )
        first = index.search("CVE-2026-12345")
        assert first[0].document_id == document["id"]
        assert "only Windows builds" in first[0].content
        chunk_id = first[0].chunk_id
        db.conn.execute("UPDATE rag_chunks SET content = 'Replacement identifier SKU-A17-B' WHERE id = ?", (chunk_id,))
        db.conn.commit()
        assert index.search("CVE-2026-12345") == []
        assert index.search("SKU-A17-B")[0].chunk_id == chunk_id
        db.conn.execute("UPDATE rag_chunks SET is_deleted = 1 WHERE id = ?", (chunk_id,))
        db.conn.commit()
        assert index.search("SKU-A17-B") == []
    finally:
        db.close()


def test_fts_excludes_staged_and_stale_web_but_includes_current_web(tmp_path: Path) -> None:
    db, documents, rag, index = stack(tmp_path)
    try:
        staged = add_document(tmp_path, documents, rag, "staged.md", "Staged", "STAGED-SECRET-77")
        stale = add_document(tmp_path, documents, rag, "stale.md", "Old Web", "OLD-REVISION-88")
        current = add_document(tmp_path, documents, rag, "current.md", "Current Web", "CURRENT-REVISION-99")
        db.conn.execute("UPDATE documents SET is_staging = 1 WHERE id = ?", (staged["id"],))
        db.conn.execute(
            "UPDATE documents SET source_origin='web', canonical_url='https://example.com/page', web_revision_current=0 WHERE id = ?",
            (stale["id"],),
        )
        db.conn.execute(
            "UPDATE documents SET source_origin='cached_web', canonical_url='https://example.com/page', web_revision_current=1 WHERE id = ?",
            (current["id"],),
        )
        db.conn.commit()
        assert index.search("STAGED-SECRET-77") == []
        assert index.search("OLD-REVISION-88") == []
        assert index.search("CURRENT-REVISION-99")[0].document_id == current["id"]
    finally:
        db.close()


def test_fts_bm25_title_weighting_is_deterministic(tmp_path: Path) -> None:
    db, documents, rag, index = stack(tmp_path)
    try:
        title_hit = add_document(tmp_path, documents, rag, "a.md", "Quasarneedle handbook", "A short general manual.")
        add_document(tmp_path, documents, rag, "b.md", "General handbook", "Quasarneedle appears once in this general body.")
        results = index.search("quasarneedle")
        assert results[0].document_id == title_hit["id"]
        assert [row.chunk_id for row in results] == [row.chunk_id for row in index.search("quasarneedle")]
        assert results[0].raw_lexical_rank <= results[1].raw_lexical_rank
    finally:
        db.close()


def test_hybrid_rrf_preserves_lexical_and_dense_rescues(tmp_path: Path) -> None:
    db, documents, rag, index = stack(tmp_path)
    try:
        exact = add_document(tmp_path, documents, rag, "exact.md", "Parts", "Use SKU-A17-B for the blue assembly.")
        semantic = add_document(tmp_path, documents, rag, "semantic.md", "Policy", "Employees may work away from the office two days weekly.")
        exact_chunk = documents.chunks(exact["id"])[0]
        semantic_chunk = documents.chunks(semantic["id"])[0]

        class DenseFixture:
            def search_with_audit(self, query: str, **_kwargs: object) -> dict:
                chunk = semantic_chunk if "telecommute" in query else exact_chunk
                return {
                    "results": [SimpleNamespace(
                        chunk_id=chunk["id"], document_id=chunk["document_id"], content=chunk["content"],
                        score=0.91, page_start=chunk["page_start"], page_end=chunk["page_end"], metadata=chunk["metadata"],
                    )],
                    "embedding_backend": "fixture-semantic",
                    "embedding_model": "fixture",
                }

        exact_result = index.hybrid_search("SKU-A17-B", DenseFixture(), limit=4)
        exact_row = next(row for row in exact_result["results"] if row.chunk_id == exact_chunk["id"])
        assert exact_row.lexical_rank == 1
        semantic_result = index.hybrid_search("telecommute allowance", DenseFixture(), limit=4)
        semantic_row = next(row for row in semantic_result["results"] if row.chunk_id == semantic_chunk["id"])
        assert semantic_row.dense_rank == 1
        assert semantic_row.lexical_rank is None
        assert exact_result["fused_candidates"] >= 1
    finally:
        db.close()


def test_frontier_canonical_dedup_multiple_relationships_and_unfetched_search(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db)
        html_one = """<html><body><p>Python tools <a href='/library/sqlite3.html'>SQLite database interface</a> guide.</p></body></html>"""
        html_two = """<html><body><nav><a href='https://example.com/library/sqlite3.html?utm_source=x'>sqlite3 module</a></nav></body></html>"""
        frontier.discover_links(source_url="https://example.com/start", html_body=html_one, source_title="Start", source_depth=0)
        frontier.discover_links(source_url="https://example.com/other", html_body=html_two, source_title="Other", source_depth=0)
        rows = db.conn.execute("SELECT * FROM crawl_frontier").fetchall()
        assert len(rows) == 1
        assert rows[0]["canonical_url"] == "https://example.com/library/sqlite3.html"
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_discoveries").fetchone()[0] == 2
        matches = frontier.search("Python SQLite database interface")
        assert matches[0]["status"] == "unfetched"
        assert "evidence" not in matches[0]
        assert "destination page text" not in matches[0]["surrounding_text"]
    finally:
        db.close()


def test_frontier_limits_failure_restart_and_cancellation(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    db = Database(profile)
    frontier = FrontierStore(db)
    for index in range(5):
        frontier.discover(f"https://example.com/{index}", discovery_kind="manual_seed", anchor_text=f"needle {index}")
    provider = LocalDiscoveryProvider(db, limits=LocalDiscoveryLimits(max_new_urls=2, max_urls_per_domain=1))
    candidates = [
        {"canonical_url": f"https://example.com/{index}"} for index in range(3)
    ] + [{"canonical_url": "https://other.example/1"}]
    bounded = provider.bound_candidates(candidates, 8)
    assert [row["canonical_url"] for row in bounded] == ["https://example.com/0", "https://other.example/1"]
    frontier.mark_fetch_failure("https://example.com/0", "fetch_failed")
    event = threading.Event()
    event.set()
    with cancellation_scope(event), pytest.raises(JobCancelledError):
        frontier.discover_links(
            source_url="https://example.com/", html_body="<a href='/x'>x</a>", source_title="x", source_depth=0
        )
    db.close()
    reopened = Database(profile)
    try:
        row = reopened.conn.execute(
            "SELECT status, failure_code, attempt_count, next_retry_at FROM crawl_frontier WHERE canonical_url=?",
            ("https://example.com/0",),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["failure_code"] == "fetch_failed"
        assert row["attempt_count"] == 1
        assert int(row["next_retry_at"]) > int(time.time() * 1000)
        assert "https://example.com/0" not in {
            candidate["canonical_url"] for candidate in FrontierStore(reopened).search("needle 0")
        }
    finally:
        reopened.close()


def test_navigation_link_parser_is_bounded_rejects_unsafe_and_honors_deadline(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db)
        body = """<html><head><title>Navigation Map</title></head><body>
        <a href='/one'>one needle</a><a href='/two'>two needle</a><a href='/three'>three needle</a>
        </body></html>"""
        rows = frontier.discover_links(
            source_url="https://example.com/start",
            html_body=body,
            source_title="Fallback",
            source_depth=0,
            max_links=2,
        )
        assert [row["canonical_url"] for row in rows] == [
            "https://example.com/one",
            "https://example.com/two",
        ]
        assert all(row["source_title"] == "Navigation Map" for row in rows)

        unsafe = frontier.discover_links(
            source_url="https://example.com/start",
            html_body="""<a href='javascript:alert(1)'>bad scheme</a>
            <a href='https://user:pass@example.com/private'>credentials</a>
            <a href='http://127.0.0.1/private'>literal private host</a>""",
            source_title="Unsafe",
            source_depth=0,
        )
        assert unsafe == []
        with pytest.raises(LocalBudgetExhausted):
            frontier.discover_links(
                source_url="https://example.com/start",
                html_body="<a href='/late'>late</a>",
                source_title="Late",
                source_depth=0,
                deadline=time.monotonic() - 1,
            )
        cancelled = threading.Event()
        cancelled.set()
        with cancellation_scope(cancelled), pytest.raises(JobCancelledError):
            frontier.discover_links(
                source_url="https://example.com/start",
                html_body="<a href='/cancelled'>cancelled</a>",
                source_title="Cancelled",
                source_depth=0,
            )
    finally:
        db.close()


def test_sitemap_index_simple_malformed_oversize_and_unsafe(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db)
        parser = DiscoveryParser(db, frontier, limits=LocalDiscoveryLimits(max_sitemap_entries=2))
        simple = "<urlset><url><loc>https://example.com/a</loc></url><url><loc>file:///bad</loc></url></urlset>"
        assert [row["canonical_url"] for row in parser.parse_sitemap(simple, source_url="https://example.com/sitemap.xml")] == ["https://example.com/a"]
        index = "<sitemapindex><sitemap><loc>https://example.com/one.xml</loc></sitemap><sitemap><loc>https://example.com/two.xml</loc></sitemap><sitemap><loc>https://example.com/three.xml</loc></sitemap></sitemapindex>"
        rows = parser.parse_sitemap(index, source_url="https://example.com/root.xml")
        assert len(rows) == 2
        assert all(json.loads(row["metadata_json"])["container"] for row in rows)
        assert parser.parse_sitemap("<broken", source_url="https://example.com/nope.xml") == []
    finally:
        db.close()


def test_rss_atom_duplicates_updates_and_metadata_is_not_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db)
        parser = DiscoveryParser(db, frontier, limits=LocalDiscoveryLimits(max_feed_entries=3))
        rss = """<rss><channel><title>Releases</title><item><title>Version A</title><link>https://example.com/a</link><description>First summary</description><pubDate>2026-01-01</pubDate></item><item><title>Bad</title><link>javascript:bad</link></item></channel></rss>"""
        assert len(parser.parse_feed(rss, source_url="https://example.com/feed.xml")) == 1
        updated = rss.replace("First summary", "Updated summary")
        parser.parse_feed(updated, source_url="https://example.com/feed.xml")
        assert db.conn.execute("SELECT COUNT(*) FROM discovery_feed_entries").fetchone()[0] == 1
        assert db.conn.execute("SELECT summary FROM discovery_feed_entries").fetchone()[0] == "Updated summary"
        atom = """<feed xmlns='http://www.w3.org/2005/Atom'><title>Atom feed</title><entry><title>Entry B</title><link href='https://example.org/b'/><updated>2026-02-02</updated><summary>Metadata only</summary></entry></feed>"""
        assert parser.parse_feed(atom, source_url="https://example.org/feed.atom")[0]["discovery_kind"] == "atom"
        assert db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM search_evidence").fetchone()[0] == 0
    finally:
        db.close()


def test_source_pack_parse_match_dedup_and_no_evidence_or_model_call(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db)
        store = SourcePackStore(db, frontier)
        pack = store.load(FIXTURES / "source_packs" / "potatocs-fixture.md")
        store.load(FIXTURES / "source_packs" / "potatocs-fixture.md")
        assert pack.title == "PotatoCS Fixture"
        assert store.matching("PotatoCS fixture discovery")[0]["title"] == pack.title
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier").fetchone()[0] == 0
        store.activate(pack)
        store.activate(pack)
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE canonical_url='https://fixture.example/start'").fetchone()[0] == 1
        assert db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM search_evidence").fetchone()[0] == 0
        malformed = tmp_path / "bad.md"
        malformed.write_text("---\ntype: wrong\ntitle: Bad\n---\n- file:///bad", encoding="utf-8")
        with pytest.raises(ValueError, match="type"):
            store.parse(malformed)
    finally:
        db.close()


def test_source_pack_unsafe_schemes_are_rejected(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        path = tmp_path / "unsafe.md"
        path.write_text(
            "---\ntype: source-pack\ntitle: Unsafe\ntags:\n  - test\n---\n\n# Seeds\n\n- file:///etc/passwd\n- https://example.com/safe\n",
            encoding="utf-8",
        )
        pack = SourcePackStore(db, FrontierStore(db)).load(path)
        assert pack.seeds == ("https://example.com/safe",)
    finally:
        db.close()


def test_robots_uses_safe_fetcher_and_discovers_sitemap(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db)
        parser = DiscoveryParser(db, frontier)

        class FixtureFetcher:
            def fetch(self, url: str, **_kwargs: object) -> FetchResponse:
                body = b"User-agent: *\nDisallow: /private\nSitemap: https://example.com/sitemap.xml\n"
                return FetchResponse(url, url, 200, {"content-type": "text/plain"}, body, len(body), 0, 1)

        assert parser.robots_allows("https://example.com/public", FixtureFetcher())
        assert not parser.robots_allows("https://example.com/private/secret", FixtureFetcher())
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE discovery_kind='sitemap'").fetchone()[0] == 1
        blocked_fetcher = SafeHttpFetcher(resolver=lambda _host, _port: ["127.0.0.1"])
        assert not parser.robots_allows("https://blocked.example/page", blocked_fetcher)
    finally:
        db.close()


def test_frontier_fetch_promotion_changes_candidate_not_evidence_contract(tmp_path: Path) -> None:
    db, documents, rag, index = stack(tmp_path)
    try:
        frontier = FrontierStore(db)
        row = frontier.discover("https://example.com/page", discovery_kind="manual_seed", anchor_text="Decisive page")
        assert row["status"] == "unfetched"
        document = add_document(tmp_path, documents, rag, "page.md", "Decisive page", "Verified destination text 4242.")
        db.conn.execute(
            "UPDATE documents SET source_origin='cached_web', canonical_url='https://example.com/page', web_revision_current=1 WHERE id=?",
            (document["id"],),
        )
        db.conn.commit()
        fetch = SimpleNamespace(headers={"etag": "v1", "last-modified": ""})
        frontier.mark_fetch_success("https://example.com/page", fetch, "hash")
        assert db.conn.execute("SELECT status FROM crawl_frontier WHERE id=?", (row["id"],)).fetchone()[0] == "fetched"
        assert index.search("Verified destination text")[0].document_id == document["id"]
        assert db.conn.execute("SELECT COUNT(*) FROM search_evidence").fetchone()[0] == 0
    finally:
        db.close()


def test_fts_expression_is_bounded_and_quoted() -> None:
    expression = fts_match_expression("CVE-2026-12345 sqlite3.Connection " + "word " * 100)
    assert '"cve-2026-12345"' in expression
    assert expression.count(" OR ") < 20


def _html(title: str, text: str, links: str = "") -> bytes:
    return (
        f"<html><head><title>{title}</title></head><body><main><h1>{title}</h1>"
        f"<p>{text}</p>{links}</main></body></html>"
    ).encode("utf-8")


def _latest_metrics(db: Database) -> dict[str, object]:
    row = db.conn.execute("SELECT metrics_json FROM search_runs ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
    assert row is not None
    return json.loads(row["metrics_json"])


def _make_cached_web(
    tmp_path: Path,
    db: Database,
    documents: DocumentService,
    rag: RAGService,
    frontier: FrontierStore,
    *,
    name: str,
    url: str,
    text: str,
    current: bool = True,
) -> dict:
    document = add_document(tmp_path, documents, rag, f"{name}.md", name, text)
    db.conn.execute(
        """
        UPDATE documents
        SET source_origin='cached_web', canonical_url=?, final_url=?,
            fetched_at=1, web_revision_current=?
        WHERE id=?
        """,
        (url, url, 1 if current else 0, document["id"]),
    )
    db.conn.commit()
    frontier.discover(url, discovery_kind="manual_seed", anchor_text=f"{name} reacquisition")
    frontier.mark_fetch_success(url, {"etag": f"{name}-etag", "last-modified": "yesterday"}, f"{name}-hash")
    return documents.get(document["id"])


def test_deleted_cached_observation_releases_frontier_and_can_be_reacquired(tmp_path: Path) -> None:
    db, documents, rag, _index = stack(tmp_path)
    sessions = SessionService(db)
    frontier = FrontierStore(db)
    url = "https://fixture.example/reacquire"
    try:
        document = _make_cached_web(
            tmp_path, db, documents, rag, frontier,
            name="reacquire", url=url, text="The earlier cached observation.",
        )
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url=?", (url,)
        ).fetchone()[0] == "fetched"
        rag.delete_document(document["id"])
        released = db.conn.execute(
            """
            SELECT status, failure_code, last_fetch_at, etag, last_modified,
                   content_hash, attempt_count, last_attempt_at, next_retry_at
            FROM crawl_frontier WHERE canonical_url=?
            """,
            (url,),
        ).fetchone()
        assert dict(released) == {
            "status": "unfetched", "failure_code": "", "last_fetch_at": None,
            "etag": "", "last_modified": "", "content_hash": "", "attempt_count": 0,
            "last_attempt_at": None, "next_retry_at": None,
        }
        assert url in [row["canonical_url"] for row in frontier.search("reacquire")]

        fetcher = LocalFixtureFetcher({url: _html("Reacquired", "The reacquired destination observation.")})
        provider = LocalDiscoveryProvider(db)
        service = SearchService(db, sessions, EmptyEvidenceModel(), documents, rag, provider, fetcher=fetcher)
        session = sessions.create("reacquire", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="reacquire observation", session_id=session["id"], model="fixture", second_round_enabled=False)
        assert fetcher.calls.count(url) == 1
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url=?", (url,)
        ).fetchone()[0] == "fetched"
        assert db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE canonical_url=? AND is_deleted=0 AND web_revision_current=1", (url,)
        ).fetchone()[0] == 1
    finally:
        db.close()


def test_purge_stale_revision_and_local_source_have_correct_frontier_effects(tmp_path: Path) -> None:
    db, documents, rag, _index = stack(tmp_path)
    frontier = FrontierStore(db)
    current_url = "https://fixture.example/current"
    local_frontier_url = "https://fixture.example/unrelated"
    try:
        current = _make_cached_web(
            tmp_path, db, documents, rag, frontier,
            name="current", url=current_url, text="Current web revision.", current=True,
        )
        stale = _make_cached_web(
            tmp_path, db, documents, rag, frontier,
            name="stale", url=current_url, text="Historical web revision.", current=False,
        )
        documents.purge_document(stale["id"])
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url=?", (current_url,)
        ).fetchone()[0] == "fetched"

        local = add_document(tmp_path, documents, rag, "local.md", "Local", "An ordinary local Source.")
        frontier.discover(local_frontier_url, discovery_kind="manual_seed", anchor_text="unrelated local")
        frontier.mark_fetch_success(local_frontier_url, {"etag": "u"}, "u-hash")
        rag.delete_document(local["id"])
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url=?", (local_frontier_url,)
        ).fetchone()[0] == "fetched"

        documents.purge_document(current["id"])
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url=?", (current_url,)
        ).fetchone()[0] == "unfetched"
    finally:
        db.close()


def test_nested_sitemap_production_path_reaches_page_without_container_evidence(tmp_path: Path) -> None:
    root_url = "https://maps.example/root-sitemap.xml"
    child_url = "https://maps.example/child-sitemap.xml"
    page_url = "https://maps.example/final-page-fixture"
    root_xml = f"<sitemapindex><sitemap><loc>{child_url}</loc></sitemap></sitemapindex>".encode()
    child_xml = f"<urlset><url><loc>{page_url}</loc></url></urlset>".encode()
    fetcher = LocalFixtureFetcher(
        {
            root_url: (root_xml, "application/xml"),
            child_url: (child_xml, "application/xml"),
            page_url: _html("Final Page", "A destination page reached through nested sitemap containers."),
            "https://maps.example/sitemap.xml": (b"<broken", "application/xml"),
        },
        default_robots=b"User-agent: *\nAllow: /\n",
    )
    limits = LocalDiscoveryLimits(max_new_urls=1, max_sitemap_entries=2, max_urls_per_domain=8)
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path, EmptyEvidenceModel(), fetcher, limits=limits
    )
    try:
        provider.frontier.discover(
            root_url,
            discovery_kind="sitemap",
            anchor_text="root sitemap fixture",
            metadata={"container": True},
        )
        for question in ("root sitemap fixture", "child sitemap", "final-page-fixture"):
            session = sessions.create(question, "fixture")
            with pytest.raises(SearchNoEvidenceError):
                service.run(question=question, session_id=session["id"], model="fixture", second_round_enabled=False)
        states = {
            row["canonical_url"]: row["status"]
            for row in db.conn.execute(
                "SELECT canonical_url, status FROM crawl_frontier WHERE canonical_url IN (?, ?, ?)",
                (root_url, child_url, page_url),
            ).fetchall()
        }
        assert states == {root_url: "fetched", child_url: "fetched", page_url: "fetched"}
        child_metadata = json.loads(db.conn.execute(
            "SELECT metadata_json FROM crawl_frontier WHERE canonical_url=?", (child_url,)
        ).fetchone()[0])
        assert child_metadata["container"] is True
        assert db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE canonical_url IN (?, ?)", (root_url, child_url)
        ).fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM search_evidence").fetchone()[0] == 0
        assert fetcher.calls.count(root_url) == fetcher.calls.count(child_url) == fetcher.calls.count(page_url) == 1
    finally:
        db.close()


def test_sitemap_cycle_terminates_without_container_evidence(tmp_path: Path) -> None:
    first_url = "https://cycle.example/cycle-a.xml"
    second_url = "https://cycle.example/cycle-b.xml"
    first_xml = f"<sitemapindex><sitemap><loc>{second_url}</loc></sitemap></sitemapindex>".encode()
    second_xml = f"<sitemapindex><sitemap><loc>{first_url}</loc></sitemap></sitemapindex>".encode()
    fetcher = LocalFixtureFetcher(
        {
            first_url: (first_xml, "application/xml"),
            second_url: (second_xml, "application/xml"),
            "https://cycle.example/sitemap.xml": (b"<broken", "application/xml"),
        },
        default_robots=b"User-agent: *\nAllow: /\n",
    )
    db, sessions, _documents, _rag, provider, service = local_search_stack(tmp_path, EmptyEvidenceModel(), fetcher)
    try:
        provider.frontier.discover(
            first_url, discovery_kind="sitemap", anchor_text="cycle a", metadata={"container": True}
        )
        for question in ("cycle a", "cycle b", "cycle a"):
            session = sessions.create(question, "fixture")
            with pytest.raises(SearchNoEvidenceError):
                service.run(question=question, session_id=session["id"], model="fixture", second_round_enabled=False)
        assert fetcher.calls.count(first_url) == 1
        assert fetcher.calls.count(second_url) == 1
        assert db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE status='fetched' AND canonical_url IN (?, ?)",
                               (first_url, second_url)).fetchone()[0] == 2
    finally:
        db.close()


def test_aggregate_byte_exhaustion_leaves_uncontacted_candidate_immediately_eligible(tmp_path: Path) -> None:
    url = "https://capacity.example/page"
    robots_url = "https://capacity.example/robots.txt"
    limits = LocalDiscoveryLimits(max_new_urls=1, max_total_bytes=64 * 1024)
    prefix = b"User-agent: *\nAllow: /\n"
    robots = prefix + (b"#" * (limits.max_total_bytes - len(prefix)))
    fetcher = LocalFixtureFetcher({robots_url: (robots, "text/plain"), url: _html("Page", "healthy")})
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path, EmptyEvidenceModel(), fetcher, limits=limits
    )
    try:
        provider.frontier.discover(url, discovery_kind="hyperlink", anchor_text="capacity candidate")
        session = sessions.create("capacity", "fixture")
        with pytest.raises(SearchBudgetError):
            service.run(question="capacity candidate", session_id=session["id"], model="fixture", second_round_enabled=False)
        row = db.conn.execute(
            "SELECT status, attempt_count, next_retry_at FROM crawl_frontier WHERE canonical_url=?", (url,)
        ).fetchone()
        assert dict(row) == {"status": "unfetched", "attempt_count": 0, "next_retry_at": None}
        assert fetcher.calls == [robots_url]
        assert url in [candidate["canonical_url"] for candidate in provider.frontier.search("capacity candidate")]
    finally:
        db.close()


def test_genuine_normal_response_limit_violation_remains_remote_failure(tmp_path: Path) -> None:
    url = "https://oversize.example/page"
    fetcher = LocalFixtureFetcher({url: _html("Oversize", "x" * (80 * 1024))})
    fetcher.max_response_bytes = 64 * 1024
    limits = LocalDiscoveryLimits(max_new_urls=1, max_total_bytes=128 * 1024)
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path, EmptyEvidenceModel(), fetcher, limits=limits
    )
    try:
        provider.frontier.discover(url, discovery_kind="manual_seed", anchor_text="oversize response")
        session = sessions.create("oversize", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="oversize response", session_id=session["id"], model="fixture", second_round_enabled=False)
        row = db.conn.execute(
            "SELECT status, failure_code, attempt_count, next_retry_at FROM crawl_frontier WHERE canonical_url=?", (url,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["failure_code"] == "fetch_limit"
        assert row["attempt_count"] == 1
        assert int(row["next_retry_at"]) > 0
    finally:
        db.close()


def test_repair_round_aggregate_exhaustion_does_not_poison_candidate(tmp_path: Path) -> None:
    first_url = "https://repair.example/initial"
    second_url = "https://repair.example/secondary"
    limits = LocalDiscoveryLimits(max_new_urls=2, max_total_bytes=64 * 1024, max_urls_per_domain=8)
    first_body = _html("Initial", "x" * (55 * 1024))
    second_body = _html("Secondary", "y" * (20 * 1024))
    fetcher = LocalFixtureFetcher({first_url: first_body, second_url: second_body})
    model = EmptyEvidenceModel(request_second_round=True, repair_query="secondary repair omega")
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path, model, fetcher, limits=limits
    )
    try:
        provider.frontier.discover(
            first_url, discovery_kind="manual_seed", anchor_text="initial capacity alpha"
        )
        provider.frontier.discover(
            second_url, discovery_kind="manual_seed", anchor_text="secondary repair omega"
        )
        session = sessions.create("repair", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="initial capacity alpha", session_id=session["id"], model="fixture", second_round_enabled=True)
        second = db.conn.execute(
            "SELECT status, attempt_count, next_retry_at FROM crawl_frontier WHERE canonical_url=?", (second_url,)
        ).fetchone()
        assert dict(second) == {"status": "unfetched", "attempt_count": 0, "next_retry_at": None}
        assert fetcher.calls.count(second_url) == 1
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url=?", (first_url,)
        ).fetchone()[0] == "fetched"
    finally:
        db.close()


def test_robots_cache_is_independent_by_origin_and_normalizes_default_ports(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        parser = DiscoveryParser(db, FrontierStore(db))
        outcomes = {
            "https://dual.example/robots.txt": (b"User-agent: *\nAllow: /", "text/plain"),
            "http://dual.example/robots.txt": (b"User-agent: *\nDisallow: /", "text/plain"),
            "http://inverse.example/robots.txt": (b"User-agent: *\nAllow: /", "text/plain"),
            "https://inverse.example/robots.txt": (b"User-agent: *\nDisallow: /", "text/plain"),
        }
        fetcher = LocalFixtureFetcher(outcomes)
        assert parser.robots_allows("https://dual.example/page", fetcher)
        assert not parser.robots_allows("http://dual.example/page", fetcher)
        assert parser.robots_allows("http://inverse.example/page", fetcher)
        assert not parser.robots_allows("https://inverse.example/page", fetcher)

        assert parser.robots_allows("https://dual.example:443/another", fetcher)
        assert not parser.robots_allows("http://dual.example:80/another", fetcher)
        assert fetcher.calls.count("https://dual.example/robots.txt") == 1
        assert fetcher.calls.count("http://dual.example/robots.txt") == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM robots_cache WHERE domain IN ('https://dual.example', 'http://dual.example')"
        ).fetchone()[0] == 2
    finally:
        db.close()


def test_local_no_evidence_promotes_observation_and_repeat_reuses_without_refetch(tmp_path: Path) -> None:
    url = "https://fixture.example/calibration"
    fetcher = LocalFixtureFetcher({url: _html("Calibration", "A retained local observation with no selected quote.")})
    db, sessions, _documents, _rag, provider, service = local_search_stack(tmp_path, EmptyEvidenceModel(), fetcher)
    try:
        provider.frontier.discover(url, discovery_kind="manual_seed", anchor_text="calibration archive")
        first = sessions.create("first", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="calibration archive", session_id=first["id"], model="fixture", second_round_enabled=False)

        document = db.conn.execute(
            "SELECT is_staging, web_revision_current, source_origin FROM documents WHERE canonical_url = ?", (url,)
        ).fetchone()
        frontier = db.conn.execute(
            "SELECT status, failure_code, next_retry_at FROM crawl_frontier WHERE canonical_url = ?", (url,)
        ).fetchone()
        assert dict(document) == {"is_staging": 0, "web_revision_current": 1, "source_origin": "cached_web"}
        assert dict(frontier) == {"status": "fetched", "failure_code": "", "next_retry_at": None}
        assert fetcher.calls.count(url) == 1

        second = sessions.create("second", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="calibration archive", session_id=second["id"], model="fixture", second_round_enabled=False)
        assert fetcher.calls.count(url) == 1
    finally:
        db.close()


def test_local_second_round_cannot_refetch_same_page_and_shares_page_budget(tmp_path: Path) -> None:
    urls = ["https://fixture.example/one", "https://fixture.example/two", "https://fixture.example/three"]
    outcomes = {url: _html(f"Page {index}", "shared calibration details without selected evidence") for index, url in enumerate(urls)}
    limits = LocalDiscoveryLimits(max_new_urls=2, max_urls_per_domain=8)
    model = EmptyEvidenceModel(request_second_round=True, repair_query="secondary calibration details")
    db, sessions, _documents, _rag, provider, service = local_search_stack(tmp_path, model, LocalFixtureFetcher(outcomes), limits=limits)
    try:
        for url in urls:
            provider.frontier.discover(
                url,
                discovery_kind="manual_seed",
                anchor_text="calibration archive secondary details",
            )
        session = sessions.create("budget", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="calibration archive", session_id=session["id"], model="fixture", second_round_enabled=True)
        metrics = _latest_metrics(db)
        assert metrics["round_count"] == 2
        assert metrics["page_fetches"] == 2
        assert metrics["network_requests_total"] == 2
        assert sum(outcomes_url in service.fetcher.calls for outcomes_url in urls) == 2
        assert all(service.fetcher.calls.count(url) <= 1 for url in urls)
    finally:
        db.close()


def test_failed_extraction_persistence_and_cancellation_do_not_promote_or_mark_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failed_url = "https://fixture.example/empty"
    failed_fetcher = LocalFixtureFetcher({failed_url: b"<html><body><script>empty</script></body></html>"})
    db, sessions, _documents, _rag, provider, service = local_search_stack(tmp_path, EmptyEvidenceModel(), failed_fetcher)
    try:
        provider.frontier.discover(failed_url, discovery_kind="manual_seed", anchor_text="empty extraction")
        session = sessions.create("failure", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(question="empty extraction", session_id=session["id"], model="fixture", second_round_enabled=False)
        row = db.conn.execute(
            "SELECT status, failure_code, next_retry_at FROM crawl_frontier WHERE canonical_url = ?", (failed_url,)
        ).fetchone()
        assert row["status"] == "failed"
        assert row["failure_code"] == "extraction_failed"
        assert int(row["next_retry_at"]) > 0
        assert db.conn.execute("SELECT COUNT(*) FROM documents WHERE canonical_url = ?", (failed_url,)).fetchone()[0] == 0
    finally:
        db.close()

    persistence_url = "https://fixture.example/persistence"
    persistence_fetcher = LocalFixtureFetcher(
        {persistence_url: _html("Persistence", "A complete extraction that cannot be persisted.")}
    )
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path / "persistence-profile", EmptyEvidenceModel(), persistence_fetcher
    )
    try:
        provider.frontier.discover(
            persistence_url, discovery_kind="manual_seed", anchor_text="persistence failure"
        )

        def fail_persist(**_kwargs: object) -> object:
            raise WebSourceStoreError("fixture persistence failure")

        monkeypatch.setattr(service.web_sources, "persist", fail_persist)
        session = sessions.create("persistence", "fixture")
        with pytest.raises(WebSourceStoreError):
            service.run(
                question="persistence failure",
                session_id=session["id"],
                model="fixture",
                second_round_enabled=False,
            )
        row = db.conn.execute(
            "SELECT status, failure_code FROM crawl_frontier WHERE canonical_url = ?", (persistence_url,)
        ).fetchone()
        assert dict(row) == {"status": "failed", "failure_code": "search_cache_failed"}
        assert db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE canonical_url = ?", (persistence_url,)
        ).fetchone()[0] == 0
    finally:
        db.close()

    cancel_url = "https://fixture.example/cancel"
    cancel_event = threading.Event()
    cancel_fetcher = LocalFixtureFetcher({cancel_url: _html("Cancel", "This observation is cancelled before promotion.")})
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path / "cancel-profile", EmptyEvidenceModel(cancel_on_selection=cancel_event), cancel_fetcher
    )
    try:
        provider.frontier.discover(cancel_url, discovery_kind="manual_seed", anchor_text="cancel observation")
        session = sessions.create("cancel", "fixture")
        with cancellation_scope(cancel_event), pytest.raises(JobCancelledError):
            service.run(question="cancel observation", session_id=session["id"], model="fixture", second_round_enabled=False)
        assert db.conn.execute("SELECT COUNT(*) FROM documents WHERE canonical_url = ?", (cancel_url,)).fetchone()[0] == 0
        assert db.conn.execute(
            "SELECT status FROM crawl_frontier WHERE canonical_url = ?", (cancel_url,)
        ).fetchone()[0] == "unfetched"
    finally:
        db.close()


def test_extraction_failed_navigation_discovers_and_fetches_verified_destination(tmp_path: Path) -> None:
    navigation_url = "https://sqlite.example/docs.html"
    target_url = "https://sqlite.example/wal.html"
    quote = "The default automatic checkpoint threshold is 1000 pages."
    navigation = _html(
        "SQLite Documentation",
        "SQLite default autocheckpoint documentation index.",
        "<a href='/wal.html'>Write-Ahead Log (WAL) Mode</a>",
    )
    target = _html("WAL", quote)
    fetcher = LocalFixtureFetcher(
        {navigation_url: navigation, target_url: target},
        default_robots=b"User-agent: *\nAllow: /\n",
    )
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path,
        QuoteEvidenceModel(quote),
        fetcher,
        limits=LocalDiscoveryLimits(max_new_urls=3, max_urls_per_domain=3),
        extractor=FixtureHTMLExtractor(fail_urls={navigation_url}),
    )
    try:
        provider.frontier.discover(
            navigation_url,
            discovery_kind="manual_seed",
            anchor_text="SQLite default WAL autocheckpoint threshold",
        )
        session = sessions.create("navigation", "fixture")
        result = service.run(
            question="What is the default SQLite WAL autocheckpoint threshold?",
            session_id=session["id"],
            model="fixture",
            second_round_enabled=False,
        )
        metrics = result["metrics"]
        assert metrics["extraction_failures"] == 1
        assert metrics["links_discovered_after_extraction_failure"] >= 1
        assert metrics["page_fetches"] == 2
        assert db.conn.execute(
            "SELECT COUNT(*) FROM documents WHERE canonical_url=?", (navigation_url,)
        ).fetchone()[0] == 0
        target_document = db.conn.execute(
            "SELECT id, source_origin, is_staging FROM documents WHERE canonical_url=?", (target_url,)
        ).fetchone()
        assert dict(target_document) == {
            "id": target_document["id"],
            "source_origin": "cached_web",
            "is_staging": 0,
        }
        assert db.conn.execute(
            "SELECT COUNT(*) FROM rag_chunks WHERE document_id=? AND is_deleted=0",
            (target_document["id"],),
        ).fetchone()[0] >= 1
        evidence = db.conn.execute("SELECT exact_quote FROM search_evidence").fetchall()
        assert [row["exact_quote"] for row in evidence] == [quote]
        assert result["citations"][0]["canonical_url"] == target_url
        assert fetcher.calls.count(navigation_url) == fetcher.calls.count(target_url) == 1
    finally:
        db.close()


def test_successful_html_extraction_still_persists_and_discovers_link(tmp_path: Path) -> None:
    navigation_url = "https://success.example/docs.html"
    target_url = "https://success.example/target.html"
    quote = "The verified destination value is SUCCESS-417."
    fetcher = LocalFixtureFetcher(
        {
            navigation_url: _html(
                "Successful Navigation",
                "An ordinary successfully extractable navigation page.",
                "<a href='/target.html'>verified destination value</a>",
            ),
            target_url: _html("Destination", quote),
        },
        default_robots=b"User-agent: *\nAllow: /\n",
    )
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path,
        QuoteEvidenceModel(quote),
        fetcher,
        limits=LocalDiscoveryLimits(max_new_urls=3, max_urls_per_domain=3),
    )
    try:
        provider.frontier.discover(
            navigation_url,
            discovery_kind="manual_seed",
            anchor_text="verified destination value documentation",
        )
        session = sessions.create("success", "fixture")
        result = service.run(
            question="What is the verified destination value?",
            session_id=session["id"],
            model="fixture",
            second_round_enabled=False,
        )
        metrics = result["metrics"]
        assert metrics["extraction_successes"] == 2
        assert metrics["extraction_failures"] == 0
        assert metrics["links_discovered"] >= 1
        assert metrics["links_discovered_after_extraction_failure"] == 0
        navigation_document = db.conn.execute(
            "SELECT id, source_origin, is_staging FROM documents WHERE canonical_url=?",
            (navigation_url,),
        ).fetchone()
        assert navigation_document is not None
        assert navigation_document["source_origin"] == "cached_web"
        assert navigation_document["is_staging"] == 0
        assert db.conn.execute(
            "SELECT COUNT(*) FROM rag_chunks WHERE document_id=? AND is_deleted=0",
            (navigation_document["id"],),
        ).fetchone()[0] >= 1
        assert result["citations"][0]["canonical_url"] == target_url
        assert fetcher.calls.count(navigation_url) == fetcher.calls.count(target_url) == 1
    finally:
        db.close()


def test_hostile_navigation_anchor_never_becomes_evidence_when_target_fails(tmp_path: Path) -> None:
    navigation_url = "https://hostile.example/docs.html"
    target_url = "https://hostile.example/wal.html"
    hostile_claim = "Default threshold is 1000 pages"
    navigation = _html(
        "Hostile Map",
        "Untrusted navigation metadata.",
        f"<a href='/wal.html'>{hostile_claim}</a>",
    )
    fetcher = LocalFixtureFetcher(
        {navigation_url: navigation, target_url: FetchError("target unavailable")},
        default_robots=b"User-agent: *\nAllow: /\n",
    )
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path,
        QuoteEvidenceModel(hostile_claim),
        fetcher,
        extractor=FixtureHTMLExtractor(fail_urls={navigation_url}),
    )
    try:
        provider.frontier.discover(
            navigation_url,
            discovery_kind="manual_seed",
            anchor_text="default threshold documentation",
        )
        session = sessions.create("hostile", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(
                question="What is the default threshold?",
                session_id=session["id"],
                model="fixture",
                second_round_enabled=False,
            )
        metrics = _latest_metrics(db)
        assert metrics["links_discovered_after_extraction_failure"] >= 1
        assert db.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0] == 0
        assert db.conn.execute("SELECT COUNT(*) FROM search_evidence").fetchone()[0] == 0
        assert [row["role"] for row in sessions.messages(session["id"])] == ["user"]
        assert fetcher.calls.count(navigation_url) == fetcher.calls.count(target_url) == 1
    finally:
        db.close()


def test_frontier_retry_backoff_blocked_fairness_and_literal_ip_rejection(tmp_path: Path) -> None:
    now = [1_000_000]
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db, clock=lambda: now[0])
        failed = "https://example.com/failed"
        blocked = "https://example.com/blocked"
        valid = "https://example.org/valid"
        frontier.discover(failed, discovery_kind="manual_seed", anchor_text="retry needle")
        frontier.discover(blocked, discovery_kind="hyperlink", anchor_text="retry needle")
        frontier.discover(valid, discovery_kind="manual_seed", anchor_text="retry needle")
        frontier.mark_fetch_failure(failed, "fetch_failed")
        frontier.mark_fetch_failure(blocked, "robots_disallowed", blocked=True)
        eligible = [row["canonical_url"] for row in frontier.search("retry needle")]
        assert eligible == [valid]
        retry_at = db.conn.execute(
            "SELECT next_retry_at FROM crawl_frontier WHERE canonical_url = ?", (failed,)
        ).fetchone()[0]
        now[0] = int(retry_at)
        assert failed in [row["canonical_url"] for row in frontier.search("retry needle")]
        with pytest.raises(FetchBlockedError):
            frontier.discover("http://127.0.0.1/private", discovery_kind="manual_seed", anchor_text="unsafe")
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE canonical_url LIKE '%127.0.0.1%'").fetchone()[0] == 0
    finally:
        db.close()


def test_robots_caps_sitemaps_retries_transient_cache_and_fails_closed_on_patterns(tmp_path: Path) -> None:
    now = [2_000_000]
    db = Database(tmp_path / "profile")
    try:
        frontier = FrontierStore(db, clock=lambda: now[0])
        parser = DiscoveryParser(db, frontier, limits=LocalDiscoveryLimits(max_sitemap_entries=3))
        sitemap_lines = "\n".join([f"Sitemap: https://example.com/map-{index}.xml" for index in range(1000)])
        fetcher = LocalFixtureFetcher(
            {"https://example.com/robots.txt": (f"User-agent: *\nAllow: /\n{sitemap_lines}".encode(), "text/plain")}
        )
        decision = parser.robots_allows("https://example.com/public", fetcher)
        assert decision.allowed and decision.sitemap_candidates == 3
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE discovery_kind='sitemap'").fetchone()[0] == 3

        transient_url = "https://retry.example/robots.txt"
        retry_fetcher = LocalFixtureFetcher({transient_url: FetchError("temporary")})
        assert not parser.robots_allows("https://retry.example/page", retry_fetcher)
        retry_fetcher.outcomes[transient_url] = (b"User-agent: *\nAllow: /", "text/plain")
        assert not parser.robots_allows("https://retry.example/page", retry_fetcher)
        assert retry_fetcher.calls.count(transient_url) == 1
        now[0] += 5 * 60 * 1000
        assert parser.robots_allows("https://retry.example/page", retry_fetcher)
        assert retry_fetcher.calls.count(transient_url) == 2

        wildcard = LocalFixtureFetcher(
            {"https://wild.example/robots.txt": (b"User-agent: *\nDisallow: /*.pdf$", "text/plain")}
        )
        unsupported = parser.robots_allows("https://wild.example/manual.pdf", wildcard)
        assert not unsupported and unsupported.code == "robots_unsupported_pattern"
    finally:
        db.close()


def test_robots_sitemap_expansion_is_cancellable_deduped_and_deadline_bounded(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        event = threading.Event()
        frontier = FrontierStore(db)
        parser = DiscoveryParser(db, frontier, limits=LocalDiscoveryLimits(max_sitemap_entries=20))
        original_discover = frontier.discover

        def cancelling_discover(*args: object, **kwargs: object) -> dict[str, object]:
            row = original_discover(*args, **kwargs)
            event.set()
            return row

        frontier.discover = cancelling_discover  # type: ignore[method-assign]
        body = b"User-agent: *\nAllow: /\nSitemap: https://cancel.example/one.xml\nSitemap: https://cancel.example/two.xml"
        fetcher = LocalFixtureFetcher({"https://cancel.example/robots.txt": (body, "text/plain")})
        with cancellation_scope(event), pytest.raises(JobCancelledError):
            parser.robots_allows("https://cancel.example/page", fetcher, deadline=time.monotonic() + 10)
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE discovery_kind='sitemap'").fetchone()[0] == 1

        event.clear()
        frontier.discover = original_discover  # type: ignore[method-assign]
        duplicate_body = b"User-agent: *\nAllow: /\n" + b"Sitemap: https://dup.example/map.xml\n" * 10
        duplicate_fetcher = LocalFixtureFetcher({"https://dup.example/robots.txt": (duplicate_body, "text/plain")})
        decision = parser.robots_allows("https://dup.example/page", duplicate_fetcher, deadline=time.monotonic() + 10)
        assert decision.sitemap_candidates == 1
        assert db.conn.execute(
            "SELECT COUNT(*) FROM crawl_frontier WHERE canonical_url='https://dup.example/map.xml'"
        ).fetchone()[0] == 1

        expired = LocalFixtureFetcher({"https://deadline.example/robots.txt": (body, "text/plain")})
        with pytest.raises(LocalBudgetExhausted):
            parser.robots_allows("https://deadline.example/page", expired, deadline=time.monotonic() - 1)
        assert expired.calls == []
    finally:
        db.close()


def test_run_budget_counts_robots_and_enforces_total_bytes(tmp_path: Path) -> None:
    url = "https://metered.example/page"
    robots_url = "https://metered.example/robots.txt"
    robots = b"User-agent: *\nAllow: /\n" + (b"# padding\n" * 3000)
    oversized_page = _html("Metered", "x" * 50_000)
    limits = LocalDiscoveryLimits(max_new_urls=2, max_total_bytes=64 * 1024, max_urls_per_domain=2)
    fetcher = LocalFixtureFetcher({robots_url: (robots, "text/plain"), url: oversized_page})
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path, EmptyEvidenceModel(), fetcher, limits=limits
    )
    try:
        provider.frontier.discover(url, discovery_kind="hyperlink", anchor_text="metered accounting")
        session = sessions.create("metered", "fixture")
        with pytest.raises(SearchBudgetError):
            service.run(question="metered accounting", session_id=session["id"], model="fixture", second_round_enabled=False)
        metrics = _latest_metrics(db)
        assert metrics["metadata_fetches"] == 1
        assert metrics["page_fetches"] == 1
        assert metrics["network_requests_total"] == 2
        assert metrics["bytes_downloaded_total"] == len(robots)
        assert metrics["bytes_downloaded_total"] <= limits.max_total_bytes
        assert db.conn.execute("SELECT COUNT(*) FROM documents WHERE canonical_url = ?", (url,)).fetchone()[0] == 0
        frontier = db.conn.execute(
            "SELECT status, attempt_count, next_retry_at FROM crawl_frontier WHERE canonical_url=?", (url,)
        ).fetchone()
        assert dict(frontier) == {"status": "unfetched", "attempt_count": 0, "next_retry_at": None}
    finally:
        db.close()


def test_transient_robots_fetch_failure_uses_retryable_frontier_state(tmp_path: Path) -> None:
    url = "https://transient.example/page"
    robots_url = "https://transient.example/robots.txt"
    fetcher = LocalFixtureFetcher({robots_url: FetchError("temporary robots outage"), url: _html("Page", "text")})
    db, sessions, _documents, _rag, provider, service = local_search_stack(tmp_path, EmptyEvidenceModel(), fetcher)
    try:
        provider.frontier.discover(url, discovery_kind="hyperlink", anchor_text="transient robots retry")
        session = sessions.create("robots", "fixture")
        with pytest.raises(SearchNoEvidenceError):
            service.run(
                question="transient robots retry", session_id=session["id"], model="fixture", second_round_enabled=False
            )
        row = db.conn.execute(
            "SELECT status, failure_code, attempt_count, next_retry_at, last_attempt_at "
            "FROM crawl_frontier WHERE canonical_url = ?",
            (url,),
        ).fetchone()
        assert row["status"] == "failed"
        assert row["failure_code"] == "fetch_failed"
        assert row["attempt_count"] == 1
        assert 0 < int(row["next_retry_at"]) - int(row["last_attempt_at"]) <= 5 * 60 * 1000
        assert fetcher.calls == [robots_url]
    finally:
        db.close()


def test_source_pack_activation_is_query_gated_preferred_and_malformed_safe(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        pack_dir = db.profile_dir / "source-packs"
        pack_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "source_packs" / "sqlite.md", pack_dir / "sqlite.md")
        shutil.copy(FIXTURES / "source_packs" / "python.md", pack_dir / "python.md")
        (pack_dir / "broken.md").write_text("---\ntype: wrong\ntitle: TOP-SECRET-NAME\n---\n", encoding="utf-8")
        provider = LocalDiscoveryProvider(db)

        relevant = provider.search("SQLite WAL checkpoint", limit=10, timeout=1)
        assert any(row.url == "https://www.sqlite.org/docs.html" for row in relevant)
        assert not any("python.org" in row.url for row in relevant)
        sqlite_row = db.conn.execute(
            "SELECT priority FROM crawl_frontier WHERE canonical_url='https://www.sqlite.org/docs.html'"
        ).fetchone()
        assert sqlite_row is not None
        assert not any("fixture.example" in row.url for row in relevant)
        assert provider.query_metrics["source_pack_errors"] == 1

        unrelated = provider.search("python snake habitat", limit=10, timeout=1)
        assert not any("python.org" in row.url for row in unrelated)
        assert db.conn.execute("SELECT COUNT(*) FROM crawl_frontier WHERE canonical_url LIKE '%python.org%'").fetchone()[0] == 0

        python_rows = provider.search("Python asyncio task cancellation", limit=10, timeout=1)
        assert any("python.org" in row.url for row in python_rows)
        python_priority = db.conn.execute(
            "SELECT priority FROM crawl_frontier WHERE canonical_url='https://docs.python.org/'"
        ).fetchone()[0]
        assert float(python_priority) == 2.5
    finally:
        db.close()


def test_shipped_sqlite_pack_is_real_only_and_test_fixture_pack_keeps_feed_coverage(tmp_path: Path) -> None:
    db = Database(tmp_path / "profile")
    try:
        store = SourcePackStore(db, FrontierStore(db))
        sqlite_pack = store.parse(FIXTURES / "source_packs" / "sqlite.md")
        fixture_pack = store.parse(FIXTURES / "source_packs" / "potatocs-fixture.md")
        assert sqlite_pack.seeds == ("https://www.sqlite.org/docs.html",)
        assert sqlite_pack.feeds == ()
        assert "fixture.example" not in (FIXTURES / "source_packs" / "sqlite.md").read_text(encoding="utf-8")
        assert fixture_pack.feeds == ("https://fixture.example/feed.atom",)
    finally:
        db.close()


def test_runtime_verifiers_require_html_extraction_dependencies() -> None:
    repo = Path(__file__).resolve().parents[2]
    for name in ("prepare-python-runtime.ps1", "verify-python-runtime.ps1"):
        text = (repo / "scripts" / name).read_text(encoding="utf-8")
        assert '"lxml"' in text
        assert '"readability"' in text


def test_malformed_pack_warning_is_safe_while_valid_pack_completes(tmp_path: Path) -> None:
    quote = "The Safe Fixture release code is PACK-SAFE-17."
    url = "https://safe.example/start"
    db, sessions, _documents, _rag, provider, service = local_search_stack(
        tmp_path,
        QuoteEvidenceModel(quote),
        LocalFixtureFetcher({url: _html("Safe Fixture", quote)}, default_robots=b"User-agent: *\nAllow: /\n"),
    )
    try:
        pack_dir = db.profile_dir / "source-packs"
        (pack_dir / "safe.md").write_text(
            "---\ntype: source-pack\ntitle: Safe Fixture\ntags:\n  - safe\n  - fixture\n---\n\n"
            f"# Seeds\n\n- {url}\n",
            encoding="utf-8",
        )
        (pack_dir / "broken-secret-name.md").write_text(
            "---\ntype: wrong\ntitle: PRIVATE-PACK-CONTENT\n---\n\n# Seeds\n\n- https://malformed.example/leak\n",
            encoding="utf-8",
        )
        session = sessions.create("packs", "fixture")
        result = service.run(
            question="Safe Fixture release code", session_id=session["id"], model="fixture", second_round_enabled=False
        )
        assert result["metrics"]["source_pack_errors"] == 1
        assert result["metrics"]["source_pack_hits"] == 1
        assistant = sessions.messages(session["id"])[-1]
        trace = assistant["metadata"]["operation_trace"]
        assert trace["warnings"] == ["1 malformed Source Pack(s) were skipped."]
        serialized_trace = json.dumps(trace)
        assert "broken-secret-name" not in serialized_trace
        assert "PRIVATE-PACK-CONTENT" not in serialized_trace
        assert not any("malformed.example" in call for call in service.fetcher.calls)
    finally:
        db.close()


def test_job_search_mode_routes_local_without_external_factory_and_preserves_external(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "profile")
    executor = DocumentJobExecutor.__new__(DocumentJobExecutor)
    executor.db = db
    executor.services = SimpleNamespace(sessions=object(), models=object(), documents=object(), rag=object())
    local_provider = object()
    external_provider = object()
    seen: list[tuple[object, str]] = []

    fail_local = [True]

    class FakeSearchService:
        def __init__(self, _db: object, _sessions: object, _models: object, _documents: object, _rag: object, provider: object):
            self.provider = provider

        def run(self, *, question: str, **_kwargs: object) -> dict[str, str]:
            seen.append((self.provider, question))
            if fail_local[0]:
                raise SearchNoEvidenceError("no local evidence")
            return {"assistant_message_id": "message", "run_id": "run"}

    import odysseus_desktop_backend.services.local_discovery as local_module
    import odysseus_desktop_backend.services.search_provider as provider_module
    import odysseus_desktop_backend.services.search_service as search_module

    monkeypatch.setattr(search_module, "SearchService", FakeSearchService)
    monkeypatch.setattr(local_module, "LocalDiscoveryProvider", lambda _db: local_provider)
    monkeypatch.setattr(provider_module, "configured_search_provider", lambda: (_ for _ in ()).throw(AssertionError("external called")))
    db.set_setting("search_mode", "local")
    job = JobRecord(id="local", kind="search", session_id="session", query="private local query", model="fixture")
    with pytest.raises(JobFailure) as failure:
        executor._run_search(job, lambda: None)
    assert failure.value.code == "search_no_evidence"
    assert seen == [(local_provider, "private local query")]

    fail_local[0] = False
    monkeypatch.setattr(provider_module, "configured_search_provider", lambda: external_provider)
    db.set_setting("search_mode", "external")
    external_job = JobRecord(id="external", kind="search", session_id="session", query="external query", model="fixture")
    executor._run_search(external_job, lambda: None)
    assert seen[-1] == (external_provider, "external query")

    monkeypatch.setattr(
        local_module,
        "LocalDiscoveryProvider",
        lambda _db: (_ for _ in ()).throw(AssertionError("local provider built for invalid mode")),
    )
    monkeypatch.setattr(
        provider_module,
        "configured_search_provider",
        lambda: (_ for _ in ()).throw(AssertionError("external provider built for invalid mode")),
    )
    db.set_setting("search_mode", "hybrid")
    invalid_job = JobRecord(id="invalid", kind="search", session_id="session", query="invalid query", model="fixture")
    with pytest.raises(JobFailure) as invalid_failure:
        executor._run_search(invalid_job, lambda: None)
    assert invalid_failure.value.code == "search_provider_unconfigured"
    assert seen[-1] == (external_provider, "external query")
    db.close()
