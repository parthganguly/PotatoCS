from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.robotparser
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from odysseus_desktop_backend.cancellation import check_cancelled
from odysseus_desktop_backend.services.search_provider import ProviderSearchResult
from odysseus_desktop_backend.services.web_fetcher import (
    FetchBlockedError,
    FetchError,
    FetchLimitError,
    SafeHttpFetcher,
    canonicalize_url,
)
from odysseus_desktop_backend.storage import Database, utc_ms


FTS_TOKEN_RE = re.compile(r"[\w][\w.-]*", re.UNICODE)
PACK_SECTION_RE = re.compile(r"^#{1,6}\s+(.+?)\s*$")
PACK_BULLET_RE = re.compile(r"^\s*[-*]\s+(.+?)\s*$")
DISCOVERY_KINDS = {"manual_seed", "source_pack", "hyperlink", "sitemap", "rss", "atom", "cached"}
DEFAULT_RRF_K = 60
ROBOTS_USER_AGENT = "PotatoCS-Search"
TRANSIENT_RETRY_BASE_MS = 5 * 60 * 1000
TRANSIENT_RETRY_MAX_MS = 24 * 60 * 60 * 1000
BLOCKED_RETRY_MS = 6 * 60 * 60 * 1000
ROBOTS_SUCCESS_TTL_MS = 24 * 60 * 60 * 1000
ROBOTS_FAILURE_TTL_MS = 5 * 60 * 1000
ROBOTS_POLICY_TTL_MS = 6 * 60 * 60 * 1000
TECHNICAL_QUERY_TOKENS = {
    "api", "asyncio", "cancellation", "checkpoint", "class", "code", "database",
    "docs", "documentation", "function", "fts", "library", "package", "programming",
    "release", "runtime", "software", "sql", "sqlite", "task", "wal",
}
PACK_STOPWORDS = {
    "a", "an", "and", "about", "for", "from", "in", "is", "of", "on", "or",
    "the", "to", "what", "with",
}
FEED_CONTENT_TYPES = {
    "application/atom+xml",
    "application/rss+xml",
    "application/xml",
    "text/xml",
    "text/plain",
}


class LocalBudgetExhausted(RuntimeError):
    code = "search_budget_exhausted"


class DiscoveryBlockedError(FetchBlockedError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


class DiscoveryTransientError(FetchError):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class LocalRunBudget:
    limits: "LocalDiscoveryLimits"
    deadline: float
    page_fetches: int = 0
    metadata_fetches: int = 0
    network_requests_total: int = 0
    bytes_downloaded_total: int = 0
    observed_urls: set[str] = field(default_factory=set)

    @property
    def remaining_pages(self) -> int:
        return max(0, self.limits.max_new_urls - self.page_fetches)

    @property
    def remaining_bytes(self) -> int:
        return max(0, self.limits.max_total_bytes - self.bytes_downloaded_total)

    def ensure_available(self, *, request_kind: str) -> None:
        check_cancelled()
        if time.monotonic() >= self.deadline:
            raise LocalBudgetExhausted("local discovery wall-time budget exhausted")
        if self.remaining_bytes <= 0:
            raise LocalBudgetExhausted("local discovery byte budget exhausted")
        if request_kind == "page" and self.remaining_pages <= 0:
            raise LocalBudgetExhausted("local discovery page-fetch budget exhausted")

    def fetch(self, fetcher: Any, url: str, *, request_kind: str, **kwargs: Any) -> Any:
        self.ensure_available(request_kind=request_kind)
        canonical = canonicalize_url(url)
        if request_kind == "page":
            self.page_fetches += 1
        else:
            self.metadata_fetches += 1
        self.network_requests_total += 1
        original_limit = getattr(fetcher, "max_response_bytes", None)
        active_limit = int(original_limit) if original_limit is not None else None
        if original_limit is not None:
            active_limit = min(int(original_limit), self.remaining_bytes)
            fetcher.max_response_bytes = active_limit
        try:
            response = fetcher.fetch(canonical, **kwargs)
        except FetchLimitError as exc:
            aggregate_limit_applied = (
                original_limit is not None
                and active_limit is not None
                and active_limit < int(original_limit)
            )
            if aggregate_limit_applied and "redirect limit" not in str(exc).casefold():
                raise LocalBudgetExhausted(
                    "local discovery remaining byte budget could not accept the response"
                ) from exc
            raise
        finally:
            if original_limit is not None:
                fetcher.max_response_bytes = original_limit
        downloaded = max(0, int(getattr(response, "bytes_downloaded", 0) or 0))
        if downloaded > self.remaining_bytes:
            self.bytes_downloaded_total += downloaded
            raise LocalBudgetExhausted("local discovery response exceeded remaining byte budget")
        self.bytes_downloaded_total += downloaded
        return response


class BudgetedMetadataFetcher:
    def __init__(self, fetcher: Any, budget: LocalRunBudget):
        self.fetcher = fetcher
        self.budget = budget

    def fetch(self, url: str, **kwargs: Any) -> Any:
        return self.budget.fetch(self.fetcher, url, request_kind="metadata", **kwargs)


@dataclass(frozen=True)
class RobotsDecision:
    allowed: bool
    code: str = ""
    sitemap_candidates: int = 0

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class LocalDiscoveryLimits:
    max_new_urls: int = 4
    max_total_bytes: int = 4 * 1024 * 1024
    max_depth: int = 2
    max_urls_per_domain: int = 3
    max_wall_time_seconds: int = 90
    max_sitemap_entries: int = 100
    max_feed_entries: int = 50

    @classmethod
    def from_database(cls, db: Database) -> "LocalDiscoveryLimits":
        def bounded(key: str, default: int, low: int, high: int) -> int:
            try:
                value = int(db.get_setting(key, str(default)) or default)
            except (TypeError, ValueError):
                value = default
            return max(low, min(value, high))

        return cls(
            max_new_urls=bounded("search_local_max_new_urls", 4, 0, 8),
            max_total_bytes=bounded("search_local_max_total_bytes", 4 * 1024 * 1024, 64 * 1024, 8 * 1024 * 1024),
            max_depth=bounded("search_local_max_depth", 2, 0, 3),
            max_urls_per_domain=bounded("search_local_max_urls_per_domain", 3, 1, 8),
            max_wall_time_seconds=bounded("search_timeout_seconds", 90, 30, 300),
            max_sitemap_entries=bounded("search_local_max_sitemap_entries", 100, 1, 500),
            max_feed_entries=bounded("search_local_max_feed_entries", 50, 1, 200),
        )


@dataclass(frozen=True)
class LexicalCandidate:
    chunk_id: str
    document_id: str
    content: str
    title: str
    source_origin: str
    canonical_url: str
    final_url: str
    fetched_at: int
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any]
    raw_lexical_rank: float
    lexical_rank: int


@dataclass(frozen=True)
class HybridCandidate:
    chunk_id: str
    document_id: str
    content: str
    score: float
    page_start: int | None
    page_end: int | None
    metadata: dict[str, Any]
    title: str
    source_origin: str
    canonical_url: str
    final_url: str
    fetched_at: int
    lexical_rank: int | None = None
    dense_rank: int | None = None
    raw_lexical_rank: float | None = None
    dense_score: float | None = None


class LocalSearchIndex:
    """FTS5 over active authoritative RAG chunks plus deterministic RRF."""

    def __init__(self, db: Database):
        self.db = db

    def fts5_available(self) -> bool:
        try:
            self.db.conn.execute("SELECT rowid FROM local_fts LIMIT 1").fetchall()
            return True
        except sqlite3.Error:
            return False

    def sync(self) -> int:
        """Transactionally rebuild the small active-term index.

        Chunk text remains authoritative in rag_chunks. Rebuilding is deliberate
        for v1: it makes staging/current-revision semantics simple and auditable.
        """
        rows = self.db.conn.execute(
            """
            SELECT c.id AS chunk_id, c.document_id, c.content, c.page_start, c.page_end,
                   c.metadata_json, d.title, d.source_origin, d.canonical_url,
                   d.final_url, d.fetched_at
            FROM rag_chunks c
            JOIN documents d ON d.id = c.document_id
            WHERE c.is_deleted = 0
              AND d.is_deleted = 0
              AND d.index_status = 'indexed'
              AND COALESCE(d.is_staging, 0) = 0
              AND (
                    COALESCE(d.source_origin, 'local') NOT IN ('web', 'cached_web')
                    OR COALESCE(d.web_revision_current, 1) = 1
                  )
            ORDER BY c.id
            """
        ).fetchall()
        with self.db.conn:
            self.db.conn.execute("INSERT INTO local_fts(local_fts) VALUES('delete-all')")
            self.db.conn.execute("DELETE FROM local_fts_rows")
            for row in rows:
                check_cancelled()
                cursor = self.db.conn.execute(
                    "INSERT INTO local_fts_rows(chunk_id, document_id) VALUES (?, ?)",
                    (row["chunk_id"], row["document_id"]),
                )
                metadata = _json_object(row["metadata_json"])
                headings = " ".join(
                    str(metadata.get(key) or "")
                    for key in ("heading", "section", "section_title", "page_title")
                ).strip()
                self.db.conn.execute(
                    "INSERT INTO local_fts(rowid, title, headings, body) VALUES (?, ?, ?, ?)",
                    (cursor.lastrowid, row["title"], headings, row["content"]),
                )
        return len(rows)

    def search(self, query: str, *, limit: int = 12, synchronize: bool = True) -> list[LexicalCandidate]:
        if synchronize:
            self.sync()
        expression = fts_match_expression(query)
        if not expression:
            return []
        rows = self.db.conn.execute(
            """
            SELECT m.chunk_id, m.document_id, c.content, c.page_start, c.page_end,
                   c.metadata_json, d.title, d.source_origin, d.canonical_url,
                   d.final_url, d.fetched_at,
                   bm25(local_fts, 8.0, 4.0, 1.0) AS lexical_score
            FROM local_fts
            JOIN local_fts_rows m ON m.rowid = local_fts.rowid
            JOIN rag_chunks c ON c.id = m.chunk_id
            JOIN documents d ON d.id = m.document_id
            WHERE local_fts MATCH ?
            ORDER BY lexical_score ASC, m.chunk_id ASC
            LIMIT ?
            """,
            (expression, max(0, int(limit))),
        ).fetchall()
        return [
            LexicalCandidate(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                content=str(row["content"]),
                title=str(row["title"] or "Local Source"),
                source_origin=str(row["source_origin"] or "local"),
                canonical_url=str(row["canonical_url"] or ""),
                final_url=str(row["final_url"] or ""),
                fetched_at=int(row["fetched_at"] or 0),
                page_start=row["page_start"],
                page_end=row["page_end"],
                metadata=_json_object(row["metadata_json"]),
                raw_lexical_rank=float(row["lexical_score"]),
                lexical_rank=rank,
            )
            for rank, row in enumerate(rows, start=1)
        ]

    def hybrid_search(self, query: str, rag: Any, *, limit: int = 12) -> dict[str, Any]:
        started = time.perf_counter()
        lexical = self.search(query, limit=max(limit * 4, 32))
        dense_audit = rag.search_with_audit(
            query,
            limit=max(limit * 4, 32),
            include_search_cache=True,
        )
        dense = list(dense_audit.get("results") or [])
        by_chunk: dict[str, dict[str, Any]] = {}
        for item in lexical:
            by_chunk[item.chunk_id] = {
                "lexical": item,
                "dense": None,
                "score": 1.0 / (DEFAULT_RRF_K + item.lexical_rank),
            }
        for dense_rank, item in enumerate(dense, start=1):
            chunk_id = str(item.chunk_id)
            slot = by_chunk.setdefault(chunk_id, {"lexical": None, "dense": None, "score": 0.0})
            slot["dense"] = (dense_rank, item)
            slot["score"] += 1.0 / (DEFAULT_RRF_K + dense_rank)

        ordered = sorted(by_chunk.values(), key=lambda row: (-float(row["score"]), _hybrid_chunk_id(row)))
        candidates: list[HybridCandidate] = []
        for slot in ordered[: max(0, int(limit))]:
            lexical_item: LexicalCandidate | None = slot["lexical"]
            dense_pair = slot["dense"]
            dense_rank = dense_pair[0] if dense_pair else None
            dense_item = dense_pair[1] if dense_pair else None
            if lexical_item is not None:
                document_id = lexical_item.document_id
                chunk_id = lexical_item.chunk_id
                content = lexical_item.content
                page_start = lexical_item.page_start
                page_end = lexical_item.page_end
                metadata = dict(lexical_item.metadata)
                title = lexical_item.title
                source_origin = lexical_item.source_origin
                canonical_url = lexical_item.canonical_url
                final_url = lexical_item.final_url
                fetched_at = lexical_item.fetched_at
            else:
                document_id = str(dense_item.document_id)
                chunk_id = str(dense_item.chunk_id)
                content = str(dense_item.content)
                page_start = dense_item.page_start
                page_end = dense_item.page_end
                metadata = dict(dense_item.metadata or {})
                doc_row = self.db.conn.execute(
                    "SELECT title, source_origin, canonical_url, final_url, fetched_at FROM documents WHERE id = ?",
                    (document_id,),
                ).fetchone()
                doc = dict(doc_row) if doc_row is not None else {}
                title = str(doc.get("title") or metadata.get("title") or "Local Source")
                source_origin = str(doc.get("source_origin") or metadata.get("source_origin") or "local")
                canonical_url = str(doc.get("canonical_url") or metadata.get("canonical_url") or "")
                final_url = str(doc.get("final_url") or metadata.get("final_url") or "")
                fetched_at = int(doc.get("fetched_at") or metadata.get("fetched_at") or 0)
            metadata.update(
                {
                    "retrieval": "local_rrf",
                    "lexical_rank": lexical_item.lexical_rank if lexical_item else None,
                    "dense_rank": dense_rank,
                    "raw_lexical_rank": lexical_item.raw_lexical_rank if lexical_item else None,
                    "dense_score": float(dense_item.score) if dense_item is not None else None,
                }
            )
            candidates.append(
                HybridCandidate(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    content=content,
                    score=float(slot["score"]),
                    page_start=page_start,
                    page_end=page_end,
                    metadata=metadata,
                    title=title,
                    source_origin=source_origin,
                    canonical_url=canonical_url,
                    final_url=final_url,
                    fetched_at=fetched_at,
                    lexical_rank=lexical_item.lexical_rank if lexical_item else None,
                    dense_rank=dense_rank,
                    raw_lexical_rank=lexical_item.raw_lexical_rank if lexical_item else None,
                    dense_score=float(dense_item.score) if dense_item is not None else None,
                )
            )
        return {
            "results": candidates,
            "fts_hits": len(lexical),
            "dense_hits": len(dense),
            "fused_candidates": len(by_chunk),
            "embedding_backend": dense_audit.get("embedding_backend", ""),
            "embedding_model": dense_audit.get("embedding_model", ""),
            "retrieval_latency_ms": int((time.perf_counter() - started) * 1000),
        }

    def metrics(self) -> dict[str, int]:
        row = self.db.conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM documents d WHERE d.is_deleted = 0 AND COALESCE(d.is_staging, 0) = 0) AS current_documents,
                (SELECT COUNT(*) FROM rag_chunks c JOIN documents d ON d.id = c.document_id
                    WHERE c.is_deleted = 0 AND d.is_deleted = 0 AND COALESCE(d.is_staging, 0) = 0) AS current_chunks,
                (SELECT COUNT(*) FROM local_fts_rows) AS fts_rows,
                (SELECT COUNT(*) FROM documents d WHERE d.is_deleted = 0 AND COALESCE(d.is_staging, 0) = 0
                    AND d.source_origin IN ('web', 'cached_web') AND d.web_revision_current = 1) AS cached_web_documents,
                (SELECT COUNT(*) FROM crawl_frontier WHERE status = 'unfetched') AS known_unfetched_urls,
                (SELECT COUNT(DISTINCT domain) FROM crawl_frontier) AS domains_represented
            """
        ).fetchone()
        return {key: int(row[key] or 0) for key in row.keys()}


class FrontierStore:
    def __init__(self, db: Database, *, clock: Any = utc_ms):
        self.db = db
        self.clock = clock

    def discover(
        self,
        url: str,
        *,
        discovery_kind: str,
        discovered_from_url: str = "",
        anchor_text: str = "",
        surrounding_text: str = "",
        source_title: str = "",
        depth: int = 0,
        priority: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if discovery_kind not in DISCOVERY_KINDS:
            raise ValueError(f"unsupported discovery kind: {discovery_kind}")
        canonical = canonicalize_url(url)
        domain = str(urllib.parse.urlsplit(canonical).hostname or "")
        reject_literal_nonpublic_host(domain)
        now = int(self.clock())
        clean_anchor = _clean_text(anchor_text, 500)
        clean_context = _clean_text(surrounding_text, 1000)
        clean_title = _clean_text(source_title, 300)
        row = self.db.conn.execute(
            "SELECT * FROM crawl_frontier WHERE canonical_url = ?", (canonical,)
        ).fetchone()
        with self.db.conn:
            if row is None:
                frontier_id = str(uuid.uuid4())
                self.db.conn.execute(
                    """
                    INSERT INTO crawl_frontier(
                        id, url, canonical_url, domain, discovered_from_url, anchor_text,
                        surrounding_text, source_title, discovery_kind, depth, priority,
                        first_seen_at, last_seen_at, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        frontier_id, str(url), canonical, domain, discovered_from_url,
                        clean_anchor, clean_context, clean_title, discovery_kind,
                        max(0, int(depth)), float(priority), now, now,
                        json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            else:
                frontier_id = str(row["id"])
                self.db.conn.execute(
                    """
                    UPDATE crawl_frontier
                    SET last_seen_at = ?, priority = MAX(priority, ?), depth = MIN(depth, ?),
                        anchor_text = CASE WHEN length(?) > length(anchor_text) THEN ? ELSE anchor_text END,
                        surrounding_text = CASE WHEN length(?) > length(surrounding_text) THEN ? ELSE surrounding_text END,
                        source_title = CASE WHEN length(?) > length(source_title) THEN ? ELSE source_title END
                    WHERE id = ?
                    """,
                    (
                        now, float(priority), max(0, int(depth)), clean_anchor, clean_anchor,
                        clean_context, clean_context, clean_title, clean_title, frontier_id,
                    ),
                )
            self.db.conn.execute(
                """
                INSERT OR IGNORE INTO crawl_discoveries(
                    id, frontier_id, discovered_from_url, anchor_text, surrounding_text,
                    source_title, discovery_kind, depth, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()), frontier_id, discovered_from_url, clean_anchor,
                    clean_context, clean_title, discovery_kind, max(0, int(depth)), now,
                ),
            )
            current = self.db.conn.execute(
                "SELECT * FROM crawl_frontier WHERE id = ?", (frontier_id,)
            ).fetchone()
            self.db.conn.execute("DELETE FROM frontier_fts WHERE frontier_id = ?", (frontier_id,))
            self.db.conn.execute(
                """
                INSERT INTO frontier_fts(frontier_id, canonical_url, anchor_text, surrounding_text, source_title)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    frontier_id, current["canonical_url"], current["anchor_text"],
                    current["surrounding_text"], current["source_title"],
                ),
            )
        return dict(current)

    def discover_links(
        self,
        *,
        source_url: str,
        html_body: bytes | str,
        source_title: str,
        source_depth: int,
        max_links: int = 200,
    ) -> list[dict[str, Any]]:
        try:
            from lxml import html
        except ImportError as exc:
            raise RuntimeError("link discovery requires readability-lxml") from exc
        decoded = html_body.decode("utf-8", errors="replace") if isinstance(html_body, bytes) else str(html_body)
        try:
            root = html.fromstring(decoded, base_url=source_url)
        except Exception:
            return []
        discovered: list[dict[str, Any]] = []
        for anchor in root.xpath("//a[@href]"):
            check_cancelled()
            if len(discovered) >= max(0, int(max_links)):
                break
            href = str(anchor.get("href") or "").strip()
            text = _clean_text(anchor.text_content(), 500)
            if not href or not text:
                continue
            absolute = urllib.parse.urljoin(source_url, href)
            parent_text = _clean_text(anchor.getparent().text_content() if anchor.getparent() is not None else text, 1000)
            try:
                row = self.discover(
                    absolute,
                    discovery_kind="hyperlink",
                    discovered_from_url=canonicalize_url(source_url),
                    anchor_text=text,
                    surrounding_text=parent_text,
                    source_title=source_title,
                    depth=max(0, int(source_depth)) + 1,
                    priority=1.0 / (max(0, int(source_depth)) + 2),
                )
            except FetchBlockedError:
                continue
            discovered.append(row)
        return discovered

    def search(
        self,
        query: str,
        *,
        limit: int = 20,
        max_depth: int = 2,
        allowed_source_packs: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        expression = fts_match_expression(query)
        if not expression:
            return []
        now = int(self.clock())
        rows = self.db.conn.execute(
            """
            SELECT f.*, bm25(frontier_fts, 0.0, 0.5, 6.0, 2.0, 4.0) AS lexical_score
            FROM frontier_fts
            JOIN crawl_frontier f ON f.id = frontier_fts.frontier_id
            WHERE frontier_fts MATCH ?
              AND (
                    f.status = 'unfetched'
                    OR (f.status IN ('failed', 'blocked') AND COALESCE(f.next_retry_at, 0) <= ?)
                  )
              AND f.depth <= ?
            ORDER BY lexical_score ASC, f.priority DESC, f.depth ASC, f.canonical_url ASC
            LIMIT ?
            """,
            (expression, now, max(0, int(max_depth)), max(0, int(limit)) * 4),
        ).fetchall()
        output: list[dict[str, Any]] = []
        allowed = set(allowed_source_packs or set())
        for row in rows:
            item = {**dict(row), "raw_lexical_rank": float(row["lexical_score"])}
            source_pack = str(_json_object(row["metadata_json"]).get("source_pack") or "")
            if source_pack and source_pack not in allowed:
                continue
            output.append(item)
            if len(output) >= max(0, int(limit)):
                break
        return output

    def mark_fetch_success(self, canonical_url: str, fetch: Any, content_hash: str) -> None:
        now = int(self.clock())
        headers = fetch if isinstance(fetch, dict) else getattr(fetch, "headers", {})
        self.db.conn.execute(
            """
            UPDATE crawl_frontier
            SET status = 'fetched', failure_code = '', last_fetch_at = ?, etag = ?,
                last_modified = ?, content_hash = ?, last_seen_at = ?,
                attempt_count = attempt_count + 1, last_attempt_at = ?, next_retry_at = NULL
            WHERE canonical_url = ?
            """,
            (
                now, str(headers.get("etag", "")), str(headers.get("last-modified", "")),
                content_hash, now, now, canonicalize_url(canonical_url),
            ),
        )
        self.db.conn.commit()

    def mark_fetch_failure(self, canonical_url: str, code: str, *, blocked: bool = False) -> None:
        canonical = canonicalize_url(canonical_url)
        now = int(self.clock())
        row = self.db.conn.execute(
            "SELECT attempt_count FROM crawl_frontier WHERE canonical_url = ?", (canonical,)
        ).fetchone()
        attempts = int((row or {"attempt_count": 0})["attempt_count"] or 0) + 1 if row is not None else 1
        if blocked:
            status = "blocked"
            retry_delay = BLOCKED_RETRY_MS
        else:
            status = "failed"
            retry_delay = min(TRANSIENT_RETRY_MAX_MS, TRANSIENT_RETRY_BASE_MS * (2 ** min(attempts - 1, 8)))
        self.db.conn.execute(
            """
            UPDATE crawl_frontier
            SET status = ?, failure_code = ?, last_fetch_at = ?, attempt_count = ?,
                last_attempt_at = ?, next_retry_at = ?
            WHERE canonical_url = ?
            """,
            (status, _clean_text(code, 80), now, attempts, now, now + retry_delay, canonical),
        )
        self.db.conn.commit()

    def finalize_successful_pages(self, revisions: list[tuple[str, str]]) -> None:
        """Atomically promote complete page observations and their frontier state."""
        now = int(self.clock())
        with self.db.conn:
            for document_id, canonical_url in revisions:
                canonical = canonicalize_url(canonical_url)
                old_rows = self.db.conn.execute(
                    """
                    SELECT id FROM documents
                    WHERE is_deleted = 0 AND source_origin IN ('web', 'cached_web')
                      AND canonical_url = ? AND id <> ? AND web_revision_current = 1
                    """,
                    (canonical, document_id),
                ).fetchall()
                old_ids = [str(row["id"]) for row in old_rows]
                if old_ids:
                    placeholders = ",".join("?" for _ in old_ids)
                    self.db.conn.execute(
                        f"UPDATE documents SET web_revision_current = 0, updated_at = ? WHERE id IN ({placeholders})",
                        (now, *old_ids),
                    )
                    self.db.conn.execute(
                        f"UPDATE rag_chunks SET is_deleted = 1, updated_at = ? WHERE document_id IN ({placeholders})",
                        (now, *old_ids),
                    )
                self.db.conn.execute(
                    """
                    UPDATE documents
                    SET is_staging = 0, web_revision_current = 1,
                        source_origin = 'cached_web', updated_at = ?
                    WHERE id = ?
                    """,
                    (now, document_id),
                )
                self.db.conn.execute(
                    "UPDATE rag_chunks SET is_deleted = 0, updated_at = ? WHERE document_id = ?",
                    (now, document_id),
                )
                document = self.db.conn.execute(
                    "SELECT http_etag, http_last_modified, content_hash FROM documents WHERE id = ?",
                    (document_id,),
                ).fetchone()
                self.db.conn.execute(
                    """
                    UPDATE crawl_frontier
                    SET status = 'fetched', failure_code = '', last_fetch_at = ?, etag = ?,
                        last_modified = ?, content_hash = ?, last_seen_at = ?,
                        attempt_count = attempt_count + 1, last_attempt_at = ?, next_retry_at = NULL
                    WHERE canonical_url = ?
                    """,
                    (
                        now,
                        str(document["http_etag"] or ""),
                        str(document["http_last_modified"] or ""),
                        str(document["content_hash"] or ""),
                        now,
                        now,
                        canonical,
                    ),
                )

    def seed_cached_documents(self) -> int:
        rows = self.db.conn.execute(
            """
            SELECT canonical_url, title, fetched_at FROM documents
            WHERE is_deleted = 0 AND COALESCE(is_staging, 0) = 0
              AND source_origin IN ('web', 'cached_web') AND web_revision_current = 1
              AND canonical_url <> ''
            ORDER BY canonical_url
            """
        ).fetchall()
        for row in rows:
            candidate = self.discover(
                str(row["canonical_url"]), discovery_kind="cached", source_title=str(row["title"] or ""), priority=2.0
            )
            self.db.conn.execute(
                "UPDATE crawl_frontier SET status = 'fetched', last_fetch_at = COALESCE(last_fetch_at, ?) WHERE id = ?",
                (int(row["fetched_at"] or self.clock()), candidate["id"]),
            )
        self.db.conn.commit()
        return len(rows)


@dataclass(frozen=True)
class SourcePack:
    path: str
    title: str
    tags: tuple[str, ...]
    seeds: tuple[str, ...]
    feeds: tuple[str, ...]
    sitemaps: tuple[str, ...]
    preferred_domains: tuple[str, ...]
    notes: str
    content_hash: str


class SourcePackStore:
    def __init__(self, db: Database, frontier: FrontierStore):
        self.db = db
        self.frontier = frontier
        self.directory = db.profile_dir / "source-packs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last_errors = 0

    def parse(self, path: str | Path) -> SourcePack:
        file_path = Path(path).resolve()
        text = file_path.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(text)
        if str(metadata.get("type") or "").strip() != "source-pack":
            raise ValueError("Source Pack frontmatter requires type: source-pack")
        title = _clean_text(metadata.get("title"), 200)
        if not title:
            raise ValueError("Source Pack frontmatter requires a title")
        tags = tuple(_unique_clean(metadata.get("tags") if isinstance(metadata.get("tags"), list) else []))
        sections: dict[str, list[str]] = {}
        section = "seeds"
        notes_lines: list[str] = []
        for line in body.splitlines():
            heading = PACK_SECTION_RE.match(line)
            if heading:
                section = heading.group(1).strip().casefold()
                continue
            bullet = PACK_BULLET_RE.match(line)
            if bullet:
                sections.setdefault(section, []).append(bullet.group(1).strip())
            elif section == "notes" and line.strip():
                notes_lines.append(line.strip())
        seeds: list[str] = []
        feeds: list[str] = []
        sitemaps: list[str] = []
        preferred_domains: list[str] = []
        for name, values in sections.items():
            if "feed" in name or "rss" in name or "atom" in name:
                feeds.extend(values)
            elif "sitemap" in name:
                sitemaps.extend(values)
            elif "preferred domain" in name:
                preferred_domains.extend(_domain_from_value(value) for value in values)
            elif "note" in name:
                notes_lines.extend(values)
            else:
                seeds.extend(values)
        front_domains = metadata.get("preferred_domains")
        if isinstance(front_domains, list):
            preferred_domains.extend(_domain_from_value(value) for value in front_domains)
        return SourcePack(
            path=str(file_path),
            title=title,
            tags=tuple(_unique_clean(tags)),
            seeds=tuple(_safe_urls(seeds)),
            feeds=tuple(_safe_urls(feeds)),
            sitemaps=tuple(_safe_urls(sitemaps)),
            preferred_domains=tuple(value for value in _unique_clean(preferred_domains) if value),
            notes="\n".join(notes_lines).strip(),
            content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def load(self, path: str | Path) -> SourcePack:
        pack = self.parse(path)
        pack_id = str(uuid.uuid5(uuid.NAMESPACE_URL, pack.path))
        now = utc_ms()
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO source_packs(id, path, title, tags_json, preferred_domains_json, notes, content_hash, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET title = excluded.title, tags_json = excluded.tags_json,
                    preferred_domains_json = excluded.preferred_domains_json, notes = excluded.notes,
                    content_hash = excluded.content_hash, updated_at = excluded.updated_at
                """,
                (
                    pack_id, pack.path, pack.title, json.dumps(pack.tags),
                    json.dumps(pack.preferred_domains), pack.notes, pack.content_hash, now,
                ),
            )
        return pack

    def activate(self, pack: SourcePack) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        preferred = set(pack.preferred_domains)
        for url in pack.seeds:
            domain = str(urllib.parse.urlsplit(url).hostname or "")
            rows.append(self.frontier.discover(
                url,
                discovery_kind="source_pack",
                source_title=pack.title,
                priority=2.5 if domain in preferred else 2.0,
                metadata={"source_pack": pack.title},
            ))
        for url in pack.sitemaps:
            domain = str(urllib.parse.urlsplit(url).hostname or "")
            rows.append(self.frontier.discover(
                url,
                discovery_kind="sitemap",
                source_title=pack.title,
                priority=2.0 if domain in preferred else 1.5,
                metadata={"source_pack": pack.title, "container": True},
            ))
        for url in pack.feeds:
            domain = str(urllib.parse.urlsplit(url).hostname or "")
            kind = "atom" if "atom" in url.casefold() else "rss"
            rows.append(self.frontier.discover(
                url,
                discovery_kind=kind,
                source_title=pack.title,
                priority=2.0 if domain in preferred else 1.5,
                metadata={"source_pack": pack.title, "container": True},
            ))
        return rows

    def sync_directory(self) -> list[SourcePack]:
        packs: list[SourcePack] = []
        self.last_errors = 0
        for path in sorted([*self.directory.glob("*.md"), *self.directory.glob("*.markdown")]):
            check_cancelled()
            try:
                packs.append(self.load(path))
            except (OSError, UnicodeError, ValueError):
                self.last_errors += 1
        return packs

    def matching_packs(self, query: str, packs: Iterable[SourcePack]) -> list[SourcePack]:
        query_tokens = set(_meaningful_pack_tokens(query))
        technical_context = bool(query_tokens & TECHNICAL_QUERY_TOKENS)
        matches: list[tuple[float, SourcePack]] = []
        for pack in packs:
            pack_tokens = set(_meaningful_pack_tokens(f"{pack.title} {' '.join(pack.tags)}"))
            overlap = query_tokens & pack_tokens
            if len(overlap) < 2 and not (len(overlap) == 1 and technical_context):
                continue
            score = len(overlap) / max(1, len(pack_tokens))
            matches.append((score, pack))
        return [pack for _score, pack in sorted(matches, key=lambda item: (-item[0], item[1].title, item[1].path))]

    def matching(self, query: str) -> list[dict[str, Any]]:
        query_tokens = set(_meaningful_pack_tokens(query))
        technical_context = bool(query_tokens & TECHNICAL_QUERY_TOKENS)
        rows = self.db.conn.execute("SELECT * FROM source_packs ORDER BY title, path").fetchall()
        matches: list[dict[str, Any]] = []
        for row in rows:
            tags = json.loads(row["tags_json"] or "[]")
            searchable = set(_meaningful_pack_tokens(f"{row['title']} {' '.join(tags)}"))
            overlap = query_tokens & searchable
            if len(overlap) >= 2 or (len(overlap) == 1 and technical_context):
                matches.append({**dict(row), "match_score": len(overlap) / max(1, len(searchable))})
        return sorted(matches, key=lambda item: (-float(item["match_score"]), str(item["title"])))


class LocalDiscoveryProvider:
    """SearchProvider-compatible deterministic view of the persistent frontier."""

    name = "local"

    def __init__(self, db: Database, *, limits: LocalDiscoveryLimits | None = None):
        self.db = db
        self.limits = limits or LocalDiscoveryLimits.from_database(db)
        self.frontier = FrontierStore(db)
        self.packs = SourcePackStore(db, self.frontier)
        self._lock = threading.Lock()
        self.query_metrics: dict[str, int] = {
            "frontier_candidates": 0,
            "source_pack_hits": 0,
            "source_pack_errors": 0,
        }
        self._prepared_packs: list[SourcePack] | None = None

    def prepare(self) -> list[SourcePack]:
        self.frontier.seed_cached_documents()
        if self._prepared_packs is None:
            self._prepared_packs = self.packs.sync_directory()
            self.query_metrics["source_pack_errors"] += self.packs.last_errors
        return list(self._prepared_packs)

    def search(self, query: str, *, limit: int, timeout: float) -> list[ProviderSearchResult]:
        del timeout
        check_cancelled()
        packs = self.prepare()
        pack_hits = self.packs.matching_packs(query, packs)
        for pack in pack_hits:
            self.packs.activate(pack)
        allowed_pack_titles = {pack.title for pack in pack_hits}
        rows = self.frontier.search(
            query,
            limit=max(1, int(limit)) * 4,
            max_depth=self.limits.max_depth,
            allowed_source_packs=allowed_pack_titles,
        )
        with self._lock:
            self.query_metrics["frontier_candidates"] += len(rows)
            self.query_metrics["source_pack_hits"] += len(pack_hits)
        results: list[ProviderSearchResult] = []
        for position, row in enumerate(rows[: max(0, int(limit))], start=1):
            snippet = " ".join(
                value for value in (str(row["anchor_text"]), str(row["surrounding_text"])) if value
            )[:1000]
            results.append(
                ProviderSearchResult(
                    url=str(row["canonical_url"]),
                    title=str(row["source_title"] or row["anchor_text"] or row["domain"]),
                    snippet=snippet,
                    position=position,
                    provider=self.name,
                    query=query,
                    metadata={
                        "frontier_id": str(row["id"]),
                        "discovery_kind": str(row["discovery_kind"]),
                        "depth": int(row["depth"]),
                        "domain": str(row["domain"]),
                        "raw_lexical_rank": float(row["raw_lexical_rank"]),
                        "etag": str(row["etag"] or ""),
                        "last_modified": str(row["last_modified"] or ""),
                        **_json_object(row["metadata_json"]),
                    },
                )
            )
        return results

    def bound_candidates(
        self,
        candidates: list[dict[str, Any]],
        max_fetches: int,
        *,
        remaining_pages: int | None = None,
        excluded_urls: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        domain_counts: dict[str, int] = {}
        selected: list[dict[str, Any]] = []
        page_ceiling = self.limits.max_new_urls if remaining_pages is None else max(0, int(remaining_pages))
        excluded = set(excluded_urls or set())
        page_count = 0
        for candidate in candidates:
            if str(candidate["canonical_url"]) in excluded:
                continue
            domain = str(urllib.parse.urlsplit(candidate["canonical_url"]).hostname or "")
            if domain_counts.get(domain, 0) >= self.limits.max_urls_per_domain:
                continue
            is_metadata = bool((candidate.get("metadata") or {}).get("container"))
            if not is_metadata and page_count >= page_ceiling:
                continue
            selected.append(candidate)
            domain_counts[domain] = domain_counts.get(domain, 0) + 1
            if not is_metadata:
                page_count += 1
            if len(selected) >= max(0, int(max_fetches)):
                break
        return selected


class DiscoveryParser:
    def __init__(self, db: Database, frontier: FrontierStore, *, limits: LocalDiscoveryLimits | None = None):
        self.db = db
        self.frontier = frontier
        self.limits = limits or LocalDiscoveryLimits.from_database(db)

    def parse_sitemap(
        self, body: bytes | str, *, source_url: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(body)
        except (ET.ParseError, ValueError):
            return []
        local_name = _xml_name(root.tag)
        rows: list[dict[str, Any]] = []
        considered = 0
        for node in root.iter():
            check_cancelled()
            _ensure_discovery_deadline(deadline)
            if _xml_name(node.tag) != "loc":
                continue
            if considered >= self.limits.max_sitemap_entries:
                break
            considered += 1
            value = str(node.text or "").strip()
            try:
                row = self.frontier.discover(
                    value,
                    discovery_kind="sitemap",
                    discovered_from_url=source_url,
                    source_title="Sitemap",
                    depth=0 if local_name == "sitemapindex" else 1,
                    priority=0.8 if local_name == "sitemapindex" else 1.0,
                    metadata={"container": local_name == "sitemapindex"},
                )
            except FetchBlockedError:
                continue
            rows.append(row)
        return rows

    def parse_feed(
        self, body: bytes | str, *, source_url: str, deadline: float | None = None
    ) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(body)
        except (ET.ParseError, ValueError):
            return []
        is_atom = _xml_name(root.tag) == "feed"
        kind = "atom" if is_atom else "rss"
        feed_id = str(uuid.uuid5(uuid.NAMESPACE_URL, canonicalize_url(source_url)))
        channel_title = _first_xml_text(root, "title")
        with self.db.conn:
            self.db.conn.execute(
                """
                INSERT INTO discovery_feeds(id, canonical_url, kind, title, last_checked_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(canonical_url) DO UPDATE SET kind = excluded.kind, title = excluded.title,
                    last_checked_at = excluded.last_checked_at, failure_code = ''
                """,
                (feed_id, canonicalize_url(source_url), kind, channel_title, utc_ms()),
            )
        entry_tags = {"entry"} if is_atom else {"item"}
        rows: list[dict[str, Any]] = []
        considered = 0
        for entry in root.iter():
            check_cancelled()
            _ensure_discovery_deadline(deadline)
            if _xml_name(entry.tag) not in entry_tags:
                continue
            if considered >= self.limits.max_feed_entries:
                break
            considered += 1
            url = _feed_entry_url(entry, is_atom)
            if not url:
                continue
            title = _first_xml_text(entry, "title")
            summary = _first_xml_text(entry, "summary") or _first_xml_text(entry, "description") or _first_xml_text(entry, "content")
            published = _first_xml_text(entry, "published") or _first_xml_text(entry, "pubdate")
            updated = _first_xml_text(entry, "updated")
            try:
                canonical = canonicalize_url(url)
                row = self.frontier.discover(
                    canonical,
                    discovery_kind=kind,
                    discovered_from_url=source_url,
                    anchor_text=title,
                    surrounding_text=summary,
                    source_title=channel_title,
                    depth=1,
                    priority=1.2,
                    metadata={"published": published, "updated": updated, "feed_url": source_url},
                )
            except FetchBlockedError:
                continue
            with self.db.conn:
                self.db.conn.execute(
                    """
                    INSERT INTO discovery_feed_entries(
                        id, feed_id, canonical_url, title, summary, published_at, updated_at_text, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(feed_id, canonical_url) DO UPDATE SET title = excluded.title,
                        summary = excluded.summary, published_at = excluded.published_at,
                        updated_at_text = excluded.updated_at_text, observed_at = excluded.observed_at
                    """,
                    (str(uuid.uuid4()), feed_id, canonical, title, summary, published, updated, utc_ms()),
                )
            rows.append(row)
        return rows

    def robots_allows(
        self,
        url: str,
        fetcher: SafeHttpFetcher,
        *,
        explicit_manual: bool = False,
        deadline: float | None = None,
    ) -> RobotsDecision:
        if explicit_manual:
            return RobotsDecision(True)
        canonical = canonicalize_url(url)
        parsed = urllib.parse.urlsplit(canonical)
        domain = str(parsed.hostname or "")
        origin = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        cached = self.db.conn.execute("SELECT * FROM robots_cache WHERE domain = ?", (origin,)).fetchone()
        if cached is None:
            legacy = self.db.conn.execute(
                "SELECT * FROM robots_cache WHERE domain = ?", (domain,)
            ).fetchone()
            if legacy is not None:
                try:
                    legacy_url = canonicalize_url(str(legacy["robots_url"] or ""))
                    legacy_parsed = urllib.parse.urlsplit(legacy_url)
                    legacy_origin = urllib.parse.urlunsplit(
                        (legacy_parsed.scheme, legacy_parsed.netloc, "", "", "")
                    )
                except FetchBlockedError:
                    legacy_origin = ""
                if legacy_origin == origin:
                    with self.db.conn:
                        self.db.conn.execute("DELETE FROM robots_cache WHERE domain = ?", (domain,))
                        self.db.conn.execute(
                            """
                            INSERT OR REPLACE INTO robots_cache(
                                domain, robots_url, body, fetched_at, allowed, failure_code
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                origin,
                                legacy["robots_url"],
                                legacy["body"],
                                legacy["fetched_at"],
                                legacy["allowed"],
                                legacy["failure_code"],
                            ),
                        )
                    cached = self.db.conn.execute(
                        "SELECT * FROM robots_cache WHERE domain = ?", (origin,)
                    ).fetchone()
        if cached is not None:
            failure_code = str(cached["failure_code"] or "")
            if failure_code in {"robots_disallowed", "robots_unsupported_pattern"}:
                ttl_ms = ROBOTS_POLICY_TTL_MS
            elif failure_code:
                ttl_ms = ROBOTS_FAILURE_TTL_MS
            elif not bool(cached["allowed"]):
                ttl_ms = ROBOTS_POLICY_TTL_MS
            else:
                ttl_ms = ROBOTS_SUCCESS_TTL_MS
            age_ms = max(0, int(self.frontier.clock()) - int(cached["fetched_at"] or 0))
            if age_ms < ttl_ms:
                if failure_code:
                    return RobotsDecision(False, failure_code)
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(str(cached["robots_url"]))
                parser.parse(str(cached["body"] or "").splitlines())
                allowed = bool(parser.can_fetch(ROBOTS_USER_AGENT, canonical))
                return RobotsDecision(allowed, "" if allowed else "robots_disallowed")
        robots_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/robots.txt", "", ""))
        sitemap_candidates = 0
        try:
            _ensure_discovery_deadline(deadline)
            response = fetcher.fetch(robots_url, allowed_content_types={"text/plain", "text/html"})
            text = response.body.decode("utf-8", errors="replace")[: 512 * 1024]
            if robots_has_unsupported_pattern(text):
                allowed = False
                failure = "robots_unsupported_pattern"
            else:
                parser = urllib.robotparser.RobotFileParser()
                parser.set_url(robots_url)
                parser.parse(text.splitlines())
                allowed = bool(parser.can_fetch(ROBOTS_USER_AGENT, canonical))
                failure = "" if allowed else "robots_disallowed"
            seen_sitemaps: set[str] = set()
            declarations_considered = 0
            for line in text.splitlines():
                check_cancelled()
                _ensure_discovery_deadline(deadline)
                if not line.strip().casefold().startswith("sitemap:"):
                    continue
                if declarations_considered >= self.limits.max_sitemap_entries:
                    break
                declarations_considered += 1
                sitemap_url = line.split(":", 1)[1].strip()
                try:
                    sitemap_canonical = canonicalize_url(sitemap_url)
                    reject_literal_nonpublic_host(str(urllib.parse.urlsplit(sitemap_canonical).hostname or ""))
                except FetchBlockedError:
                    continue
                if sitemap_canonical in seen_sitemaps:
                    continue
                seen_sitemaps.add(sitemap_canonical)
                self.frontier.discover(
                    sitemap_canonical,
                    discovery_kind="sitemap",
                    discovered_from_url=robots_url,
                    source_title="robots.txt",
                    priority=0.8,
                    metadata={"container": True},
                )
                sitemap_candidates += 1
            if allowed and not seen_sitemaps:
                check_cancelled()
                _ensure_discovery_deadline(deadline)
                default_sitemap = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, "/sitemap.xml", "", ""))
                self.frontier.discover(
                    default_sitemap,
                    discovery_kind="sitemap",
                    discovered_from_url=robots_url,
                    source_title="Conventional sitemap",
                    priority=0.4,
                    metadata={"container": True, "conventional": True},
                )
                sitemap_candidates = 1
        except FetchError as exc:
            text = ""
            allowed = False
            failure = getattr(exc, "code", "fetch_failed")
        self.db.conn.execute(
            "INSERT OR REPLACE INTO robots_cache(domain, robots_url, body, fetched_at, allowed, failure_code) VALUES (?, ?, ?, ?, ?, ?)",
            (origin, robots_url, text, int(self.frontier.clock()), 1 if allowed else 0, failure),
        )
        self.db.conn.commit()
        return RobotsDecision(allowed, failure, sitemap_candidates)


def fts_match_expression(query: str) -> str:
    tokens: list[str] = []
    for token in FTS_TOKEN_RE.findall(str(query or "")):
        clean = token.strip(".-").casefold()
        if len(clean) < 2 or clean in tokens:
            continue
        tokens.append(clean)
        if len(tokens) >= 20:
            break
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in tokens)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.startswith("---\n"):
        raise ValueError("Source Pack requires YAML frontmatter")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Source Pack frontmatter is not closed")
    header = normalized[4:end]
    body = normalized[end + 5 :]
    result: dict[str, Any] = {}
    active_list: str | None = None
    for raw in header.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if raw.startswith("  - ") or raw.startswith("- "):
            if active_list is None:
                raise ValueError("Source Pack frontmatter list has no key")
            result.setdefault(active_list, []).append(raw.split("-", 1)[1].strip())
            continue
        if ":" not in raw:
            raise ValueError("Source Pack frontmatter contains an invalid line")
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        active_list = key if not value else None
        result[key] = [] if not value else value
    return result, body


def _safe_urls(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        try:
            canonical = canonicalize_url(str(value))
        except FetchBlockedError:
            continue
        if canonical not in output:
            output.append(canonical)
    return output


def reject_literal_nonpublic_host(host: str) -> None:
    clean = str(host or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(clean)
    except ValueError:
        return
    if not address.is_global:
        raise FetchBlockedError("literal non-public URL targets are not retained")


def robots_has_unsupported_pattern(text: str) -> bool:
    applies = False
    directives_seen = False
    for raw_line in str(text or "").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.casefold()
        if key == "user-agent":
            if directives_seen:
                applies = False
                directives_seen = False
            if value.casefold() in {"*", ROBOTS_USER_AGENT.casefold()}:
                applies = True
            continue
        if key in {"allow", "disallow"}:
            directives_seen = True
            if applies and ("*" in value or value.endswith("$")):
                return True
    return False


def _ensure_discovery_deadline(deadline: float | None) -> None:
    check_cancelled()
    if deadline is not None and time.monotonic() >= deadline:
        raise LocalBudgetExhausted("local discovery metadata deadline exhausted")


def _domain_from_value(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(urllib.parse.urlsplit(canonicalize_url(raw)).hostname or "")
    except FetchBlockedError:
        return raw.casefold().strip("./") if "/" not in raw else ""


def _unique_clean(values: Iterable[Any]) -> list[str]:
    output: list[str] = []
    for value in values:
        clean = _clean_text(value, 300)
        if clean and clean not in output:
            output.append(clean)
    return output


def _clean_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split()).strip()[:limit]


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in FTS_TOKEN_RE.findall(str(value or "")) if len(token) >= 2]


def _meaningful_pack_tokens(value: str) -> list[str]:
    return [token for token in _tokens(value) if token not in PACK_STOPWORDS]


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _hybrid_chunk_id(slot: dict[str, Any]) -> str:
    if slot.get("lexical") is not None:
        return str(slot["lexical"].chunk_id)
    if slot.get("dense") is not None:
        return str(slot["dense"][1].chunk_id)
    return ""


def _xml_name(tag: Any) -> str:
    return str(tag or "").rsplit("}", 1)[-1].casefold()


def _first_xml_text(root: ET.Element, name: str) -> str:
    expected = name.casefold()
    for node in root.iter():
        if _xml_name(node.tag) == expected and node.text:
            return _clean_text(node.text, 1000)
    return ""


def _feed_entry_url(entry: ET.Element, is_atom: bool) -> str:
    if is_atom:
        for node in entry:
            if _xml_name(node.tag) == "link":
                href = str(node.attrib.get("href") or "").strip()
                rel = str(node.attrib.get("rel") or "alternate").casefold()
                if href and rel in {"alternate", ""}:
                    return href
    return _first_xml_text(entry, "link")
