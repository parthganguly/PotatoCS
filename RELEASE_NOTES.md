# Release Notes

## v0.1.12 - Benchmark Trustworthiness and Report Finalization

v0.1.12 fixes remaining benchmark-grading and campaign-report finalization
defects found in a real installed v0.1.11 Quick campaign. It does not add
agents, MCP, cloud services, telemetry, model downloads, new model providers,
or new benchmark models.

Important versioning note:

- App version is `0.1.12`.
- Eval suite version is `v0.1.12`.
- Prompt version is `rag-benchmark-v0.1.12`.
- The fixture corpus still lives under `evals\rag_cases_v018`, but grader and
  scoring semantics changed comparability, so current recommendations exclude
  v0.1.11 and older runs.

Highlights:

- Replaced broad phrase-overlap grading with typed deterministic assertions for
  positive facts, negative facts, absence/abstention, exact identifiers,
  quantities, date/time values, codes, and relation bindings.
- Added a general absence evaluator that recognizes target-scoped forms such
  as "does not identify an approver", "does not list who approved", "cannot
  determine", and "the context is silent on" without requiring a stock refusal
  phrase.
- Added clause-local relation binding so Arun/Kolkata/six-month facts are not
  contaminated by Leela's unrelated two-hour train wait.
- Kept exact identifiers, codes, quantities, and times exact across confusing
  pairs such as slot A/B, MAPLE-4/7, HB-204/240, 9:30/10:00, and six
  months/two hours.
- Distinguished answer conclusions from quoted evidence and recorded which
  segment caused each match.
- Changed scorer semantics so `grader_review` is neither a pass nor a
  confirmed model failure. Reports now include attempted, passed, failed,
  grader-review, timeout, runtime-error, adjudicated pass rate, and coverage.
- Recommendation eligibility now requires sufficient grading coverage; a
  relative winner is not described as deployment-ready merely because it beat a
  weak comparison set.
- Added one canonical report view model consumed by JSON, HTML, PDF, fallback
  screenshots, and the DOM screenshot view.
- Replaced ambiguous report `running` state with explicit report-generation
  states including `awaiting_capture`, `capturing`, `generating`,
  `completed`, `completed_with_warnings`, and `error`.
- Fixed automatic campaign report handoff so the backend waits for frontend DOM
  screenshots instead of racing directly to backend fallback snapshots.
- Made backend fallback screenshots explicit, content-fitted, and warning
  backed.
- Improved PDF summary terminology, table wrapping, and visual appendix density
  so screenshots are supplementary rather than mostly empty pages.
- Improved ETA metadata with compatible-history confidence and fallback-source
  wording.

Validation commands for v0.1.12:

```powershell
python -m pytest python\tests
npm run build
cargo check --manifest-path src-tauri\Cargo.toml
python -m py_compile python\odysseus_desktop_backend\services\eval_service.py python\odysseus_desktop_backend\services\campaign_service.py python\odysseus_desktop_backend\services\report_service.py python\rpc_server.py
git diff --check
npm run tauri:build
```

## v0.1.11 - Benchmark Correctness and Report Integrity

v0.1.11 fixes scorer and report-integrity bugs found in real v0.1.10 campaign
reports. It does not add agents, MCP, cloud services, model downloads, or new
benchmark fixtures.

Important versioning note:

- App version is `0.1.11`.
- Eval suite version is `v0.1.11`.
- The fixture corpus still lives under `evals\rag_cases_v018`, but grader
  semantics changed comparability, so current recommendations exclude older
  `v0.1.8` runs.

Highlights:

- Added typed expected-fact matching for positive, negative, abstention,
  exact identifier, quantity/date, and code-like facts.
- Fixed negative expected facts such as "no emergency" and abstention facts
  such as "no approver" so they are not rejected merely because the expected
  claim itself is negated.
- Restricted negation handling to the matched sentence/clause and predicate,
  so unrelated later negative sentences do not negate quantity facts.
- Required exact normalized matching for short identifiers, alphanumeric
  codes, times, dates, and quantities.
- Added explicit source-policy handling for required-source presence,
  exclusive-source cases, no-conflicting-evidence cases, and abstention source
  cases.
- Normalized pipeline diagnosis to one disjoint taxonomy:
  `retrieval_only`, `generation_only`, `both`, `grader_review`, `timeout`,
  `runtime_error`, and `passed`.
- Fixed report terminology to distinguish job execution errors, benchmark
  assertion failures, grader-review cases, and timeouts.
- Fixed not-run report metrics so quick campaigns display `N/A` / `Not run`
  instead of `0%` / `0/0`.
- Fixed recommendation reporting so equal-quality configurations produce a
  quality tie while speed and balanced recommendations remain separate.
- Fixed case-difficulty labels for small observation counts and source/
  forbidden-failure panels.
- Fixed report finalization so successful reports end as `completed` or
  `completed_with_warnings`, not `running`.
- Improved DOM screenshot handoff timing and kept backend fallback snapshots as
  a warning-path safety net.
- Added hardware context for Python/Ollama/model metadata and observed
  CPU/GPU offload when available.
- Improved PDF layout by reducing forced page breaks, shortening wide-table
  headers, and scaling report snapshots larger.

Validation commands for v0.1.11:

```powershell
python -m pytest python\tests
npm run build
cargo check --manifest-path src-tauri\Cargo.toml
python -m py_compile python\odysseus_desktop_backend\services\campaign_service.py python\odysseus_desktop_backend\services\report_service.py python\odysseus_desktop_backend\services\eval_service.py python\rpc_server.py
git diff --check
npm run tauri:build
```

## v0.1.10 - Automated Benchmark Campaigns and Local Reports

v0.1.10 adds profile-local benchmark campaigns and completely local report
export while keeping the active eval suite at `v0.1.8`. It does not add agents,
cloud services, telemetry, hidden HTTP, model auto-downloads, or new benchmark
fixtures.

Highlights:

- Added persistent benchmark campaign and campaign job tables with additive
  SQLite migrations. Existing benchmark history remains intact.
- Added Quick comparison, Standard diagnostic, and Thorough comparison campaign
  presets with deterministic job planning, ETA estimates, long-run warnings,
  and explicit user Start action.
- Standard campaigns deduplicate retrieval-only work for a shared embedding
  configuration instead of rerunning it for every chat model.
- Installed-model selection now classifies chat, embedding-only, and unknown
  Ollama models where possible, and excludes embedding-only models from
  automatic chat benchmark selection.
- Campaign execution runs one benchmark job at a time, preserves completed
  results when later jobs fail or time out, and supports pause, cancel, retry,
  and interrupted-campaign resume.
- Added campaign setup, active progress, and campaign history UI to Diagnostics
  without starting benchmarks automatically on launch.
- Added local report export through a Python `ReportService`: searchable PDF
  via ReportLab, self-contained offline HTML, raw JSON schema version `1`, and
  Odysseus-generated visual snapshots.
- Added React report snapshot rendering with `html2canvas`, plus backend
  fallback snapshots when DOM capture is unavailable.
- Default reports redact full private filesystem paths and omit raw prompts,
  thinking traces, and large raw answers unless detailed audit export is
  enabled.
- Added ReportLab runtime preparation/verification and campaign/report tests.

Validation commands for v0.1.10:

```powershell
python -m pytest python\tests
npm run build
cargo check --manifest-path src-tauri\Cargo.toml
python -m py_compile python\odysseus_desktop_backend\services\campaign_service.py python\odysseus_desktop_backend\services\report_service.py python\odysseus_desktop_backend\services\eval_service.py python\rpc_server.py
git diff --check
npm run tauri:build
```

Packaging note: this release is ready for manual campaign testing only after
the package build succeeds. Do not claim fully release-ready until a real
installed-app campaign generates and opens a valid report.

## v0.1.8 - RAG Benchmark Validity, Thinking, and Timeout Safety

v0.1.8 redesigns the local benchmark path so weak-model evaluation can separate
retrieval quality, model comprehension with known-good evidence, and full
end-to-end RAG behavior. It keeps the local-first architecture and does not add
agents, shell tools, MCP, cloud services, Chroma, LanceDB, Docker, hidden HTTP,
or model auto-downloads.

Highlights:

- Added structured Ollama chat responses with content, thinking text, done
  reason, token counts, durations, derived token rates, and model name while
  preserving the existing string-returning `chat()` compatibility wrapper.
- Added explicit benchmark thinking modes: `off`, `on`, and `auto`; the Ollama
  `think` field is sent top-level, never inside `options`.
- Bumped the active eval suite to `v0.1.8` and added a diverse synthetic suite
  for clean retrieval, direct extraction, chronology/comprehension,
  cross-document contamination, abstention/no-answer, and negation-adversarial
  cases.
- Added separate benchmark modes for Retrieval only, Oracle generation, and
  End-to-end RAG. Comparison groups never combine different modes.
- Added additive benchmark storage fields for run status, prompt version,
  benchmark mode, thinking mode, answer style, repeat count, timeout policy,
  model/offload diagnostics, raw prompts, raw answers, thinking text, supplied
  evidence, retrieval candidates, grader matches, timings, and per-case
  diagnosis.
- Persisted benchmark runs incrementally: a run row is created at start and
  each case is stored as it finishes. A case timeout or runtime error is stored
  and the run continues.
- Bounded verifier and correction requests with thinking off, temperature `0.0`,
  JSON-format request where supported, and output-token limits. Verifier or
  correction failure preserves the original/last completed answer.
- Added request-specific timeout constants for interactive chat, benchmark
  answer generation, verifier JSON, and correction generation.
- Added local `/api/ps` diagnostics for loaded model size, VRAM size,
  parameter/quantization details, estimated CPU/GPU loaded fraction, and
  partial CPU-offload warning text.
- Added retrieval audit score components: original vector score/rank, lexical
  contribution, metadata contribution, phrase bonus, final score, and final
  rerank.
- Separated retrieved candidate contamination from supplied-evidence
  contamination for source grading.
- Added deterministic negation-aware grading with match spans/windows, token
  overlap, negation detection, final decision, and grader-review status.
- Updated benchmark UI controls for model, mode, thinking, verifier, repeats,
  and richer result/comparison metadata.

Validation commands for v0.1.8:

```powershell
python -m pytest python\tests
npm run build
python -m py_compile python\odysseus_desktop_backend\services\model_service.py python\odysseus_desktop_backend\services\eval_service.py python\odysseus_desktop_backend\services\rag_service.py python\odysseus_desktop_backend\services\chat_service.py python\rpc_server.py
git diff --check
```

Packaging note: do not claim benchmark quality is validated until a package
build is installed and real post-package benchmark runs are performed.

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
