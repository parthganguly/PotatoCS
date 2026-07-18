# Local Runtime Benchmarks — Measured Results

Status: Phase 1/4/5 evidence for the local-runtime-acceleration track.
Raw machine-readable artifacts:
`projects/odysseus/benchmarks/local-runtime/*.json` (schema v1,
redaction-checked at write time). Summary tables below are medians of
warm runs unless stated; cold values are single measured cold runs
(model unloaded first, OS file cache state `unknown_warmish` — a true
cold-disk measurement would require a reboot per run).

Hardware (constant across all artifacts, embedded in each):
AMD Ryzen 5 4600H (6C/12T, AVX2, no AVX-512), 15.4 GB RAM,
NVIDIA RTX 3050 Laptop 4 GB, Windows 11 Home 10.0.26200, tier P3.
Runtime: Ollama 0.31.1 (loopback, `OLLAMA_NO_CLOUD=1` on controlled
servers). llama.cpp comparison runtime: release b10064 (`86d86ed43`),
Windows x64 CPU build, obtained as the official release zip, SHA256
`C9B770B584A007A1AEEA1B729E0E4724FB79A2CB136ECE46BE92704AAEE5099E`
(CUDA 12.4 zip also archived:
`D3DF8C73874D9BF00CB3631A902A6AFEA556D57F11CB226E165689BE9AA9E34B`);
binaries live outside the repo and are never shipped.

Quality gate: every run carries a fixed in-process quality check
(exact-token / planted-fact assertions at temperature 0, seed 42);
outputs are never persisted, only verdicts. A run that fails quality can
never support a speed claim.

## 1. Baseline matrix (Ollama 0.31.1, runtime defaults)

Shapes: tiny (~50-token prompt, 32-token cap), medium (~700-token
prompt, 256-token cap), long_context (~5,100-token document + planted
codeword, 64-token cap), grounded (source-quoting task, 256-token cap).

| Model (quant) | Shape | Cold total | Warm total (median) | Warm TTFT | Gen tok/s | GPU residency | Quality |
|---|---|---|---|---|---|---|---|
| llama3.2:1b (Q8_0, 1.3 GB) | tiny | 3.30 s | 0.35 s | 0.30 s | 122–124 | 100 % VRAM | pass 4/4 |
| llama3.2:1b | medium | 4.32 s | 1.34 s | 0.29 s | 103–104 | 100 % | pass 4/4 |
| llama3.2:1b | long_context | 3.74 s | 0.37 s | 0.34 s | 196–203 | 100 % | **fail 4/4** (see §2) |
| llama3.2:1b | grounded | 3.64 s | 0.81 s | 0.34 s | 105–106 | 100 % | pass 4/4 |
| llama3.2:latest = 3b (Q4_K_M, 2.0 GB) | tiny | 10.59 s | 0.48 s | 0.37 s | 52–54 | 100 % | pass 4/4 |
| llama3.2:3b | medium | 10.10 s | 2.70 s | 0.38 s | 41–45 | 100 % | pass 4/4 |
| llama3.2:3b | long_context | 11.86 s | 1.13 s | 0.47 s | 32–36 | 100 % | **fail 4/4** (§2) |
| llama3.2:3b | grounded | 9.71 s | 1.26 s | 0.40 s | 42–48 | 100 % | pass 4/4 |
| llama3.2:3b | repeat ×4 (warm) | — | 2.84–3.26 s | 0.40–0.63 s | 39–43 | 100 % | pass 4/4 |
| qwen3:8b (Q4_K_M, 5.2 GB) | tiny | 35.60 s | 1.38 s | 0.46 s | 6.5–7.4 | **39 % VRAM / 61 % CPU** | pass 4/4 |
| qwen3:8b | medium | 41.11 s | 19.28 s | 0.52 s | 4.6 | 39 % | pass 4/4 |
| qwen3:8b | long_context | 28.43 s | 1.29 s | 0.55 s | 5.2–6.3 | 39 % | **fail 4/4** (§2) |

Reading:

- The two llama3.2 models are genuinely interactive on this machine
  (TTFT < 0.5 s warm, 41–124 tok/s). qwen3:8b is the boundary case: it
  no longer fits 4 GB VRAM, runs 61 % on CPU, and generates at ~4.6
  tok/s — right at the planner's `interactive` floor (5 tok/s), i.e.
  honestly classified `slow_interactive` for medium-length answers.
- Cold-start dominates the first impression: 3.3 s (1b), ~10 s (3b),
  28–41 s (8b). Keep-alive policy (§3) is therefore the highest-value
  free lever on this hardware.
- qwen3:8b thinking mode was disabled (`think: off`) for these cells;
  thinking multiplies latency and is a separate product decision.

## 2. Context length is a correctness lever, not just a speed lever (measured)

At Ollama 0.31.1 defaults on this 4 GB-VRAM machine the effective
context is 4096; the long-context document is ~5,100 tokens. Result at
defaults: the server silently truncates the prompt
(`prompt_eval_count` 2050 of 5138 actual tokens) and **every model
answers from a document whose middle — containing the planted
codeword — was cut**. Quality: 0/12 across three models. This is a
measured instance of potato-failure-mode #3/#4 ("silently degrades with
no explanation").

Same model (llama3.2:1b), same prompt, `num_ctx: 8192`
(`ollama-0311-llama32-1b-longctx-ctx8192`):

| Metric | default (4096) | num_ctx 8192 | Delta |
|---|---|---|---|
| Quality | fail 4/4 | **pass 4/4** | truncation eliminated |
| Prompt tokens evaluated | 2050 (truncated) | 5138 (full) | +150 % real work |
| Cold total | 3.74 s | 11.99 s | prompt eval of full doc + load |
| Warm total | 0.37 s | 0.40 s | ≈ neutral (prompt cache, §4) |
| Gen tok/s | ~200 (on wrong context) | 103–107 | tok/s on the *correct* context |

Status: **research finding (measured_exploratory).** The planner does
NOT emit evidence-length-driven context sizing; it emits a fixed
objective-sized `num_ctx` and charges the architecture-aware KV cost in
its memory estimate. Sizing context to actual evidence length is a
candidate for a future reviewed phase; a truncated prompt is a
correctness failure, not an acceptable speed win.

## 3. Keep-alive: the cost of not keeping the model resident (measured)

`ollama-0311-llama32-3b-tiny-keepalive0`: identical tiny requests with
`keep_alive: 0` (model unloaded after every call) versus the baseline
warm path (default `keep_alive` 5 m):

| Path | Per-turn total (3 runs) | TTFT |
|---|---|---|
| keep_alive 0 | 6.20 s / 6.59 s / 8.70 s | 6.1–8.6 s |
| default keep-alive (warm) | 0.46–0.49 s | 0.34–0.38 s |

**A dropped keep-alive costs ~13–18× per turn on the 3b default
model.** Status: **research finding (measured_exploratory)** on one
machine. The planner emits no keep-alive recommendation; the finding
documents why `keep_alive: 0` "free memory" folk tuning would be
harmful (the server default of 5 m is already sane), and any future
recommendation field must carry its own safety gates.

## 4. Prompt-cache reuse (measured, existing behavior)

Repeat identical long-context prompts warm
(`ollama-0311-llama32-1b-longctx-ctx8192`): prompt eval 879.8 ms cold →
**12.0–12.4 ms on repeat** (~73× faster prompt phase; total 0.40 s).
The repeat shape on 3b (medium prompt) shows the same effect at smaller
scale. This is Ollama's built-in prompt cache doing its job; the
benchmark pins that it works and how large the effect is. No product
change required; the finding argues for keeping conversation prefixes
stable (system prompt churn destroys this cache).

## 5. Thread count (measured — neutral, documented honestly)

qwen3:8b medium (CPU-bound: 61 % of weights on CPU), warm runs:

| num_thread | Gen tok/s (3 runs) |
|---|---|
| default | 4.58 / 4.64 / 4.60 |
| 3 | 4.33 / 4.24 / 4.40 |
| 6 (physical) | 4.85 / 4.63 / 4.38 |
| 12 (logical) | 4.67 / 4.44 / 4.65 |

Generation on this machine is memory-bandwidth-bound, not
compute-bound: thread count is **neutral within noise** (§ artifacts).
No thread rule ships. The planner emits `num_thread = physical cores`
only as a conservative default (matching Ollama's own default behavior)
and claims no speedup for it.

## 6. GPU offload: marginal for minority-offloaded models (measured)

qwen3:8b medium, warm: default partial offload (39 % of weights in
VRAM) 4.58–4.64 tok/s versus forced CPU-only (`num_gpu: 0`) 4.05–4.35
tok/s (`ollama-0311-qwen3-8b-medium-gpu0`). Offloading a minority of
layers buys only **6–13 %**. No manual-offload rule ships; the runtime's
own placement is left alone. (Caveat observed and recorded: changing
`num_gpu` per-request triggers a silent model reload — the first run
after an options change pays a hidden 20–72 s cold cost.)

## 7. num_batch: inconclusive (documented honestly)

Cold prompt-eval of the 5,138-token document, `num_ctx` 8192:
`num_batch` 512 (default) 880 ms, 1024 → 1,001 ms, 128 → 1,209 ms.
The three cells ran under drifting ambient VRAM (desktop apps consumed
the 4 GB card between batches), which confounds prompt-eval placement.
Verdict: **inconclusive; no rule ships.** A clean re-measurement needs
a machine with stable free VRAM.

## 8. Flash attention + q8_0 KV cache: measured_exploratory, NOT a recommendation

First pass produced a spurious "regression" traced to ambient VRAM
drift (desktop apps had consumed the card between batches; the
artifact's own hardware snapshot showed 85 MB free at capture and 84.7 %
offload). The paired re-run held ambient VRAM roughly equal
(both arms ~180 MiB free before the batch, back-to-back server
restarts, `ollama-0311-llama32-3b-medium-paired-*`):

| Arm (llama3.2:3b, medium, warm) | GPU placement | Gen tok/s |
|---|---|---|
| default env | 19.7 % of layers | 10.04–10.21 |
| `OLLAMA_FLASH_ATTENTION=1` + `OLLAMA_KV_CACHE_TYPE=q8_0` | 100 % | 23.19–23.69 |

The 2.3× generation delta under VRAM pressure is a real observation
with a visible mechanism (halving KV size let the memory fit place all
layers instead of 20 %), and quality checks passed in both arms. Where
VRAM was ample (1b fully resident) the same env was neutral-to-noise;
on the CPU-bound 8b it was neutral (4.6–4.8 vs 4.6).

**Status: `measured_exploratory`. This is not a shipped or
production-ready recommendation, and the planner emits nothing based on
it.** Two defects in the comparison forbid promotion:

1. the optimized arm's warm runs reached critically low minimum
   available system RAM (~16–177 MB vs ~1.3–2.1 GB in the default arm)
   — the arms were not equivalent in system-memory conditions and the
   optimized arm itself ran close to destabilizing the machine;
2. the optimized arm's hardware snapshot failed to enumerate the GPU at
   capture time, so its ambient-VRAM record is incomplete.

Safety acceptance criteria that must ALL hold before this can become a
recommendation:

- a minimum available-system-RAM floor maintained during every run in
  both arms;
- stable GPU detection in every hardware snapshot;
- comparable ambient VRAM across arms, recorded per run;
- multiple alternating A/B rounds (A,B,A,B,…), not one A batch then one
  B batch;
- no hidden model reload inside measured warm samples;
- quality checks pass in all arms;
- no increase in failures or timeouts.

## 9. llama.cpp b10064 vs Ollama 0.31.1, same GGUF blob (measured — parity)

The exact blob Ollama uses for llama3.2:1b (Q8_0, `sha256-74701a8c…`,
verified GGUF magic) served directly by upstream `llama-server`
(CPU build, `-c 8192 -t 6`, prompt cache on) versus Ollama with
`num_gpu: 0` (CPU-only), same shapes, same day, warm runs:

| Shape | llama.cpp b10064 CPU | Ollama 0.31.1 CPU |
|---|---|---|
| tiny | 17.2–18.4 tok/s | 18.9–22.1 tok/s |
| medium | 15.5–16.9 tok/s | 13.8–15.8 tok/s |
| long_context (warm, cached) | 10.8 tok/s, ~1.0 s total | (not run CPU-only) |

Verdict: **parity within ~±10 % (noise on a loaded desktop)** — Ollama
adds no material generation overhead over its embedded llama.cpp on
CPU. No "bypass Ollama" rule ships. Direct llama.cpp remains
interesting only for launch-time capabilities Ollama does not expose
(per-server KV type, speculative decoding with a draft model), each of
which would need its own measured proof. llama.cpp long-context quality
passes at `-c 8192` (full document, planted fact retrieved), confirming
the §2 finding is about context sizing, not the runtime.

## 10. Honest failure/overcommit case (measured)

`ollama-0311-qwen3-8b-overcommit-gpu99`: all layers of the 5.9 GB
resident model forced onto the 4 GB card. It does not error — the
driver spills into shared system memory: reported placement 5.58 GB
"VRAM", physical card pegged at 3,858 MiB, **system available RAM
crushed to 0.7 GB**, 20 s first token on a 16-token answer. Technically
"runs", practically destabilizes the whole machine. The planner's
margin rule (plan VRAM ≤ free − 256 MB) exists to forbid exactly this
configuration; the benchmark records what happens without it.

## 11. Experiment outcome summary

Nothing in this table is a shipped rule. Every row is a research
finding at `measured_exploratory` status (one P3 machine), except where
marked neutral/inconclusive. The planner emits only an objective-sized
`num_ctx`; it makes no keep-alive, context-sizing, offload, or
flash-KV recommendations.

| Experiment | Verdict | Status |
|---|---|---|
| Keep-alive vs reload-every-turn | 13–18× per-turn cost measured | measured_exploratory |
| Context sizing on long documents | correctness effect (0/12 → 4/4 pass at num_ctx 8192) | measured_exploratory |
| Prompt-cache reuse | ~73× prompt-eval speedup on repeat (built-in runtime behavior) | measured_exploratory |
| Thread count | neutral (bandwidth-bound) | neutral; no finding |
| GPU offload (partial vs CPU-only, minority offload) | +6–13 % only | measured_exploratory |
| num_batch 128 vs 1024 | inconclusive (ambient confound) | inconclusive; no finding |
| Flash attention + q8_0 KV | 2.3× under VRAM pressure in ONE paired batch; §8 defects block promotion | measured_exploratory |
| llama.cpp vs Ollama same model (CPU) | parity ±10 % | measured_exploratory |
| Overcommit (forced full offload > VRAM) | runs but crushes system RAM to 0.7 GB free | measured failure case; the planner margin rule (which IS implemented) forbids planning it |

## 12. Measurement limitations

1. **Ambient VRAM drift**: desktop applications consumed 2–4 GB of the
   card across the session; only paired same-ambient arms (§8) support
   cross-arm claims. Every artifact embeds its own free-VRAM snapshot,
   which is how the drift was caught.
2. **Process RSS under-counts**: artifacts written before the sampler
   fix matched only `ollama*.exe` process names and missed Ollama's
   `llama-server.exe` runner subprocess; RSS figures in those artifacts
   are lower bounds. VRAM and residency figures are unaffected. (Fixed
   in the harness: the sampler now matches the runner too.)
3. **Options changes trigger hidden reloads**: the first run after any
   `num_gpu`/`num_thread` change includes a silent model reload; only
   subsequent warm runs are comparable (visible in the artifacts as an
   anomalous first warm run; medians quoted exclude it).
4. **One machine, P3 tier.** The rules shipped are ratios and guards
   parameterized by the hardware inventory, not absolute numbers; on
   unmeasured hardware the planner degrades to `derived` or
   `conservative_default` confidence by design. Nothing here validates
   P0–P2 tiers.
