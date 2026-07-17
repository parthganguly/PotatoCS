# v0.4 Essential Semantics Contract

Status: engineering contract for Issues #16 (indexing control), #17 (storage
and deletion), #18 (diagnostics and support bundle). Written before
implementation; implementation must match this document or change it in the
same PR with justification. Baseline commit: `e8a36451` on `main`.

Audience: future maintainers and smaller models. Where this contract and code
disagree, treat it as a defect in one of them and fix the disagreement —
never silently diverge.

## Architectural ground truth this contract is built on

1. The Python sidecar processes JSON-RPC requests **serially** from stdin
   (`python/rpc_server.py: main`). A blocking request blocks every later
   request.
2. The Rust host serializes all `rpc_call`s behind `Mutex<BackendClient>`
   (`src-tauri/src/lib.rs`) and reads until the matching response id.
3. Therefore a cancel command can never interrupt an in-flight blocking RPC.
   Cancellable heavy work **must** run on a background worker thread inside
   the sidecar, with short RPCs for submit/status/cancel. The precedent is
   `CampaignService` (worker thread, its own `Database` connection, WAL,
   cooperative cancel flag, interrupted-state recovery at startup).
4. Progress events already travel out-of-band (sidecar stderr → Rust →
   `operation_progress` Tauri event) with a fixed-vocabulary label contract
   (`progress.py`), so job progress needs no new channel.
5. Non-idempotent RPCs are never replayed after sidecar loss
   (`can_restart_and_retry` allowlist in `lib.rs` contains reads only).

## A. Heavy-job state machine

A **heavy job** is a unit of background work owned by the sidecar job
worker. v0.4 job kinds: `import` (import + index one file, including the OCR
fallback path) and `ocr` (OCR + reindex an existing committed document).

### States

| State | Meaning | Terminal |
|---|---|---|
| `queued` | Accepted, waiting for the single worker | no |
| `preflighting` | Validating input, checking guardrails (page count, pixel ceiling) | no |
| `running` | Doing work (extract / OCR / chunk / embed / commit) | no |
| `cancel_requested` | User asked to cancel; job has not yet reached a safe point | no |
| `cancelled` | Job stopped at a safe point; rollback complete | yes |
| `completed` | Work committed; source visible | yes |
| `failed` | Work stopped on error; rollback complete | yes |

### Legal transitions

```
queued        -> preflighting | cancel_requested
preflighting  -> running | failed | cancel_requested
running       -> completed | failed | cancel_requested
cancel_requested -> cancelled | completed | failed
```

- `cancel_requested -> completed` is legal only when the cancel arrived
  after the job passed its final commit boundary; the job reports
  `completed` and the UI shows the source. Cancel never un-commits.
- Nothing transitions out of `cancelled`, `completed`, or `failed`.
- A job id never repeats and is never reused after restart.

### Pause: deferred

Pause is **not implemented in v0.4**. A genuine pause would have to persist
partially staged import state (extracted pages, partial embeddings) across an
arbitrary gap during which the embedding backend, model, or settings can
change, making resume semantics ambiguous. Cancel + re-import is cheap at the
document sizes v0.4 supports. No pause control appears in the UI; the state
machine has no paused state. This deferral is recorded, not hidden.

### Rules

1. **Cancel is idempotent.** `jobs.cancel` on any state returns the current
   job snapshot; repeated calls are no-ops. Cancelling a terminal job does
   not change its state.
2. **Cancel never replays an RPC.** Cancel is a new, short, idempotent RPC.
   `jobs.submit` is non-idempotent and stays off the host's
   restart-and-retry allowlist; `jobs.status`/`jobs.list` are reads and may
   be added to it.
3. **Cancel never kills the sidecar.** The mechanism is a cooperative flag
   (`threading.Event`) checked at bounded safe points, plus terminating any
   *current page's* OCR/render subprocess where safe. Killing the sidecar
   remains only the host's last-resort shutdown path, unrelated to job
   cancel.
4. **Safe points** (checked, in order, at least at): job start; document
   preflight; after page-extraction; before/after each OCR page render;
   between OCR subprocess invocations for a page's variant passes (coarse:
   per page is the guaranteed granularity); after chunk production; between
   embedding batches; immediately before the final DB commit. A cancellation
   check never runs inside an open write transaction.
5. **Progress vocabulary is fixed.** Job progress reuses the existing
   fixed labels and adds only fixed new stages (registered in
   `progress.py:_FIXED_LABELS`). No raw errors, paths, filenames, document
   text, or RPC payloads in any progress event or job status payload. Job
   status carries fixed state names, counts, and duration only, plus a fixed
   `message_code` drawn from a closed set the frontend maps to plain copy.
6. **A cancelled import is never visible as a source.** See §B.
7. **Restart does not resurrect work.** The job registry is in-memory. On
   sidecar restart the queue is empty by construction; startup repair (§B)
   deletes any staged partial state left by a dead process. Cancelled or
   interrupted work can only be re-run by an explicit new user action.
8. **The UI can distinguish cancelling from failed.** `cancel_requested`
   surfaces as "Cancelling…", `cancelled` as "Cancelled", `failed` as
   "Failed" — three distinct states, never conflated. A job that stops
   because of cancel is recorded `cancelled` even if the interruption
   surfaced internally as an exception.

### Queue

One worker thread; one job executes at a time; FIFO order. The queue is
bounded (structural constant, 32 pending); submissions beyond the bound are
rejected with a fixed message. This matches the "one bounded heavy-job
queue" requirement; nothing in the current architecture proves a higher
concurrency safe on potato hardware.

## B. Import/OCR atomicity

### Model: staged rows, visible only on commit

- **Before import**: no database or file state for the new source exists.
- **During import (staging)**: the job creates, in this order:
  1. the copied file under `<profile>/files/documents/<document_id>.<ext>`;
  2. a `documents` row with `is_staging = 1` (a dedicated column, like
     `is_internal` — **not** a `status` value, because the existing
     `mark_indexing`/`mark_indexed`/`mark_ocr_*` helpers legitimately
     overwrite `status`/`index_status` mid-job and would destroy a
     status-encoded marker);
  3. `document_pages`, `ocr_pages`, `rag_chunks`, `embedding_cache` rows as
     work proceeds, with the normal `status`/`index_status` progression.
  Staging rows are excluded from `documents.list`, `sources.list`, and
  vector similarity search (explicit `is_staging = 0` filter — a staging
  document legally reaches `index_status='indexed'` before commit, so the
  status filter alone is not sufficient), and every user surface. They are
  reachable only through the job API and `documents.get` by id.
- **Visibility / commit**: the last act of a successful import job is the
  status flip to the normal terminal state (`indexed` / `ocr_indexed` /
  `low_text` per existing semantics). Only after that flip does the source
  appear anywhere.
- **Rollback (cancel or failure)** deletes, in this order: the `documents`
  row (CASCADE removes `document_pages`, `ocr_pages`, `rag_chunks`), then
  the copied file, then any temporary render files. Embedding-cache entries
  written during the job are left in place: they are keyed by content hash,
  shared, bounded, and reclaimed by the §D cache cleanup — compensating
  deletion there would risk deleting entries other documents share.
- **OCR jobs on an existing committed document** must not destroy the
  committed document on cancel. OCR performs no destructive DB write until
  all pages are processed (`replace_pages_from_ocr` runs after the full page
  loop), so cancel during the render/OCR page loop only requires restoring
  the document's status fields to their pre-job values. The OCR **commit
  sequence** (`replace_pages_from_ocr` → reindex → `mark_ocr_indexed`) spans
  multiple transactions and is therefore **shielded from cancellation**
  (`cancellation.shield()`): no cancellation check fires inside it, a cancel
  requested during it resolves as `completed`, and the embedding-batch
  checkpoints inside reindex are inert while shielded. A mid-commit
  *failure* (not cancel) leaves `ocr_status='needed'` with partial OCR rows
  present, which a later OCR run replaces idempotently.
- **Process crash repair**: at sidecar startup, any `documents` row with
  `status='staging'` belongs to a dead process (single-instance assumption,
  same as the existing `recover_interrupted_*` recovery); repair hard-deletes
  those rows and their copied files, logging only fixed labels and counts.
  Any committed document left with `ocr_status='running'` is reset to
  `ocr_status='needed'` at startup.
- **Duplicate import** keeps the existing behavior: importing the same file
  twice creates two independent sources (content hash is stored, dedup is
  not a v0.4 feature). This is recorded as accepted, not accidental.
- **Legacy partial states**: rows from older versions cannot have
  `status='staging'` (new value), so repair never touches pre-v0.4 data.
  Existing `pending`/`error` documents keep their current meaning and
  recovery paths (reindex).

Why staging + hard rollback instead of compensating cleanup: the CASCADE
foreign keys already give transactional-grade cleanup for every derived row
in one statement, and a single hidden-status flag makes "visible iff
committed" checkable by tests with one query.

The synchronous RPCs `documents.import` and `sources.import` remain,
unchanged, for compatibility (tests, scripted callers). The UI import path
moves to the job API. The synchronous path keeps its current non-atomic
semantics and is documented as such.

## C. OCR safety policy

Grounded in the Issue #20 audit: fixed 400-DPI rendering legally produced a
~115-megapixel page image and ≥1.5 GB sidecar working set; no cap exists.

### Structural invariants (implemented now)

All constants live in one place (`ocr_service.py`, top of file), are
importable by tests, and are documented as **structural safety ceilings —
not measured Potato Mode defaults** (Issue #14/#20 still own tuning).

| Constant | Value | Meaning |
|---|---|---|
| `OCR_MAX_RENDER_PIXELS` | 40,000,000 | Hard ceiling on rendered pixels per page (matches the host's `MAX_CAPTURE_PIXELS`) |
| `OCR_MIN_RENDER_DPI` | 120 | Lowest DPI adaptive downscale may choose |
| `OCR_MAX_PAGES` | 400 | Hard ceiling on pages OCR'd per document |
| `OCR_PDF_RENDER_DPI` | 400 (existing) | Preferred DPI when under the ceiling |
| `EMBEDDING_BATCH_SIZE` | 16 | Texts per embedding call (bounds memory; cancel checkpoint between batches) |

### Preflight algorithm (before any rasterization)

For each page, read the PDF mediabox via pypdf (already a dependency; no
rendering):

1. `width_in = mediabox_width_pts / 72`, same for height; treat missing,
   zero, negative, non-finite, or absurd (> 10,000 pt) dimensions as
   guardrail violations, not exceptions.
2. Prospective pixels at DPI d: `px(d) = ceil(width_in*d) * ceil(height_in*d)`
   computed in floats with explicit caps — no unchecked integer products.
3. Choose the largest `d ≤ OCR_PDF_RENDER_DPI` with
   `px(d) ≤ OCR_MAX_RENDER_PIXELS` (closed form via square root, then
   verify). If `d ≥ OCR_MIN_RENDER_DPI`, render that page at `d`
   (adaptive downscale; recorded in page metadata as `render_dpi`).
4. If even `OCR_MIN_RENDER_DPI` exceeds the ceiling, the page is not
   rendered and the document's OCR stops with the fixed message below.
5. If page count exceeds `OCR_MAX_PAGES`, OCR stops in preflight with the
   fixed page-limit message. No partial silent OCR of "just the first N".

The giant image is therefore never allocated: the decision is made from
metadata before the renderer runs.

### Rendering changes

- Pages are rendered **one at a time** (`pdftoppm -f N -l N` /
  `mutool draw` page range) into the job's temporary directory; each page's
  render and derived preprocessing images are deleted before the next page
  starts. Cancellation is checked between pages.
- Temporary directories are always cleaned (context manager), including on
  cancel and failure.

### User-visible fixed copy (exact strings, frontend maps codes to these)

- `ocr_page_too_large`: "One of this document's pages is too large to read
  safely on this computer. Try a version of the document with smaller
  pages."
- `ocr_too_many_pages`: "This document has more pages than this app will
  read in one go (limit: 400). Split the document and import the parts you
  need."
- No override switch ships in v0.4. If a power-user override is ever added
  it is an Issue #14-style settings decision, not a guardrail bypass hidden
  in this slice.

## D. Storage and deletion semantics

### What "Delete source" means (documents)

Deleting a document source reclaims everything the app created for it:

| Item | Action |
|---|---|
| `rag_chunks` rows | Hard `DELETE` |
| `document_pages` rows | Hard `DELETE` |
| `ocr_pages` rows (incl. OCR text) | Hard `DELETE` |
| Imported file copy (`files/documents/<id>.<ext>`) | Deleted from disk after path validation |
| Generated previews/thumbnails | Documents have none today (thumbnails belong to artifacts); if added later they follow the same rule |
| `documents` row | Hard `DELETE` when no `message_documents` reference exists; otherwise a **tombstone** (`is_deleted=1`, existing behavior) so chat history keeps rendering "attachment deleted" — but its derived rows and file copy are still purged as above |
| Benchmark/report references | Benchmark rows reference documents only by recorded ids in JSON result columns; they are historical records and are not rewritten. Reports never resolve deleted documents. |
| `embedding_cache` rows | Not deleted per-document (shared by content hash). Reclaimed by the cache cleanup action below. |
| Active jobs | Deletion of a document with an active/queued job for it first requests cancel and waits for the job to reach a terminal state (bounded wait); if the job cannot stop in time the delete fails with a fixed "busy" message and remains retryable. Never both at once. |

Tombstone-vs-hard choice, justified: hard deletion is the honest default
(bytes actually reclaimed); the tombstone survives only where a foreign key
(`message_documents … ON DELETE RESTRICT`) proves chat history references
the document, and even then all content-bearing derived data and the file
copy are removed. Chat history keeps the title the user already saw.

Artifact (image) deletion already has its own path (`artifacts.delete`);
Issue #17 verifies it removes the stored file and leaves no orphans, and
fixes it if it does not, but does not redesign it.

### Path safety (mandatory, fail closed)

Before deleting any file:

1. Resolve the profile root once (`Path.resolve()`).
2. The recorded `stored_path` must not be a symlink/junction
   (`Path.is_symlink()` and Windows reparse-point check via `os.lstat`
   attributes); resolve it and require the resolved path to be strictly
   inside `<profile>/files/documents` (or the owning app directory for other
   categories).
3. Any check failure aborts the file deletion with a fixed message; DB
   deletion of derived rows still proceeds (rows are unambiguous app data),
   and the failure is reported honestly (`file_removed: false`).
4. The user's original external file (`source_path`) is **never** touched by
   any code path. No deletion API accepts an arbitrary path.

### Idempotency and honesty

- Deleting an already-deleted source returns the same shape with zeroed
  reclaim numbers; missing files are reported as `file_missing`, not as
  reclaimed bytes.
- `bytes_reclaimed` counts only bytes verified gone: file sizes measured
  before deletion and confirmed removed. DB row bytes are not estimated;
  the storage report shows database size separately.
- Partial filesystem failure (e.g. Windows file lock): DB purge commits,
  file deletion failure is reported, the source shows as deleted with a
  pending-file note in the storage report's `orphan_files` count; the
  cleanup action retries orphan removal.

### Storage status RPC (`storage.status`)

Returns only aggregates — never file lists or paths beyond the profile root
itself:

- profile root (single known path, shown so the user can find their data),
  total profile bytes;
- category breakdown: database (app.db + WAL + SHM), documents (imported
  copies), images/artifacts, logs, models, other. (Amended by Issue #17:
  campaign reports live outside the profile — default `~/Downloads` — and
  support bundles do not exist until Issue #18, so neither is a profile
  category; the optional Florence model pack is counted but never cleanable.)
- per-source stored bytes come from the existing `sources.list` summaries
  (`size_bytes`), not from a new file walk;
- free disk bytes for the volume containing the profile;
- `low_disk: true` below a structural threshold (1 GiB free — structural,
  not tuned).

### Cleanup actions (explicit, user-initiated, fixed-scope)

Amended by Issue #17 (`V04_STORAGE_CLEANUP_DESIGN.md` §9/§12): cleanup is
exposed as one read (`storage.cleanup_preview`) plus one mutation
(`storage.cleanup`) sharing the same candidate enumeration, so the preview
provably matches execution. The mutation covers exactly three allow-listed
categories:

1. **embedding cache**: delete embedding-cache rows whose
   `(content_hash, embedding_model)` no longer matches any live chunk, then
   report rows + blob bytes (`VACUUM` is **not** run automatically — DB file
   shrink is reported honestly as "space becomes reusable inside the
   database file").
2. **orphan files**: delete files directly under app-owned directories
   (`files/documents`, `files/artifacts/*`) not referenced by any live DB
   row, with the same path-safety rules and a structural minimum-age guard.
   Reports count + bytes.
3. **legacy deleted sources**: purge derived rows and owned files of
   pre-v0.4 soft-deleted (`is_deleted=1`) documents/artifacts; the row
   itself is hard-deleted only when chat history holds no reference.

Cleanup is refused with a fixed message while any heavy job is active. Log
rotation is out of scope for v0.4 (logs are bounded and fixed-label).

## E. Diagnostics redaction policy (allowlist)

The support surface is built **only from an allowlist**; anything not
explicitly allowed is forbidden by default. Assembly code constructs every
field explicitly — no pass-through of raw dicts, no `str(exception)`.

### Allowed

- App/backend versions, schema version, sidecar Python version (major.minor).
- Coarse OS facts: platform name, major release, CPU core count, total RAM
  rounded to GB.
- Readiness booleans: backend running; Ollama reachable; chat model
  installed (bool + model *name* only when it is one the user installed and
  it appears in the installed-models list); embedding model installed;
  retrieval mode (`semantic` | `lexical`); OCR available (bool per
  dependency name: tesseract/renderer found true/false — names of the tools,
  never their paths).
- Storage aggregates from §D (byte counts, low-disk boolean; the profile
  root path is **excluded** from the bundle — it contains the username).
- Job states: fixed state names, fixed message codes, counts, durations.
- Counts: documents, chunks, sessions (counts only).
- Durations/timings already produced as numbers.
- Success/failure states as fixed labels; migration/schema versions.

### Forbidden (non-exhaustive; allowlist governs)

Prompts; model responses; document text; OCR text; filenames; source
titles; absolute or relative paths; usernames; URLs from user content;
images or image metadata; raw exception strings; tracebacks; raw RPC
payloads; environment variables; API keys/tokens; clipboard data; database
contents; full command lines; anything typed by the user.

### Surfaces

1. **Copy diagnostics**: a fixed-schema JSON payload (sorted keys) built by
   `diagnostics.summary`, rendered by the frontend as plain-language rows
   with a copy button. Every row's copy text is fixed vocabulary.
2. **Support bundle** (`diagnostics.support_bundle`): created only on
   explicit user action; previewable (the RPC has a `preview` mode returning
   the category list without writing anything); written locally to
   `<profile>/support-bundles/support-bundle-<utc-timestamp>.zip`; never
   uploaded; no network. Contents exactly: `manifest.json` (lists every
   file and field category included, with the policy version) and
   `diagnostics.json` (the same §E-allowlisted payload). **No log files**:
   `backend.log` provably contains import paths today, so logs stay out
   until a fixed-label-only log stream exists.
3. Deterministic: sorted keys, fixed entry order, zeroed zip timestamps —
   two bundles from the same state are byte-identical except the measured
   values themselves.
4. Size cap: bundle must stay under 1 MiB (structural); exceeding it is a
   bug and the RPC fails closed with a fixed message rather than truncating
   silently.
5. Sentinel tests (§G) prove hostile strings planted in every reachable
   input never appear in the summary, bundle, archive names, or manifest.
6. No auto-upload, no telemetry, no crash reporting — unchanged and
   re-asserted by the no-egress test suite.

## F. Issue #14 boundary (what this work may and may not do)

May be built now (architecture only):

- The settings layer keeps its inspectable KV snapshot (`settings.get`).
- The job/queue/guardrail machinery, with structural ceilings from §C.
- Nothing in this slice blocks a future atomic preset apply/revert.

Remains forbidden until Issue #20 P1/P2 evidence exists and the maintainer
approves values:

- Locking final context length, retrieval limit, embedding batch size, or
  OCR caps as *Potato Mode defaults* (the §C values are safety ceilings,
  explicitly not tuned defaults);
- automatic hardware-tier detection;
- calling anything "Potato Proof".

Issue #14 remains open.

## G. Proof obligations

| Rule | Proof |
|---|---|
| State machine transitions, idempotent cancel, cancel-after-terminal | Python unit tests on the job service (fake work units) |
| Cancel at each safe point leaves no visible source, no orphan chunks/pages/file copy | Python integration tests: submit real import of synthetic fixtures, cancel at injected checkpoints, assert DB + filesystem clean |
| Cancelled ≠ failed distinction | Unit test on job snapshots + frontend contract test on state labels |
| No RPC replay | Existing Rust tests (`forced_sidecar_death_never_replays_non_idempotent_request`) + assertion that `jobs.submit` is not in `can_restart_and_retry` |
| Staging rows invisible | Python tests: documents.list / sources.list / rag.search exclude staging |
| Startup repair | Python test: fabricate staging row + file, new SidecarApp/Database boot repairs it |
| Pixel preflight, adaptive DPI, absurd dimensions, overflow | Python unit tests on the preflight function (pure math, synthetic boxes) + integration with a synthetic huge-page PDF (never rendered — asserted by absence of render temp files) |
| Page-limit guardrail | Synthetic many-page PDF fixture (tiny pages), assert fixed message |
| Fixed progress vocabulary | Extend `test_progress.py` + `scripts/test-chat-progress.mjs` contract |
| Privacy of job/status/progress payloads | Sentinel assertions in job tests (hostile filenames/titles in fixtures) |
| Deletion reclaims bytes; idempotent; original file untouched; traversal/symlink refused | Python tests on scratch profiles with measured directory sizes; symlink/junction fixtures where creatable on Windows without elevation (junctions are), else the check is unit-tested directly |
| Storage accounting matches disk | Test compares RPC totals to `os.walk` byte sums on a scratch profile |
| Redaction | Adversarial fixtures with sentinels in every §E-forbidden category; assert absence in summary, bundle bytes, manifest, zip names; determinism test (two bundles byte-compare); size-cap test |
| No egress | Existing autouse egress guard covers all new tests; no `allow_egress` markers added |
| Frontend behavior | `npm run test:progress` (extended), new job-panel contract test, `test:readiness`, `test:backend-status`, `tsc` build |
| Rust | `cargo check`, `cargo test` (allowlist unchanged except documented additions) |
| Failure injection | Python tests monkeypatch embedding/OCR/file ops to raise at each boundary; assert `failed` state + rollback |

Scratch-profile smoke scenarios are specified in
`POTATO_PROOF_SMOKE_v0.4.md` (Issue #21) and executed on the P3 dev
environment as development evidence only.
