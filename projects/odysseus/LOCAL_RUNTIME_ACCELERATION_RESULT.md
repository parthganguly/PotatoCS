# Local Runtime Acceleration — Fable Implementation Result

Status: research/engineering track complete on
`feat/local-runtime-acceleration` (stacked on `feat/deep-local-fable`).
Backend-only; no UI, no settings mutation, no chat-routing change, no
change to `main`, the installer, release assets, or the v0.4 gate.

Date: 2026-07-17/18. Author: Claude Code (Fable).

## Commits

- **Base:** `8ab22318` — tip of `feat/deep-local-fable` = PR #33 head
  (verified unmoved against GitHub at session start and before push).
- **Build commits:** `4e44fe59` RFC → `a43afe18` inventory →
  `0f667d0c` benchmark harness → `546af85e` planner → `61f57831`
  `runtime.*` RPCs → `a65d54a6` measured evidence (31 artifacts) →
  `d7fcfe10` this report (amended to record its own hash; the PR head
  is the amended commit).
- **Upstream runtimes audited/measured:**
  - Ollama **0.31.1** (user's installation, probed live: binary help,
    `/api/version`, `/api/chat` option acceptance, `/api/ps` residency).
  - llama.cpp release **b10064** (`86d86ed43`), official Windows x64
    CPU zip, SHA256 `C9B770B5…099E` (CUDA 12.4 zip archived,
    `D3DF8C73…34B` — unused: its cudart DLL dependency was not
    installed and GPU arms were out of scope for the comparison).
    Binaries stay outside the repo; nothing is vendored or shipped.
  - Colibrì proof from PR #33 preserved untouched (109 focused tests
    re-run green on this branch, upstream `54cfe563`).

## Hardware tested

One machine, tier P3: AMD Ryzen 5 4600H (6C/12T, AVX2, no AVX-512),
15.4 GB RAM, NVIDIA RTX 3050 Laptop 4 GB (driver 596.36), Windows 11
Home 10.0.26200, NVMe-class SSD (kind reported "unknown" by design —
no reliable shell-free probe). Every artifact embeds its own hardware
snapshot including free VRAM at capture, which proved essential (§
limitations).

## Models and quantizations measured

| Model | Params | Quant | Disk | Placement on this machine |
|---|---|---|---|---|
| llama3.2:1b | 1.2B | Q8_0 | 1.3 GB | 100 % VRAM (when free) |
| llama3.2:latest | 3.2B | Q4_K_M | 2.0 GB | 100 % VRAM ↔ 20 % under ambient pressure |
| qwen3:8b | 8.2B | Q4_K_M | 5.2 GB | 39 % VRAM / 61 % CPU |
| same 1b GGUF blob via llama.cpp b10064 | 1.2B | Q8_0 | — | CPU only (comparison arm) |

Also inventoried (not benchmarked): nemotron-nano-chat:8b,
nomic-embed-text, qwen3-vl:2b.

## What was built

1. **RFC** — `LOCAL_RUNTIME_ACCELERATION_RFC.md`: architecture audit,
   candidate-runtime matrix at exact versions, schemas, methodology,
   fail-safe rules, acceptance thresholds, anti-overfitting rules.
2. **Benchmark harness** — `python/odysseus_desktop_backend/runtime_bench/`
   (dev tool, never launched by the product): six deterministic shapes
   with in-process quality checks (outputs never persisted, only
   verdicts); streaming Ollama and llama.cpp clients (TTFT at first
   content token; ns-precision load/prompt/generation splits from the
   runtime's own counters); ctypes process-tree RSS + system RAM +
   nvidia-smi VRAM sampler; schema-validated artifacts with a
   byte-level redaction sentinel (write refused on violation) and an
   allow-listed `server_env` record; failed runs recorded as data.
3. **Inventory layer** — `services/runtime_inventory.py`: shell-free
   ctypes probes (ISA, physical cores, RAM), bounded subprocesses
   (nvidia-smi, `llama-server --version`), Ollama detection and model
   inventory over loopback; fixed error categories
   (`probe_timeout|probe_unavailable|probe_failed`); no paths or
   usernames in any output (tested); read-only, no DB writes, no
   downloads. NPU: reported `none_detected` — no fake detection stub.
4. **Planner** — `services/runtime_planner.py`: pure, deterministic;
   memory model (weights + context-dependent KV estimate + measured
   runtime overhead) against named margins (RAM: available − max(1.5 GB,
   12 %); VRAM: free − 256 MB); fit classes `fits_gpu_full |
   fits_gpu_partial | fits_cpu_ram | reachable_deep_local |
   not_runnable`; execution classes `interactive | slow_interactive |
   persisted_job` with provisional thresholds recorded in every plan;
   stale or hardware-mismatched evidence demotes confidence
   `measured → derived`; unknown hardware → `conservative_default`;
   unproven flags omitted; rejected alternatives carry reason codes and
   numbers.
5. **RPCs** — `runtime.inventory`, `runtime.benchmarks`,
   `runtime.plan {objective}`, `runtime.recommendations` via
   `RuntimePlanService`: read-only (no mutating surface, pinned by
   test), stub-engine artifacts excluded from planner evidence, plans
   labeled "estimates, not guarantees". IPC golden fixture updated on
   the python side only; nothing frontend-facing.

## Measured results (full detail in LOCAL_RUNTIME_BENCHMARKS.md; raw JSON in benchmarks/local-runtime/)

**Wins (shipped as bounded rules/recommendations):**

| Finding | Size | Rule shipped |
|---|---|---|
| Dropping keep-alive costs 6.2–8.7 s per turn vs 0.4 s warm (3b) | **13–18×** | never recommend keep_alive 0 for interactive use |
| Default 4096 context silently truncated a 5,138-token document (2,050 evaluated); planted-fact retrieval failed 0/12 across three models; `num_ctx` 8192 → 4/4 pass | **correctness** | size context to evidence length within memory margins |
| Flash attention + q8_0 KV cache under VRAM pressure, paired same-ambient arms: 10.1 → 23.5 tok/s, mechanism visible (20 % → 100 % of layers placed) | **2.3×** | conditional recommendation only when partial offload is predicted; neutral cases make no claim |
| Prompt-cache reuse (existing Ollama behavior): repeat prompt eval 880 ms → 12 ms | ~73× (prompt phase) | keep conversation prefixes stable; no product change |

**Neutral / inconclusive / negative (documented with the same prominence):**

- Thread count on the CPU-bound 8b: neutral (memory-bandwidth-bound);
  no rule.
- Manual GPU offload of a minority of layers: +6–13 % only; runtime
  placement left alone. Changing `num_gpu`/options triggers a hidden
  model reload on the next request (measured 20–72 s) — a reason the
  planner recommends rather than flips options mid-conversation.
- `num_batch`: inconclusive (ambient VRAM drift confound); no rule.
- llama.cpp b10064 vs Ollama 0.31.1, same GGUF blob, CPU-only: parity
  within ±10 %; no "bypass Ollama" rule.
- Overcommit (all layers of a 5.9 GB model forced onto the 4 GB card):
  runs via driver spillover but crushes system RAM to 0.7 GB free with
  20 s TTFT — recorded as the honest failure case the planner's margin
  rule forbids.
- Speculative decoding: not exposed by Ollama 0.31.1; llama.cpp path
  requires a local draft model that is not installed — not measured, not
  claimed, capability matrix says so.

## Model-reach results (Phase 5)

Live `runtime.recommendations` against the real machine inventory and
real benchmark evidence (captured mid-session under RAM pressure,
available RAM ≈ 3.4 GB — snapshot recorded verbatim):

- **fast** → llama3.2:1b, `fits_gpu_full`, `interactive`, confidence
  **measured** (TTFT 292 ms, 103 tok/s from batch evidence); qwen3:8b
  and nemotron-nano-chat:8b rejected `exceeds_ram_margin` with numbers;
  nomic-embed-text rejected `not_a_text_generation_model`.
- **balanced** → llama3.2:latest (3b), `fits_gpu_full`, `interactive`,
  confidence measured (TTFT 383 ms, 44 tok/s).
- **deep** → qwen3:8b, `reachable_deep_local`, `slow_interactive`,
  confidence measured (4.6 tok/s, `expect_slow_generation` warning) —
  proven by real completed quality-passing runs, not inference.

Proof levels satisfied: small model (1b) and medium models (3b, 8b)
completed controlled tasks with passing quality checks on the exact
configurations; the honest-rejection requirement is met twice
(planner rejections with reason codes, and the measured overcommit
pathology); the Colibrì real-upstream/stub-engine tier is preserved
(109 tests green; stub artifacts are excluded from planner evidence by
`engine_kind`); no giant model was downloaded and the real Colibrì
engine remains unmeasured, exactly as before.

## Tests run and exact results (final head)

| Command | Result |
|---|---|
| `python -m pytest python\tests` | **582 passed, 7 skipped** (skips = env-gated Colibrì E2E module, as on base) |
| `npm run test:backend-status` | pass (`backend-status-tests-ok`) |
| `npm run test:progress` | pass (`chat-progress-tests-ok`) |
| `npm run test:readiness` | pass (readiness row mapping tests passed) |
| `npm run test:jobs-ui` | pass (99 assertions) |
| `npm run build:frontend` | pass (pre-existing >500 kB chunk warning) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | pass |
| `cargo test --manifest-path src-tauri\Cargo.toml` | pass (24 passed, 4 ignored) |
| `git diff --check` | clean |

New focused suites: inventory 18, harness/artifacts 23, planner 25,
RPC service 12 — **78 new tests**, plus the 109 preserved Deep
Local/Colibrì tests re-verified. `src/` is untouched relative to the
base (no frontend change of any kind).

## Privacy / no-egress status

- All probes and benchmark traffic are loopback-only; the suite runs
  under the existing pytest egress guard; controlled benchmark servers
  get `OLLAMA_NO_CLOUD=1`.
- No downloads, installs, or registry contact by any shipped code. The
  llama.cpp release zips were fetched manually by the operator for
  dev-machine benchmarking only, recorded by tag + SHA256, stored
  outside the repo.
- Artifacts carry no usernames, home paths, prompts, or model outputs:
  enforced at write time (write refused on violation), re-swept across
  all 31 committed artifacts, and pinned by tests.
- Inventory/planner logs contain counts, categories, and versions only;
  fixed error categories throughout; no new settings keys, no DB
  writes, no telemetry.

## Limitations

1. **One P3 machine.** All rules are ratios/guards parameterized by the
   inventory; unmeasured hardware degrades to `derived`/
   `conservative_default` confidence. Nothing validates P0–P2.
2. **Ambient VRAM drift** from desktop apps confounded two experiment
   families until caught via per-artifact VRAM snapshots; only the
   paired same-ambient design supports the flash-KV claim. Single-batch
   cross-comparisons on shared machines are untrustworthy — the
   methodology now says so.
3. **RSS under-counts in pre-fix artifacts** (sampler missed Ollama's
   `llama-server.exe` runner; fixed, limitation recorded; VRAM and
   residency figures unaffected).
4. **True cold-disk loads unmeasured** (`file_cache_state:
   unknown_warmish`); a reboot-per-run protocol was out of scope.
5. **GPU llama.cpp arm not run** (cudart dependency); the CPU parity
   result does not cover CUDA-vs-CUDA.
6. The planner's KV-cache estimate is a conservative heuristic, not a
   per-architecture calculation; it over-reserves on GQA models.
7. `runtime.benchmarks` returns empty on production profiles (evidence
   ships with the repo only for dev machines); plans there run at
   `derived` confidence — by design, but it means end users get
   heuristic numbers until a local measurement story exists.

## Recommendation

**Research-only for now; prepare for production integration behind two
gates.** The evidence supports (a) the context-sizing correctness rule
and (b) the conditional flash-attention/q8_0-KV recommendation as the
first production candidates, but both should ship only after: (1) PR
#33 merges and this branch rebases onto its final form; (2) a second
machine (ideally P1/P2-tier) reproduces the paired flash-KV result and
the context-truncation fix through the same harness. A future
`runtime.apply_plan` (mutating) remains a separate reviewed phase; the
minimal experimental UI described in the brief was deliberately not
built (backend contracts first — the same scope reduction the PR #33
review demanded of Deep Local).
