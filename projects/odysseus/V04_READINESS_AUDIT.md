# v0.4 Readiness Audit — Status Sources for First-Run Readiness

Status: audit only (backlog Issue 2, GitHub issue #12). No code changed.
Question answered: what exact existing signals can a first-run readiness
screen consume, and what is missing?

## 1. Summary

Readiness can already know today, from existing RPCs, with no backend work:
shell/backend liveness and degraded state (`app_status`, `backend_degraded`
event), Ollama installed/reachable/version/models (`models.detect_ollama`),
whether an approved chat model is installed (`OllamaStatus.models` +
`conversation_models` roles), embedding backend semantic-vs-lexical with a
human message (`rag.health` → `embedding`), lexical fallback being active
(`embedding.semantic === false`), OCR availability with per-dependency
detail and already-plain copy (`ocr.status`), heavy vision availability
(`diagnostics.get` → `image_vision.florence.ready`), and profile basics
(`app_status.profile_dir`, `diagnostics.get.db_path`).

Readiness cannot know today: RAM/CPU/disk headroom (no sensing anywhere in
`python/` or `src-tauri/`), whether an installed model is too heavy for the
machine, profile size on disk, or a queryable import/indexing job state
(progress is push-only events, not a pollable job list).

Verdict: **v0.4.1 can be built by wiring existing signals — no new
aggregation RPC is required.** See §6.

Scope §3 unknowns resolved:
- **C3 context defaults:** the app never sets `num_ctx`; chat generation
  options contain only `temperature` (`chat_service.py:344`). Context length
  is whatever Ollama's model default is. A conservative context default does
  not exist → confirmed Issue 4 scope.
- **D2 drag/drop:** exists. A window-level `onDragDropEvent` routes dropped
  paths to import (`src/App.tsx:284-308`).
- **G2 delete semantics:** `rag.delete_document` deletes chunk rows via
  `vector_store.delete_by_document` (`rag_service.py:164-168`,
  `vector_store.py:116`) but the document row is only flagged
  (`mark_deleted`, `document_service.py:461-473`) and the imported file copy
  created at `document_service.py:60-61` is **never unlinked**. Disk is not
  reclaimed → confirmed Issue 7 scope.

## 2. Proposed readiness rows

| Row | User-facing meaning | Source signal | File/function | States available today | Missing states/copy | New RPC? |
|---|---|---|---|---|---|---|
| App shell | "The app itself is running" | rendering at all; `AppStatus.backend_ready` | `src/tauri.ts:3-8`, `lib.rs:817-831` | ready | none (trivially true if UI renders) | no |
| Python backend | "The local engine is running" | `AppStatus.backend_degraded`; `backend_degraded` event | `lib.rs:817-831`, `lib.rs:883-897`, `backendStatus.ts:1-37` | ready / degraded | "checking/starting" state; noob copy beyond banner | no |
| Ollama runtime | "The AI runtime is installed and running" | `models.detect_ollama` → `installed`, `reachable`, `version` | `model_service.py:60-109`, `src/tauri.ts:97-108` | installed±, reachable±, version | copy for installed-but-not-running; install guidance (Issue 5) | no |
| Text chat model | "A chat model is available" | `OllamaStatus.models`, `conversation_models[].role === "chat"` + `installed` | `model_service.py:92-96`, `src/tauri.ts:110-119` | present / absent; per-model size/params | recommendation of *which* model (human decision, Issue 5) | no |
| Embedding model / doc search | "Document search quality" | `rag.health` → `embedding.semantic`, `.model`, `.message` | `rag_service.py:247-261`, `embedding_service.py:340-397`, `src/tauri.ts:364-372` | semantic / lexical, with message | message is semi-jargon ("Semantic retrieval active: nomic-embed-text"); needs noob copy | no |
| Lexical fallback | "Search still works, honestly labeled" | `embedding.semantic === false` + `"Lexical fallback active."` | `embedding_service.py:372-397` | active / inactive | honest user-facing copy ("basic keyword search until you install …") | no |
| OCR / scanned docs | "Scanned documents can be read" | `ocr.status` → `available`, `message`, `dependencies{tesseract,pdftoppm,mutool}` | `ocr_service.py:268-318`, `src/tauri.ts:374-389` | available / unavailable, per-dep found+path, plain message | none critical — copy already plain | no |
| Vision / heavy features | "Optional heavy features" | `diagnostics.get` → `image_vision.florence.ready`, `model_capabilities[].vision` | `rpc_server.py:236-250`, `src/tauri.ts:624-657` | ready / not ready, per-model vision capability | "heavy — may be slow on this machine" labeling (Issues 4/12) | no |
| Profile / storage | "Where your data lives" | `app_status.profile_dir`; `diagnostics.get.db_path` | `lib.rs:817-831`, `rpc_server.py:220-229` | location only | size, low-disk warning (Issue 7 — needs new RPC there) | not in v0.4.1 |
| Import/indexing activity | "Something is working in the background" | `operation_progress` events, fixed labels | `lib.rs:15-16`, `progress.py:28-46,92-101` | push events: stage, label, status, counts | pollable job list; cancel (Issue 6) | not in v0.4.1 |

## 3. Existing status sources

**`app_status` (Tauri command, `lib.rs:817-831`; TS `getAppStatus`,
`src/tauri.ts:1149-1151`).** Returns `{profile_id, profile_dir,
backend_ready, backend_degraded}`. Failure: never fails; degraded is a bool.
Privacy: `profile_dir` is a local path — fine in-app, must not enter a
support bundle. Fast (in-process lock read) → safe to poll on potatoes.

**`backend_degraded` event (`lib.rs:17,883-897` → `backendStatus.ts:7-16`).**
Push on transition, both directions. Payload is discriminator + bool only —
privacy-safe by construction. Zero polling cost; readiness should subscribe,
same as the banner.

**`retry_backend` (`lib.rs:864-879`, `user_retry` `lib.rs:546-579`).**
User-initiated restart; frontend already wraps it (`App.tsx:442-454`) and
swallows raw errors. Reusable as a readiness "Retry" action.

**`models.detect_ollama` (`model_service.py:60-109`).** Returns
`installed` (PATH check), `reachable` (loopback TCP, 0.5 s timeout),
`endpoint` (hardcoded `http://127.0.0.1:11434`, `model_service.py:18`),
`version`, `models[]`, `model_details[]`, `conversation_models[]` (role +
installed per model), `error`, `updated_at`. Failure: fields degrade to
empty; **`error` is a raw `str(exc)`** (`model_service.py:76-77`) — jargon,
must never render verbatim. Network is loopback-only. Cost: TCP probe +
two HTTP calls with 2–3 s timeouts — fine for on-demand re-check, do not
poll more than ~once per few seconds on a potato.

**`rag.health` (`rag_service.py:247-261`).** Returns `ok`, `version`,
document/chunk/cache counts, reindex counts, and `embedding`
(`backend/provider/model/cache_key/semantic/dimensions/message`,
`embedding_service.py:372-397`). Failure: provider selection itself probes
Ollama (`installed()`, `embedding_service.py:133-145` — TCP + `/api/tags`,
3 s timeout) and falls back to lexical status with a message; no raw
document content. Safe for first-run use; same polling caveat as above.

**`ocr.status` (`ocr_service.py:268-318`).** Returns `available`,
`engine_name`, `renderer`, `message` (already plain-language), and per-dep
`{found, path, source}`. Failure: pure local detection, cannot throw.
Privacy: dependency `path` values are local paths — fine in-app. Cost:
`shutil.which` plus several `glob` patterns including recursive ones
(`ocr_service.py:283-318`) — noticeable on an HDD; call on demand and cache,
don't poll.

**`diagnostics.get` (`rpc_server.py:220-251`).** Aggregates everything
including `ollama_ps`, model capabilities, Florence diagnostics, source
counts. It is the existing "aggregation RPC" — but heavyweight: it re-runs
detection, `ps`, and capability reads in one call, and returns local paths,
settings, and sidecar launch info. **Not suitable for first-run polling on
potato hardware**; suitable for the Diagnostics view it already serves.

**`operation_progress` events (`lib.rs:15-16,275`; `progress.py:146-181`).**
Fixed-vocabulary labels only (`progress.py:28-46`); identifiers are
UUID-validated; `detail` forced to `None`. Privacy-proofed by tests
(`test_trace_privacy_sentinel.py`). Push-only: readiness can show "busy"
but cannot enumerate or cancel jobs.

**`health.ping` (`rpc_server.py:210-218`).** Liveness + version + paths.
Used by the supervisor; readiness gets the same truth from `app_status`.

## 4. Missing or unsafe gaps

- **No first-run/onboarding UI exists.** `bootstrap()` (`App.tsx:380-440`)
  loads straight into the chat shell; a fresh profile shows an empty chat.
- **No unified readiness model.** Status lives in four unrelated shapes
  (`AppStatus`, `OllamaStatus`, `RAGHealth.embedding`, `OCRStatus`);
  nothing maps them to user-facing states.
- **Raw/jargony errors:** `OllamaStatus.error` (raw exception text),
  `EmbeddingStatus.message` (semi-technical), boot failure path renders
  `readError(err)` directly (`App.tsx:436-439`). All need translation.
- **No model recommendation** and no "model too big for this machine"
  warning (no RAM sensing anywhere) — Issues 5/10/4.
- **No hardware/RAM/disk status** of any kind — Issue 10, then 7/8.
- **No import/indexing cancel or pollable job state** — Issue 6.
- **No profile size / storage cleanup status**; delete does not reclaim the
  file copy (§1 G2) — Issue 7.
- **No embedding setup guidance** — detection exists, next-step copy does
  not (Issue 5).

## 5. Recommended v0.4.1 state model

Single enum for every row: `ready | missing | degraded | heavy | unavailable
| checking | error`.

| State | Icon/label idea | Noob copy style | Allowed next action |
|---|---|---|---|
| ready | green check, "Ready" | short confirmation ("Chat model installed") | none |
| missing | hollow circle, "Not set up" | name the gap + one concrete step ("Install Ollama to enable AI chat") | open setup step (Issue 5); Re-check |
| degraded | yellow dot, "Working with limits" | honest limit ("Search uses basic keyword matching until an embedding model is installed") | optional fix step; Re-check |
| heavy | weight icon, "Optional — heavy" | cost warning ("Vision features can be slow on this computer") | enable knowingly (Issue 4 labels) |
| unavailable | gray dash, "Not available" | plain reason, no blame ("Scanned-document reading needs Tesseract, which isn't installed") | guidance link; Re-check |
| checking | spinner, "Checking..." | none beyond label | wait; never blocks other rows |
| error | red dot, "Something went wrong" | fixed copy only — raw error text never renders (backendStatus.ts precedent) | Retry / Re-check |

Mapping is a pure frontend function (statuses in → rows out), unit-testable
like `backendBannerState` (`backendStatus.ts:30-37`).

## 6. New aggregation RPC decision: **No**

Do not add `readiness.get_status` for v0.4.1. The four cheap existing calls
are sufficient and already fetched together at boot (`App.tsx:399-411`):
`app_status` + `models.detect_ollama` + `rag.health` + `ocr.status`, plus
the `backend_degraded` event subscription. A new RPC would add backend
surface, a second source of truth to keep honest, and new privacy review —
while returning no information the frontend cannot already get. The heavy
existing aggregate (`diagnostics.get`) stays for the Diagnostics view only.
Frontend stitching also keeps working when the sidecar is degraded:
`app_status` is a Tauri command and still answers, which a Python-side
aggregation RPC could not.

Re-evaluate only when readiness needs data with no existing source
(hardware sensing, profile size — Issues 10/7); those justify *new narrow
RPCs*, not a v0.4.1 aggregate.

## 7. First-run trigger recommendation

Show the readiness view when any of: fresh profile (0 sessions and 0
user documents from `sessions.list`/`documents.list`, already fetched at
boot); no installed chat model (`conversation_models` has no
`role=="chat" && installed`); Ollama not reachable. Always reachable
manually from Diagnostics/Settings. After a degraded→recovered transition,
do **not** auto-open — keep the existing banner behavior; readiness offers
a "Check setup" link at most. Never auto-open over an active conversation.

## 8. Risks for implementation

- **Expensive checks on startup:** `ocr.status` globbing and Ollama probes
  add seconds on HDD potatoes. Mitigate: reuse the bootstrap results (rows
  render from already-fetched data), re-probe only on explicit Re-check.
- **Blocking UI while probing:** probes have 0.5–3 s timeouts; rows must
  render `checking` independently, never gate the shell on Promise.all.
- **Raw error leakage:** `OllamaStatus.error` and `readError` paths leak
  jargon; the row mapper must map to fixed copy, mirroring
  `backendStatus.ts`.
- **Frontend/backend status divergence:** readiness derives from the same
  RPCs the rest of the app uses — no second probe path, or rows will lie.
- **Readiness lying about fit:** "chat model: ready" when the model swaps
  an 8 GB machine to death. Until Issue 10/4, copy must claim presence,
  not performance ("installed", not "will run well").

## 9. Acceptance criteria for Issue 3 (first-run readiness panel, #13)

- Fresh profile (new `profile_dir`, empty DB) → readiness view on launch;
  existing profiles with sessions boot to chat unchanged.
- Pure mapping helper (statuses → rows) with unit tests covering: all-ready;
  Ollama uninstalled; Ollama installed but unreachable; no chat model;
  embeddings missing (lexical row = degraded, honest copy); Tesseract
  missing; backend degraded; raw `error` strings never appear in row copy.
- No private data in any readiness payload rendered or logged: no document
  text, no prompts; paths only where already shown in-app today.
- No network beyond existing loopback Ollama checks; zero non-user-initiated
  downloads (`test_no_egress.py` stays green).
- Degraded banner behavior unchanged (`npm run test:backend-status` green).
- Commands: `npm run test:backend-status`, `npm run test:progress`,
  `npm run build:frontend`, `cargo check --manifest-path src-tauri\Cargo.toml`,
  plus new mapper unit tests; `python -m pytest python\tests` only if any
  Python file is touched (audit says none is needed).

## 10. Final recommendation

Implement first-run readiness only after this audit is merged. Do not
implement Potato Mode defaults until hardware/resource audit is done.
