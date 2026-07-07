# v0.4 Pareto Value Map — The 20% That Makes the App Worth Using

Status: planning only. This is the priority filter over
`V04_EXECUTION_PLAN.md`, `V04_ISSUE_BREAKDOWN.md`, `POTATO_PROOF_MATRIX.md`,
and `POTATO_NICHE_ESSENTIALS.md`. It answers one question: what are the few
functions that make Odysseus Desktop worthwhile for a noob on a potato, and
how done — honestly — is each one? Statuses come from the audited checklist
in `V04_POTATO_MODE_SCOPE.md` §3; "Planned" never counts as "Done".

## 1. Thesis

The app is worthwhile only if a noob on weak hardware can install it,
understand readiness, set up a small model, import one document, ask one
sourced question, survive missing dependencies/slowness, and recover
without leaking private data.

Everything below is ranked by how directly it serves that sentence.

## 2. Top 20% value list

| Rank | Function | Why it creates value | Status | Existing evidence | Planned issue/slice | Fable/human needed? | Must happen before |
|---|---|---|---|---|---|---|---|
| 1 | First-run readiness screen | First minutes decide adoption; empty chat = abandonment | Missing | No first-run/onboarding code in `src/` (scope §3 A2) | Issue 3 / v0.4.1 | Fable: state model + copy | Setup helper (rank 3), Potato Mode UX |
| 2 | Plain-language missing-dependency guidance | Raw errors are the #1 noob wall | Partial | Boot fetches status but shows jargon (`src/App.tsx:402-407`); OCR deps already plain (`ocr_service.py:259-306`) | Issues 2→3 (audit, then rows) | Fable: copy + error-state model | Any claim the app is noob-ready |
| 3 | Ollama/text-model/embedding-model detection + setup helper | Model download is the setup cliff | Partial | Detection Done (`model_service.py:60-88`, `embedding_service.py:118-146`); guidance/recommendation Missing (scope §3 B2/B5/B6) | Issue 5 / v0.4.3 | Fable/human: model recommendation; no auto-downloads | First sourced answer for a fresh user |
| 4 | One document → one sourced answer | The core value path; everything else is support | Done (unproven on P1/P2) | Imports (`document_service.py:15`), progress (`progress.py:92-101`), snippets + abstain (`chat_service.py:2274`) | Keep; proof via issue 11 | No — verify only | v0.4 gate (needs potato-tier proof) |
| 5 | Honest lexical fallback when embeddings missing | App works before full setup, without lying | Partial | Fallback works (`embedding_service.py:388`, `rag_service.py:815-855`); honest user-facing copy missing | Issue 3 (readiness row copy) | Fable: copy | Readiness screen claiming "document search: ready" |
| 6 | Potato Mode conservative preset | One switch beats ten knobs for noobs | Missing | Settings are a bare KV store (`settings_service.py:8-22`); no preset concept | Issue 4 / v0.4.2 | Fable/human: default values — locked only after issue 10 audit | Any "runs well on 8 GB" claim |
| 7 | Giant PDF/OCR guardrails | A 500-page scan must not freeze a potato | Missing | No page limits/throttles in `ocr_service.py` (scope §3 C6/E3) | Issue 6 / v0.4.4 | Fable: limit/queue design | Advertising large-document support |
| 8 | Pause/cancel indexing/OCR | Users must escape heavy work they started | Missing | Cancel exists only for benchmark campaigns (`campaign_service.py`) | Issue 6 / v0.4.4 | Fable: cancellation semantics (written spec) | Rank 7 being claimed done |
| 9 | Storage visibility and cleanup | Disk is scarce; deletion must actually reclaim | Partial/Missing | `delete_document` uses `mark_deleted` (`rag_service.py:164-168`), file-copy cleanup unverified; no size UI (scope §3 G1-G4) | Issue 7 / v0.4.5 | Fable/human: deletion semantics | Heavy local workflows (many imports, reports) |
| 10 | Degraded backend banner + Retry | Honest failure + self-service recovery | Done | `backendStatus.ts:1-37`, `retry_backend` (`lib.rs:865`); shipped in v0.3.1, gate GREEN | Keep; regression-test only | No | — |
| 11 | Noob "why is it slow?" diagnostics | Slowness reads as broken without explanation | Missing | `OperationTrace.tsx` is nerd-stats; no RAM/CPU/disk sensing anywhere (scope §3 G5/G6) | Issue 8 / v0.4.6 | Fable: what to sense + copy | Support-bundle usefulness |
| 12 | Redacted support bundle | Safe help-seeking without leaking documents | Missing | Trace exclusions designed (`README.md:184-186`); no bundle exists (scope §3 H4/H5) | Issue 8 / v0.4.6 | Fable/human ONLY: redaction policy | Telling users "share diagnostics" |
| 13 | Core-vs-heavy package clarity | Wrong download = instant failure on a potato | Partial | Core build without Florence shipped (`GATE_v0.3.1.md` §5); in-app heavy labels missing (scope §3 C7/E5); docs pending | Issue 12 | Fable reviews tier claims | v0.4 release notes |
| 14 | P1/P2 Potato Proof smoke matrix | Without it every potato claim is a guess | Planned, not run | Scenarios written (`POTATO_PROOF_MATRIX.md` §D); zero recorded runs | Issue 11 → gates issue 9 | Fable: procedure design; human may execute | v0.4 tag/publish |
| 15 | Long-term foundations: Artifact Service, Model Capability Registry, Generic Job Engine | Multiply future value; nothing in v0.4 depends on them | Missing (by design) | `LONG_TERM_PRODUCT_ROADMAP.md` §B Era 2 | Era 2 / v0.5 — not v0.4 | Fable: architecture | Nothing in v0.4 — do not pull forward |

## 3. Pareto status summary

Blunt accounting — planned is not done:

- **Already strong:** rank 4 (one doc → sourced answer, on strong hardware),
  rank 10 (degraded banner + Retry). Two of fifteen.
- **Exists but needs noob/potato UX:** rank 2 (status exists, copy is
  jargon), rank 3 (detection done, guidance absent), rank 5 (fallback works,
  honesty copy missing), rank 13 (core package shipped, labels/docs absent).
- **Planned but not implemented:** ranks 1, 6, 7, 8, 11, 12, 14. That is
  seven of the fifteen — nearly half the value list is paper.
- **Needs audit before implementation:** rank 6 defaults (blocked on issue
  10 hardware audit), rank 9 deletion semantics (`mark_deleted` unverified),
  rank 1/2 status sources (blocked on issue 2 readiness audit).
- **Long-term foundation, not v0.4:** rank 15. Valuable, and explicitly
  deferred; building it now would be executing the wrong things in the
  right-looking order.

## 4. Critical path

Execution order for the value-driving path:

A. Readiness audit (issue 2) → B. First-run readiness (issue 3) →
C. Model setup helper (issue 5) → D. Potato Mode defaults, after the
hardware/resource audit (issues 10→4) → E. Document import/index guardrails
(issue 6) → F. Storage cleanup (issue 7) → G. Noob diagnostics/redaction
(issue 8) → H. Potato Proof smoke (issue 11) → I. v0.4 release gate
(issue 9).

Why the order is not negotiable:

- **Readiness before setup helper:** the helper hangs guidance off
  readiness rows; building guidance before the state model exists means
  building it twice.
- **Hardware/resource audit before Potato Mode defaults:** defaults chosen
  without measured numbers are folklore; issue 10 must confirm or revise
  the `POTATO_PROOF_MATRIX.md` §B budgets first.
- **Indexing cancellation before claiming giant-PDF support:** a guardrail
  the user cannot escape is a trap; cancel semantics must exist before any
  large-document claim.
- **Storage cleanup before heavy local workflows:** encouraging many
  imports while deletion is unverified fills a potato's disk with data the
  user cannot reclaim.
- **Potato Proof before release:** v0.4's entire promise is potato-tier
  behavior; shipping on P3 gaming-laptop evidence alone repeats the exact
  mistake the matrix exists to prevent.

## 5. What Fable must own

Strong-model/human-only decisions — smaller models present options and stop:

- Model recommendations (which model, per tier).
- RAM thresholds and hardware-tier boundaries.
- Potato Mode default values (context, retrieval limit, batch size, OCR caps).
- Cancellation semantics for indexing/OCR (written spec before code).
- Deletion semantics (what "delete" reclaims, `mark_deleted` fate).
- Redaction policy for diagnostics and support bundles.
- Proof gate acceptance — only Fable + maintainer declare GREEN.
- What long-term features get deferred when a slice grows.

## 6. What smaller models can safely execute

- Docs updates (drift fixes, release notes, checklist text).
- Static readiness UI **after** the state model is designed.
- Copyable-command UI **after** the model recommendation is approved.
- Unit tests for pure mappings (status→row, state→guidance).
- Storage size formatting **after** deletion semantics are approved.
- Checklist/gate updates and proof-report scaffolding.

## 7. Long-term "do not lose the thesis" note

Future features from the original Odysseus inventory — vision, screenshots,
Artifact Service, Model Capability Registry, Job Engine, bounded tools, deep
research, memory, skills, compare, Cookbook — are valuable only if they
reinforce the PotatoCS thesis: small local models + deterministic specialist
systems + honest constraints on ordinary hardware. A feature that cannot
state its cost on P0–P2 hardware, or that ships before the ranks above are
green, is off-thesis no matter how good the demo looks.
