# Hardware-Relative Model Uplift Result

Status: MEASUREMENT HARNESS IMPLEMENTED; REAL INFERENCE SAFETY-BLOCKED (2026-07-19).

This result is measurement evidence, not product policy. It does not set an
uplift threshold, promote a runtime configuration, select a model, change a
planner rule, or declare a release gate. PRs #33, #35, and #36 remain
unmodified.

## Implementation completed

The existing development-only `runtime_bench` infrastructure now supports a
versioned schema-v2 paired experiment in addition to unchanged schema-v1
artifacts.

Each v2 file represents one baseline or candidate arm and records the shared
experiment and pair identity, balanced global execution order, repetition and
cold/warm state, exact runtime version, model digest and privacy-safe file
identity, architecture and parameter metadata where known, fixture identity
and SHA-256, tokenizer and chat-template identity, protected context/output/
sampling requirements, backend options, process-local server environment
evidence, placement and residency state, timings and token counts, quality
evidence, bounded RAM/pagefile/process/GPU/disk observations, fixed failure and
truncation states, explicit preflight rejection categories, ambient CPU/RAM/GPU
observations, and optional cancellation measurements.

The artifact schema remains closed at every object level. Unknown fields,
non-finite or out-of-range numbers, booleans used as numbers, malformed
timestamps, unsafe enum values, prompts, generated output, arbitrary notes,
usernames, path separators, absolute paths, and private environment keys are
rejected before writing.

The paired executor uses cold baseline/candidate runs first and alternates warm
order as `baseline → candidate`, then `candidate → baseline`. It requires at
least three warm repetitions. Temperature, seed, output-token limit, context,
and fixture are protected across arms.

Before building a lookup map or aggregate, comparison requires exactly one
non-cancellation cold run and at least three non-cancellation warm runs per arm,
unique run indexes within each arm, unique global execution orders, identical
`(cold, repetition_index)` sets, and contiguous performance orders beginning at
zero. Cold order is baseline then candidate; warm repetitions alternate B/C,
C/B, B/C thereafter. Cancellation probes must be appended after performance
and cannot satisfy performance counts. The paired CLI always includes cold
runs and has no `--no-cold` escape hatch.

The comparison command validates every input independently, groups arm files
by pair, excludes invalid pairs from every aggregate, and reports cold and warm
distributions separately. It emits absolute candidate-minus-baseline
differences and candidate/baseline ratios for TTFT, prompt processing,
generation, wall-clock completion, load duration, available-RAM floor,
process RSS, VRAM use, and disk reads. Cancellation is reported separately.
No materiality threshold or promotion verdict is applied by default.

Ambient drift is always reported as baseline, candidate, and absolute paired
distributions for pre-arm CPU, available RAM, GPU used/free bytes, elapsed arm
gaps, and during-run mean CPU. Default comparison invents no universal
interference threshold. A caller may supply a closed schema-v1 JSON policy with
nullable limits for CPU, RAM, GPU-used, and elapsed-gap differences. Only that
explicit policy can produce `system_interference`; an unavailable required CPU
or GPU probe produces `incomplete_run`.

## Comparability rules

A pair is `valid_comparison` only when all protected evidence is present and
equal. Fixed invalid reasons are:

- `model_mismatch`
- `model_digest_mismatch`
- `prompt_fixture_mismatch`
- `tokenizer_mismatch`
- `template_mismatch`
- `context_mismatch`
- `output_limit_mismatch`
- `sampling_mismatch`
- `generated_token_count_mismatch`
- `runtime_version_mismatch`
- `cold_warm_mismatch`
- `placement_not_recorded`
- `hardware_snapshot_missing`
- `system_interference`
- `truncated_prompt`
- `incomplete_run`
- `quality_mismatch`
- `missing_arm`
- `duplicate_arm`
- `malformed_artifact`
- `unsupported_schema`
- `engine_kind_mismatch`
- `insufficient_cold_runs`
- `insufficient_warm_runs`
- `duplicate_run`
- `duplicate_execution_order`
- `unbalanced_execution_order`
- `repetition_set_mismatch`
- `file_identity_missing`
- `tokenizer_identity_missing`
- `template_identity_missing`
- `runtime_identity_missing`

GPU unavailability is represented as an explicit fixed state with null byte
measurements. It is never silently converted to zero. A malformed input is
counted and isolated; it cannot poison other pairs.

## Safety and cancellation behavior

The harness reuses the reviewed PR #35 RAM policy by exact value:

`safety floor = max(1.5 GiB, 12% of total physical RAM)`

The pre-arm estimator mirrors PR #35 known/unknown semantics. A positive
integral disk size is known; otherwise a supported, strictly positive parameter
count uses the reviewed Q2, Q3, Q4, Q5, Q6, Q8, F16/FP16/BF16, or F32/FP32
multiplier. Boolean, zero, negative, non-integral, or malformed disk/parameter
metadata is malformed rather than coerced. Complete positive integral KV
geometry uses the architecture formula; boolean, zero, negative, or fractional
geometry is malformed. Missing/partial geometry uses the conservative 512
KiB/token value but remains unknown. It records `weights_bytes`,
`kv_cache_bytes`, `total_bytes`, `weights_known`, `kv_geometry_known`, and a
fixed rejection category: empty, `unknown_weight_size`, `unknown_kv_geometry`,
or `malformed_model_metadata`. Budget and probe rejection use
`insufficient_memory_budget` and `memory_probe_unavailable`.

Execution controls are validated before model inventory, `/api/show`, hardware
inventory, runtime detection, unload, or arm execution. `context_limit` must be
an integer at least 1, `repeats` an integer at least 3, `timeout` finite and
positive, and `cancel_after_ms` a non-negative integer. Booleans are rejected
as numbers. With cancellation enabled, its delay must be strictly less than
the arm timeout. The endpoint must remain loopback HTTP.

Protected identities cannot compare merely because both arms say
`unavailable`. Missing digest reuses `model_digest_mismatch`; file, tokenizer,
template, and runtime identity use their narrow fixed reasons above. Empty
identity text remains a closed-schema `malformed_artifact`.

If available memory, including safely
reclaimable residency for the target model, cannot satisfy the estimate plus
the floor, no inference request is launched. A
`preflight_safety_abort` artifact run is retained and the paired command exits
nonzero.

During execution, the sampler checks the same floor on its bounded 250 ms
cadence. CPU uses `GetSystemTimes` on that cadence; pre-arm CPU uses one bounded
100 ms sample. GPU sampling remains at 1000 ms, each `nvidia-smi` subprocess is
bounded to 4 seconds, and no more than 256 system samples are retained per arm.
A crossing closes the streaming response first, then uses the existing
bounded unload/poll controls. The run is recorded as `safety_abort`, completed
evidence is retained, remaining arms are stopped, and comparison metrics are
withheld.

The request timeout is a hard per-arm wall-clock deadline backed by one
watchdog, not an inactivity timeout. It closes a stream even while chunks keep
arriving, records `timeout`, performs bounded cleanup, and joins timer/sampler
threads.

The explicit cancellation probe is not run inside every normal arm. When
enabled, one appended run per arm records `request_to_cancel_ms`,
`client_stream_closed_ms`, nullable `runtime_idle_ms` (only after `/api/ps`
confirms the model is absent), `process_completion_ms`, final state, resource
release, and runtime responsiveness. Cancellation runs are excluded from
performance aggregates. No field calls client-stream closure a runtime
acknowledgement.

## Synthetic validation

Deterministic local tests cover:

- schema-v2 accept/reject and schema-v1 compatibility;
- balanced A/B then B/A order;
- all protected-field mismatches;
- generated-token shortening and prompt truncation;
- explicit unavailable GPU evidence;
- estimator known/unknown cases and pre-launch low-RAM/unknown-state aborts;
- hostile zero/boolean/fractional/negative metadata rejection before execution;
- execution-control rejection before inventory, probes, unload, or execution;
- unavailable and empty protected-identity rejection;
- bounded cancellation on a sampled floor crossing;
- honest cancellation milestone reporting;
- hard deadline against an endlessly chunking loopback response;
- duplicate, cherry-picked, and unbalanced paired-design rejection;
- ambient drift reporting with optional policy enforcement;
- malformed-input isolation and privacy-safe CLI output;
- valid-pair-only aggregation;
- separate cold and warm results; and
- absence of hardcoded comparison thresholds.

Focused result: **146 passed** across the legacy and paired benchmark test
modules, including **82 paired-test cases**.

## Validation matrix

- `python -m pytest python\tests`: **777 passed, 8 skipped**, 1 existing
  Pillow decompression-size warning; 785 collected.
- `npm run test:backend-status`: `backend-status-tests-ok`.
- `npm run test:progress`: `chat-progress-tests-ok`.
- `npm run test:readiness`: readiness row mapping tests passed.
- `npm run test:jobs-ui`: **99 assertions** passed.
- `npm run build:frontend`: 1,614 modules transformed; build passed with the
  existing large-chunk advisory.
- `cargo check --manifest-path src-tauri\Cargo.toml`: passed.
- `cargo test --manifest-path src-tauri\Cargo.toml`: **24 passed, 4 ignored**
  helper-process fixtures, 0 failed.
- `git diff --check`: passed.

The isolated worktree initially lacked locked Node dependencies. `npm ci`
installed them locally without changing package manifests; the requested Node
matrix then passed. The package audit reported one moderate and one high
advisory. No out-of-scope forced dependency upgrade was applied.

## Models found locally

Inventory was read from the installed Ollama 0.32.1 runtime. No model was
downloaded, moved, converted, or modified.

| Role | Installed identity | Digest | Size | Disposition |
| --- | --- | --- | ---: | --- |
| Fast-floor control | `llama3.2:1b` Q8_0, 1.2B dense | `baf6a787fdffd633537aa2eb51cfd54cb93ff08e28040095462bb63daf552878` | 1,321,098,329 bytes | present |
| Comfortable control | `llama3.2:latest` Q4_K_M, 3.2B dense | `a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72` | 2,019,393,189 bytes | present |
| Distinct borderline ~4B dense | none | — | — | absent; no download permitted |
| Uplift target | `qwen3:8b` Q4_K_M, 8.2B dense | `500a1f067a9f782620b40bee6f7b0c89e17ae61f686b92c24933e4ca4b2b8b41` | 5,225,388,164 bytes | present |

Other installed models were inventory-only and were not assigned benchmark
roles: `qwen3-vl:2b`, `nemotron-nano-chat:8b`, and
`nomic-embed-text:latest`.

## Real control-model evidence

No control-model inference was launched. At the final preflight sample:

- total physical RAM: 16,556,589,056 bytes;
- available physical RAM: 1,597,263,872 bytes;
- reviewed safety floor: 1,986,790,686 bytes; and
- estimated `llama3.2:latest` requirement at context 4096:
  3,118,300,837 bytes before the safety floor.

Available RAM was already below the safety floor itself. Starting the model
would have violated the reviewed policy, so the real control comparison is
`blocked`, not a no-uplift result.

Server-level flash-attention or KV-cache experiments are additionally blocked:
the repository contains no reviewed isolated Ollama benchmark-server mechanism
that uses a separate port and process-local environment. The ordinary Ollama
service was not stopped or reconfigured.

## Real borderline-model evidence

No distinct approximately 4B dense borderline model is installed. Downloads
are prohibited for this phase. This measurement is `blocked` by model absence.

## Real uplift-target evidence

No `qwen3:8b` inference was launched. Under the same memory sample, its
estimated requirement at context 4096 was 6,458,513,540 bytes before the
safety floor. The arm was rejected by preflight rather than forcing memory
pressure. This measurement is `blocked`, not a no-uplift result.

## No-uplift results

None. No valid real pair completed, so there is no performance result to
interpret as uplift or no uplift.

## Invalid comparisons

None from real inference, because no real arm was launched. Synthetic invalid
pairs exist only as validator and comparison tests and do not represent model
performance.

## Still-proposed policy

Promotion thresholds, per-tier latency classes, acceptable quality tradeoffs,
and planner-visible defaults remain proposed in
`HARDWARE_RELATIVE_MODEL_UPLIFT_STRATEGY.md`. Fable and the maintainer retain
authority over those decisions. The comparison layer deliberately reports
measurements and comparability only.

## Next-stage blockers

1. Available RAM must recover enough for the target model's reviewed fit
   estimate plus the existing floor before any real arm can launch.
2. A real borderline experiment requires an already-installed suitable model
   or a separately approved download in a later phase.
3. Server-level Ollama optimization measurements require a separately reviewed
   isolated benchmark-server mechanism; this PR does not build a runtime
   manager.
4. Promotion still requires human-approved thresholds and multi-machine
   evidence. No P3-only result may become a universal default.
