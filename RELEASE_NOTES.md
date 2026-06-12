# Release Notes

## v0.1.7 - Current-Suite Benchmark Comparison

v0.1.7 fixes a follow-up Benchmark Comparison issue found in installed builds:
old 5-case `v0.1.3` benchmark runs with missing embedding metadata could still
appear above current 7-case `v0.1.5` runs and win the recommendation.

Highlights:

- Benchmark Comparison now compares and recommends only current eval suite
  runs.
- Older/incompatible benchmark suites remain in Benchmark History, but are
  excluded from comparison ranking.
- The comparison header now shows how many current-suite runs are included and
  which older suite versions were excluded.
- Eval suite remains `v0.1.5`; this patch changes comparison filtering only.

## v0.1.6 - Benchmark Comparison Fix

v0.1.6 fixes the Benchmark Comparison section so repeated benchmark runs do not
inflate a configuration's apparent quality. It does not change the eval case
definitions or scoring rules, so the eval suite remains `v0.1.5`.

Highlights:

- Comparison rows now show latest run score, best run score, average pass/run,
  run count, median average latency, verifier state, and deterministic guidance
  labels.
- Cumulative totals are no longer used as the primary score or recommendation
  driver.
- Recommendation now prefers latest/mean pass rate, then lower latency, with
  verifier-off winning ties.
- Verifier-on configurations are recommended only when they materially improve
  pass score enough to justify latency.
- Added a deterministic Case Difficulty summary for usually passing cases,
  usually failing cases, frequent source failures, and frequent forbidden-claim
  failures.
- Guidance labels now use latest/average behavior instead of cumulative raw
  totals.

Validation commands for v0.1.6:

```powershell
python -m pytest python\tests
npm run build
git diff --check
```

## v0.1.5 - Benchmark Hardening and Weak-Model Guidance

v0.1.5 hardens Diagnostics and Model Benchmark so Odysseus Desktop can evaluate
weak local models for RAG use more safely. It does not add agents, MCP, shell
tools, browser tools, cloud sync, email/calendar, Chroma, Docker, hidden HTTP,
or new architecture.

Highlights:

- Split Diagnostics retrieval status into App Document Retrieval and Benchmark
  Retrieval so the current user document library is not confused with the
  latest temporary benchmark fixture run.
- App Document Retrieval now shows backend, model, semantic active yes/no,
  documents needing reindex, and how many indexed user documents match the
  active backend.
- Benchmark Retrieval now shows the backend/model used by the latest benchmark
  run, whether semantic retrieval was used, and the eval suite version.
- Added an explicit warning when a benchmark used semantic retrieval while the
  user's document library is lexical or not reindexed.
- Added benchmark comparison summaries grouped by chat model, embedding
  backend/model, verifier on/off, and temperature.
- Comparison rows show passed/total, expected failures, forbidden failures,
  source failures, average latency, total runtime, and deterministic guidance
  labels.
- Added deterministic recommendation rules: highest pass count wins, lower
  latency breaks ties, and verifier is recommended only when it improves pass
  count enough to justify latency.
- Added deterministic model guidance labels such as `Good for direct
  extraction`, `Weak at chronology`, `Source contamination risk`, `Verifier not
  useful here`, `Recommended for Potato Mode`, and `Not recommended for
  evidence-sensitive answers`.
- Added Potato Mode as an explicit RAG preset for weak models: quote-first,
  short, fewer chunks, strict no-answer behavior, verifier off, temperature
  `0.0`, Evidence Only formatting, and no speculative synthesis.
- Added Evidence Only answer style with `Answer:`, `Evidence:`, and
  `Not found / cannot confirm:` sections.
- Bumped the eval suite to `v0.1.5` because benchmark comparison behavior and
  result interpretation changed.

Validation commands for v0.1.5:

```powershell
python -m pytest python\tests
npm run build
```

## v0.1.4 - Real Retrieval and Honest Evaluation

v0.1.4 makes RAG retrieval and local benchmarks more honest for weak local
models on limited hardware. It keeps the privacy-first desktop architecture:
React/TypeScript UI, Tauri/Rust shell, Python JSON-RPC sidecar over stdio,
profile-local SQLite, and Ollama at `127.0.0.1:11434`. It does not add agents,
tools, shell access, cloud sync, Chroma, Docker, hidden HTTP, or model
auto-downloads.

Highlights:

- Added an Ollama semantic embedding provider using the local `/api/embed`
  endpoint, with `nomic-embed-text` as the default configured candidate.
- Kept `local-hash-v1` as an automatic deterministic lexical fallback when
  Ollama is unavailable, the embedding model is missing, or embedding calls
  fail.
- Made diagnostics report the active embedding backend/model honestly:
  semantic Ollama embeddings versus lexical fallback.
- Preserved content-hash embedding caching while separating cache keys by
  embedding backend/model.
- Added document indexed-embedding metadata and diagnostics for documents that
  need reindex after an embedding model change.
- Skipped old vector rows with incompatible dimensions instead of mixing them
  into current retrieval.
- Reduced lexical fallback noise with normalized tokenization and stopword
  stripping.
- Weighted semantic vector matches more strongly during reranking so surface
  word decoys do not drown out the semantic hit.
- Bumped the eval suite to `v0.1.4` because old benchmark results are not
  directly comparable.
- Replaced exact-substring-only expected fact checks with paraphrase-tolerant
  fact coverage while keeping phrase-aware forbidden-claim checks.
- Added unscoped retrieval and semantic-vs-lexical decoy eval fixtures.
- Stored retrieved document IDs, retrieved chunk IDs, embedding backend/model,
  answer style, verifier state, and temperature for benchmark cases.
- Passed explicit generation temperature through production chat and eval
  paths, defaulting to `0.0` for parity.
- Updated the Diagnostics benchmark UI and copyable benchmark summary to show
  embedding backend/model and temperature.

Scope intentionally unchanged:

- No agents.
- No tools.
- No shell execution module.
- No email or calendar modules.
- No MCP.
- No Cookbook.
- No gallery/editor.
- No Chroma.
- No Docker.
- No hidden HTTP service.
- No model auto-download.
- No cloud-model dependency.

Validation commands for v0.1.4:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py --models llama3.2:latest
python scripts\run_rag_evals.py --models llama3.2:latest --verify
```

## v0.1.3 - Diagnostics and Model Benchmark

v0.1.3 turns the local RAG eval harness into an in-app Diagnostics / Model
Benchmark area. This supports the broader Odysseus Desktop thesis: helping
small local models become more useful on limited hardware with retrieval,
source scoping, OCR, answer styles, verification, search, and benchmarks while
remaining private and local-first. It does not add agents, autonomous tools,
shell access, cloud sync, Chroma, Docker, or hidden HTTP.

Highlights:

- Added a Diagnostics tab with app version, profile path, backend/database/log
  paths, Ollama status, installed Ollama models, OCR dependency status, and RAG
  health.
- Added an in-app Model Benchmark runner for the existing local RAG eval
  fixtures.
- Added verifier on/off benchmark runs so local models can be compared by
  pass/fail and latency.
- Persisted compact benchmark history in profile-local SQLite.
- Added a copyable Markdown benchmark summary table.
- Reused the shared eval service from both the app and
  `scripts\run_rag_evals.py`.
- Bundled the `evals\` fixtures into the Windows installer resources.
- Clarified that benchmarks use bundled temporary eval fixtures, not the user's
  imported Documents library.
- Clarified benchmark model guidance: 1B-class models are a survival baseline,
  `llama3.2:3b` / `llama3.2:latest` is the intended everyman candidate to
  benchmark first, and verifier mode is slower and not a magic fix for very
  small models.
- Showed basic Ollama model stats where Ollama reports them.
- Kept evals as regression examples only; runtime behavior is not
  special-cased for any named fixture, document, or query.

Scope intentionally unchanged:

- No agents.
- No tools.
- No shell execution module.
- No email or calendar modules.
- No MCP.
- No Cookbook.
- No gallery/editor.
- No Chroma.
- No Docker.
- No hidden HTTP service.
- No model auto-download.
- No cloud-model dependency.

Validation commands for v0.1.3:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py --models llama3.2:latest
python scripts\run_rag_evals.py --models llama3.2:latest --verify
npm run tauri:build
```

## v0.1.2 - RAG Answer Quality

v0.1.2 improves general RAG answer quality for small local models without
claiming frontier-model correctness and without adding agents, tools, shell
access, cloud sync, Chroma, Docker, or hidden HTTP.

Highlights:

- Added RAG answer styles: Precise, Layman, Detailed, and Extract only.
- Added a lightweight intent/style instruction layer for RAG prompts.
- Kept Precise as the default style to preserve current behavior.
- Strengthened general evidence discipline without hard-coding specific
  documents, fixture names, or example queries.
- Preserved source-scoped retrieval for selected documents.
- Tuned verifier wording so it reads as a warning system: grounding looks okay,
  grounding needs review, unsupported claim detected, possible contradiction
  detected.
- Added a compact answer-style selector near the existing RAG and Verify
  controls.
- Added general eval cases for chronology preservation, cross-document
  contamination, procedural-document interpretation, layman explanation, and
  extract-only behavior.
- Extended tests for JSON-RPC style pass-through, style-specific prompt shaping,
  scoped retrieval, styled verifier correction, and non-RAG chat behavior.

The eval fixtures remain regression examples only. Runtime behavior is not
special-cased for any named fixture, document, or query.

Validation commands for v0.1.2:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py
python scripts\run_rag_evals.py --verify
npm run tauri:build
```

## v0.1.1 - RAG Reliability

v0.1.1 improves RAG grounding for local models while keeping the app
privacy-first and Windows-first.

Highlights:

- Added quote-first RAG answers using short evidence snippets instead of full
  noisy chunk text.
- Preserved source/page/chunk/snippet metadata for retrieved evidence.
- Added source-scoped retrieval so chat can prefer a selected indexed document.
- Added optional verifier mode to classify claims as supported, unsupported, or
  contradicted against retrieved snippets.
- Added one regeneration attempt when verifier mode detects contradicted claims.
- Added retrieved snippet and grounding status display in the chat UI.
- Added a local RAG eval harness under `evals\` and `scripts\run_rag_evals.py`.
- Added eval cases for grandfather chronology and cross-document contamination.
- Added model benchmark support for installed Ollama models with local-only
  pass/fail and latency reporting.

Scope intentionally unchanged:

- No agents.
- No tools.
- No shell execution module.
- No email or calendar modules.
- No MCP.
- No Cookbook.
- No gallery/editor.
- No Chroma.
- No Docker.
- No hidden HTTP service.
- No cloud-model dependency.

Validation commands used for v0.1.1:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py
python scripts\run_rag_evals.py --verify
npm run tauri:build
```
