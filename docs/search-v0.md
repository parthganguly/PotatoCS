# Search v0

Search v0 is the first empirical public-web research baseline in Odysseus
Desktop. It is intentionally an information-retrieval pipeline with constrained
local-model judgment, not a browser agent.

## Implemented flow

```text
question
  -> bounded local-model query rewrite (original question always retained)
  -> indexed local Sources + one configured public Search provider
  -> canonical URL dedupe and title/snippet/rank prioritization
  -> public-address-only bounded HTTP GET
  -> block-aware Readability/native PDF/plain-text extraction
  -> stable contextual passages and BM25 ranking
  -> compact evidence dossier
  -> local model copies exact supporting quotes by passage ID
  -> deterministic quote verification
  -> optional one non-repeated repair query
  -> synthesis from surviving verified evidence IDs
  -> software resolves citation IDs and persists source observations
```

The normal path does not start Chromium, execute page JavaScript, submit forms,
use browser credentials, or give the model browser/shell tools.

## Deterministic and model responsibilities

Software owns URL normalization, provider calls, result deduplication, fetch
budgets, DNS/address checks, redirects, extraction, passage IDs, BM25,
query-history deduplication, round limits, quote matching, citation-to-URL
resolution, persistence, cancellation checkpoints, and metrics.

The selected local conversation model is used for three narrow judgments in
the text baseline: bounded query variation, exact evidence selection, and
final synthesis. If evidence selection requests more public research, a
separate repair-query call receives only public inputs. A second
evidence-selection call is used only when that query acquires a new passage and
the S-mode budgets permit it.
Malformed query JSON falls back to the original question. Malformed evidence
JSON falls back to deterministic contextual excerpts of at least 24 characters
and four tokens. That path is marked degraded in the answer, warning list,
metrics, and Operation Trace. Any selected support span must contain at least
eight characters and two tokens, so a bare value such as `2024` is rejected.
By contrast, a well-formed response with an empty `evidence` list is an
explicit abstention, not a parser failure. It may request the one bounded repair
round, but it never triggers deterministic quotation and ends as
`search_no_evidence` if repair also selects nothing.
An unavailable model fails the job without promoting an unsupported answer.

Fetched text is explicitly labeled as untrusted data in every evidence and
synthesis prompt. It cannot grant permissions or add tools. Only a validated,
bounded query from the separate public repair stage can cause another
acquisition.

## Provider and privacy configuration

The v0 real provider adapter is Brave Web Search. Set a user/process environment
variable and restart the app:

```powershell
$env:ODYSSEUS_BRAVE_SEARCH_API_KEY = "<key>"
$env:ODYSSEUS_SEARCH_PROVIDER = "brave" # optional; brave is the default
```

Do not commit the key. The adapter sends only each public search query and a
bounded result count. Local Sources, private retrieved passages, chats, and
evidence dossiers are never sent to the Search provider. Local-model inference
continues through the existing configured Ollama abstraction.

The deterministic fixture provider is test-only and requires no network or
credentials.

The public repair-query boundary is structural. The mixed local/web dossier
may tell the model that more research is needed, but any `next_query` emitted
from that context is discarded. A separate model prompt can see only the
original user question, prior public queries, and public-web passages. Local
Source text, filenames, and metadata therefore have no data path into a Brave
repair request. Brave also applies a provider-local one-request-per-second
throttle and one bounded `429` retry, honoring a numeric `Retry-After` up to
five seconds.

The throttle is scoped to one `BraveSearchProvider` instance. The current
single-worker FIFO Search queue serializes normal Search jobs within one
running app process. Multiple PotatoCS processes using the same key can still
exceed that policy. Before concurrent Search workers are added, throttling must
move to shared per-key state; that change is explicitly deferred from v0.

## Budgets

Defaults are profile settings with hard implementation ceilings:

| Setting | Default | Hard ceiling |
| --- | ---: | ---: |
| `search_max_queries` | 4 | 4 first-round queries |
| `search_results_per_query` | 5 | 10 |
| `search_max_fetches` | 8 | 8 total attempts |
| `search_max_response_bytes` | 2 MiB | 4 MiB per response, compressed and decoded |
| `search_max_concurrent_fetches` | 3 | 3 |
| `search_max_passages` | 12 | 15 |
| `search_max_dossier_chars` | 24,000 | 30,000 |
| `search_max_rounds` | 2 | 2 |
| `search_max_model_calls` | 5 | 5 |
| `search_timeout_seconds` | 90 | 300 |
| `search_second_round_enabled` | `true` | boolean |

The queue runs one heavyweight job at a time. Cancellation is cooperative at
provider, fetch-chunk, extraction, persistence, retrieval, and model-call safe
points. Before optional repair, software reserves model-call capacity for
repair query generation, second evidence selection, and mandatory synthesis.
If call or fetch budget is insufficient, repair is skipped and verified
round-one evidence is synthesized. With second-round Search enabled, two of
the default eight fetch attempts are reserved for repair: round one may use six
and round two may use the remaining two. T mode may use all eight. The hard
total remains eight, and unused repair capacity remains unused. A typed
provider/model/budget or Search-cache I/O failure inside optional repair is recorded as
`search.repair_failed`; verified round-one evidence survives and synthesis
continues in degraded mode. A blocking local-model request may take
until its bounded request timeout to reach the next safe point. Search jobs are
not resumed after a sidecar restart: orphaned persisted runs are marked
`interrupted`, and staged source writes are purged.

## Network and content security boundary

Research acquisition permits only read-only `GET`/`HEAD` over `http` or
`https`, on default ports. User information in URLs and all other schemes are
rejected. Every request and redirect target is DNS-resolved, every resolved
address must be globally routable, and the actual socket is pinned to a
validated address while HTTPS authenticates the original hostname. Loopback,
private, link-local, multicast/reserved, and metadata-style destinations are
therefore blocked. Proxies are not used by the fixed Search-provider request.

Redirect count, socket/deadline time, compressed bytes, decompressed bytes,
content type, and supported content encoding are bounded. Executable/unknown
content is rejected. HTML extraction uses `readability-lxml`; PDF text uses the
existing `pypdf` dependency. The retained HTML serializer inserts separators
only at semantic block, list, table-cell/row, code, and line-break boundaries,
so minified adjacent elements cannot fuse into invented tokens. SVG and MathML
subtrees are deliberately excluded from exact retained-text evidence in v0;
their interpretation belongs to the deferred visual path. Their presence
remains instrumentation. Select options receive semantic boundaries, while
ordinary inline composition such as `17.5%` and `1,234` remains intact.
JavaScript is never evaluated.

## Evidence and citation contract

Each retained evidence record identifies a source document and stable passage,
stores the exact quote, source offsets (or chunk/page location for existing
local Sources), provenance kind, and `verified_exact` status. Verification uses
Unicode NFC, line-ending normalization, and whitespace collapse only. Numbers,
punctuation, signs, identifiers, code, and negation are not relaxed.

“Verified” means the selected quote exists in the retained source
representation. It does **not** prove semantic entailment or that the model's
interpretation is correct. The Chat evidence card shows the exact quote so the
user can evaluate that relationship.

The synthesis model receives evidence IDs, not source URLs. Software replaces
only known IDs with numbered citations, omits unsupported IDs, removes any URL
invented in answer prose, and displays the retained canonical URL separately.

## Persistence and Operation Trace

Fetched pages reuse `documents`, `document_pages`, `rag_chunks`, and the
existing embedding cache/vector store. Additive document fields hold
`web`/`cached_web` origin, canonical/final URL, fetch time, HTTP metadata,
content hash, and extraction/visual-candidate metadata. They are internal
Search cache artifacts, not user-imported local Sources, and ordinary non-
Search RAG excludes them by default. New revisions remain staged until Search
succeeds and are purged on failure/cancellation. Identical URL/content
observations reuse the same cache artifact. When canonical content changes,
only the latest successful revision participates in Search retrieval; older
documents remain historical so stored citations stay resolvable. Current web
discovery still runs, and a cache hit does not suppress it. If cached artifacts
are visible in Sources, their `web`/`cached_web` origin remains explicit.

`search_runs` stores query history, budgets, outcome, round count, and measured
metrics. `search_evidence` stores verified spans. Neither table stores model
confidence, truth judgments, contradictions, source reputation, or final answer
prose. Normal chat messages still retain the user-visible answer as conversation
history, but that prose is never treated as evidence.

The existing per-answer Operation Trace includes Search operation names and
counts for queries/results/dedupe, fetches/failures/bytes, cache hits,
extraction/visual candidates, passages/dossier estimate, model calls/tokens,
round use, verified/rejected quotes, and wall time. Runtime values that are not
observable are not fabricated.

## T and S evaluation hooks

The fixture manifest is `evals/search_v0/manifest.json`. Run the offline proof:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\run-search-fixture-proof.ps1 -Mode all
```

The command first prints machine-readable observed metrics and citations, then
runs the matching controlled tests. This proof exercises Search orchestration,
extraction, retrieval, verification, and persistence, but stubs the actual
network transport and local model. SSRF/transport and malformed-peer behavior
are covered separately by focused fetcher tests. `-Mode T` runs the single-round text
baseline. `-Mode S` runs the same pipeline with one repair query. The RPC/job request also accepts
`second_round_enabled=false` so future benchmark runners can compare T and S
without changing pipeline code. Fixtures define stable evidence instead of
inventing ground truth for changing live facts.

## First live dogfood profile safety

Schema v11 is not readable by the previous committed build. The normal Windows
profile is `%APPDATA%\dev.odysseus.desktop\profiles\default`, and its database
is `app.db` inside that directory. Do not use the only copy of a primary profile
for the first live Search run.

Close PotatoCS before copying or moving profile directories. Use an isolated
Windows account, or preserve the original `default` directory under a clearly
named sibling backup and place a disposable copy at the `default` path for the
dogfood run. The new build may migrate only that disposable copy. Close the app
again before archiving the dogfood copy or restoring the untouched primary
directory. The deterministic proof and Python tests create temporary profiles
and never open the primary profile path.

## Deliberate exclusions and experiment gates

Search v0 deliberately excludes autonomous navigation, clicking, browser
credentials, arbitrary JavaScript, screenshot-first research, visual models,
learned visual/provider routing, rich claim or contradiction ledgers, recursive
research loops, multi-agent research, frontier escalation, and permanent model
conclusions.

Future V experiments may consume the recorded `very_low_text_yield`,
`js_shell_suspected`, canvas/SVG/chart, image-dominant, and scanned-document
signals, then produce provenance-labeled visual transcriptions through the same
evidence contract. Vision should remain selective unless measured rescue rate
justifies its time/memory cost.

Future P experiments should measure actual cross-session contribution from
`cached_web` Sources before calling it research memory. A richer research-state
or contradiction ledger should be attempted only if a controlled comparison
improves completeness or multi-hop success without unacceptable false
contradictions, anchoring, token use, or latency.

## Current limitations

- One real provider adapter (Brave) and environment-based secret configuration.
- No rendered-page fallback; JS-only and visual pages are instrumentation-only.
- Readability text flattens some layout semantics even though code/table text is
  retained.
- Exact quote verification proves existence, not entailment.
- Cooperative cancellation cannot interrupt a local-model HTTP call before its
  bounded timeout.
- Search jobs restart as interrupted rather than resuming mid-acquisition.
- Provider result snippets prioritize fetching but are never citation evidence.
