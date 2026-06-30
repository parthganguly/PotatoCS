# Odysseus Desktop

Windows-first local AI workspace for small models on limited hardware.

Odysseus Desktop is a standalone product fork of the upstream Odysseus project.
It keeps the spirit of a local AI workspace, but it deliberately changes both
the product thesis and delivery model: this is a packaged desktop app for
making modest local models more useful, not a Docker/web app or a general
model runner.

Odysseus Desktop is designed to help small Ollama models work better on modest
hardware. Instead of relying only on model size, it adds local support around
the model: semantic retrieval when a local embedding model is installed,
lexical fallback when it is not, document RAG, OCR, source-scoped retrieval,
answer styles, verification, snippets, search, image/screenshot understanding,
and benchmarks.

This app is a Tauri desktop shell with a Rust supervisor, a bundled Python
JSON-RPC sidecar over stdio, profile-local SQLite storage, React/TypeScript UI,
and local model runtime detection for Ollama at `127.0.0.1:11434`.

No Docker is required. No app-internal service binds to `0.0.0.0`. Localhost is
used only for external local runtimes such as Ollama and the Vite dev server.

## Why This Fork Exists

The original Odysseus project is broad: it includes a web UI, Docker-oriented
deployment, server routes, and a much larger assistant surface area. That is a
good shape for the original project, but it is not the right shape for this
fork's goal.

This fork exists because the target product is a Windows-first local desktop
app. A user should be able to install it, launch it from the Start Menu, keep
their data in the normal Windows app-data folder, talk to a local model runtime,
import documents, and quit/relaunch without needing Docker, a web server, or a
manually managed Python environment.

That requirement changes the architecture. The app boundary moves from
browser-to-server into desktop-shell-to-sidecar:

```text
React UI
   |
   v
Tauri / Rust desktop shell
   |
   +-- supervises bundled Python sidecar
   +-- owns profile/app-data paths
   +-- talks JSON-RPC over stdio
   +-- detects local Ollama runtime
   |
   v
Python services
   |
   +-- chat/session/settings
   +-- document import
   +-- RAG/chunking/embeddings
   +-- optional OCR
   +-- legacy import
   |
   v
SQLite profile storage
```

## How It Is Different From Upstream

This is not intended as a drop-in patch to upstream Odysseus. It is a focused
desktop fork with a different product contract.

Key differences:

- Desktop-first instead of web-first.
- Tauri/Rust process supervision instead of a long-running app web server.
- JSON-RPC over stdio instead of app-internal HTTP.
- Bundled embedded Python runtime for packaged Windows builds.
- Profile-local SQLite data under `%APPDATA%`.
- Ollama-first local model runtime detection.
- SQLite + NumPy VectorStore MVP, with the VectorStore boundary preserved for
  future sqlite-vec or LanceDB experiments.
- Ollama semantic embeddings when a local embedding model such as
  `nomic-embed-text` is installed, with an honest lexical fallback when it is
  not.
- Optional OCR by detecting local tools such as Tesseract and MuPDF/Poppler.
- Non-destructive import from compatible legacy Odysseus data folders.
- Grounding, search, verification, and benchmark surfaces that help modest
  local models punch above their weight without claiming frontier-model
  correctness.

Removed from the MVP on purpose:

- Docker deployment
- hidden HTTP services
- tools and agents
- email and calendar
- shell execution surfaces
- Cookbook
- gallery/editor
- full MCP
- Chroma

Those features may still make sense in upstream Odysseus. They are intentionally
out of scope here because the first job of this fork is to prove a reliable
local desktop foundation.

## Philosophy

Odysseus Desktop favors a small durable spine over broad feature parity. It is
not just a local model runner; it is a local AI workspace for limited hardware.
The goal is to make modest private models more useful by surrounding them with
retrieval, document search, OCR, grounding, verification, and benchmarks.

The architecture is intentionally boring in the parts that need to be reliable:

- Rust owns the desktop shell, lifecycle, bundled resources, and process
  supervision.
- Python owns the AI orchestration and document intelligence.
- SQLite owns local memory and restart persistence.
- Ollama owns local model execution.
- Optional native helpers can be added later only when they clearly earn their
  complexity.

The design principle is: prove the desktop foundation first, then add features
only when they fit that foundation.

That is why the MVP is narrow. It is not trying to rebuild every upstream module
inside a desktop wrapper, and it does not claim that small models become as
capable as frontier cloud systems. It is trying to become a dependable local AI
workspace that helps local models work better, improves reliability where it
can, and stays installable, inspectable, repairable, and private on Windows.

## Screenshots

Screenshots are intentionally documented as part of the product surface. The
current MVP should show these screens when captured from a packaged Windows
build:

- `docs/screenshots/chat-rag-sources.png` - chat with RAG enabled and retrieved
  source chips visible.
- `docs/screenshots/sources-library.png` - unified Sources library with
  documents, images, screenshots, indexing state, and attention state.
- `docs/screenshots/legacy-import-report.png` - non-destructive legacy import
  report with imported, skipped, incompatible, and failed entries.
- `docs/screenshots/chat-attachments.png` - universal chat attachments with
  PDFs, text files, images, pasted images, and screenshots.
- `docs/screenshots/image-benchmark.png` - compact image benchmark results in
  Diagnostics.
- `docs/screenshots/installer-start-menu.png` - installed app launched from the
  Windows Start Menu.

These screenshots are not committed yet because they should come from a clean
packaged build capture, not a development or automation artifact.

## MVP Scope

Included:

- Basic non-RAG chat.
- Sessions, settings, default profile, and restart persistence.
- `.txt`, `.md`, and extractable `.pdf` document import.
- SQLite + NumPy VectorStore-backed RAG.
- v0.1.12 RAG diagnostics: semantic Ollama embeddings where available, honest
  lexical fallback, quote-first answers, source-scoped retrieval,
  answer styles, optional verifier pass, retrieved snippets, local benchmark
  runs, structured Ollama metadata, thinking-mode benchmark support, timeout
  safety, separate retrieval/oracle/end-to-end benchmark modes, comparison
  summaries, deterministic model guidance, persistent benchmark campaigns,
  local PDF/HTML/JSON report export, Potato Mode, and Evidence Only style.
- Embedding cache by chunk/content hash.
- Optional OCR for scanned/low-text PDFs when Tesseract plus `pdftoppm` or
  `mutool` are locally installed.
- Sources and universal attachments in v0.2.0: PDFs, TXT/Markdown files,
  PNG/JPEG/WebP images, pasted images, and explicit full-screen/coordinate
  screenshot captures can be saved as reusable Sources or attached directly to
  a chat session. Direct attachments are session-scoped by default, hidden from
  the global Sources library until promoted, and direct PDF/TXT/Markdown
  attachments use scoped retrieval instead of dumping whole files into the
  model context.
- Local image/screenshot understanding in v0.2.0: profile-local originals,
  thumbnails, bounded vision-input derivatives, lossless OCR-input derivatives,
  direct image OCR, confirmed Ollama vision-model analysis, optional Florence 2
  Basic local vision from a prepared local model pack, automatic routing,
  separated OCR/vision/final-answer evidence, optional indexing of derived text
  into existing text-only RAG, and a separate local image benchmark suite.
- Florence 2 Basic uses `microsoft/Florence-2-base-ft` only when an optional
  local pack has been prepared. Normal app startup and normal runtime do not
  download model files or import Torch/Transformers; Diagnostics reports pack
  missing/runtime missing until the pack and optional dependencies are staged.
- Florence and external-eyes image answers use curated visual evidence for weak
  final text models: duplicated entities are normalized, question-relevant
  observations are separated from supported inference, raw perception output
  remains inspectable, unsupported identity/location/event/brand claims are
  guarded, and OCR no-text is informational for normal photo questions.
- Conversation model selection is filtered to installed chat-capable Ollama
  models. Alias tags are deduplicated, sized variants stay distinct,
  embedding-only models remain available for retrieval but are excluded from
  chat selectors, and historical sessions that reference deleted models remain
  readable with a not-installed label.
- v0.2.0 chat shell clarification: the left sidebar contains New chat, primary
  navigation, recent chats, compact Ollama/profile status, and Settings. The
  default model in Settings applies to future chats only; the active
  conversation model lives in the chat header and is persisted per session.
- Non-destructive import from compatible legacy Odysseus data folders.
- Profile-local logs and app data.

Excluded from the MVP:

- Tools, agents, email, calendar, shell, Cookbook, gallery/editor, full MCP,
  Chroma, Docker, and hidden HTTP.

## RAG Reliability

v0.1.12 focuses on making retrieval, diagnostics, and evaluation more trustworthy for small local
models such as `llama3.2` on limited hardware. It does not require cloud
models, does not auto-download models, and does not require a larger default
chat model.

The RAG path now uses quote-first grounding:

- Retrieval still uses the existing VectorStore-backed search path.
- If a local Ollama embedding model such as `nomic-embed-text` is installed,
  retrieval can use semantic embeddings through Ollama's local `/api/embed`
  endpoint.
- If Ollama is unavailable, the embedding model is missing, or embedding calls
  fail, retrieval falls back to `local-hash-v1`, a deterministic lexical
  fallback. The app reports this as lexical fallback, not semantic retrieval.
- Embeddings are cached by content hash and embedding model/cache key. If the
  active embedding model changes, diagnostics report documents needing reindex,
  and old vector dimensions are skipped safely instead of being mixed.
- The answer prompt receives short evidence snippets extracted from retrieved
  chunks, not the full noisy chunk text.
- Each snippet keeps source document, page, chunk, and snippet metadata.
- The chat UI shows retrieved snippets under the answer so the user can inspect
  exactly what the model saw.

RAG chat also supports source-scoped retrieval. Users can attach files directly
to a message or choose saved Sources near the composer, and retrieval can stay
within those selected or attached Sources. This reduces cross-source
contamination while keeping page/chunk citations inspectable.

RAG answers support five general styles:

- `Precise` - default. Best for factual questions where the answer should stay
  close to the retrieved evidence and preserve chronology.
- `Layman` - best when a document is procedural, bureaucratic, legal,
  technical, medical, or institutional and the user wants practical meaning in
  plain English. It still states what the context does not prove.
- `Detailed` - best when the user wants a fuller answer with organization,
  caveats, and clear source separation.
- `Extract only` - best when the user wants only directly stated facts and no
  interpretation or speculation.
- `Evidence only` - best for weak local models and evidence-sensitive answers.
  It asks the model to use this shape: `Answer:`, `Evidence:`, and
  `Not found / cannot confirm:`.

Potato Mode is an optional RAG preset for weak models and limited hardware. It
is not the global default. It uses quote-first evidence, fewer retrieved chunks,
temperature `0.0`, verifier off, Evidence Only formatting, strict no-answer
behavior, and no speculative synthesis.

## Sources And Image Understanding

v0.2.0 uses one `Sources` library for reusable PDFs, text files, images, and
screenshots, plus one universal chat attachment system for session-scoped
files. Image files are stored as profile-local artifacts with an untouched
original copy, a thumbnail, a bounded JPEG vision derivative, a lossless PNG OCR
derivative, derived OCR text, model visual observations, and combined evidence.
Existing document tables and APIs remain backward compatible; internal
generated documents are hidden from the normal Sources list.

Supported direct attachments are PDF, TXT, Markdown, PNG, JPEG/JPG, and WebP.
Supported image imports are PNG, JPEG/JPG, and WebP. The backend validates
decoded image bytes with Pillow, applies EXIF orientation, bounds original file
size and decoded pixels, flattens transparency deterministically for model
inputs, and never stores base64. Clipboard and screenshot capture write a local
file first through dedicated Rust commands, then Python imports that file as a
session-scoped artifact. Native vision requests and historical image follow-ups
use the persisted vision derivative; OCR uses the OCR derivative so text edges
are not degraded by the compressed vision input.

Image analysis defaults to Automatic:

- `Automatic` chooses OCR, native vision, or external vision evidence based on
  confirmed local capabilities.
- `OCR only` runs local OCR and sends exact extracted text to the selected chat
  model for final synthesis.
- `Vision only` requires a confirmed Ollama model whose `/api/show`
  capabilities include `vision`.
- `Combined` keeps OCR text and model observations separate before producing a
  final answer.

OCR, vision output, and combined evidence can be indexed into the existing
text-only RAG path only when the user chooses an indexing action. Chunks keep
artifact provenance so sources can be labeled as image-derived evidence.

Screenshot support is Windows-first and user-initiated. Full-screen and
coordinate-region capture are wired through Rust. Window capture is deliberately
reported as unsupported in v0.2.0 until a reliable window picker can be added.
There is no continuous capture, background screen monitoring, model download,
cloud fallback, image generation, or hidden HTTP service.

The chat shell separates global defaults from active chat state. The sidebar's
Settings panel labels the future-chat default as `Default model for new chats`.
The chat header shows the current session model, and changing it affects future
messages in that conversation without changing the global default. Vision
specialist routing is shown separately when image evidence is active.

The original RAG benchmark suite remains `v0.1.12`. Image understanding has a
separate `local-image-understanding` suite at `v0.2.0` under
`evals\image_cases_v020`, with deterministic packaged fixtures and typed
assertions for OCR text, objects, spatial relationships, counts, forbidden
objects, and not-visible abstention.

The optional verifier pass can be enabled from the chat UI. It asks the selected
local model to classify factual claims as supported, unsupported, or
contradicted against the retrieved snippets. If a contradicted claim is found,
the app regenerates once with the correction. The verifier is local-only,
best-effort, and optional because it costs extra tokens. It is not a rescue
mechanism for 1B/1.5B-class models.

The local eval harness lives under `evals\` and `scripts\run_rag_evals.py`.
The active suite is `v0.1.12` under `evals\rag_cases_v018`; older suites remain
on disk for legacy history and regression context. v0.1.12 uses the same
fixture corpus but changes grader semantics for absence/abstention, relation
binding, exact values, grader-review scoring, source policies, and report
finalization, so old v0.1.11 and earlier benchmark runs are not compared as
current-suite results. The fixtures cover
clean retrieval, direct extraction, chronology/comprehension, cross-document
contamination, abstention/no-answer, and negation-adversarial cases across
technical instructions, event notices, logistics memos, family narrative,
policy/procedure, and product/spec domains. The app does not hard-code behavior
for any fixture, document name, question, or model.

Benchmarks now run in three separate modes: Retrieval only, Oracle generation,
and End-to-end RAG. Comparison never combines different modes. End-to-end runs
record retrieved candidate document IDs separately from evidence snippets
actually supplied to the model, so a wrong rank-4 candidate is reported as
candidate contamination without automatically failing source grounding if it
was not supplied. The deterministic grader records typed expected/forbidden
assertion details, matched clauses, answer/evidence segment source, absence
construction, relation components, exact value decisions, and review-required
decisions; negated forbidden claims such as "there is no emergency" are not
treated as affirmative forbidden claims.

Benchmark runs store structured Ollama response metadata, requested thinking
mode (`off`, `on`, or `auto`), whether thinking text was returned, token counts,
durations, prompts, raw answers, supplied evidence, timings, timeout/error
status, and model/offload diagnostics from local `/api/ps` when available.
Older benchmark suites remain visible in history, but only completed current
suite runs are eligible for current recommendations.

The Diagnostics tab exposes the same local eval suite inside the app. It shows
the app version, profile path, backend/database/log paths, Ollama reachability,
installed Ollama models with basic size/parameter/quantization stats, OCR
dependency status, VectorStore health, active embedding backend/model, whether
semantic retrieval is active, and documents needing reindex. It separates App
Document Retrieval from Benchmark Retrieval so a semantic benchmark run is not
confused with a lexical or not-reindexed document library. The Model Benchmark
panel can run Retrieval only, Oracle generation, or End-to-end RAG with
thinking off/on/auto, verifier on/off where valid, and one or three repeats.
It saves progress incrementally so completed cases remain auditable if a later
case times out, marks interrupted sidecar runs in history, and retries lost
benchmark RPC responses after restarting the sidecar. It also shows a compact
comparison table grouped by suite,
mode, prompt version, chat model, thinking mode, embedding backend/model,
verifier state, temperature, and generation limits. It does not auto-download
models and does not use cloud services.

v0.1.10 adds persistent benchmark campaigns in Diagnostics. A campaign is a
profile-local SQLite queue of planned benchmark jobs with Quick comparison,
Standard diagnostic, and Thorough comparison presets. Campaigns run only after
an explicit Start action, execute one Ollama benchmark job at a time, preserve
partial results after failed or timed-out jobs, and support pause, cancel,
retry, and interrupted-campaign resume without rewriting existing benchmark
history. The planner lists installed Ollama models, filters embedding-only
models out of automatic chat-model selection, deduplicates Standard
retrieval-only jobs for a shared embedding configuration, and shows local ETA
warnings before heavy runs.

Completed campaigns can generate fully local reports without cloud services or
external assets. Reports include a searchable PDF generated with ReportLab, a
self-contained offline HTML backup, raw JSON with schema version `1`, and
Odysseus-generated visual snapshots. Default report data omits full private
profile paths, raw prompts, thinking traces, and large raw answers unless the
user enables detailed audit export.

## User Setup

Install the Windows build from:

```powershell
src-tauri\target\release\bundle\nsis\Odysseus Desktop_0.2.0_x64-setup.exe
```

The installer includes the app, Python sidecar code, and a bundled embedded
Python runtime with required Python dependencies. A system Python install is not
required to run the installed app.

For local chat, install and run Ollama separately:

```powershell
ollama serve
ollama pull llama3.2
```

Semantic retrieval is optional and also local. To enable it, install an Ollama
embedding model yourself:

```powershell
ollama pull nomic-embed-text
```

If no embedding model is installed, Odysseus Desktop still works with the
`local-hash-v1` lexical fallback and clearly reports that semantic retrieval is
not active.

OCR is optional. To OCR scanned PDFs, install Tesseract and at least one PDF
renderer:

- Tesseract OCR
- MuPDF `mutool`, or Poppler `pdftoppm`

If OCR tools are missing, scanned PDFs remain listed with a clear OCR-unavailable
state. Normal text, Markdown, extractable PDF import, and non-RAG chat still work.

## Data and Logs

User data is stored in the app-data profile folder:

```powershell
%APPDATA%\dev.odysseus.desktop\profiles\default
```

Important files:

- `app.db` - profile-local SQLite database, including benchmark history.
- `files\documents` - profile-local copies of imported documents.
- `logs\backend.log` - Python sidecar startup, JSON-RPC errors, Ollama detection,
  document import, OCR, and legacy import logs.

Quit and relaunch should preserve sessions, settings, documents, chunks,
embeddings, OCR pages, and RAG behavior.

## Developer Setup

Install prerequisites:

- Node.js
- Rust and Cargo
- Windows WebView2 runtime
- Optional: Ollama for real chat/RAG smoke testing
- Optional: Tesseract plus `mutool` or `pdftoppm` for real OCR smoke testing

Install Node dependencies:

```powershell
npm install
```

Run backend tests:

```powershell
python -m pip install -r python\requirements.txt
python -m pytest python/tests
```

Run local RAG reliability evals against installed Ollama models:

```powershell
python scripts\run_rag_evals.py
python scripts\run_rag_evals.py --models llama3.2 --verify
python scripts\run_rag_evals.py --models llama3.2 --style layman
```

The eval fixtures live under `evals\`. They check required facts, forbidden
claims, required source scoping, and latency without using cloud models.

## Benchmark Guide

In the app:

- Open `Diagnostics`.
- Select an installed Ollama model.
- Pick a mode: `Retrieval only`, `Oracle generation`, or `End-to-end`.
- Pick thinking mode: `Off`, `On`, or `Auto`. The benchmark default is `Off`
  for bounded weak-hardware comparison.
- Toggle `Verify` only for End-to-end runs when you want the slower, more
  careful verifier pass.
- Pick one or three repeats.
- Click `Run`.
- Use `Copy` to copy a Markdown benchmark summary table.

Chat and embedding models must already be installed in Ollama. Odysseus Desktop
will list local models but will not pull or download them.

Model guidance:

- Survival baseline: `llama3.2:1b`. It runs on weak hardware and can be useful
  for `Extract only`, simple Q&A, and retrieval inspection. It is not
  recommended for reliable RAG reasoning.
- Practical everyman candidate: `llama3.2:3b` / `llama3.2:latest`. It is the
  intended weak-model baseline to benchmark first, but recommendations should
  come from your local eval results, not from the model name alone.
- Comfortable local models: larger 7B-ish models. They can improve reliability
  on stronger machines, but benchmark them before relying on them.

Verifier guidance:

- `Verify` can improve grounding for some models.
- `Verify` increases latency because the selected local model performs an
  extra checking pass.
- `Verify` may not help 1B-class models much; very small models may not verify
  their own answers reliably.
- Let benchmark results decide the recommendation, not model size or vibes.

Recommendation guidance:

- The comparison table groups saved runs by suite, benchmark mode, prompt
  version, chat model, thinking mode, embedding backend/model, verifier on/off,
  temperature, and generation limits.
- It compares only completed current-suite runs. Older, incompatible, partial,
  or still-running benchmark suites stay in history, but they are excluded from
  the recommendation and listed in the comparison header.
- It shows latest run score, best run score, average pass/run, practical score,
  adversarial score, run count, timeout rate, median average latency, verifier
  state, thinking state, and deterministic guidance labels.
- Cumulative pass totals are kept only as informational data and are not the
  main score, because repeated runs of the same config should not look better
  merely because they were run more often.
- The recommended configuration is selected from completed current-suite
  end-to-end core cases using mean practical pass rate, worst-run practical
  pass rate, retrieval quality, and lower latency. Verifier-off and thinking-off
  win ties, and verifier/thinking-on runs are recommended only when they
  materially improve quality enough to justify latency.
- The Case Difficulty summary identifies cases that usually pass, usually
  fail, often fail source scoping, or often produce forbidden-claim failures.
- Guidance labels are deterministic examples such as `Fastest usable config`,
  `Good extraction baseline`, `Weak at chronology`, `Source contamination
  risk`, `Verifier not worth latency`, `Verifier improved score but high
  latency`, `Recommended for Potato Mode`, and `Not evidence-safe`.

The `Documents` tab is your user-imported library for normal RAG chat. The
benchmark eval fixtures are separate bundled documents used for repeatable
model testing. Benchmark runs create temporary/internal eval documents while
they run, so benchmark results can exist even when the user document count is
0, and those eval documents will not appear in `Documents`.

Benchmark results also report the embedding backend/model. `semantic/<model>`
means the eval used a local Ollama embedding model. `lexical/local-hash-v1`
means it used the deterministic lexical fallback. Lexical fallback is useful
for inspection and basic matching, but semantic retrieval is expected to do
better on synonym/paraphrase cases such as `ancestor` versus `grandfather`.

Run all RAG eval cases against every installed Ollama model:

```powershell
python scripts\run_rag_evals.py
```

Run the same evals with the optional verifier pass enabled:

```powershell
python scripts\run_rag_evals.py --verify
```

Run selected models only:

```powershell
python scripts\run_rag_evals.py --models llama3.2 qwen2.5:3b qwen2.5:7b mistral:7b
```

Override the answer style for all cases:

```powershell
python scripts\run_rag_evals.py --models llama3.2 --style extract_only
```

The runner defaults eval generations to `--temperature 0.0` to reduce local
sampling noise. Use `--temperature` to compare a different setting.

Interpretation:

- `PASS` means the answer included all expected facts, avoided forbidden claims,
  and satisfied the source policy for the case.
- `FAIL` lists the missing expected facts, forbidden claims, or source-scope
  issue that caused the failure.
- `latency_ms` is wall-clock time for that case and model on the current
  machine, including retrieval and generation.
- `expected`, `forbidden`, and `source` show which part of the case passed.

Sample benchmark table placeholder:

| Model | Embeddings | Temp | Verify | Passed | Failed | Avg latency | Notes |
| --- | --- | ---: | --- | ---: | ---: | ---: | --- |
| llama3.2:1b | lexical/local-hash-v1 or semantic/nomic-embed-text | 0.00 | off | TBD | TBD | TBD | Survival baseline; inspect retrieval, avoid relying on RAG reasoning |
| llama3.2:3b / llama3.2:latest | semantic/nomic-embed-text | 0.00 | off | TBD | TBD | TBD | Practical everyman candidate; benchmark locally |
| qwen2.5:7b | semantic/nomic-embed-text | 0.00 | off | TBD | TBD | TBD | Comfortable local candidate |
| mistral:7b | semantic/nomic-embed-text | 0.00 | off | TBD | TBD | TBD | Comfortable local candidate |

Run the frontend build:

```powershell
npm run build
```

Run the desktop app in development:

```powershell
npm run tauri:dev
```

`tauri:dev` stages the embedded Python runtime first, then starts Tauri. The
development UI uses Vite on `127.0.0.1:1420`.

## Packaging

Stage and verify the embedded Python runtime:

```powershell
npm run prepare:python
npm run verify:python
```

The staged runtime lives in `python-runtime` and is bundled as a Tauri resource.
It must import:

- `json`
- `sqlite3`
- `numpy`
- `pypdf`
- `rpc_server`
- `odysseus_desktop_backend`

Build the Windows installer:

```powershell
npm run tauri:build
```

The NSIS installer is emitted under:

```powershell
src-tauri\target\release\bundle\nsis
```

Run the smoke gate:

```powershell
npm run smoke
```

To skip re-downloading/revalidating the embedded runtime during repeated local
checks:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\smoke-mvp.ps1 -SkipRuntime
```

The smoke gate covers fresh profile startup, Ollama missing/reachable detection,
basic chat, document import, RAG chat/retrieval, OCR unavailable/available mocked
paths, legacy import reporting, restart persistence, frontend build, and Rust
`cargo check`.

## License and Notices

Odysseus Desktop preserves the upstream Odysseus MIT license in `LICENSE`.
Third-party and optional OCR notices are tracked in `THIRD_PARTY_NOTICES.md`.
