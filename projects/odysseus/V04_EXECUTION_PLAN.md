# v0.4 Execution Plan — Potato Mode + First-Run Runtime Simplification

Status: planning only. No implementation started. Scope authority:
`V04_POTATO_MODE_SCOPE.md` (accepted for issue #3, closed). Issue drafts:
`V04_ISSUE_BREAKDOWN.md`. Rules for lower-context models:
`V04_BATON_FOR_SMALLER_MODELS.md`.

## Potato Proof addendum

Hardware-tier proof requirements live in `POTATO_PROOF_MATRIX.md`
(tiers P0–P4, proposed resource budgets, failure modes, proof scenarios)
and `POTATO_NICHE_ESSENTIALS.md` (the niche low-end behavior checklist).
The v0.4 release gate must include a first-run/potato proof smoke run
against that matrix. Proof from an old gaming laptop (tier P3) alone is
insufficient: v0.4 is not done until the proof scenarios pass on
P1/P2-class hardware or a clearly documented simulated equivalent.

## A. Mission

Make Odysseus Desktop excellent for a noob on a low-end Windows machine:
first launch shows readiness in plain language; local model setup is guided
instead of assumed; the user imports one document and gets one
evidence-grounded, sourced answer in tolerable time; the app recovers
honestly from a missing runtime, a lost backend, or missing OCR
dependencies; and performance and storage limits are explained truthfully
instead of hidden. Every v0.4 change serves that first experience or waits.

## B. Release shape

Ship v0.4 as a sequence of small PRs, each gated on the previous:

- **v0.4.0** — planning/docs/readiness audit (this baton + audit issue).
- **v0.4.1** — first-run readiness screen.
- **v0.4.2** — Potato Mode settings preset.
- **v0.4.3** — Ollama/model setup helper.
- **v0.4.4** — indexing throttle/pause/cancel + giant-doc guardrails.
- **v0.4.5** — profile storage visibility and cleanup.
- **v0.4.6** — noob diagnostics / redacted support bundle.
- **v0.4 release** — gate document, installer build, proof report, publish.

Each slice is one issue, one branch, one PR, small diff. A slice may ship as
its own tagged patch or accumulate into one v0.4 release at the maintainer's
choice; the gate/proof step is mandatory either way.

## C. Execution order

### Slice 0 — v0.4.0 planning/docs/readiness audit
- Objective: land this baton, fix docs drift, then audit the real first-run
  code paths (boot status fetches, settings store, cancel paths, storage
  layout) and confirm/correct the "Unknown/Partial" rows in
  `V04_POTATO_MODE_SCOPE.md` §3.
- Files: `README.md`, `docs/releases/v0.3.1.md`, `projects/odysseus/*.md`;
  audit reads `src/App.tsx`, `python/**/{settings,chat,rag,embedding,ocr,
  document}_service.py`, `src-tauri/src/lib.rs` (read-only).
- Tests: none required (docs only); audit output is a markdown report.
- Acceptance: drift gone; audit report names exact status fields/RPCs a
  readiness panel can consume, with file:line references.
- Do not touch: any app source.
- Risk: low. Model: audit report needs a strong model (Fable preferred);
  drift fixes are smaller-model safe.

### Slice 1 — v0.4.1 first-run readiness screen
- Objective: on launch (or empty profile), show plain-language readiness
  rows: app/backend, local AI runtime (Ollama), chat model, document search
  (embedding model + lexical fallback), scanned-document reading (OCR).
  Reuse existing status sources; add no new probes unless the audit proves a
  gap.
- Files: new `src/features/readiness/*`, `src/App.tsx` wiring; possibly a
  small aggregation RPC in `python/` if the audit says existing
  `models.detect_ollama` / `ocr.status` / backend state are insufficient.
- Tests: `npm run test:backend-status`, `npm run test:progress`, new unit
  tests for the readiness-row mapping helper, `npm run build:frontend`,
  `cargo check`.
- Acceptance: fresh profile shows the readiness view before chat; every row
  has a plain-words state and next step; degraded banner still works.
- Do not touch: RPC lifecycle semantics, privacy exclusions, chat pipeline.
- Risk: medium. Model: architecture + copy design is Fable; wiring existing
  status fields into rows and tests are smaller-model safe afterwards.

### Slice 2 — v0.4.2 Potato Mode preset
- Objective: one action applies conservative settings: small recommended
  model, short context, conservative retrieval breadth, low embedding batch
  size, OCR guardrails on, heavy features labeled. Reversible; plain
  explanation of what changed.
- Files: `python/.../settings_service.py` (preset concept),
  `chat_service.py` (context default audit), `rag_service.py` (retrieval
  limit), `embedding_service.py` (batch bound), settings UI in `src/`.
- Tests: new Python tests for preset application/reversal; existing suites;
  `npm run build:frontend`; `cargo check`.
- Acceptance: preset applies and reverts atomically; values are visible;
  nothing silently changes outside the preset.
- Do not touch: model routing logic, trace privacy, storage schema (unless a
  migration-gated settings key is unavoidable — then follow schema gates).
- Risk: medium-high (semantics ripple into chat/RAG). Model: settings
  semantics and default values are Fable/human; mechanical plumbing after
  the design is smaller-model safe.

### Slice 3 — v0.4.3 Ollama/model setup helper
- Objective: when Ollama or a model is missing, show guided steps in noob
  words: install link, copyable `ollama pull <recommended-model>` command,
  re-detect button. No in-app downloads without explicit maintainer
  approval (human decision point).
- Files: `src/features/readiness/*` extension, `model_service.py`
  (detection already exists at `detect_ollama()`), copy strings.
- Tests: unit tests for state→guidance mapping; existing suites; frontend
  build.
- Acceptance: each missing-dependency state shows a concrete next step;
  re-detect updates rows without restart; no hidden network calls.
- Do not touch: the loopback-only default endpoint; no auto-download.
- Risk: medium. Model: which model to recommend is Fable/human; the
  guidance UI given a decided recommendation is smaller-model safe.

### Slice 4 — v0.4.4 indexing throttle/pause/cancel
- Objective: user can cancel (and ideally pause) document indexing/OCR;
  giant documents get page limits or queued processing instead of freezing
  a weak machine.
- Files: `document_service.py`, `ocr_service.py`, `rag_service.py`,
  progress plumbing (`progress.py`, `lib.rs` fixed labels), Sources UI.
- Tests: Python tests for cancel/limit semantics; `npm run test:progress`
  (fixed-label guarantees must hold); full standard set.
- Acceptance: cancel mid-import leaves the profile consistent (no orphan
  chunks); progress labels remain fixed-vocabulary; giant-PDF guardrail has
  an explicit user-visible message.
- Do not touch: non-idempotent RPC no-replay guarantee, progress-ID rules.
- Risk: high (concurrency + data consistency). Model: Fable designs the
  cancellation semantics; smaller models only implement against a written
  spec with tests.

### Slice 5 — v0.4.5 profile storage visibility and cleanup
- Objective: show profile location and size in-app; delete a Source and its
  derived data completely (verify `mark_deleted` + file-copy cleanup);
  cleanup for caches/reports; low-disk warning.
- Files: `storage.py`, `rag_service.py` (delete path), new storage RPC,
  settings/about UI.
- Tests: Python tests proving delete reclaims files and chunks; standard
  set.
- Acceptance: reported size matches disk; delete leaves no orphaned file
  copies or chunks; cleanup never touches user documents outside the
  profile.
- Do not touch: profile path/identifier (`dev.odysseus.desktop`), schema
  without migration gates.
- Risk: medium-high (deletes user data). Model: deletion semantics reviewed
  by Fable/human; size-reporting plumbing is smaller-model safe.

### Slice 6 — v0.4.6 noob diagnostics / support bundle
- Objective: a "copy diagnostics" surface and optional support bundle that
  explain slowness/failures in plain words, excluding raw prompts,
  documents, paths, and model output — same exclusion discipline as
  Operation Trace.
- Files: new diagnostics assembly in `python/`, `OperationTrace.tsx`
  plain-words summary, copy/redact UI.
- Tests: redaction tests (assert forbidden fields absent), private-sentinel
  sweep, standard set.
- Acceptance: bundle contains versions, readiness states, timings, fixed
  labels — and provably no user content.
- Do not touch: trace privacy exclusions (may only tighten), no telemetry
  or auto-upload of any kind.
- Risk: high (privacy). Model: redaction policy is Fable/human only;
  implementation against the written policy plus adversarial tests can be a
  smaller model with Fable review.

### Slice 7 — v0.4 release gate and installer proof
- Objective: `GATE_v0.4.md` checklist, full test suites, installer build,
  checksum, installed-app verification, proof report, publish.
- Files: `projects/odysseus/GATE_v0.4.md`, `RELEASE_PROOF_v0.4*.md`,
  `docs/releases/`, version bumps in `package.json`, `Cargo.toml`,
  `tauri.conf.json`, sidecar `__version__`.
- Tests: full matrix per `GATE_v0.3.1.md` precedent, plus a scripted
  first-run smoke on a clean profile.
- Acceptance: gate GREEN with commit-tied evidence before tag/publish.
- Do not touch: nothing ships with unchecked gate boxes.
- Risk: medium. Model: final review is Fable/human; checklist assembly and
  mechanical verification runs are smaller-model safe.

## D. Fable-only tasks (strong model or human)

- Readiness architecture: which status sources feed the panel, whether a new
  aggregation RPC is justified, state model (ready/degraded/failed).
- Model recommendation heuristics and RAM-threshold guidance.
- Potato Mode settings semantics: which knobs, values, reversibility,
  interaction with existing sessions.
- Privacy/redaction design for diagnostics and the support bundle.
- Deciding what gets deferred when a slice grows.
- Final v0.4 release gate review and go/no-go.

## E. Smaller-model-safe tasks (Sonnet / Codex / GPT-5.5)

- README/docs drift fixes and release-notes updates.
- Small UI copy changes from approved copy text.
- Static checklist/readiness screens rendering already-designed states.
- Wiring existing status fields (`detect_ollama`, `ocr.status`, backend
  degraded state) into a designed readiness panel.
- Unit tests for pure helpers (state mapping, size formatting, redaction
  assertions against a written policy).
- Issue templates, gate/checklist updates, proof-report scaffolding.
- Mechanical refactors with an explicit file list and diff budget.

## F. Stop conditions

- No app source change without an accepted issue.
- No agents or tools work of any kind.
- No new model backend; Ollama loopback stays the only runtime.
- No cloud, account, or sync features.
- No hidden downloads — every network fetch is user-initiated and disclosed.
- No telemetry, ever.
- No installer build or publish outside the gated release slice.
- No "just one more feature" PRs; anything outside a slice becomes a new
  issue for the maintainer to accept or defer.
