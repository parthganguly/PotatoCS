# Potato Proof Matrix

Status: planning addendum to `V04_EXECUTION_PLAN.md`. Docs only; no
implementation, no measurements taken yet. All numbers below are **proposed
budgets**, not measured results. Companion: `POTATO_NICHE_ESSENTIALS.md`.

## A. What "potato computer" means — hardware tiers

"Works on my old gaming laptop" is not potato proof. Define tiers:

| Tier | Name | Spec sketch |
|---|---|---|
| P0 | Survival machine | 4 GB RAM, dual-core/old CPU, integrated graphics, slow SSD or HDD, Windows 10/11 |
| P1 | Ordinary weak laptop | 8 GB RAM, 4-core CPU, iGPU, modest SSD |
| P2 | Old office desktop | 8–16 GB RAM, older Intel/AMD CPU, maybe HDD, no dGPU |
| P3 | Old gaming laptop | 16 GB RAM, low-end dGPU (GTX/RTX x50-class) |
| P4 | Modern modest machine | 16 GB RAM, decent recent CPU, maybe no dGPU |

Support policy:

- **P1/P2 must be genuinely usable** — the core journey (install → readiness
  → small model → one PDF → one sourced answer) completes in tolerable time.
- **P0 must degrade gracefully** — reduced features (lexical-only search,
  OCR capped or deferred, smallest model or honest "too little RAM" copy),
  never a hang, freeze, or unexplained failure.
- **P3/P4 are the easy cases.** Passing on P3 alone proves nothing about
  the Potato thesis. v0.4 must not be validated only on P3.

## B. Resource budgets

### B.1 Proposed thresholds (unmeasured)

Every number here is a proposed budget until the v0.4 hardware/resource
audit measures real values on real or simulated tier hardware. These
thresholds are the target — they are not overwritten by §B.2 below.

| Budget | P0 | P1/P2 | P3/P4 |
|---|---|---|---|
| Cold launch to interactive readiness view | ≤ 15 s | ≤ 8 s | ≤ 5 s |
| Idle RAM (app + sidecar, no model loaded) | ≤ 400 MB | ≤ 500 MB | ≤ 600 MB |
| Idle CPU (steady state, no jobs) | ~0%, no busy polling | same | same |
| Profile growth after one 30-page PDF | ≤ 3× source file size | same | same |
| Import/indexing CPU | throttled, UI stays responsive | bounded, cancellable | bounded |
| OCR peak memory | page-at-a-time; capped, queued | capped | capped |
| Time to first sourced answer (small model, 30-page PDF) | best-effort, honest slow copy | ≤ 60 s | ≤ 30 s |
| Max document size before guardrail prompt | ~100 pages | ~300 pages | ~300 pages |
| UI responsiveness during any background job | input echo < 200 ms | same | same |
| Orphan sidecars after close/crash | 0 | 0 | 0 |
| Non-user-initiated downloads | 0, ever | 0 | 0 |

Rules: budgets get measured before Potato Mode default values are locked
(see `V04_ISSUE_BREAKDOWN.md` audit issue). A budget miss on P1/P2 blocks
the v0.4 gate; a miss on P0 requires an honest documented degradation, not
silent failure.

### B.2 Status (v0.4 hardware/resource audit, 2026-07-12)

Backend-only, P3-tier measurements from `V04_HARDWARE_AUDIT.md`
(candidate `3cd01b55`, branch `audit/v0.4-hardware-resource-metrics`).
Status is one of `measured-pass`, `measured-fail`, `still-proposed` per
tier column. P3 native measurements cannot pass a P1/P2 budget by
themselves; packaged-host/UI-scoped budgets stay `still-proposed` until
the §7 runbook in the audit report runs on real or simulated P1/P2
hardware. "Component measured" evidence notes what the backend-only P3
run showed; it never upgrades a still-proposed P0/P1/P2 status. A
measured-fail, however, carries to all tiers when the failing property
is hardware-independent (bytes written by code on fixed input) or can
only be worse on weaker hardware (an uncapped allocation on less RAM)
— that a-fortiori rule is why the two fails below cover every tier.

| Budget | P0 | P1/P2 | P3/P4 | Evidence |
|---|---|---|---|---|
| Cold launch to interactive readiness view | still-proposed | still-proposed | still-proposed | UI metric; component measured: sidecar spawn→ping median 1.22 s. Blocker: packaged-host run needs clean second Windows user / VM. |
| Idle RAM (app + sidecar, no model) | still-proposed | still-proposed | still-proposed | Host RAM unmeasured; component: sidecar idle WS ≈ 47 MB. |
| Idle CPU (steady state) | still-proposed | still-proposed | still-proposed | Host unmeasured; component: sidecar 0.00 %, no busy polling. |
| Profile growth after one 30-page PDF | measured-fail | measured-fail | **measured-fail** | 17.7× vs ≤ 3× budget — fixture-ratio artifact; absolute growth ≈435 KB. Recommend restating budget with an absolute floor (e.g. "≤ 3× source size + 1 MB"). Hardware-independent (bytes on disk are determined by code + input, identical across all 3 runs; threshold is the same for every tier), measured on P3. |
| Import/indexing CPU (bounded, cancellable) | still-proposed | still-proposed | still-proposed | No cancel path exists yet (planned v0.4.4); UI responsiveness unmeasured. Import itself completed in 1.8–7.6 s. |
| OCR peak memory (page-at-a-time, capped) | measured-fail | measured-fail | **measured-fail** | No cap: sidecar WS ≥ 1,586 MB on a legal 2-page input, 997 MB on 1 page; page-at-a-time loop is honored but render size is uncapped (400 dpi fixed). Cap absence is structural (same code renders the same uncapped image on any tier), so the "capped" budget fails tier-independently; the WS peak values are P3 observations only. |
| Time to first sourced answer (small model, 30-page PDF) | still-proposed | still-proposed | **measured-pass** | 16.3 s chat / 20.8 s end-to-end cold vs ≤ 30 s (n=3, model pre-installed). P1/P2 ≤ 60 s needs tier hardware. |
| Max document size before guardrail prompt | still-proposed | still-proposed | still-proposed | Guardrail not implemented yet (Issue 6 scope). |
| UI responsiveness during background job | still-proposed | still-proposed | still-proposed | UI unmeasured. |
| Orphan sidecars after close/crash | still-proposed | still-proposed | still-proposed | Backend-only graceful shutdown: 10/10 pass, no audit orphan processes at final sweep. Crash path and packaged-host close/crash path not measured in this v0.4 audit. Historical v0.3.1 smoke is context only, not v0.4 proof. |
| Non-user-initiated downloads | still-proposed | still-proposed | still-proposed | No network monitor was run in this audit; harness enforced HF offline env and nothing was pulled, but that is not proof. |

## C. Potato-specific failure modes

Niche but essential ways low-end UX dies. Each must map to a scenario in
§D or an item in `POTATO_NICHE_ESSENTIALS.md`.

1. App launches to an empty chat with no setup guidance — first impression
   is "broken."
2. Ollama missing; chat fails with jargon or silence and the user has no
   idea why.
3. Installed model too large for RAM; machine swaps and everything crawls.
4. Embedding model missing; RAG silently degrades with no explanation.
5. OCR starts on a huge scanned PDF; laptop becomes unusable for an hour.
6. Heavy vision path (VLM / Florence-class) enabled accidentally on a
   machine that cannot afford it.
7. Profile grows until disk is full; no visibility, no cleanup.
8. Deleting a source does not reclaim derived chunks/file copies.
9. Backend sidecar dies; user thinks the whole app is broken (banner +
   Retry exist since v0.3.1 — verify on weak hardware).
10. Progress says "working" forever with no cancel path.
11. Low disk or low RAM is never detected or explained.
12. Logs/diagnostics expose paths, prompts, or document text.
13. Background jobs survive app close and eat scarce RAM/CPU.
14. Installer too large, or core-vs-heavy package variant confusion.
15. HDD machines suffer from many small random reads/writes (SQLite/chunk
    patterns tuned only on SSD).
16. Antivirus/SmartScreen/unsigned-app warnings scare noobs away at the
    door.
17. Offline user cannot finish setup because model instructions assume
    internet.
18. User cannot tell what is local app vs external Ollama dependency, so
    they debug the wrong thing.

## D. Potato proof scenarios

Smoke/eval scenarios for the v0.4 gate. "Pass" always means: plain-words
explanation, a concrete next step, no hang, no hidden network.

1. Fresh install, no Ollama installed → readiness names the gap + install
   guidance.
2. Fresh install, Ollama installed, no text model → copyable `ollama pull`
   step.
3. Text model installed, no embedding model → readiness says search is
   degraded and how to fix it.
4. Embeddings missing: lexical fallback works and labels itself honestly.
5. 30-page normal PDF import → completes within budget, progress visible.
6. 300-page giant PDF import → guardrail prompt; cancel/throttle works;
   profile consistent after cancel.
7. Scanned PDF, OCR dependencies missing → plain explanation, no crash.
8. Scanned PDF, OCR available but slow → queued/capped, cancellable, UI
   responsive.
9. Low disk → profile warning before corruption, not after.
10. Delete a source → derived chunks and file copies reclaimed; profile
    size drops.
11. Backend killed during chat → degraded banner + Retry restores.
12. App closed during indexing → no corrupt profile; job resumable or
    cleanly failed on restart.
13. Fully offline machine → no cloud fallback, no hidden network attempt,
    setup instructions still make sense.
14. Tier matrix run: each scenario above marked pass / degrade /
    unsupported per tier (P0 may degrade; P1/P2 must pass; P3 is not the
    proof machine).

## E. Metrics table

Evidence column reflects the repo today (`V04_POTATO_MODE_SCOPE.md` §3
audit); "Missing instrumentation" is what must exist before proof is real.

| # | Scenario | Tier | Expected behavior | Proof needed | Existing evidence | Missing instrumentation | v0.4 slice |
|---|---|---|---|---|---|---|---|
| 1 | No Ollama | P0–P2 | Readiness names gap + steps | Screenshot + copy review | `detect_ollama()` exists; no UI | Readiness panel | v0.4.1/v0.4.3 |
| 2 | No text model | P0–P2 | Copyable pull command | Screenshot | `/api/tags` listing | Setup helper UI | v0.4.3 |
| 3 | No embedding model | P0–P2 | Degraded-search copy | Screenshot | `installed()` check | Readiness row + copy | v0.4.1 |
| 4 | Lexical fallback | P0–P2 | Works + labels itself | Query transcript | `_lexical_status`, rerank | Honest fallback copy | v0.4.1 |
| 5 | 30-page PDF | P1/P2 | In-budget import + answer | Timed run log | Progress labels exist | Timing capture | v0.4.4/gate |
| 6 | 300-page PDF | P1/P2 | Guardrail + cancel | Run log + profile check | None (no cancel/limit) | Guardrail + cancel + consistency check | v0.4.4 |
| 7 | OCR missing | P0–P2 | Plain explanation | Screenshot | `ocr_service` dep detection | Readiness row | v0.4.1 |
| 8 | OCR slow | P0/P1 | Queued/capped, cancellable | Run log + responsiveness | None (no throttle) | Page cap, queue, cancel | v0.4.4 |
| 9 | Low disk | P0–P2 | Warning before corruption | Simulated-low-disk run | None | Disk check + warning | v0.4.5 |
| 10 | Delete reclaims | all | Size drops; no orphans | Before/after size proof | `mark_deleted` only; unverified | Size RPC + delete verification | v0.4.5 |
| 11 | Backend killed | all | Banner + Retry | Existing smoke rerun | v0.3.1 smoke GREEN | Rerun on weak/simulated HW | gate |
| 12 | Close mid-index | P1/P2 | No corruption; clean state | Kill-during-job test | WAL persistence proved | Job-state crash test | v0.4.4 |
| 13 | Offline | all | No network, setup sensible | Network monitor run | Loopback-only endpoint | Offline setup docs + sweep | v0.4.3/gate |
| 14 | Tier matrix | P0–P3 | Pass/degrade/unsupported map | Per-tier run records | None — only dev machine | Tier hardware or simulated caps (VM/job-object limits) | gate |

## F. Recommendation

v0.4 should not be called done until Potato Proof passes on at least
P1/P2-class hardware or a clearly documented simulated equivalent
(e.g., a RAM/CPU-capped VM with the constraint method recorded in the
proof report). P3 old gaming laptop success is not enough.
