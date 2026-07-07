# v0.4 Issue Breakdown — Draft Backlog

Status: draft. **Do not create these GitHub issues until the maintainer
approves.** Ordering and scope come from `V04_EXECUTION_PLAN.md`; scope
authority is `V04_POTATO_MODE_SCOPE.md`. One issue → one branch → one PR.

Dependency chain: 1 → 2 → 3 → (4, 5 in either order, 4 recommended first)
→ 6 → 7 → 8 → 9. Issue 2's audit report may adjust later issues before
they open. Additionally: **issue 10 (hardware/resource audit) must
complete before issue 4's final Potato Mode default values are locked**;
issues 11 (Potato Proof smoke matrix) and 12 (package/hardware docs) must
be done before issue 9 can close the gate. The Pareto Value Map
(`V04_PARETO_VALUE_MAP.md`) controls priority: issues that unlock
first-run readiness, model setup, Potato Mode, guardrails, storage,
diagnostics, and proof outrank cosmetic or long-term features.

## Issue 1 — docs: fix post-v0.3.1 current-release drift

- Purpose: stop pointing users at v0.3.0 as the current release.
- Scope: README current-release/download sections → v0.3.1 with checksum;
  `docs/releases/v0.3.1.md` installer section says built/published;
  `NEXT_TASK.md` and `ROADMAP.md` reflect the accepted v0.4 theme.
- Non-goals: no README rewrite, no new claims, no app source.
- Likely files: `README.md`, `docs/releases/v0.3.1.md`,
  `projects/odysseus/NEXT_TASK.md`, `projects/odysseus/ROADMAP.md`.
- Acceptance: no doc says v0.3.0 is current or v0.3.1 installer is unbuilt;
  v0.3.1 hash `F130D92B…111D` referenced correctly.
- Tests: `git diff --check` only (docs-only).
- Model: Sonnet/Codex. **Note: delivered by the planning baton PR; open
  this issue only if that PR is not merged.**

## Issue 2 — audit: first-run readiness and runtime status sources

- Purpose: ground v0.4 UI work in the real status surface, read-only.
- Scope: enumerate every existing readiness signal (backend degraded state,
  `models.detect_ollama`, `ocr.status`, embedding install check, lexical
  fallback status, progress labels) with file:line refs; resolve the
  Unknown/Partial rows in `V04_POTATO_MODE_SCOPE.md` §3 (C3 context
  defaults, D2 drag/drop, G2 delete semantics); recommend whether v0.4.1
  needs a new aggregation RPC.
- Non-goals: no code changes at all.
- Likely files (read): `src/App.tsx`, `src/tauri.ts`,
  `src/features/shell/backendStatus.ts`, `python/**/model_service.py`,
  `ocr_service.py`, `embedding_service.py`, `chat_service.py`,
  `rag_service.py`, `settings_service.py`, `src-tauri/src/lib.rs`.
  Output: `projects/odysseus/V04_READINESS_AUDIT.md`.
- Acceptance: audit names exact fields/RPCs a readiness panel can consume;
  every claim has a file:line reference; open questions listed for the
  maintainer.
- Tests: none (read-only); `git diff --check` on the report.
- Model: **Fable** (or strong model); smaller models drift on audits.

## Issue 3 — feat: first-run readiness panel (v0.4.1)

- Purpose: first launch shows plain-language readiness, not an empty chat.
- Scope: readiness rows for app/backend, Ollama runtime, chat model,
  document search (embeddings + lexical fallback), OCR; each row: state in
  noob words + one next step; re-check without restart.
- Non-goals: no setup helper actions (issue 5), no new probes unless the
  audit demanded one, no redesign of chat.
- Likely files: new `src/features/readiness/`, `src/App.tsx`,
  `src/tauri.ts`; only audit-approved backend additions.
- Acceptance: fresh profile → readiness view on launch; all rows truthful
  against a machine with/without Ollama/models/Tesseract; degraded banner
  unaffected; no jargon or raw error strings in copy.
- Tests: new unit tests for status→row mapping helper;
  `npm run test:backend-status`; `npm run test:progress`;
  `npm run build:frontend`; `cargo check`.
- Model: Fable for the state model and copy; Sonnet/Codex for wiring and
  tests against the approved design.

## Issue 4 — feat: Potato Mode settings preset (v0.4.2)

- Purpose: one action configures the app conservatively for low-end
  hardware.
- Scope: preset concept in the settings layer; applies maintainer-approved
  values for model choice, context length, retrieval limit, embedding batch
  size, OCR guardrails; visible summary of what changed; clean revert.
- Non-goals: no auto-detection of hardware in this issue (G5 exploration is
  separate), no per-setting redesign of the settings UI.
- Likely files: `python/**/settings_service.py`, `chat_service.py`,
  `rag_service.py`, `embedding_service.py`, settings UI in `src/`.
- Acceptance: apply/revert is atomic and idempotent; preset values are
  inspectable; existing sessions unaffected except where documented.
- Tests: new Python tests for apply/revert and value propagation;
  `python -m pytest python\tests`; full standard set.
- Model: Fable for semantics + default values (human approves values);
  Sonnet/Codex for plumbing after the spec exists.

## Issue 5 — feat: Ollama/model setup helper (v0.4.3)

- Purpose: turn "Ollama missing / model missing" into guided noob steps.
- Scope: guidance UI off the readiness rows: install link, copyable
  `ollama pull <approved-model>` command, embedding-model step, re-detect
  button.
- Non-goals: **no in-app downloads**, no bundled models, no endpoint
  changes; loopback default stays.
- Likely files: `src/features/readiness/`, copy strings,
  `python/**/model_service.py` (read mostly; detection exists).
- Acceptance: every missing-dependency state has a concrete next step;
  re-detect updates without restart; zero non-user-initiated network calls.
- Tests: unit tests for state→guidance mapping; standard set.
- Model: GPT-5.5/Sonnet for UI given approved copy + approved model
  recommendation (recommendation itself is Fable/human).

## Issue 6 — feat: indexing throttle/pause/cancel (v0.4.4)

- Purpose: a heavy import must never trap the user or freeze a potato.
- Scope: cancel (and pause if feasible) for import/indexing/OCR; page
  limits or queueing for giant documents; consistent profile state after
  cancel; plain-words guardrail messages.
- Non-goals: no parallelism tuning beyond the guardrail, no benchmark
  cancel changes (already exists).
- Likely files: `document_service.py`, `ocr_service.py`, `rag_service.py`,
  `progress.py`, `src-tauri/src/lib.rs` (fixed labels), Sources UI.
- Acceptance: cancel mid-import leaves no orphan chunks/files; progress
  labels stay fixed-vocabulary; no-replay guarantee untouched.
- Tests: Python cancel/limit tests; `npm run test:progress`; full standard
  set including `cargo test`.
- Model: **Fable designs cancellation semantics (written spec required)**;
  Codex/Sonnet may implement strictly against that spec.

## Issue 7 — feat: profile storage visibility and cleanup (v0.4.5)

- Purpose: disk is scarce on potatoes; users must see and reclaim space.
- Scope: show profile location + size in-app; verify/complete source
  deletion (chunks + file copies, current `mark_deleted` semantics); cache/
  report cleanup; low-disk warning.
- Non-goals: no profile migration, no identifier change, no multi-profile
  management UI.
- Likely files: `python/**/storage.py`, `rag_service.py`, new storage RPC,
  about/settings UI.
- Acceptance: reported size matches disk; deleting a Source reclaims its
  file copies and chunks (test-proved); cleanup never touches files outside
  the profile directory.
- Tests: Python deletion/size tests; `python -m pytest python\tests`; full
  standard set.
- Model: Fable/human reviews deletion semantics; Sonnet/Codex implements
  size reporting and UI.

## Issue 8 — feat: noob diagnostics and redacted support bundle (v0.4.6)

- Purpose: let a noob ask for help without leaking documents or prompts.
- Scope: plain-words "why is it slow / broken" summary; copy-diagnostics
  button; optional support-bundle file — all under a written redaction
  policy excluding raw prompts, responses, document text, paths, images.
- Non-goals: no auto-upload, no telemetry, no crash reporter, no weakening
  of existing trace exclusions.
- Likely files: new diagnostics assembly in `python/`,
  `src/features/chat/OperationTrace.tsx`, copy/redact UI.
- Acceptance: bundle provably lacks forbidden content (adversarial tests +
  private-sentinel sweep); includes versions, readiness states, timings,
  fixed labels.
- Tests: redaction assertion tests; sentinel sweep; full standard set.
- Model: **redaction policy is Fable/human only**; implementation may be
  Sonnet/Codex with Fable review before merge.

## Issue 9 — release: v0.4 gate and proof plan

- Purpose: ship v0.4 with the same evidence discipline as v0.3.x.
- Scope: `GATE_v0.4.md` checklist (tests, version alignment, hygiene,
  installer, checksum, installed verification, first-run smoke on a clean
  profile); build/publish per gate; `RELEASE_PROOF_v0.4.md`; release notes;
  version bumps.
- Non-goals: no feature work; nothing ships with unchecked boxes.
- Likely files: `projects/odysseus/GATE_v0.4.md`, `RELEASE_PROOF_v0.4.md`,
  `docs/releases/v0.4*.md`, `package.json`, `src-tauri/Cargo.toml`,
  `src-tauri/tauri.conf.json`, sidecar `__version__`.
- Acceptance: gate GREEN with commit-tied evidence before tag/publish;
  downloaded-asset hash independently verified.
- Tests: full matrix per `GATE_v0.3.1.md` precedent plus first-run smoke
  and the Potato Proof smoke matrix (issue 11) on P1/P2-class hardware or
  a documented simulated equivalent — P3 gaming-laptop proof alone does
  not close this gate.
- Model: checklist assembly Sonnet/Codex; **final review and go/no-go is
  Fable + maintainer**.

## Issue 10 — audit: hardware/resource readiness and Potato Proof metrics

- Purpose: replace the proposed budgets in `POTATO_PROOF_MATRIX.md` §B
  with measured numbers before Potato Mode defaults are locked.
- Scope: measure cold launch, idle RAM/CPU, profile growth per PDF,
  import/OCR CPU and memory behavior, and time-to-first-sourced-answer on
  at least one P1/P2-class machine or a documented RAM/CPU-capped
  simulated equivalent; record method + raw numbers; confirm or revise
  each §B budget; flag any budget that current code cannot meet.
- Non-goals: no code changes; no tuning; measurement only.
- Likely files (read): app at runtime; output
  `projects/odysseus/V04_HARDWARE_AUDIT.md` updating
  `POTATO_PROOF_MATRIX.md` §B statuses.
- Acceptance: every §B budget marked measured-pass, measured-fail, or
  still-proposed with reason; constraint method (real HW vs VM caps)
  documented.
- Ordering: **must complete before issue 4's final default values are
  locked** (context length, retrieval limit, batch size, OCR caps).
- Tests: none (measurement report); `git diff --check`.
- Model: Fable or strong model designs method; runs may be human-assisted.

## Issue 11 — test: Potato Proof smoke matrix

- Purpose: turn `POTATO_PROOF_MATRIX.md` §D scenarios 1–14 into a
  repeatable, evidence-producing smoke procedure for the v0.4 gate.
- Scope: scripted or checklisted runs for each scenario (fresh install /
  missing Ollama / missing models / giant PDF / OCR / low disk / delete
  reclaim / kill mid-job / offline), each producing recorded evidence
  (output, screenshot, or log) tied to a commit; per-tier
  pass/degrade/unsupported record per §D scenario 14.
- Non-goals: no new app features; gaps found become issues, not inline
  fixes.
- Likely files: `projects/odysseus/POTATO_PROOF_SMOKE_v0.4.md` (procedure
  + results), possibly small scripts under `scripts/`.
- Acceptance: every scenario has a recorded result; failures block issue
  9; no scenario marked pass without evidence.
- Tests: the smoke itself plus `git diff --check` on docs.
- Model: procedure design Fable; execution smaller-model or human-safe.

## Issue 12 — docs: core vs heavy package and hardware expectations

- Purpose: a noob must know which download fits their machine before
  downloading.
- Scope: README/release-notes section mapping tiers P0–P4 to
  expectations (what works, what degrades, what is unsupported); core vs
  heavy (Florence-class) package distinction with install sizes;
  unsigned-installer/SmartScreen explanation; offline setup notes;
  hash-verification steps kept current.
- Non-goals: no packaging changes; no new installer variants; docs only.
- Likely files: `README.md`, `docs/releases/v0.4*.md`,
  `projects/odysseus/POTATO_PROOF_MATRIX.md` (tier table is the source).
- Acceptance: a reader can pick the right package and predict behavior on
  their tier; no claim contradicts measured issue 10/11 results.
- Tests: `git diff --check` only (docs-only).
- Model: Sonnet/Codex from approved copy; tier claims reviewed by Fable.
