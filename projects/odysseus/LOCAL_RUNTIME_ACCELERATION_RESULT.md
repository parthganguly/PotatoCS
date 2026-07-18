# Local Runtime Acceleration — Fable Implementation Result

Status: research/engineering track on `feat/local-runtime-acceleration`
(stacked on `feat/deep-local-fable`); first independent review round
(PR #35, 2026-07-17) addressed. Backend-only; no UI, no settings
mutation, no chat-routing change, no change to `main`, the installer,
release assets, or the v0.4 gate.

Date: 2026-07-17/18, updated after review 2026-07-18.
Author: Claude Code (Fable).

## Commits

- **Base:** `8ab22318` — tip of `feat/deep-local-fable` = PR #33 head.
- **Build commits:** `4e44fe59` RFC → `a43afe18` inventory →
  `0f667d0c` benchmark harness → `546af85e` planner → `61f57831`
  `runtime.*` RPCs → `a65d54a6` measured evidence (31 artifacts) →
  `aa45ec19` first report.
- **Review-response commits (2026-07-18):** `869e75c2` architecture-
  aware KV estimation + planner honesty → `d73ae33b` evidence
  fingerprints + bounded cached RPCs → `e556d34e` artifact-writer
  hardening → this report update + doc honesty pass (final head in the
  PR).
- **Upstream runtimes:** Ollama **0.31.1** (probed live); llama.cpp
  release **b10064** (`86d86ed43`), official Windows x64 CPU zip,
  SHA256 `C9B770B5…099E` (CUDA zip archived `D3DF8C73…34B`, unused);
  Colibrì proof from PR #33 preserved untouched (109 tests green,
  upstream `54cfe563`).

## Review findings (PR #35, first round) and resolutions

| # | Finding | Resolution |
|---|---|---|
| 1 | KV-cache estimator dimensionally wrong (`ctx × params_B × 24` ≈ 0.3 MB for 3.2B@4096; real ≈ 448 MB) — fit classes could be wrong by orders of magnitude | **Replaced** (`869e75c2`): KV = (K_len+V_len) × kv_heads × layers × dtype × tokens from `/api/show` geometry (verified live for llama + qwen3 architectures). Unknown geometry → documented 512 KiB/token upper bound, `unknown_kv_geometry` rejection, never a fit claim. Estimates tested against committed runtime-residency observations within a documented tolerance band (≥ 0.6× and ≤ 4× of observed non-weight residency). |
| 2 | "Measured" evidence identity unsafe (tag + logical threads only; variant batches merged) | **Replaced** (`d73ae33b`): full fingerprint (runtime+version, tag/digest/quant/bytes, CPU arch/ISA/cores, GPU identity + total VRAM, RAM total, context, tuning options, server env, placement band, shape). Only identical fingerprints aggregate; summaries carry sample count, median, range, and exact batch ids; free RAM/VRAM compared via the measured placement band; parametrized tests prove any single mismatched field disqualifies evidence and that variant batches never merge. |
| 3 | `runtime.*` RPCs could freeze the single-threaded sidecar (up to 32 × 5 s `/api/show` calls) | **Fixed** (`d73ae33b`): detail probes run in a worker thread under a 2 s total budget (8-model probe cap; a hanging probe is abandoned); every probe individually bounded; completed snapshots cached 60 s with explicit `cache_age_ms`/`partial`; declared worst-case cold ceiling **15 s** with its arithmetic in code. Test: 32 deliberately hanging probes return in < 5 s, flagged partial. |
| 4 | `reachable_deep_local` mislabeled an Ollama total-RAM state | **Renamed** (`869e75c2`) to `reachable_after_memory_reclaim`: Ollama-honest, `persisted_job` execution class, `requires_memory_reclaim` warning; Deep Local terminology never appears in an Ollama plan (tested). Genuine Deep Local planning remains exclusively the Colibrì `deep_local.plan`/`doctor` surface. |
| 5 | Report claimed shipped rules the planner does not emit | **Docs corrected** (this commit): the planner emits only an objective-sized `num_ctx`; keep-alive/context-sizing/flash-KV are research findings (`measured_exploratory`), stated as such everywhere, and `runtime.recommendations` now exposes them under `research_findings` with that status. |
| 6 | Flash-KV comparison not a safe recommendation (optimized arm at ~16–177 MB min system RAM; GPU missing from its snapshot) | **Downgraded** (this commit): preserved as exploratory evidence with both defects stated; safety acceptance criteria added (min system-RAM floor, stable GPU detection, comparable ambient VRAM, alternating A/B rounds, no hidden reloads, quality pass, no failure increase); no recommendation exists in code or docs until met. |
| 7 | Artifact writer path escape + narrow redaction | **Hardened** (`e556d34e`): safe-slug batch ids, resolved-parent containment proof, nested field allowlists, server-env value validation, bounded notes, recursive rejection of Windows-any-drive/UNC/Unix absolute paths in every string, matching serialized-payload sentinel patterns; hostile tests for `../../escape`, `D:\…`, UNC, `/home/…`, and paths embedded in notes/model/env fields. All 31 committed artifacts still validate byte-unchanged. |
| 8 | Unmeasured-speed fallback could label unmeasured configs interactive | **Fixed** (`869e75c2`): no numeric TTFT/TPS without compatible evidence (`estimates.basis: unmeasured`, values null, `speed_unmeasured` warning); unmeasured configurations are never `interactive`; the derived score ranks candidates only. |

## Corrected memory-estimation examples (geometry verified live)

| Model | Geometry (layers × kv_heads × head_dim) | KV/token | KV @4096 | KV @8192 | Old formula @4096 |
|---|---|---|---|---|---|
| llama3.2:1b | 16 × 8 × 64 | 32 KiB | 128 MiB | 256 MiB | ~0.1 MB (wrong ~1,000×) |
| llama3.2:3b | 28 × 8 × 128 | 112 KiB | 448 MiB | 896 MiB | ~0.3 MB (wrong ~1,400×) |
| qwen3:8b | 36 × 8 × 128 | 144 KiB | 576 MiB | 1.125 GiB | ~0.8 MB (wrong ~700×) |
| unknown geometry | — | 512 KiB (documented upper bound) | 2 GiB | 4 GiB | n/a — and no fit claim permitted |

Residency cross-check (committed artifacts): 3b paired-default reported
2,776 MB resident vs 2,019 MB weights → 757 MB non-weight; estimator
predicts 448 MB KV + 600 MB overhead = 1,048 MB (conservative, 1.4×
observed — within the documented band). qwen3:8b: observed 736 MB
non-weight vs estimated 1,176 MB (1.6×, conservative).

## Hardware / models (unchanged from first report)

One P3 machine: Ryzen 5 4600H, 15.4 GB RAM, RTX 3050 Laptop 4 GB,
Windows 11. Models measured: llama3.2:1b Q8_0, llama3.2:3b Q4_K_M,
qwen3:8b Q4_K_M, plus the same 1b GGUF blob via llama.cpp b10064
(CPU arm). All 31 artifacts preserved byte-identical through the
review response.

## What the implementation now guarantees

1. **Benchmark harness** (dev-only): six deterministic shapes,
   in-process quality gates, streaming TTFT measurement, resource
   sampling, schema-validated artifacts whose writer refuses hostile
   content and path escape.
2. **Inventory**: shell-free hardware probes; model inventory including
   per-model KV geometry; every probe bounded; detail probes under a
   2 s total budget; explicit `details_complete`.
3. **Planner** (pure, deterministic): architecture-aware memory
   estimation with named margins; fit classes `fits_gpu_full |
   fits_gpu_partial | fits_cpu_ram | reachable_after_memory_reclaim |
   not_runnable` (+ `unknown_kv_geometry` rejection); measured
   confidence only via full fingerprint compatibility; no speed claims
   and no `interactive` class without measurements; Deep Local
   vocabulary excluded from Ollama plans.
4. **RPCs** (read-only): `runtime.inventory/benchmarks/plan/
   recommendations` with a declared 15 s worst-case cold ceiling, 60 s
   snapshot cache, explicit cache-age/partial state, evidence summaries
   with sample counts/ranges/batch ids, and `research_findings` marked
   `measured_exploratory` throughout.

## Measured results — all `measured_exploratory` (one P3 machine)

Real measurements, preserved artifacts, honest caveats; **none of these
is a shipped rule or production recommendation**, and the planner emits
nothing from them:

- Keep-alive loss costs 13–18× per turn (3b reload 6.2–8.7 s vs 0.4 s
  warm).
- Default 4096 context silently truncated a 5,138-token document
  (planted-fact quality 0/12); `num_ctx` 8192 → 4/4. A correctness
  effect of context sizing.
- Prompt-cache reuse: repeat prompt eval 880 ms → 12 ms (~73×).
- Flash attention + q8_0 KV: 10.1 → 23.5 tok/s in ONE paired batch
  under VRAM pressure with a visible placement mechanism — but the
  optimized arm ran at critically low system RAM (~16–177 MB minimum
  available) and its hardware snapshot missed the GPU; blocked from
  promotion until the safety acceptance criteria in
  `LOCAL_RUNTIME_BENCHMARKS.md` §8 are met.
- Manual offload of minority layers: +6–13 %. llama.cpp vs Ollama CPU:
  parity ±10 %. Threads: neutral. num_batch: inconclusive (ambient
  confound).
- Overcommit failure case: forced full offload of a 5.9 GB model onto
  the 4 GB card runs via driver spillover but crushes system RAM to
  0.7 GB free — the configuration the planner's (implemented) margin
  rule forbids.

## Model-reach results (corrected planner, live run 2026-07-18)

- **fast** → llama3.2:1b, `fits_gpu_full`, `interactive`, confidence
  **measured** (fingerprint-compatible baseline: 291 ms TTFT / 103
  tok/s median, n=3, batch `ollama-0311-llama32-1b-medium-baseline`).
- **balanced** → llama3.2:3b, `fits_gpu_full`, `slow_interactive`,
  confidence **derived** — the 8192-context configuration has no
  compatible evidence, so the plan carries **no numeric speed claims**
  (`speed_unmeasured`).
- **deep** → qwen3:8b, `reachable_after_memory_reclaim`,
  `persisted_job`, confidence derived, `requires_memory_reclaim` —
  an honest Ollama statement, not a Deep Local claim.

Proof levels: small/medium models completed controlled quality-passing
tasks on their measured configurations; honest rejections are exercised
(embedding model, memory margins, unknown-geometry); the Colibrì
real-upstream/stub-engine proof is preserved and stub artifacts are
excluded from evidence by `engine_kind`. **Unproven reach:** any claim
beyond this machine's tier; the real Colibrì engine has still never
generated a token here; balanced/deep configurations are fit-classified
but speed-unmeasured.

## Maximum runtime-RPC latency

Declared worst-case cold ceiling: **15 s** (arithmetic: nvidia-smi 3.0
+ tcp 0.5 + Ollama version 2.5 + llama-server --version 3.0 + tags 2.5
+ detail budget 2.0 + 0.5 join slack = 14.0 s, +1 s margin). Cached
calls answer from the 60 s snapshot without probing. The
32-hanging-model test pins the detail phase.

## Tests run and exact results (post-review head)

| Command | Result |
|---|---|
| `python -m pytest python\tests` | **630 passed, 7 skipped** (skips = env-gated Colibrì E2E, as on base) |
| `npm run test:backend-status` | pass (`backend-status-tests-ok`) |
| `npm run test:progress` | pass (`chat-progress-tests-ok`) |
| `npm run test:readiness` | pass (readiness row mapping tests passed) |
| `npm run test:jobs-ui` | pass (99 assertions) |
| `npm run build:frontend` | pass (pre-existing >500 kB chunk warning) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | pass |
| `cargo test --manifest-path src-tauri\Cargo.toml` | pass (24 passed, 4 ignored) |
| `git diff --check` | clean |

Focused suites (post-review): inventory 22, harness/artifacts 45,
planner 39, RPC service 20 — **126 focused tests** (78 → 126), plus
the 109 preserved Deep Local/Colibrì tests.

## Privacy / no-egress status

Unchanged and strengthened: loopback-only; no downloads/telemetry/cloud;
artifact writer now rejects hostile path content on any drive, UNC, and
Unix roots, with containment proof at write time; fixed error
categories; no DB writes from the new layer; committed artifacts
re-verified clean.

## Limitations

1. One P3 machine; nothing validates P0–P2 tiers.
2. The flash-KV observation awaits a controlled re-run under its
   acceptance criteria before it can be more than exploratory.
3. Pre-fix artifacts under-count process RSS (sampler missed the
   runner exe; recorded as lower bounds; VRAM/residency unaffected).
4. True cold-disk loads unmeasured; GPU llama.cpp arm not run.
5. The runtime-overhead constant (600 MB) is a coarse measured class,
   not a per-configuration calculation; the tolerance tests bound it.
6. `runtime.benchmarks` is empty on production profiles; end users get
   `derived`-confidence plans until a local measurement story exists.

## Recommendation

**Research-only.** The corrected planner is now honest about what it
knows (measured vs derived vs conservative), memory-safe by
construction, and bounded in latency — but its positive speed claims
cover exactly one machine and one measured configuration. Production
integration remains gated on: (1) PR #33 merging and a rebase; (2) a
second machine reproducing the context-truncation and keep-alive
findings through the same harness; (3) the flash-KV acceptance criteria
before any optimization recommendation ships; (4) an `apply_plan`
surface, if ever, as its own reviewed phase.
