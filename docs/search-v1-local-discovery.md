# Search v1: local discovery

Search v1 is an experiment in owning a useful part of personal web discovery
with SQLite, observations PotatoCS has already retained, and small curated maps.
It is not an attempt to replace Google, Brave, or another global index.

> FTS5 searches only material PotatoCS already knows about. It does not create a global web index.

Search v0 remains the external-discovery control. Its `SearchProvider`, Brave
adapter, safe acquisition, extraction, evidence selection, exact-quote
verification, citation resolution, jobs, cancellation, and Operation Trace
remain intact. The profile setting `search_mode` selects `external` (the
default) or `local`. Local mode never silently invokes the external provider;
an exhausted local corpus/frontier ends with `search_no_evidence`. Any other
setting value fails as `search_provider_unconfigured` and constructs neither
provider.

## Existing infrastructure reused

Fetched pages and local Sources continue to use `documents`, `document_pages`,
`rag_chunks`, `embedding_cache`, and `SQLiteNumPyVectorStore`. Web observations
retain canonical/final URLs, response and extracted-text hashes, fetch time,
HTTP metadata, staging state, and current-revision state. Search v1 produces the
same `EvidencePassage` objects consumed by Search v0's evidence dossier,
3.2B-model selection, deterministic quote verification, synthesis, and citation
resolver. There is no second evidence representation.

Every page, robots file, sitemap, RSS document, and Atom document is acquired
through `SafeHttpFetcher`. The pinned public-address socket, original-host TLS,
redirect revalidation, proxy suppression, content/decompression bounds, and
cooperative cancellation therefore remain the only network boundary.

## FTS5 and hybrid retrieval

Schema v13 adds `local_fts_rows`, a small stable mapping from integer FTS rowids
to authoritative chunk/document IDs, and a contentless `local_fts` table. The
FTS table stores terms for `title`, `headings`, and `body`; retrievable text is
always joined back from `rag_chunks`. The active index is rebuilt
transactionally before local retrieval. It includes only:

- non-deleted, non-staged, indexed local Sources; and
- non-deleted, non-staged, current successful web revisions.

Historical web revisions remain available for old citation provenance but are
not active Search results. Failed staged artifacts are never indexed.

Ranking uses SQLite FTS5 `bm25()` with fixed weights `title=8`, `headings=4`,
and `body=1`. Lower raw BM25 values rank first and are retained in retrieval
metadata. No imaginary BM25 tuning or adaptive ranking is applied.

The existing dense candidates and FTS candidates are combined with equal-weight
reciprocal-rank fusion (`k=60`). A candidate found by either system remains
eligible; a candidate found by both receives both rank contributions. The raw
lexical rank, dense score, lexical rank, and dense rank are retained for
evaluation. There is no neural reranker. The existing evidence-selection model
remains the expensive semantic judgment downstream.

## Persistent frontier and unfetched links

`crawl_frontier` stores canonical URL, domain, primary discovery context,
depth, deterministic priority, first/last observation and fetch times, HTTP
validators, content hash, status, failure code, attempt count, last-attempt time,
and next-retry time. `crawl_discoveries` retains
multiple link relationships when different pages discover the same canonical
URL. Canonical URL uniqueness prevents duplicate acquisition.

The separate `frontier_fts` indexes only acquisition metadata: URL, anchor
text, small surrounding context, and source-page title. A matching unfetched
link may trigger bounded acquisition, but it is not an `EvidencePassage` and
cannot be cited. Only after the destination is safely fetched, extracted,
persisted, promoted as the successful current revision, and retrieved from
`rag_chunks` can its text enter the evidence dossier.

Normal relative links are resolved against the source URL. Unsupported schemes
and credentialed or non-default-port URLs are rejected by canonicalization.
Literal non-public IP targets are rejected before durable frontier insertion;
hostnames are not DNS-resolved during discovery. SSRF protection is enforced
again at fetch time after DNS resolution.

Transient acquisition failures are excluded from candidate ranking until a
deterministic exponential backoff expires (five minutes initially, capped at
24 hours). Robots denials and other blocked acquisitions use the `blocked`
state and are ineligible for six hours. These states and retry times survive a
restart, so a failed high-ranking URL cannot immediately consume every later
Search budget.

## Bounded acquisition and etiquette

Local mode applies deterministic profile limits with hard implementation caps:

| Setting | Default | Hard cap |
| --- | ---: | ---: |
| `search_local_max_new_urls` | 4 | 8 |
| `search_local_max_total_bytes` | 4 MiB | 8 MiB |
| `search_local_max_depth` | 2 | 3 |
| `search_local_max_urls_per_domain` | 3 | 8 |
| `search_local_max_sitemap_entries` | 100 | 500 |
| `search_local_max_feed_entries` | 50 | 200 |
| `search_timeout_seconds` | 90 s | 300 s |

One `LocalRunBudget` is shared by the initial and optional repair rounds.
`max_new_urls` is the total page-request ceiling for the Search run; the repair
round receives only the remainder. `max_total_bytes` is also a run total. Each
request is given no more than the remaining byte allowance, in addition to the
existing per-response limit. The run records `page_fetches`,
`metadata_fetches`, `network_requests_total`, and `bytes_downloaded_total`.
Robots, sitemap, RSS, and Atom requests count as metadata and contribute to the
network and byte totals without consuming the page ceiling. The local wall
deadline is threaded through robots, sitemap, feed, and page processing.
If the temporary remaining aggregate allowance causes a response-size limit,
the run stops without assigning a remote failure or retry penalty to that URL.
A response that exceeds the normal per-response safety ceiling still receives
the existing `fetch_limit` failure/backoff semantics.

Local candidates are fetched sequentially, making per-domain/order behavior
deterministic and avoiding a shared SQLite connection across worker threads. Search is the only acquisition
trigger: there is no daemon, scheduler, idle crawler, startup spider, browser,
JavaScript fallback, form submission, authentication bypass, CAPTCHA bypass,
or paywall bypass.

Automatic candidates consult `robots.txt` first. Policies are cached by
canonical origin (`scheme + hostname + normalized default port`), so HTTP and
HTTPS policies for one hostname remain independent. Legacy hostname-keyed rows
are lazily migrated only when their stored robots URL matches the requested
origin. A failed robots acquisition
is conservatively treated as disallow for that Search. Successful robots
responses are cached for 24 hours, transient fetch failures for five minutes,
and denials/unsupported policy for six hours; later automatic acquisition
revalidates after the TTL. Applicable `Allow` or `Disallow` rules containing
`*` or terminal `$` fail closed as `robots_unsupported_pattern`, because v1
does not claim syntax support beyond `urllib.robotparser`.

Explicit manual seeds may follow the existing explicit-fetch policy. Parsed
`Sitemap:` declarations are cancellation/deadline checked, canonicalized,
deduplicated, and capped by `max_sitemap_entries` before durable expansion;
when none is declared, `/sitemap.xml` is added only as a low-priority candidate
for that already-known domain. `ETag` and `Last-Modified` are retained and sent
as conditional request headers on later failed/retry candidates. A scheduler
and 304-specific refresh workflow are deferred.

## Sitemaps, RSS, and Atom

XML is parsed with the standard library under entry ceilings. Sitemap indexes
produce more candidates using the canonical `container` metadata flag; those
children are fetched and parsed as sitemap documents rather than sent through
HTML extraction. URL sets produce page candidates. Fetched containers never
become evidence, and already-fetched frontier status terminates A→B→A sitemap
cycles without arbitrary recursion.
Malformed XML yields no candidates. Unsafe URLs are skipped. The crawler does
not ingest an entire large sitemap merely because it exists.

RSS and Atom entries retain URL, title, short summary, published/updated text,
feed identity, and observation time in `discovery_feeds` and
`discovery_feed_entries`. Duplicate entries update the observation. Feed and
sitemap metadata is discovery metadata, not verified page evidence. Entry text
can become evidence only after the entry URL is fetched through the normal
pipeline.

## Source Packs

Source Packs are plain Markdown files with a deliberately small YAML-like
frontmatter subset. They require no model call and are easy to diff and
version. Packs placed in a profile's `source-packs` directory are parsed at
local discovery time.

```markdown
---
type: source-pack
title: Python
tags:
  - software
  - python
preferred_domains:
  - docs.python.org
---

# Official documentation

- https://docs.python.org/

# Feeds

- https://fixture.example/python/feed.atom

# Notes

Prefer official documentation for runtime semantics.
```

Supported semantics are seed URLs, sitemaps, RSS/Atom feeds, topic tags,
preferred domains, and human notes. A conservative deterministic gate matches
only title and explicit tag tokens: either two meaningful tokens must overlap,
or one must overlap in a query that also contains technical context. Stopwords
are ignored, and arbitrary prose notes are not matching material. A zero-match
result activates no packs. Matching happens before seed/feed/sitemap injection,
so unrelated packs are not added to the active frontier. URLs on a pack's
`preferred_domains` receive a small fixed priority increase; this affects
ordering, not evidence authority. Each file is parsed independently, and a
malformed pack is skipped with a content-free warning/trace counter while
valid packs continue. Packs merely tell software where useful material may live; they
do not declare truth, create evidence, establish canonical authority, or form a
trust graph. Fixture packs for Python, SQLite, Ollama, and a PotatoCS test
domain live under `python/tests/fixtures/search_v1/source_packs`; automated
tests do not contact those domains.

## End-to-end local flow

```text
question
  -> active local/cache FTS5 candidates
  +  existing dense candidates
  -> deterministic reciprocal-rank fusion
  +  matching unfetched-link and Source Pack candidates
  -> bounded robots-aware acquisition (when needed)
  -> existing extraction and staged web persistence
  -> promote successful current revision
  -> same EvidencePassage dossier
  -> same local-model evidence selection
  -> same exact-quote verification and citation resolution
  -> promote every complete local observation atomically
  -> evidence_found | no_evidence
```

Software combines cheap sources deterministically. The model is never asked to
choose a tool or manage the frontier. It sees retrieved evidence, not crawler
state.

In local mode, a successfully fetched, extracted, and persisted page is a
useful observation even when no quote verifies the current answer. At the
terminal `no_evidence` boundary, all complete staged observations are promoted
to current `cached_web` documents and their matching frontier rows become
`fetched` in the same database transaction. Their chunks can participate in a
later Search-v1 retrieval, while ordinary non-Search RAG continues to exclude
web-origin material by default. Fetch/extraction/persistence failures are not
promoted. Cancellation before that boundary rolls staged pages back and does
not claim frontier success.

Deleting the final live current `web`/`cached_web` observation resets its
frontier row to `unfetched`, clears stale validators/hash/retry fields, and
preserves discovery relationships and anchor history so the URL can be
reacquired. Deleting a historical revision while another current observation
survives leaves the frontier fetched. Deleting an ordinary local Source does
not affect frontier state. Both soft document deletion and hard purge apply
the same rule.

## Persistence, trace, and corpus metrics

Search v1 stores observations: URLs, discovery relationships, anchors,
surrounding context, feed entries, sitemap candidates, fetch metadata, hashes,
and extracted documents. It does not persist model beliefs, truth confidence,
or answer conclusions as retrieval evidence.

Additive Operation Trace names are `search.local_fts`,
`search.local_dense`, `search.local_fusion`, `search.frontier_candidates`,
`search.fetch_started`, `search.sitemap_discovered`,
`search.feed_discovered`, `search.source_pack_match`, and
`search.local_no_evidence`. Metrics are counts/timings only and never contain
private passage text.

`LocalSearchIndex.metrics()` exposes current documents, current chunks, FTS
rows, current cached web documents, known unfetched URLs, and represented
domains. The offline proof reports SQLite page-count size and reports FTS/RAM
size as unavailable when the runtime does not expose `dbstat` or process-memory
instrumentation rather than fabricating a value.

## Offline proof

Run the deterministic L1-L7 proof and focused tests without network access:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-search-v1-fixture-proof.ps1
```

The proof reports question, discovery path, lexical/dense/fused/frontier
candidates, fetches, cache hits, model calls, verified evidence, final status,
and wall time for existing-corpus, unfetched-link, Source Pack, unknown-web,
exact-identifier, and semantic-paraphrase scenarios. L2 is specifically a
mechanical proof that a relevant stored anchor triggers one bounded destination
fetch and only fetched destination text becomes evidence. L3 reports a real
Source Pack match before activation. L5 and L6 isolate fusion mechanics with
stipulated dense/lexical fixtures; they do not measure a production embedder.
L7 performs two Searches: page A is retained after `no_evidence`, then its
persisted link allows Question B to acquire page B and verify an answer without
refetching page A.

## Known limitations and deferred variables

Coverage is limited to material already imported/cached or reachable from known
links, Source Packs, feeds, and sitemaps. Unknown parts of the global web remain
unknown; `no_evidence` is therefore an expected successful experimental
outcome. FTS synchronization is a full active-index rebuild suitable for the
current single-user laptop scale, not millions of pages. Robots caching has no
background refresh scheduler and deliberately fails closed on unsupported
wildcard/end-anchor syntax. Feed dates remain source-provided text rather
than a normalized truth claim. Exact-quote verification proves textual
presence, not semantic entailment.

Explicitly deferred: Crawl4AI, Marker, neural reranking, DSPy,
structured-decoding libraries, any external generic Search provider fallback,
and visual Search. Also deferred are browser automation, autonomous agents,
recursive research, learned routing, background crawling, and a trust graph.
Source Pack metadata may remain sticky after later ordinary-link rediscovery;
generic tags such as `software` may activate several technical packs; and the
fixed preferred-domain priority increment is intentionally weak. These remain
dogfood observation targets rather than classifier/ranking work for v1.
