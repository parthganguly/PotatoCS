# Hardware-Relative Model Uplift Strategy

Status: PROPOSED — architecture/research strategy (Fable, 2026-07-19).
No code changed. No models downloaded. No branches or PRs created.
PRs #33 / #35 / #36 are inputs and are not modified by this document.

Authority notes:
- Every number in this document is labeled **measured** (P3 evidence in
  `projects/odysseus/benchmarks/local-runtime/` or `V04_HARDWARE_AUDIT.md`),
  **derived** (computed from measured inputs by a stated formula), or
  **proposed** (requires measurement or human approval before use).
- Nothing here locks a Potato Mode default, promotes a
  `measured_exploratory` finding to a shipped rule, or declares any gate
  GREEN. Those remain human/Fable decisions per
  `V04_BATON_FOR_SMALLER_MODELS.md` §G.

---

## 1. Corrected product goal

PotatoCS is a **hardware-relative local-model acceleration and access
system**. It is not a 12B-model product, not a GLM application, and not
a Colibrì application.

The goal, restated per machine:

> Given a particular consumer computer, make the models that already fit
> run measurably better, and make the next one or two model classes above
> its normal comfort zone practically accessible — with every claim backed
> by evidence gathered on that class of hardware, and with honest
> "unsupported" boundaries where the compromises stop being acceptable.

On the reference machine (P3), the concrete instantiation is:

| Rung | Reference-machine example | Target state |
| --- | --- | --- |
| Comfortable baseline | llama3.2:1b, llama3.2:3b | faster, longer safe context |
| Borderline | ~4B dense at long context | comfortable |
| Accessible uplift | 7B–9B dense (qwen3:8b) | usable with explicit compromises |
| Deep Local ceiling | large sparse MoE | persisted jobs only |
| Unsupported | forced overcommit (e.g. `num_gpu 99` on qwen3:8b) | rejected, with a stated reason |

These rungs are **relative**: on a 4 GB-RAM P0 machine the same ladder
might read 1B / 2B / 3B / nothing / everything-else, and on a 32 GB
desktop it might read 8B / 14B / 30B-A3B / 70B / beyond. PotatoCS never
hardcodes the rungs; it computes them from inventory + evidence
(planner v2 already does the fit half of this on the PR #35 branch).

What "not a Colibrì application" means operationally: Colibrì is one
experimental backend behind the Deep Local seam (PR #33), relevant only
to sparse-MoE expert streaming. Dense-model uplift is delivered by
conventional runtime technique (quantization, placement, KV policy,
context control, caching) on maintained runtimes — Ollama and llama.cpp
today. See §4 and the conclusion.

---

## 2. Hardware-relative baseline model

### 2.1 What "comfortable baseline" means

A model configuration is part of a machine's **comfortable baseline**
when all of the following hold (RQ1):

1. **Fit with headroom** — planner v2 fit class is `fits_gpu_full`,
   `fits_gpu_partial`, or `fits_cpu_ram` under the existing safety
   budget: RAM budget = available − max(1.5 GiB, 12 % of total); VRAM
   budget = free − 256 MiB; total = weights + KV(context) + 600 MiB
   runtime overhead (constants measured/derived on the PR #35 branch,
   `services/runtime_planner.py`).
2. **Interactive class, measured** — warm TTFT and generation rate meet
   the machine's `interactive` calibration (P3 provisional: TTFT ≤ 10 s
   and ≥ 5 tok/s on the `medium` shape) with `confidence=measured`
   evidence, not an estimate.
3. **No interference** — during generation, system available RAM never
   drops below the safety floor, and the app remains responsive
   (measured via the harness `ResourceSampler`, extended per §8).
4. **Correct at its advertised context** — the model passes the
   `long_context` quality check at the context the plan advertises.
   Measured lesson: default 4096 ctx silently truncated a 5,138-token
   document and scored 0/12 until `num_ctx 8192` (then 4/4). A config
   that truncates is not "comfortable", whatever its tok/s.
5. **Predictable lifecycle** — cold start bounded and reported,
   cancellation prompt, restart recovery honest (PR #33 semantics).

The baseline is therefore a **set of (model, quant, context, placement)
configurations**, not a parameter count. Parameter count appears only
inside the fit estimate.

### 2.2 How the baseline is computed per machine

Already implemented on the PR #35 branch and reused as-is:

- `runtime_inventory.py` — RAM/VRAM/ISA/core topology, runtime health,
  per-model KV geometry from `/api/show` (layers × kv_heads ×
  key/value length).
- `runtime_planner.py` — deterministic fit classification with the
  constants above; refuses fit claims when KV geometry or weight size
  is unknown (`unknown_kv_geometry`, `unknown_weight_size`).
- `runtime_bench` — six deterministic shapes with greedy sampling and
  closed artifact schema; execution class assigned only from compatible
  measured evidence (full fingerprint match), else
  `performance_unknown`.

The uplift strategy adds one concept on top: the **baseline vector**
for each comfortable configuration (see §3.1), which is what
"improvement" is measured against.

### 2.3 Reference machine baseline (measured, P3, Ollama 0.31.1)

- llama3.2:1b Q8_0 (1.3 GB): 100 % VRAM, medium 103–104 tok/s, warm
  TTFT ~0.3 s → comfortable, `fits_gpu_full`, `interactive`, measured.
- llama3.2:3b Q4_K_M (2.0 GB): 100 % VRAM, medium 41–45 tok/s, cold
  ~10 s → comfortable at 4096; **unproven at 8192** (no compatible
  measured evidence yet — the live "balanced" plan is honest about
  this and reports `performance_unknown`).
- qwen3:8b Q4_K_M (5.2 GB): 39 % VRAM / 61 % CPU, medium ~4.6 tok/s,
  cold 28–41 s → the boundary case: just under the 5 tok/s interactive
  floor. This is the machine's natural **uplift target**.
- Forced full offload of qwen3:8b (`num_gpu 99`): shared-memory spill,
  0.7 GB free system RAM, 20 s TTFT → **unsupported**; the planner's
  VRAM margin rule exists to forbid exactly this.

---

## 3. Definition of uplift

### 3.1 The baseline vector

For each (model, quant, context, placement, runtime+version)
configuration, record under identical shapes/sampling:

```
B = { ttft_warm_ms, ttft_cold_ms, prompt_tps, gen_tps,
      max_verified_context, peak_ram_bytes, peak_vram_bytes,
      min_system_available_ram_bytes, cancel_latency_ms,
      quality_pass_vector (per shape) }
```

### 3.2 Claim A — "existing model runs better" (RQ2, RQ10)

An optimization claim is valid only if, comparing candidate C against
baseline B with **identical** model file (digest), prompts, chat
template, tokenizer, context, sampling (greedy, seed 42), and
`num_predict`:

- at least one of: `ttft_warm` −30 % or more, `gen_tps` +25 % or more,
  `prompt_tps` +25 % or more, `max_verified_context` strictly larger
  with quality pass, peak RAM/VRAM −20 % or more at equal speed
  (**proposed** materiality thresholds — approve or adjust before
  Stage B);
- and no protected dimension regresses: quality pass vector ≥ baseline
  on every shape, generated token count within ±10 % on open-ended
  shapes, context not reduced, `min_system_available_ram` never below
  the safety floor, cancel latency not worse than 2× baseline.

This is the anti-self-deception rule (RQ10): an "uplift" produced by
shorter outputs, a truncated context, or degraded answers is an
**invalid comparison**, not a small uplift. The harness enforces it
structurally: fixed `num_predict`, planted-codeword and
grounded-fact quality checks, and prompt_eval_count recorded so silent
truncation is visible (this exact mechanism already caught the 4096-ctx
truncation).

### 3.3 Claim B — "larger model becomes accessible"

A previously non-comfortable configuration becomes **accessible** when:

- it loads and generates with no OOM and no safety-floor violation
  across cold and warm runs (n ≥ 3);
- it lands in a declared execution class (below) and its measured
  numbers satisfy that class;
- cancellation and restart behave per the PR #33 contract (interrupt
  honestly, never auto-resume, explicit retry);
- the compromises are explicit in the plan output: latency class,
  placement, context ceiling, expected cold-start cost.

### 3.4 Execution classes and calibration

Five classes (extending the RFC's three), **defined by role, calibrated
per machine** — the numeric cutoffs below are P3 provisional values
carried over from measured PR #35 work, not universal thresholds:

| Class | Role (universal) | P3 provisional cutoffs |
| --- | --- | --- |
| `fast_interactive` | feels instant; safe as the default chat path | warm TTFT ≤ 2 s AND ≥ 20 tok/s (proposed) |
| `interactive` | normal chat; user waits but does not disengage | warm TTFT ≤ 10 s AND ≥ 5 tok/s (measured basis) |
| `degraded_interactive` | usable with visible compromises; opt-in, labeled | warm TTFT ≤ 60 s AND ≥ 1 tok/s (measured basis; renames RFC `slow_interactive`) |
| `persisted_job` | answer arrives later; queue semantics, survives restart as `interrupted`+retry | below degraded_interactive but completes the grounded task within a stated wall-clock budget |
| `unsupported` | rejected with a fixed reason code | safety-floor violation, OOM risk, `not_runnable` fit, or completion budget exceeded |

Calibration process (proposed, replaces invented universal numbers):

1. **Anchor on perceived-delay bands**, not tok/s: time to first token
   (engagement), time to a useful complete answer for a fixed grounded
   task (task completion), and steady output rate only as the means to
   those ends.
2. **Measure the machine, not the model**: run the shape suite on the
   comfortable control model to establish what "fast" feels like on
   this hardware; classes are then set so that the control model's
   measured behavior defines `interactive` locally.
3. **Include interference and cancellation**: a class claim requires
   the app-responsiveness and cancel-latency measurements, and cold vs
   warm reported separately (keep-alive measured at 13–18× per-turn
   difference on 3b — cold-only measurement would misclassify).
4. **Human approval locks the cutoffs** per tier (P0–P4), same rule as
   Potato Mode defaults: no locking from P3 evidence alone.

### 3.5 Choosing among alternatives (RQ9)

When several routes exist for a task, the planner ranks candidates by,
in order: (1) highest execution class with `measured` confidence,
(2) quality evidence on the grounded shape at the required context,
(3) lower resource pressure. Concretely: prefer a smaller model that is
`interactive` and passes the task's quality check over a larger model
that is `degraded_interactive`; prefer a larger quantized dense model
over a sparse MoE only when the MoE's storage/SSD cost (§4.3) is not
justified by measured quality gain; route to `persisted_job` only when
the user's task tolerates deferred answers (Deep Local's explicit
contract). Never rank by parameter count.

---

## 4. Dense vs MoE runtime differences

### 4.1 Dense models

Every token touches every weight. Working set ≈ full quantized weights
+ KV cache; generation speed on CPU is memory-bandwidth-bound (measured:
thread count 3/6/12 was neutral on qwen3:8b). Uplift levers, all
available in maintained runtimes today (RQ6):

- quantization choice (weights) and KV-cache quantization (q8_0 KV
  halves KV bytes — measured 2.3× generation on 3b under VRAM pressure
  because halved KV let 100 % of layers stay on GPU vs 20 %);
- partial GPU offload — helps only when the GPU share is meaningful
  (measured: 39 % offload on qwen3:8b gained just +6–13 % over CPU-only;
  offload changes trigger hidden 20–72 s reloads);
- context control as a correctness and memory lever (measured
  truncation failure above);
- keep-alive/residency policy (measured 13–18× per-turn);
- prompt/KV reuse (measured ~73× on repeated prefixes);
- flash attention where genuinely supported by model+backend;
- speculative decoding (llama.cpp `--draft`; **unmeasured here** — a
  1b drafting for 8b of the same family is the natural P3 experiment);
- thread/batch tuning (measured neutral / inconclusive on P3 — do not
  ship rules from it);
- mmap so cold weights page in lazily; resident vs persisted execution.

### 4.2 Sparse MoE models

Only k experts per layer fire per token, so **compute scales with
active parameters while storage scales with total parameters**. Two
regimes matter:

- **MoE that fits in RAM** (e.g. ~7B-total/1B-active class): llama.cpp
  runs it directly; it behaves like a small-model-speed /
  bigger-model-knowledge trade. No Colibrì needed (RQ7 boundary).
- **MoE larger than RAM**: per-token expert working set may still fit;
  viability then depends on expert locality — hot-expert caching,
  prefetch, and SSD reads per token. This is Colibrì's actual domain
  (expert streaming from SSD with placement across VRAM/RAM/SSD), and
  also reachable in degraded form via llama.cpp mmap + page cache.

Additional MoE evidence axes (extend protocol §8): active vs total
parameters, expert placement map, hot-expert cache hit rate, disk bytes
read per generated token, cold vs warm cache behavior, routing/expert
quantization effects, cancellation under streaming.

### 4.3 The SSD practicality boundary (RQ8)

A technically-runnable streamed model becomes practically unusable when
SSD traffic, not compute, sets the token rate. Proposed boundary tests
(measured per machine, not assumed):

- **bytes/token test**: if disk bytes read per generated token ×
  measured effective random-read throughput implies < the
  `persisted_job` completion budget, classify `unsupported`;
- **warm-cache dependency**: if the config only meets its class with a
  warm page/expert cache, the class claim must be labeled warm-only and
  cold behavior separately reported;
- **interference**: sustained SSD saturation that starves the OS/page
  cache (app freezes, other I/O stalls) violates the interference rule
  regardless of tok/s;
- **endurance honesty**: report total bytes read per task; hundreds of
  GB re-read per session on a consumer NVMe is a cost the user must
  see. Reference point: the Colibrì spike measured GLM-5.2-int4 at
  0.03–0.11 tok/s cold — persisted-job-only at best, and out of scope
  for 16 GB machines.

---

## 5. Runtime capability comparison

| Capability | Ollama 0.31.1 | llama.cpp b10064 | Colibrì (pinned) | MLX | Future (Vulkan/DirectML/NPU) |
| --- | --- | --- | --- | --- | --- |
| Role in PotatoCS | shipping interactive runtime + model manager | measurement/parity + advanced flags | experimental Deep Local MoE expert streaming | Apple Silicon only (out of scope on P3) | not implemented; inventory reports `none_detected` |
| Dense GGUF | yes | yes | no (MoE-specific engine) | via conversion | — |
| Sparse MoE in-RAM | yes | yes | possible but not its purpose | partial | — |
| MoE expert streaming > RAM | no | degraded (mmap/page cache) | **its purpose** — unproven end-to-end here (PR #36 proves toolchain + AVX2 kernels only) | no | — |
| KV quant / flash attn | env: `OLLAMA_KV_CACHE_TYPE`, `OLLAMA_FLASH_ATTENTION` | per-server flags | n/a | n/a | — |
| Speculative decoding | no (as of 0.31.1) | yes (`--draft`) — unmeasured here | no | yes | — |
| Layer placement control | `num_gpu` (coarse, hidden reload) | `--n-gpu-layers`, `--tensor-split` (finer) | expert placement VRAM/RAM/SSD (design) | unified memory | — |
| Measured parity | same-GGUF CPU parity with llama.cpp ±10 % (measured) | — | none | — | — |
| Cancellation | HTTP close (PR #33 semantics) | HTTP close | socket-close via `RequestCancelHandle`; "closed connection ≠ completed cancellation" | — | — |

Implications: Ollama remains the shipped interactive path and model
manager; llama.cpp is the lab bench (parity is measured, so findings
transfer) and the only current route to speculative decoding and fine
placement; Colibrì stays behind the Deep Local seam until Stage E
evidence exists. MLX is recorded for the strategy's generality but has
no P3 work.

---

## 6. PotatoCS orchestration boundary

The pipeline (task → hardware profile → model compatibility → measured
evidence → backend-neutral intent → provider config → execution) maps
onto existing seams; nothing new is invented:

- **Hardware profile**: `runtime_inventory.py` (read-only, budgeted).
- **Fit + evidence + intent**: `runtime_planner.py` (pure) +
  `runtime_plan_service.py` (read-only `runtime.*` RPCs). The planner's
  output *is* the backend-neutral intent: model identity, context,
  placement band, execution class, confidence, reason codes.
- **Provider-specific configuration**: translation of intent into
  Ollama options / llama-server flags / Colibrì plan happens inside
  each provider adapter (PR #33's narrow seam: `providers/ollama.py`,
  `providers/colibri.py`), never in the planner contract.
- **Execution tiers**: interactive chat (ChatService/Ollama),
  persisted Deep Local jobs (PR #33 `DeepLocalJobService` — FIFO,
  crash-honest, explicit retry).

Boundary rules (binding for future PRs):

1. Backend flags never leak upward: `num_gpu`, `OLLAMA_KV_CACHE_TYPE`,
   `--tensor-split`, Colibrì expert maps are adapter vocabulary. The
   planner speaks fit class, context tokens, placement band, execution
   class.
2. The planner stays pure and read-only; applying a plan is a separate,
   reviewed, user-visible phase (`apply_plan` — future, per RFC §12).
3. Evidence flows one way: adapters/harness produce artifacts; the
   planner consumes fingerprint-matched summaries; no adapter reads
   another adapter's tuning.
4. Fast / Balanced / Deep remain **objectives** (today: context sizing
   4096/8192/8192 + candidate ranking), mapped to execution classes by
   evidence — they are not synonyms for the classes.

---

## 7. Candidate benchmark roles and models

Roles are hardware-relative; this table instantiates them **for the
reference machine only**. Installed models need no download. For the
rest: research candidates only — exact file sizes, digests, tokenizer/
template identity, license, and provenance must be verified at approval
time (§13), and nothing is downloaded in this phase.

| Role | Candidate | Arch | Params (total/active) | Quant / approx size | Runtime support | Status |
| --- | --- | --- | --- | --- | --- | --- |
| Fast-floor control | llama3.2:1b | dense | 1.24 B | Q8_0, 1.3 GB | Ollama + llama.cpp (parity measured) | installed, measured |
| **Control** | llama3.2:3b | dense | 3.21 B | Q4_K_M, 2.0 GB | both | installed, measured; approved potato default |
| **Borderline** | qwen3:4b | dense | ~4 B | Q4_K_M ≈ 2.5–2.6 GB (verify) | both | not installed; borderline case = full-GPU at 8k ctx with KV pressure |
| Borderline alt | gemma3:4b | dense | ~4 B | Q4_K_M ≈ 3.3 GB (verify) | both | not installed; different tokenizer/template — never compared as an equal (§11) |
| **Uplift target** | qwen3:8b | dense | 8.2 B | Q4_K_M, 5.2 GB | both | installed, measured at the 5 tok/s boundary |
| Uplift alt / draft-pair base | llama3.1:8b | dense | 8 B | Q4_K_M ≈ 4.9 GB (verify) | both | not installed; llama3.2:1b is a same-family speculative draft for it |
| **Sparse MoE (in-RAM)** | OLMoE-1B-7B Instruct | MoE | ~6.9 B / ~1.3 B | Q4_K_M ≈ 4.2–4.5 GB (verify) | llama.cpp GGUF; already the named Colibrì Stage-2 target | not installed; Apache-2.0 |
| MoE ceiling (optional) | Qwen3-30B-A3B | MoE | 30.5 B / 3.3 B | Q4_K_M ≈ 18–19 GB (verify) | llama.cpp; exceeds 15.4 GB RAM → mmap/streaming territory | not installed; storage approval required |

KV geometry (measured via `/api/show`): llama3.2:1b 32 KiB/tok;
llama3.2:3b 112 KiB/tok (448 MiB @4096, 896 MiB @8192); qwen3:8b
144 KiB/tok (576 MiB @4096, 1,152 MiB @8192; q8_0 KV halves these).
Unknown geometry uses the 512 KiB/tok conservative bound and forbids
fit claims.

Role logic (RQ3/4/5 mapping): the control proves the harness and Claim A
(§10); the borderline model proves that KV/context/placement technique
turns "fits but uncomfortable at useful context" into comfortable; the
uplift target proves Claim B on hardware whose comfort zone is ~3B; the
in-RAM MoE isolates "MoE economics without streaming" so that Colibrì's
later streaming claim (Stage E) has a fair conventional-runtime
comparison point.

---

## 8. Benchmark protocol

Base: the existing `runtime_bench` harness (PR #35) — six deterministic
shapes, greedy sampling (`temperature 0`, `seed 42`), fixed
`num_predict`, closed artifact schema v1 with redaction sentinel,
resource sampling of the runtime process tree + nvidia-smi, residency
from `/api/ps`, error categories, cold/warm separation.

Already recorded per run: hardware fingerprint, runtime+version, model
tag/digest/quant/size, context + prompt tokens (`prompt_eval_count` —
the truncation detector), output-token limit, placement
(`gpu_fraction` band), server-env allowlist, load time, TTFT,
prompt tps, generation tps, wall-clock, RAM/VRAM peaks, quality check,
failure category.

Protocol extensions required by this strategy (**proposed**, all fit
the closed-schema versioning discipline — additive schema v2):

1. **Paired-arm batches**: baseline and candidate configurations run
   interleaved in one batch with a shared `pair_id`, so ambient drift
   (the confound that made the `num_batch` experiment inconclusive and
   blocked the flash-KV promotion) is bounded; both arms must capture
   complete hardware snapshots including GPU (the flash-KV artifact's
   missing GPU snapshot was the other promotion blocker).
2. **System-interference sampling**: record system-wide available RAM
   (not just process tree) during generation; a run that dips below the
   safety floor is `unsafe`, whatever its speed (the overcommit
   experiment reached 0.7 GB free — that must fail automatically).
3. **Cancellation latency**: issue a cancel at a fixed point in one
   designated run per batch; record request-close → runtime-idle time.
4. **SSD counters** (MoE/streaming stages): process-tree read bytes
   per run, derived bytes-per-generated-token, cold vs warm cache
   labeled explicitly (`file_cache_state` already exists in schema v1).
5. **Task-completion wall clock**: for the `grounded` shape, record
   prompt-to-verified-answer wall time as the persisted-job metric.
6. **Dense/MoE fields**: total and active parameter counts; for MoE,
   expert count/top-k and (Colibrì stage) expert cache hits and
   placement map.

Validity rules (unchanged in spirit, now explicit): comparisons require
identical prompts, chat template, tokenizer, context, sampling, and
output limits; identical model digest for Claim A; fingerprint-matched
aggregation only; stub-engine artifacts never aggregate with real;
heterogeneous artifacts rejected, not averaged.

---

## 9. Proof ladder

Each stage produces artifacts + a result doc with measured-pass /
measured-fail / still-proposed dispositions, and stops at its gate.

**Stage A — Control-model baseline.** Re-run the full shape suite on
llama3.2:3b (and 1b as fast-floor) with the extended v2 protocol,
establishing the baseline vector (§3.1) including the new interference
and cancellation fields.
*Pass*: 3 clean cold + 3 warm runs per shape; quality 100 % at 4096
except the known long-context truncation (which must reproduce — it
validates the detector); no safety-floor dips; artifacts validate.
*Fail*: any harness instability, unexplained >15 % run-to-run variance
on warm generation tps.

**Stage B — Control-model acceleration (Claim A).** Paired-arm batches
on llama3.2:3b: (i) `num_ctx 8192` correctness arm; (ii) flash
attention + q8_0 KV arm (re-run of the 2.3× exploratory finding, this
time with GPU snapshot and interference sampling); (iii) keep-alive /
warm-residency policy arm.
*Pass*: at least one arm meets a §3.2 materiality threshold with zero
protected-dimension regressions and no safety violation, at
`confidence=measured`. The 8192-ctx arm passing also upgrades the live
"balanced" plan from `performance_unknown` to measured.
*Fail*: uplift only appears with quality/output-length/context
regression → recorded as invalid-comparison, not shipped.

**Stage C — Borderline accessibility.** Target: the configuration
"3B–4B dense at 8k context, fully resident" made comfortable. Uses
qwen3:4b (download gate §13) or, if downloads stay closed, the
llama3.2:3b @8192 + KV-quant configuration as the borderline instance.
*Pass*: previously failing/uncomfortable configuration now meets
`interactive` with quality parity across cold/warm and restart.
*Fail*: meets speed but violates interference/cancellation rules.

**Stage D — Uplift target (Claim B).** qwen3:8b from 4.6 tok/s
boundary into an honest class: arms for q8_0 KV (halves 576 MiB→288 MiB
@4096, may lift the 39 % GPU share), context-vs-placement trade-offs,
and (llama.cpp bench arm) speculative decoding with a same-family 1b
draft.
*Pass*: qwen3:8b classified `interactive` or `degraded_interactive`
with measured evidence, explicit compromises in the plan output, and
zero safety violations — or an evidenced `unsupported` verdict for
specific configs (the overcommit case is already such a verdict).
*Fail*: only unsafe or quality-regressed routes reach the class.

**Stage E — Sparse MoE comparison.** OLMoE-1B-7B in llama.cpp
(conventional, in-RAM) vs the same model under Colibrì, plus the SSD
practicality metrics (§4.3). Gated on the full PR #36 Stage-2
prerequisite list (license/provenance review, download approval,
storage check, conversion plan with hashes, human-installed isolated
deps, agreed oracle, privacy/cancellation plans).
*Pass*: same-model same-quant comparison with disk-bytes-per-token,
cold/warm, and cancellation measured on both paths; a clear verdict on
whether expert streaming beats mmap for this class.
*Fail*: any comparison where model bytes, template, or tokenizer
differ between paths.

**Stage F — Planner integration.** Planner consumes Stages A–E
evidence to emit, per objective, the full ladder of §1 for this
machine — including `degraded_interactive` and `unsupported` verdicts
with reason codes — still read-only, still no auto-apply.
*Pass*: plan outputs match measured artifacts exactly (no plan claim
without a fingerprint-matched artifact); property tests over synthetic
inventories keep rungs monotonic (more RAM never lowers a rung).
*Fail*: any `interactive` claim at `derived` confidence.

---

## 10. Immediate experiment (smallest proof of both claims)

**Zero downloads, zero new dependencies, two installed models, one new
harness capability.**

1. **Claim A instance** — llama3.2:3b, paired arms (Stage B):
   baseline defaults vs `num_ctx 8192` + flash attention + q8_0 KV +
   warm residency. Expected from existing exploratory evidence:
   materially faster generation under KV pressure (2.3× exploratory),
   ~73× repeat-prompt TTFT, and — the correctness headline — a 5k-token
   document actually read instead of silently truncated. Success
   upgrades these from `measured_exploratory` to promotable measured
   findings and fixes the two previous promotion blockers (missing GPU
   snapshot, unexplained RAM dip) by construction.
2. **Claim B instance** — qwen3:8b (Stage D core): paired arms
   baseline vs q8_0-KV-driven placement improvement at 4096, with
   interference sampling and cancellation latency. Success = an honest
   measured classification (`interactive` if ≥ 5 tok/s is reached,
   else `degraded_interactive` with explicit compromises) plus a
   machine-readable `unsupported` verdict for the overcommit config.

Why this is the smallest sufficient experiment: it needs only the
paired-arm harness extension (§8.1–8.3); it touches no planner logic,
no UI, no providers; it uses models already on disk; it does not need
GLM-5.2, OLMoE, or Colibrì at all — consistent with the corrected
product goal that Colibrì enters only when a genuinely
expert-streaming-suited model is selected (Stage E).

Deliverables: schema-v2 paired artifacts + `UPLIFT_MILESTONE_RESULT.md`
with per-arm dispositions and a verdict per claim.

---

## 11. Risks and invalid comparisons

- **Cross-tokenizer comparisons**: tok/s across different tokenizers
  (qwen3 vs llama3.2 vs gemma3) is not a like-for-like rate; compare
  wall-clock task completion and quality instead. Never present
  gemma3:4b vs qwen3:4b tok/s as a ranking.
- **Output-length laundering**: shorter answers inflate apparent speed;
  fixed `num_predict` + token-count parity checks are mandatory (§3.2).
- **Warm-only claims**: keep-alive makes warm 13–18× cheaper; any class
  claim must state cold behavior (cold 8b is 28–41 s).
- **Ambient drift**: single-arm before/after runs on a live desktop are
  unreliable (the `num_batch` lesson); only paired interleaved arms.
- **Hidden reloads**: `num_gpu`/option changes trigger 20–72 s reloads;
  a mid-session placement change is itself a UX cost the plan must
  surface.
- **Thinking-mode asymmetry**: qwen3 emits reasoning tokens; greedy
  fixed-budget shapes bound this, but grounded-task wall clock is the
  honest cross-model metric.
- **Estimator drift**: the 600 MiB overhead constant is Ollama-0.31.1-
  observed; residency cross-checks showed estimates 1.4–1.6×
  conservative. Re-validate constants per runtime version; never let an
  estimate override a measured residency.
- **P3 generalization**: everything measured so far is one machine.
  No tier-wide claim, no default locking, until P1/P2 evidence or a
  documented simulated equivalent exists (existing repo rule).
- **MoE quality myths**: 7B-total/1B-active is not "a 7B" in quality;
  Stage E must score the grounded task, not assume parameter-count
  quality (RQ10 applied to architecture).
- **Colibrì scope creep**: PR #36 proves toolchain + AVX2 kernel
  exactness only. Any plan output implying Colibrì can run a model
  today would be a false claim; the seam stays disabled by default
  (`deep_local_enabled` off) until Stage E.

---

## 12. Storage and dependency estimates

Immediate experiment (§10): **0 new bytes, 0 new dependencies** —
llama3.2:3b (2.0 GB) and qwen3:8b (5.2 GB) are installed; llama.cpp
b10064 zips are already archived by SHA-256 for the optional
speculative-decoding bench arm.

Stage C/D options (each gated): qwen3:4b ≈ 2.6 GB; llama3.1:8b ≈
4.9 GB (verify exact bytes + digest at approval).

Stage E (all gated): OLMoE-1B-7B GGUF Q4 ≈ 4.2–4.5 GB (+ conversion
workspace if converting from safetensors: transient ~14 GB f16 —
prefer a prequantized GGUF with verified provenance to avoid the
Torch/Transformers dependency set entirely; if upstream's stronger
teacher-forcing oracle is required, that dependency set is
human-installed and isolated per the PR #36 prerequisite list).
Optional Qwen3-30B-A3B ≈ 18–19 GB — likely exceeds "limited available
model storage" on the reference machine; requires an explicit storage
decision and is not needed for any current-stage pass condition.
GLM-5.2 (370 GB) remains explicitly out of scope.

Disk budget rule (proposed): benchmark downloads may not push the model
store below 20 % free or below 2× the largest installed model's size,
whichever is larger — checked against the inventory's storage probe
before any approval request is even made.

---

## 13. Human approval gates

Consistent with `V04_BATON_FOR_SMALLER_MODELS.md` §G — all of these are
maintainer/Fable decisions, none delegable to implementation agents:

1. **G-THRESH**: adopt or adjust the §3.2 materiality thresholds and
   §3.4 class cutoffs (per tier). Required before Stage B verdicts.
2. **G-DL(model)**: each model download — exact file, size, digest,
   license, provenance, storage check. Required before Stage C (if
   qwen3:4b) and Stage E.
3. **G-PROMOTE**: promotion of any `measured_exploratory` finding to a
   planner-visible rule (keep-alive policy, KV quant, context sizing).
   Required after Stage B/D, before Stage F.
4. **G-COLIBRI-2**: the full PR #36 Stage-2 prerequisite list before
   any Colibrì model work.
5. **G-DEFAULTS**: Potato Mode / class-cutoff defaults per tier —
   blocked on P1/P2 evidence, unchanged from existing repo rule.
6. **G-GREEN**: any release-gate GREEN declaration — maintainer only.

Standing constraints (non-negotiable, from
`LONG_TERM_PRODUCT_ROADMAP.md` §D): no auto-downloads, no telemetry,
no hidden heavy work, no raw private data in artifacts (the redaction
sentinel already enforces this at write time).

---

## 14. Recommended branch/PR stack

No branches are created by this document. Recommended sequence:

1. `docs/hardware-relative-uplift-strategy` → PR to `main`. This
   document only. Unblocks review of the strategy itself.
2. `feat/runtime-bench-paired-arms` → stacked on
   `feat/local-runtime-acceleration` (PR targets #35's branch until
   #35 merges, mirroring how #35 targets #33). Content: §8 protocol
   extensions (paired arms, interference sampling, cancellation
   latency, schema v2) + tests. **This is the Sol/Codex task (§15).**
3. `bench/uplift-milestone-p3` → stacked on (2). Runs §10, commits
   artifacts + `UPLIFT_MILESTONE_RESULT.md`. Measurement-only.
4. `feat/planner-uplift-classes` → after G-THRESH + G-PROMOTE:
   planner consumes paired evidence, emits the five classes and
   ladder rungs (Stage F). Separate review.
5. `research/moe-olmoe-eval` → after G-DL + G-COLIBRI-2 (Stage E).
   Independent of 2–4.

PRs #33, #35, #36 are not modified; the stack merges after them in
their existing order.

---

## 15. Implementation prompt for GPT-5.6 Sol/Codex

The single narrow task (branch 2 above). Everything else is out of
scope for the implementing agent.

```
TASK: Add a paired-arm uplift-experiment mode to the existing
runtime_bench harness.

Branch: feat/runtime-bench-paired-arms, created from
feat/local-runtime-acceleration (7281fdb4). Do not modify
feat/deep-local-fable, main, or any planner/RPC/UI/provider code.

CONTEXT (read first):
- python/odysseus_desktop_backend/runtime_bench/ (harness.py,
  shapes.py, artifacts.py, sampler.py) — the existing single-arm
  harness and closed artifact schema v1.
- projects/odysseus/LOCAL_RUNTIME_ACCELERATION_RFC.md §5 (shapes,
  schema), and LOCAL_RUNTIME_BENCHMARKS.md §7–§8 (why single-arm
  before/after runs were inconclusive: ambient drift, and why the
  flash-KV finding could not be promoted: missing GPU snapshot and an
  unexplained system-RAM dip).

BUILD exactly this, dev-only, stdlib-only, loopback-only:

1. Paired batches. A batch definition = one model tag + one shape
   list + exactly two named arms ("baseline", "candidate"), each an
   options/server-env dict from the existing allowlist. Runs
   interleave arms (B,C,B,C,...) with cold runs first per arm, warm
   runs after, n>=3 warm per arm per shape. Same prompts, seed 42,
   temperature 0, fixed num_predict — reuse BENCHMARK_SHAPES
   unchanged.

2. Schema v2, additive and CLOSED at every level like v1:
   schema_version 2; new fields pair_id, arm (baseline|candidate),
   system_memory_samples (system-wide available RAM from
   GlobalMemoryStatusEx via ctypes, sampled on the existing sampler
   cadence), min_system_available_ram_bytes, and
   cancel_probe {enabled, cancel_issued_at_ms, runtime_idle_at_ms,
   cancel_latency_ms} on at most one designated run per arm
   (cancel = close the streaming HTTP response, then poll /api/ps
   until the model reports idle or 30 s). validate_artifact() gains
   the v2 branch; v1 artifacts must still validate. The redaction
   sentinel applies unchanged.

3. Safety guard: if system available RAM drops below
   max(1.5 GiB, 12% of total) during any run, abort the batch,
   mark the artifact run error_category="safety_floor", and exit
   nonzero. Reuse the planner's constants by value, not by import
   (runtime_bench must stay import-clean of services/).

4. Hardware snapshot completeness: refuse to write any v2 artifact
   whose hardware section lacks a GPU entry when nvidia-smi is
   available (the prior promotion blocker). Record file_cache_state
   for cold/warm as v1 already allows.

5. Comparison report: a pure function + CLI (python -m
   odysseus_desktop_backend.runtime_bench compare <artifact...>)
   that pairs arms by pair_id and emits a JSON verdict per shape:
   deltas for ttft_warm, gen_tps, prompt_tps, peak RAM/VRAM;
   quality_parity (candidate pass-vector >= baseline);
   token_count_parity (generated tokens within +/-10% on open-ended
   shapes); verdict in {uplift_candidate, no_uplift, unsafe,
   invalid_comparison}. "unsafe" if any safety_floor or
   min_system_available_ram violation; "invalid_comparison" if
   digests, prompts, context, num_predict, or quality parity
   preconditions differ. No materiality thresholds hardcoded as
   pass/fail policy — report numbers; thresholds are a human gate.

6. Tests, same style/coverage discipline as the existing 64
   harness/artifact tests: schema v2 validation (accept/reject),
   pairing, interleave order, safety-floor abort, cancel-probe
   bookkeeping, comparison verdicts incl. invalid_comparison and
   unsafe paths, v1 backward compatibility. All fakes local; no
   network, no model execution in tests.

DO NOT: modify runtime_planner.py, runtime_plan_service.py,
runtime_inventory.py, shapes, prompts, providers, UI, or settings;
add dependencies; download anything; run real benchmarks in CI;
change artifact schema v1 semantics; import services/ from
runtime_bench.

DONE WHEN: full test suite green including existing 695 tests;
new mode produces validating v2 artifacts against a fake server
fixture; compare CLI produces correct verdicts on fixture data;
result summary lists any deviation from this contract explicitly.
```

---

## Conclusion

**What we build next.** The paired-arm harness extension (§15), then
the zero-download immediate experiment (§10) on llama3.2:3b and
qwen3:8b. That single experiment, if it passes, proves both halves of
the product claim on real hardware: an already-comfortable model made
materially faster and more correct (context), and the machine's
natural uplift target moved from an unmeasured boundary case into an
honest, safe, explicitly-labeled execution class.

**What Colibrì is and is not responsible for.** Colibrì is one
experimental Deep Local backend for sparse-MoE expert streaming when a
model's total parameters exceed RAM but its active-parameter working
set does not — entered only through Stage E, only after its Stage-2
human gates, and only for models genuinely suited to expert streaming.
It is not responsible for dense-model uplift, not the default path for
any MoE that fits in RAM, not a chat provider, and not part of any
current claim: today's proven Colibrì surface is a reproducible
Windows build and bit-exact AVX2 kernels — nothing more.

**How success proves general hardware-relative uplift.** The proof is
deliberately structured so that nothing in it is specific to the
reference machine except the measured inputs: the baseline is computed
from inventory (fit constants + KV geometry), the classes are
calibrated from local measurements rather than universal thresholds,
and every uplift verdict is a paired same-model comparison with
quality parity enforced. Running the identical ladder on a 4 GB
netbook or a 32 GB desktop changes the rung labels, not the method.
That is the product: not "PotatoCS runs 12B", but "PotatoCS knows,
with evidence, what *your* machine can comfortably run, makes that
faster, and safely unlocks the next rung — and tells you honestly
where the ladder ends."

---

## Appendix A — Direct answers to the ten research questions

1. **Comfortable baseline calculation**: fit-with-headroom via the
   existing planner constants + KV geometry, intersected with measured
   `interactive`-class evidence, interference, verified-context
   correctness, and lifecycle predictability (§2.1–2.2). A set of
   configurations, never a parameter count.
2. **Measuring a genuine baseline move**: paired-arm, same-digest,
   fixed-output comparison against the baseline vector with protected
   dimensions (quality, context, output length, headroom, cancel
   latency) that may not regress (§3.1–3.2, §8.1).
3. **Faster already-supported 3B**: warm residency / keep-alive
   (13–18× per-turn, measured), prompt/KV reuse (~73×, measured),
   flash attention + q8_0 KV under VRAM pressure (2.3× exploratory),
   correct context sizing; llama.cpp parity means its flag set is also
   available (§4.1, Stage B).
4. **Borderline 4B → comfortable**: KV-cache quantization to keep full
   GPU residency at useful context, context ceilings from measured KV
   geometry rather than defaults, cold-start amortization via
   residency policy (§4.1, Stage C).
5. **7B–8B accessible on a 3B-comfortable machine**: KV quant to raise
   the GPU layer share (qwen3:8b KV 576→288 MiB @4096), measured
   placement instead of forced offload (overcommit is the proven
   anti-pattern), context/placement trade curves, speculative decoding
   with a same-family 1b draft via llama.cpp, and honest
   `degraded_interactive` labeling where 5 tok/s is not reached
   (Stage D).
6. **Dense gains available in llama.cpp today**: all of the above
   except none require MoE — quantization, `--n-gpu-layers`/
   `--tensor-split`, mmap, KV quant, flash attention, prompt caching,
   speculative decoding, thread/batch tuning (the latter measured
   neutral on P3) (§4.1, §5).
7. **Requires sparse MoE / could benefit from Colibrì**: only the
   larger-than-RAM regime where per-token active experts fit but total
   weights do not — expert placement, hot-expert caching, prefetch,
   disk-reads-per-token economics. In-RAM MoE runs conventionally and
   is Colibrì-free (§4.2, Stage E).
8. **SSD practicality boundary**: bytes-read-per-token × measured disk
   throughput vs the persisted-job completion budget, warm-cache-only
   claims labeled, OS-starvation interference, and total-bytes-read
   honesty; classify `unsupported` past the budget (§4.3).
9. **Choosing among smaller/larger-dense/MoE/persisted**: rank by
   measured execution class, then grounded-task quality at required
   context, then resource pressure; persisted jobs only for tasks that
   tolerate deferred answers (§3.5).
10. **Avoiding fake uplift**: fixed `num_predict`, token-count parity,
    quality-parity vectors, `prompt_eval_count` truncation detection,
    same-digest requirement, paired arms, and an explicit
    `invalid_comparison` verdict distinct from `no_uplift` (§3.2,
    §8, §11).
