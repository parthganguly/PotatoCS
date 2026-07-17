# RFC: Local Runtime Acceleration and Model Reach

Status: Phase 0 deliverable of the local-runtime-acceleration track
(`feat/local-runtime-acceleration`, stacked on `feat/deep-local-fable`
at `8ab22318`, PR #33 head). Research/engineering only: nothing in this
RFC changes `main`, the installer, release assets, the v0.4 acceptance
gate, or any default setting.

Author: Claude Code (Fable), 2026-07-17.

Product objective: help ordinary computers run more capable local models
than they normally could, and make models that already fit run faster and
more efficiently — with every claim backed by a measurement on real
hardware and every recommendation carrying a safe fallback.

---

## 1. Current runtime architecture and provider boundaries

Ground truth at base `8ab22318` (all file references verified this
session):

```
React/Tauri UI ── JSON-RPC stdio ──► rpc_server.py (single-threaded dispatch)
   │                                      │
   │                                      ├─ ModelService (facade)
   │                                      │    └─ providers/ollama.py — urllib HTTP
   │                                      │       to http://127.0.0.1:11434
   │                                      │       (chat, vision, detect, ps, show)
   │                                      ├─ DeepLocalJobService / DeepLocalService
   │                                      │    └─ providers/colibri.py — loopback
   │                                      │       OpenAI-compatible client + coli
   │                                      │       plan/doctor subprocess wrapper
   │                                      ├─ EmbeddingService (Ollama embeddings)
   │                                      └─ Eval/Campaign/OCR/Vision services
   │                                         (all Ollama via the same facade)
```

Boundary facts that constrain this track:

1. **The sidecar RPC loop is single-threaded** (`rpc_server.py` dispatch).
   Anything slow must either be fast enough to be interactive, or become a
   persisted job (the Deep Local rule). The planner (Phase 3) is pure
   computation over cached inventory and must stay sub-second.
2. **Interactive chat passes almost no tuning knobs.** The chat pipeline
   sends only `temperature` and `num_predict` in `options`
   (`chat_service.py`, `eval_service.py:options_for_generation`). It never
   sets `num_ctx`, `num_thread`, `num_gpu`, `num_batch`, or `keep_alive`;
   every memory/speed decision is currently delegated to Ollama's own
   defaults. This is the entire acceleration surface for Goal B — and it
   means any future "apply plan" step has a naturally narrow diff.
3. **`ModelService`'s public surface is frozen** by contract tests and by
   `EvalModelService` subclassing (see `COLIBRI_PROVIDER_RFC.md` §2–3).
   New inventory/planner code must be a sibling consumer of the provider
   layer, not a rewrite of the facade.
4. **Provider identity rules are settled**: bare model strings mean
   Ollama forever; Colibrì is reachable only through the Deep Local
   persisted-job surface; no cloud, no telemetry, loopback only.
5. **Three execution classes already exist implicitly**: interactive chat
   (Ollama, 120 s timeout), heavy jobs (imports/OCR), and Deep Local
   persisted jobs (Colibrì). This track makes the classification
   *measured and explicit* instead of implicit and binary
   ("Ollama or nothing").

## 2. Candidate runtimes (exact versions audited this session)

| Runtime | Version audited | Why in scope | Test path |
|---|---|---|---|
| **Ollama** | 0.31.1 (installed at `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`; server probed live at `/api/version`) | The shipping interactive runtime; every user has it; exposes real tuning surface (below) | Full benchmark matrix on installed models |
| **llama.cpp `llama-server`/`llama-bench`** | upstream release **b10064** (Windows x64 CPU + CUDA 12.4 zip assets published) | The engine underneath Ollama, exposed directly: per-request control of threads/offload/batch/flash-attention/KV-cache/speculative decoding; the natural "same model, second runtime" comparison because **Ollama blobs are raw GGUF files** (verified: `application/vnd.ollama.image.model` blob begins with `GGUF` magic) | Dev-machine benchmark only in this phase; obtained as an exact upstream release zip, recorded by tag+SHA256, never vendored, never auto-downloaded by the product |
| **Colibrì** | `54cfe563` (audited in PR #33; contracts re-verified there) | The Deep Local class already exists on this branch; reach classification must include the persisted-job tier honestly | Existing real-upstream + stub-engine E2E preserved; no new Colibrì work in this track |
| **MLX** | not audited | Apple-Silicon only; this machine is Windows/x64 | **Documented future adapter only.** No code, no detection stub pretending to detect it |
| **NPU runtimes** (ONNX Runtime QNN/DirectML, etc.) | not audited | Ryzen 5 4600H has no NPU; no real local proof is possible | **Not implemented at all** — not even a detection placeholder. The inventory schema carries an `npu` field fixed to `"none_detected"` on this hardware; claiming more would be fiction |

Deliberately rejected: a generic provider-registry abstraction spanning
these (same reasoning as `COLIBRI_PROVIDER_RFC.md` §3 — one shipping
interactive runtime; a registry with one real member is decoration).
The reusable seam stays what it already is: normalized result types, the
error taxonomy, the loopback HTTP client, and the bounded-subprocess
wrapper pattern.

### Ollama 0.31.1 actual tuning surface (from the binary's own help and live probes)

Server-level (environment variables at `ollama serve` start — benchmarking
them requires a controlled server restart, which the harness does and the
product never does):

- `OLLAMA_KEEP_ALIVE` (default `5m`), `OLLAMA_CONTEXT_LENGTH` (default
  4k/32k/256k chosen by VRAM), `OLLAMA_FLASH_ATTENTION`,
  `OLLAMA_KV_CACHE_TYPE` (default `f16`), `OLLAMA_NUM_PARALLEL`,
  `OLLAMA_MAX_LOADED_MODELS`, `OLLAMA_GPU_OVERHEAD`,
  `OLLAMA_IGPU_ENABLE`, `LLAMA_ARG_FIT` / `LLAMA_ARG_FIT_TARGET`
  (llama.cpp automatic memory fit), `OLLAMA_LOAD_TIMEOUT`,
  `OLLAMA_NO_CLOUD` (0.31.x has cloud features; **the harness sets
  `OLLAMA_NO_CLOUD=1` for every controlled server it starts** and the
  product continues to never use them).

Per-request (`options` on `/api/chat`, verified accepted on a live
completion): `num_ctx` (confirmed honored via `/api/ps` reporting
`context_length: 512`), `num_predict`, `num_thread`, `num_gpu` (offload
layer count), `num_batch`, `temperature`, plus request-level `keep_alive`.
Response exposes `load_duration`, `prompt_eval_count/duration`,
`eval_count/duration` (ns), enabling load/prompt/generation split without
any external instrumentation; `/api/ps` exposes `size`, `size_vram`, and
`expires_at` for offload-fraction and keep-alive verification.

## 3. Hardware inventory schema

Read-only, local-only, computed on demand; never persisted to the DB in
this track; never logged with raw paths. Shell-free via `ctypes`
(`GlobalMemoryStatusEx`, `GetNativeSystemInfo`,
`IsProcessorFeaturePresent`) and `os`/`shutil` where possible; the only
subprocess is `nvidia-smi --query-gpu` (bounded timeout, absent = no
NVIDIA GPU reported, never an error).

```jsonc
{
  "schema_version": 1,
  "os": {"name": "Windows", "version": "10.0.26200", "arch": "AMD64"},
  "cpu": {
    "logical_threads": 12,
    "physical_cores": 6,          // logical/2 heuristic flagged via "physical_cores_source"
    "physical_cores_source": "measured|smt_heuristic",
    "isa": {"avx": true, "avx2": true, "avx512f": false}  // IsProcessorFeaturePresent
  },
  "ram": {"total_bytes": 0, "available_bytes": 0},
  "gpus": [{
    "vendor": "nvidia", "name": "GeForce RTX 3050 Laptop GPU",
    "vram_total_bytes": 0, "vram_free_bytes": 0,
    "source": "nvidia-smi"        // the only GPU probe in scope; others report []
  }],
  "npu": "none_detected",
  "storage": {
    "profile_disk_free_bytes": 0,
    "model_store_disk_free_bytes": 0,
    "kind": "unknown"             // SSD/HDD detection is NOT reliably available shell-free; stays "unknown" rather than guessing
  },
  "captured_at_ms": 0
}
```

Fixed error categories for the inventory layer: `probe_timeout`,
`probe_unavailable`, `probe_failed` — never raw exception text, never a
path. A probe failure yields a partial inventory with the failed section
marked, not an RPC error.

## 4. Installed-model inventory schema

Sources, in order of trust: Ollama `/api/tags` + `/api/show` (already
wrapped by `ModelService`), the on-disk manifest/blob store for byte-true
sizes (read-only globbing of `~/.ollama/models`, exposed only as counts
and bytes — **paths never leave the module**), and the existing
`model_capabilities` cache. No new DB writes.

```jsonc
{
  "schema_version": 1,
  "runtime": "ollama",
  "models": [{
    "tag": "llama3.2:1b",
    "digest": "sha256:…",
    "format": "gguf",
    "family": "llama",
    "parameter_size": "1.2B",
    "quantization": "Q8_0",
    "disk_bytes": 1361754193,
    "context_length_native": 131072,
    "capabilities": ["completion", "tools"],
    "role": "chat|vision|embedding|unknown"   // reuses model_role()
  }],
  "captured_at_ms": 0
}
```

## 5. Benchmark methodology

- **Harness location**: `python/odysseus_desktop_backend/runtime_bench/`
  (importable, unit-testable) plus a dev entry point
  `python -m odysseus_desktop_backend.runtime_bench`. It is a development
  tool: the product never launches it, and it is excluded from any
  installer packaging by virtue of never being wired into the app.
- **Stdlib only** (repo convention: `urllib`, `ctypes`, `subprocess`
  with argv arrays and hard timeouts). No psutil, no requests.
- **Timing sources**: Ollama's own ns-precision response fields for
  load/prompt/generation splits; harness wall clocks for cold/warm load
  and end-to-end; streaming (`stream: true`) for time-to-first-token
  measured at the first content token chunk (reasoning/keepalive chunks
  excluded).
- **Memory sources**: peak working set of the runtime process tree
  sampled every ~250 ms via ctypes (`EnumProcesses` →
  `QueryFullProcessImageNameW` filter on the runtime executable →
  `GetProcessMemoryInfo`); system RAM pressure via `GlobalMemoryStatusEx`;
  VRAM via `nvidia-smi --query-gpu=memory.used` sampling where present.
  Sampler resolution is recorded in the artifact; peaks are lower bounds.
- **Cold vs warm**: "cold model load" = first load after a controlled
  `ollama stop <model>` (and for server-level experiments, a fresh
  `ollama serve` with a fixed env); "warm" = repeat with the model
  resident (`/api/ps` verified before the run). OS file-cache state
  cannot be fully controlled without reboot; runs record a
  `file_cache_state: "unknown_warmish"` honesty field instead of claiming
  a true cold-disk measurement.
- **Repetition**: n ≥ 3 per cell for timing medians (min/median/max all
  recorded); n = 1 acceptable only for multi-minute overcommit/failure
  cases, marked as such.
- **Controlled server**: experiments that need env vars start their own
  `ollama serve` on the standard port after checking it is free
  (`OLLAMA_NO_CLOUD=1` always set), and shut it down afterwards; the
  harness refuses to kill a server it did not start.

### Benchmark shapes (fixed fixture set, deterministic where possible)

| # | Shape | Fixture |
|---|---|---|
| 1 | tiny prompt / short answer | fixed 1-sentence prompt, `num_predict` 32 |
| 2 | medium prompt / medium answer | fixed ~600-token synthetic brief, `num_predict` 256 |
| 3 | long-context retrieval-style | fixed synthetic ~4k-token document with a planted fact + question, `num_predict` 64 |
| 4 | repeat prompt (cache/warm path) | shape 2 repeated back-to-back, same session |
| 5 | PotatoCS source-grounded task | the grounded-answer prompt template over a fixed synthetic evidence block (mirrors `chat_service` grounding shape), `num_predict` 256 |
| 6 | failure/overcommit | a configuration the planner predicts must not fit (e.g. `num_ctx` far beyond RAM-safe on qwen3:8b, or `num_gpu` forcing full offload of an >VRAM model); expected outcome is a recorded error or measured pathological slowdown, reported as data |

Sampling is greedy-deterministic (`temperature 0`, fixed `seed` where the
runtime honors it) so the quality sanity check is stable. The quality
sanity check is fixed per shape: shape 1/2 exact-substring assertions on
required tokens; shape 3/5 planted-fact presence; a run whose output
fails the check is recorded `quality_check: "failed"` and can never
support an optimization claim.

### Artifact schema (machine-readable)

One JSON file per run batch under
`projects/odysseus/benchmarks/local-runtime/`, summarized in
`projects/odysseus/LOCAL_RUNTIME_BENCHMARKS.md`:

```jsonc
{
  "schema_version": 1,
  "batch_id": "…", "captured_at": "…",
  "hardware": { /* §3 snapshot, username-free */ },
  "runtime": {"name": "ollama", "version": "0.31.1",
               "server_env": {"OLLAMA_FLASH_ATTENTION": "1"}},   // only allow-listed keys
  "model": {"tag": "…", "format": "gguf", "quantization": "…",
             "parameter_size": "…", "disk_bytes": 0},
  "shape": "tiny|medium|long_context|repeat|grounded|overcommit",
  "mode": "interactive|persisted_job_sim",
  "engine_kind": "real|stub",       // stub = no real token generation (Colibrì stub tier)
  "runs": [{
    "run_index": 0, "cold": true,
    "options": {"num_ctx": 4096, "num_thread": 6},
    "timings_ms": {"total": 0, "load": 0, "prompt_eval": 0,
                    "generation": 0, "first_token": 0},
    "tokens": {"prompt": 0, "generated": 0,
                "prompt_tps": 0.0, "generation_tps": 0.0},
    "memory": {"runtime_peak_rss_bytes": 0, "system_min_available_bytes": 0,
                "vram_peak_used_bytes": 0, "sampler_interval_ms": 250},
    "quality_check": "passed|failed|not_applicable",
    "error_category": ""            // non-empty = failed run, kept as data
  }]
}
```

Privacy rules (tested): artifacts and the summary contain **no
usernames, no absolute paths, no prompts, no model outputs** (the quality
check runs in-process and only its verdict is stored); a redaction pass
asserts the profile username and home directory string are absent from
every artifact byte before it is written; benchmark failures are recorded
with fixed categories. Prompts live in the harness source (synthetic
fixtures), which is the repo itself — that is the only place they exist.

## 6. Execution-plan schema

```jsonc
{
  "plan_version": 1,
  "objective": "fast|balanced|deep",
  "runtime": {"name": "ollama", "version": "0.31.1"},
  "model": {"tag": "…", "quantization": "…", "disk_bytes": 0},
  "execution_class": "interactive|slow_interactive|persisted_job",
  "options": {                       // ONLY flags the capability matrix marks supported
    "num_ctx": 4096, "num_thread": 6, "num_gpu": null,   // null = runtime default
    "num_batch": null, "keep_alive": "5m"
  },
  "server_env": {},                  // recommendations only; the planner never applies them
  "estimates": {
    "ram_bytes": 0, "vram_bytes": 0, "disk_working_set_bytes": 0,
    "ttft_ms": null, "generation_tps": null
  },
  "confidence": "measured|derived|conservative_default",
  "warnings": ["…fixed codes…"],
  "rejected_alternatives": [{"model": "…", "reason_code": "exceeds_ram_margin", "detail_numbers": {}}],
  "evidence": {"benchmark_batch_ids": ["…"], "stale": false}
}
```

Fail-safe rules (each carries a test in Phase 3):

- estimated total memory = weights + KV cache estimate (context-dependent,
  cache-type-dependent) + measured runtime overhead; a plan is rejected
  when estimated RAM exceeds `available_ram − max(1.5 GB, 12 %)` or
  estimated VRAM exceeds `vram_free − 256 MB`; margins are constants with
  names, not magic inline numbers;
- unsupported flags for the selected runtime/version are omitted, never
  guessed;
- benchmark evidence older than 30 days or from different hardware
  (mismatched §3 snapshot) demotes confidence `measured → derived`;
  absent evidence yields `conservative_default` with Ollama defaults;
- unknown hardware fields (no GPU probe, unknown storage) always shrink
  the allowed envelope, never grow it;
- `persisted_job` classification can never be routed to `chat.send`
  (regression test), mirroring the Deep Local rule;
- the planner is **pure**: same inputs → same plan (deterministic
  ordering of alternatives), no I/O beyond its inputs, no settings writes.

## 7. Runtime capability matrix

Maintained as data (`runtime_bench/capabilities.py`), keyed by runtime
name + version-range, values `supported | unsupported | unknown`, each
entry citing its evidence (`binary_help`, `live_probe`, `measured`):

| Capability | Ollama 0.31.1 | llama-server b10064 | Colibrì `54cfe563` |
|---|---|---|---|
| per-request context | ✔ `num_ctx` (probed) | ✔ `n_ctx`/server slots | server-side |
| per-request threads | ✔ `num_thread` (accepted; effect measured in Phase 4) | ✔ `--threads` | ✘ |
| GPU offload control | ✔ `num_gpu` layers | ✔ `--n-gpu-layers` | CUDA build only |
| batch size | ✔ `num_batch` | ✔ `--batch-size`/`--ubatch-size` | ✘ |
| flash attention | server env `OLLAMA_FLASH_ATTENTION` | ✔ `--flash-attn` per server | n/a |
| KV cache quantization | server env `OLLAMA_KV_CACHE_TYPE` | ✔ `--cache-type-k/v` | n/a |
| keep-alive / residency | ✔ `keep_alive` + `/api/ps` verify | server lifetime | server lifetime |
| prompt cache reuse | implicit (measured via shape 4) | ✔ explicit slots/cache | ✘ |
| speculative decoding | ✘ not exposed in 0.31.1 API surface (no draft-model option) | ✔ `--model-draft` (requires a local draft model) | ✘ |
| streamed MoE expert exec | ✘ | ✘ | ✔ (the Deep Local tier) |
| honest failure on overcommit | measured in Phase 4 (shape 6) | measured | doctor `unsafe` |

Claims marked "measured in Phase 4" stay `unknown` in the shipped matrix
until the measurement exists.

## 8. Model fit / reach classification

Classes, from fastest to furthest reach:

1. `fits_gpu_full` — weights + KV fit in free VRAM with margin;
2. `fits_gpu_partial` — partial offload; interactive if measured tok/s
   clears the interactive floor;
3. `fits_cpu_ram` — CPU-only within RAM margin;
4. `reachable_deep_local` — exceeds interactive envelopes but a
   persisted-job path exists (Colibrì-class, or measured
   pathological-but-completing configurations explicitly classed
   `persisted_job`);
5. `not_runnable` — exceeds even the deep envelope on this hardware;
   stated plainly.

Reach claims require proof: a class 1–3 claim needs a completed
controlled task (shape 1 minimum) on that exact configuration; class 4
needs either the Colibrì E2E tier (real upstream, stub engine —
labeled `engine_kind: stub`) or a real measured slow run; class 5 needs
the arithmetic shown. "The runtime started" is not reach; "completed the
fixed task with a passing quality check" is.

## 9. Interactive vs queued-job classification

Grounded in the v0.4 budget language (first sourced answer ≤ 30 s on
P3/P4, ≤ 60 s on P1/P2):

- `interactive`: measured TTFT ≤ 10 s warm **and** generation ≥ 5 tok/s
  on shape 2;
- `slow_interactive`: TTFT ≤ 60 s and ≥ 1 tok/s — usable with honest
  "this will be slow" copy;
- `persisted_job`: anything slower, anything requiring queue semantics,
  and everything Colibrì — routed only through job surfaces.

Thresholds are named constants recorded in every plan; they are product
copy boundaries, not physics, and are marked as provisional pending
P1/P2-tier measurements.

## 10. Privacy and no-egress rules

Inherited wholesale, none weakened: loopback-only endpoints; no
downloads or registry contact by product code (the harness's controlled
`ollama serve` gets `OLLAMA_NO_CLOUD=1`; the benchmark suite runs under
the pytest egress guard's loopback-only discipline); no telemetry; no
raw paths/usernames/prompts/outputs in logs, artifacts, RPC list
surfaces, or support data; API keys never persisted; fixed error
categories everywhere; benchmark artifacts pass a byte-level redaction
sentinel before write. The llama.cpp release zip used for dev
benchmarking is fetched manually by the operator, recorded by exact tag
and SHA256 in the benchmark report, stored outside the repo, and never
shipped, vendored, or auto-downloaded.

## 11. Fallback behavior

- Planner/inventory failures degrade to "Ollama with runtime defaults"
  — exactly today's behavior; the new layer can only ever add
  information, never remove the working path.
- Every shipped optimization rule has an explicit off-switch condition
  (e.g. flash-attention rule applies only when the capability matrix
  says supported *and* the measured evidence class matches; otherwise
  the option is simply not sent).
- A failed controlled-server start (port occupied, version mismatch)
  aborts the experiment, never the user's running server.

## 12. Non-goals

No auto-apply of plans (a future `apply_plan` is a separate reviewed
phase); no settings mutation from planning; no UI in this track until
backend contracts and evidence are complete (and then only the minimal
experimental panel described in the execution contract); no model or
runtime downloads by the product; no MLX/NPU implementation; no
Modelfile authoring/rewriting of the user's models; no changes to chat
routing, Deep Local UI, storage cleanup, or Potato Mode defaults; no
claim that P3 measurements validate P0–P2 tiers.

## 13. Benchmark acceptance thresholds

An optimization graduates from "measured" to "shipped rule" only if all
hold:

1. median improvement ≥ 10 % on the primary metric for ≥ 2 shapes
   (or ≥ 25 % on one shape for single-purpose rules like keep-alive vs
   cold TTFT), with run-to-run coefficient of variation < 20 % in both
   arms;
2. no quality-check failure introduced in any shape;
3. peak RSS and VRAM regress < 5 % unless the rule is explicitly a
   speed-for-memory trade documented in the rule itself;
4. a safe fallback exists (rule not applied → today's behavior);
5. results reproduced in at least two separate batches (different
   process lifetimes).

Neutral and negative results are recorded in
`LOCAL_RUNTIME_BENCHMARKS.md` with the same prominence as wins.

## 14. How recommendations avoid overfitting to one gaming laptop

- Rules are expressed as **ratios and guards**, not absolutes: "offload
  fully only when weights + KV ≤ free VRAM − margin", "threads =
  physical cores when physical ≥ 4, never logical threads" — each
  parameterized by the §3 inventory of the machine the plan is for.
- Every benchmark artifact embeds its hardware snapshot; evidence is
  only `measured`-grade for a matching snapshot, and degrades to
  `derived` (rule shape kept, numbers re-estimated conservatively) on
  any other machine.
- The P3 dev machine is a data point, not a universe: the result report
  and every rule carry a "validated on: P3 (RTX 3050 4 GB / 15.4 GB RAM /
  6C12T)" scope line, and the planner's `conservative_default` path is
  the behavior contract for unmeasured tiers.
- No shipped rule may encode a model-specific magic number without a
  guard that detects its precondition on the target machine.

## 15. Phase gates

| Phase | Deliverable | Gate |
|---|---|---|
| 0 | this RFC | committed |
| 1 | harness + artifact schema + schema tests | schema tests green; artifacts redaction-proven |
| 2 | inventory layer + tests | shell-free probes proven on Windows; fixed categories |
| 3 | planner + safety tests | all §6 fail-safe rules pinned by tests |
| 4 | measured optimization evidence | §13 thresholds applied; neutral results documented |
| 5 | reach classification proof | §8 proof levels satisfied incl. one honest rejection |
| 6 | read-only RPCs (+ optional minimal UI) | full standard matrix green; IPC fixtures updated |
| final | `LOCAL_RUNTIME_ACCELERATION_RESULT.md` + draft PR | stacked PR targets `feat/deep-local-fable`; no merge before independent review |
