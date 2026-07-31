# Colibrì Stage 2 — reviewed converted-model registry and closed token proof

Branch `research/colibri-stage2-real-token`, based on
`f5bdf99ec1fe3f3b7141897e73a9d3746798c4fd`.

This unit promotes the independently disk-attested converted OLMoE artifact
set into the reviewed registry, binds it immutably to every identity the real
one-token run depends on, and closes the evidence boundary for the run that
has **not yet been performed**.

**No token has been generated and `olmoe.exe` has never been launched.** One
human-approved invocation has been made; it was rejected pre-launch, before
any native process existed — see "First human-approved attempt" below. That
attempt did read and SHA-256 hash the converted artifacts, solely to verify
their identity against the reviewed registry entry; it did not modify, move,
delete, or reconvert them, and loaded no model for inference. No download
occurred. Nothing was merged or marked ready.

*Development of this branch itself never accessed `D:\Colibri` at all: every
test runs against synthetic fixtures under `tmp_path`.*

## Root design

Three layers, each with exactly one job.

### 1. The reviewed record (`colibri_stage2_manifest.py`)

`REVIEWED_OLMOE_MODEL_REGISTRY` now holds exactly one
`OlmoeModelManifest`, keyed by the pinned model revision. Every value is a
literal in reviewed source. One entry binds, immutably and at construction
time:

| Bound to | Value |
| --- | --- |
| model repository | `allenai/OLMoE-1B-7B-0125-Instruct` |
| model revision | `b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e` |
| licence | `Apache-2.0` |
| Colibrì commit | `72d3d37231e922a6fa9afca16e08fa45842d5eb4` |
| engine | `olmoe.exe`, 704,275 bytes, `d7beaf6f…b528d` |
| converter kind | `bounded` |
| converter | `colibri_stage2_bounded_convert.py`, 24,033 bytes, `6f8145fc…2266b` |
| config | `config.json`, 828 bytes, `272998dd…d6ce` |
| shard 1 | `model-00001-of-00003.safetensors`, 2,709,555,648 bytes, `3b9ad7f9…61b2` |
| shard 2 | `model-00002-of-00003.safetensors`, 2,606,561,600 bytes, `8f686150…416f` |
| shard 3 | `model-00003-of-00003.safetensors`, 2,097,277,536 bytes, `06aa55f9…4eaf` |
| reference | `olmoe-stage2-one-token-ref.json`, 78 bytes, `eb27ccf4…8b1c` |
| cap / bits | `8` / `8` |
| prompt ids | `(510, 5347, 273, 6181, 310)` |
| expected token | `7785` |

Two changes to the PR #41 manifest were necessary and are the whole
adaptation:

- **Converter binding is now kind-driven.** PR #41 required
  `converter_source_sha256` to equal the *pinned upstream script*'s digest.
  The artifacts were actually produced by the reviewed **bounded** converter,
  so that check could not have accepted the truth. A manifest now states a
  `converter_kind` — the only converter selector it may state — and the
  basename, size, and digest are then dictated by the closed
  `CONVERTER_KIND -> reviewed identity` mapping in `colibri_stage2_common`.
  A correct digest paired with the other kind is rejected; so is any kind
  outside the two reviewed ones. Where a reviewed identity carries its own
  `colibri_commit` (only the pinned script does), it must still agree with
  the manifest.
- **The token contract moved into the record.** `cap_argument`,
  `bits_argument`, `prompt_token_ids`, and `expected_generated_token_id` are
  now manifest fields validated against the pinned constants. This is what
  makes the token oracle closed: there is no expected-token parameter
  anywhere in Stage 2.

Manifest schema version bumped to `colibri-stage2-olmoe-manifest-v2`.

`require_wellformed_registry()` additionally proves the registry *as a
whole* has not been substituted: an immutable mapping, at most one entry,
each value an `OlmoeModelManifest`, each key equal to both its own
`model_revision` and the pinned revision. It runs inside
`require_reviewed_manifest` **after** a lookup has already matched, so a
single entry filed under a foreign key still fails closed as
`reviewed_model_manifest_unavailable` (nothing was authorized) rather than
being reclassified, while a widened registry is rejected as
`malformed_registry` even when the pinned lookup would have succeeded.

`conversion_dependency_versions` is deliberately **empty**. The attested
facts for this artifact set are its sizes and digests; no python / torch /
safetensors version was independently reviewed alongside them. Recording an
unreviewed version string in an immutable registry entry would assert
provenance nobody checked, which is worse than asserting none — the artifact
digests are the authority either way.

### 2. The command grammar (`colibri_stage2_runner.build_token_command`)

    olmoe.exe <cap> <bits> <derived-reference-path>

Exactly three arguments. `cap` and `bits` are read from the reviewed
registry entry, never from a module constant and never from a caller. The
reference path points at a file this process derived, in a private per-run
session directory, from embedded token arrays — never a caller-supplied path
and never a tokenizer. A foreign executable basename or reference basename
is rejected. There is no flag, prompt string, model path, or tokenizer on
the command line; the engine locates the converted model through the closed
`SNAP` environment key.

### 3. The independent token oracle (`colibri_stage2_engine_output.py`)

The dialect is transcribed from the reviewed `c/olmoe.c` at pinned commit
`72d3d372`, not inferred from a sample run. Its `main` emits:

    == Streaming C engine, cache = <cap> experts/layer, experts @ <bits>-bit ==
    resident weights loaded in <seconds>s | RSS after load: <gb> GB

    Reference: <token ids>
    C engine : <generated token ids>
    Matching tokens: <matched>/<expected>

    PEAK RSS: <gb> GB
    Expert cache hit rate: <pct>%  (hit=<n> miss=<n>)
    Speed: <rate> tok/s (<seconds>s for <n> tokens)

Output is decoded **strictly** (UTF-8, no replacement characters) and each of
the five required lines must appear exactly once and be exactly well-formed.
The `resident weights` line is matched **complete**, including its
`| RSS after load: <gb> GB` half — the source prints one line, so a pattern
stopping at the seconds value would have rejected every real successful run.
The RSS value is validated (finite, non-negative, bounded) and recorded as
engine-reported evidence. The banner, `PEAK RSS`, and cache-hit lines are
tolerated and parsed for nothing: the authoritative peak-memory figure comes
from the owning Job Object, not the engine's self-report.

`Reference:` and `C engine :` are emitted as `printf("%d ")` per token, so
both carry a trailing space and are terminated by the next line's leading
newline. Both shapes are handled, and the fixtures reproduce them exactly.

The comparison is ours, not the engine's:

1. the `Reference:` line must state exactly the one reviewed expected token —
   if the engine compared against something else, its match count is
   meaningless;
2. the `C engine :` line must carry exactly one integer;
3. that integer is compared to `manifest.expected_generated_token_id`. **This
   is the oracle.**
4. the engine's own `Matching tokens` line must then agree both with the `1/1`
   the contract requires *and* with the count recomputed independently from
   the two token lines. An engine claiming `1/1` while its own token lines
   disagree is `output_internally_inconsistent` and rejected.

`generated_token_id` is retained **as soon as it is parsed and before any
comparison**, so a wrong-token run reports the actual wrong integer — the
observation is the point of having run it. It is never assigned from the
expected value, and is `None` only when no single generated token could be
safely parsed. `matched_count` and `engine_reported_expected_count` are
likewise retained on failed runs wherever they parsed; `contract_expected_count`
is always `1` and never engine-supplied, so a run where the engine disagreed
about how many tokens were expected shows both numbers rather than one
silently standing in for the other. `evidence_sha256` stays `null` unless the
proof actually passed.

Rejected with distinct categories: `output_decode_failed`,
`malformed_output` (a missing token line), `duplicate_output_line`,
`duplicate_match_line`, `reference_line_mismatch`,
`generated_token_count_unexpected`, `token_identity_mismatch`,
`output_internally_inconsistent`, `match_count_mismatch`, and
`timing_evidence_invalid`.

Raw stdout/stderr stay bounded to 4096 bytes each, are parsed in memory, and
are then discarded — only the small parsed record of integers and floats
survives.

### 4. Latency evidence, correctly attributed

The pinned engine prints its banner **before** `model_init`, so
resume-to-first-byte is not model-load latency and is not a reliable upper
bound on it. The previous `startup_latency_ms` field is gone. What is recorded
now:

| Field | Provenance |
| --- | --- |
| `model_load_latency_ms` | **engine-reported**, from `resident weights loaded in <s>s` |
| `generation_latency_ms` | **engine-reported**, from the `(<s>s for 1 tokens)` field of `Speed:` |
| `end_to_end_latency_ms` | **independently measured**, resume → observed process exit |
| `first_output_latency_ms` | **independently measured**, resume → first output byte. Named only that; no model-load or combined-load claim is made for it anywhere. |

Engine timings must be finite, non-negative, and within bounds
(`MAX_ENGINE_REPORTED_SECONDS`, `MAX_ENGINE_REPORTED_RATE`), and each timing
line must appear exactly once — otherwise `timing_evidence_invalid`. Values
are rejected, never clamped: a clamped number would look like a measurement.

These lines are parsed from the **fully drained** stdout after exit, never
used as a live milestone. That distinction matters because this engine may
buffer stdout under pipe redirection (recorded in the PR #41 result), which
makes the load line unusable as a streaming checkpoint but perfectly usable as
after-the-fact evidence.

### 5. Whole-Job-Object emptiness proof (`colibri_stage2_job_probe.py`)

A parent-PID descendant snapshot is not sufficient. It is an
absence-of-evidence argument over a parentage link that goes stale once the
root exits: Windows does not clear a child's recorded parent PID when the
parent dies, so the link can be recycled onto an unrelated live process, and
reachability from the root depends on which intermediate processes happen to
still be alive when the snapshot is taken.

`job_active_process_count` queries
`JOBOBJECT_BASIC_ACCOUNTING_INFORMATION.ActiveProcesses` — a positive count,
independent of parentage — and is *checked*: the returned byte count is
verified against the structure size, because a partially-filled structure
would hand back a zeroed `ActiveProcesses` that reads exactly like proof of
emptiness. It returns `None` for "not known", never zero.

`terminate_and_prove_job_empty` then, on **every** teardown path:

1. terminates the complete job (falling back to the lone process only when
   there is no job to terminate);
2. waits for the root process under the absolute cleanup deadline;
3. **polls until the job reports zero members.** Unavailable, or never zero
   before the deadline, is `cleanup_failed` — never an assumed zero;
4. enumerates descendants as *supplementary* evidence only.

`orphan_free` is asserted only after that zero-member proof. The job handle is
closed last — after both the peak-memory measurement and the emptiness proof,
since a closed handle can answer neither query.

### 6. The closed evidence boundary (`OneTokenRunResult`)

Closed structured fields only: `identities` (model repository/revision,
Colibrì commit, engine SHA-256, converter kind and SHA-256, config SHA-256,
all three shard SHA-256s, reference SHA-256, cap, bits), `expected_token_id`,
the parsed `generated_token_id`, `matched_count`,
`contract_expected_count`, `engine_reported_expected_count`, `exit_category`
(`clean_exit` / `nonzero_exit` / `timed_out` / `not_observed`), the four
latencies above, `peak_tree_memory_bytes`, and the cleanup proofs
(`cleanup_complete`, `job_empty_proven`, `job_member_count`,
`root_exit_confirmed`, `descendant_count`, `orphan_free`,
`reference_removed`), plus `evidence_schema_version`, `evidence_sha256`, and
numeric-only `failure_metadata`.

Every optional measurement carries its own `*_state` (`measured` /
`unavailable`), so a missing number is `None` plus an explicit state and can
never be misread as `0 ms` or `0 bytes`.

Never retained: stdout/stderr content, any filesystem path, the child
environment, the username, or the prompt token sequence.

`evidence_sha256` binds the model revision, Colibrì commit, engine, converter
kind and digest, config, all three shards, the reference, cap, bits, the
expected token id, **and the token the engine actually generated** — so it
cannot be reproduced by a run that failed to produce the reviewed token.

### 7. Closed attempt capture and CLI (`colibri_stage2_token_cli.py`)

`attempt_one_token_proof` never raises for a closed operational failure: it
returns a result with `ok=False`, the closed failure category, and every
measurement and cleanup proof actually obtained. `run_one_token_proof` is the
raising form built on top of it, so existing callers are unchanged.
Precondition failures that occur before a process exists still raise — there
is no attempt to describe, nothing was measured, and no cleanup was owed.

The CLI emits **exactly one closed JSON document** on stdout, on success and
on failure, and never a Python traceback:

- requires interactive stdin/stdout and explicit `--approve`;
- exit `0` only for a verified token; `1` for a failed attempt; `2` for a
  rejection before launch; `3` for anything unexpected;
- uses a non-printing `ArgumentParser` subclass: every `error`, `exit`,
  `print_usage`, `print_help`, and `_print_message` path is overridden, so
  argparse can never put a usage line, an error string, or the offending
  argument (a local path) onto either stream. `--help` is deliberately
  unsupported for the same reason and is rejected like any other unrecognised
  argument, with one closed document. Verified as a real subprocess: **0 bytes
  on stderr**, one JSON document on stdout;
- classifies an unexpected internal exception as `unexpected_internal_failure`,
  never as `malformed_output` — blaming the engine's output for our own defect
  would be false and would imply output was parsed when it may never have been
  reached. In that case nothing is claimed: `cleanup_complete` and
  `pre_launch_rejection` are `null` rather than `true`, and no job-empty or
  orphan proof is asserted. The exception object is discarded unread, since its
  message could carry a local path or an environment value;
- records state/category, reviewed identities, expected and parsed generated
  token, match count, exit category and code, model-load and generation
  latency, end-to-end latency, peak whole-tree memory, cleanup completeness,
  the Job Object zero-member proof, the orphan proof, and reference removal;
- takes exactly three options — `--engine`, `--converted-model-dir`, and
  `--approve`. Every expected identity still comes solely from the registry.

### Trust boundary

- No parameter anywhere accepts an expected hash, size, model revision,
  engine identity, converter identity, token oracle, prompt, tokenizer,
  reference path, cap, bits, or substitute registry. Asserted structurally
  by `test_no_stage2_entry_point_accepts_an_identity_or_registry_override`.
- `require_reviewed_manifest` takes exactly two parameters: the model
  revision (a lookup key that can only match the one pinned entry or fail
  closed) and the Colibrì commit.
- Every directory is proven an ordinary absolute directory with no
  symlink/junction/reparse point anywhere in its chain to the drive anchor,
  before any file is opened or any process is created. Every artifact path
  is proven a direct, non-reparse, regular-file child.
- The converted directory's **full** direct-child listing is now checked,
  not only `*.safetensors`: a stray subdirectory, extra file, or leftover is
  rejected as `unknown_converted_shard`.
- **The resume ledger is tolerated but never authority.** It legitimately
  sits beside the artifacts as a bounded-converter by-product, so its
  presence does not fail the run — but it is never opened or parsed, and a
  test proves a ledger full of contradictory nonsense changes nothing.
- Orphan evidence is distinct from cleanup uncertainty: a surviving
  descendant raises `orphan_detected`; an unavailable Job Object member count,
  a count that never reaches zero, or a probe that could not answer raises
  `cleanup_failed`. `orphan_free` is asserted only after the Job Object itself
  positively reported zero members. Both fail closed.
- The CLI supplies only two paths. It has no flag for any identity, and a
  test asserts the attempt function is never called with one.

## First human-approved attempt: pre-launch rejection

One real invocation has been made. **It did not run the engine and did not
generate a token.** There is still no successful token claim.

| Observed | Value |
| --- | --- |
| category | `reference_write_failed` |
| `pre_launch_rejection` | true |
| process exit | `not_observed` |
| CLI exit status | 2 |
| native process created | **none** |
| evidence schema of that capture | `colibri-stage2-olmoe-token-evidence-v2` |

### What that attempt did and did not touch

The rejection happened at reference-session creation, which is *after*
`_verify_preconditions`. So the converted artifacts were genuinely read:

- **the config and all three converted shards were read in full and SHA-256
  hashed**, solely to verify their identity against the reviewed registry
  entry. The reviewed engine binary was read and hashed for the same reason;
- they were **not** modified, moved, deleted, or reconverted;
- **no model file was loaded for inference** — identity hashing reads bytes,
  it does not initialise a model;
- **`olmoe.exe` was never launched**; no native process was created;
- **no token was generated.**

An earlier revision of this document said nothing under `D:\Colibri` was read
or hashed during that attempt. That was wrong: reading and hashing every
artifact is precisely what the pre-launch identity gate does, and roughly
7.4 GB of shard data was read to do it. The claim is corrected here rather
than quietly dropped.

### Cause: a Windows 8.3 short-name alias

`TEMP` and `TMP` pointed at `C:\Users\<8.3-ALIAS>\AppData\Local\Temp`, where
`<8.3-ALIAS>` is the volume's generated short name for a longer account
directory name — so that spelling canonicalizes to a *different* long-form
path. (The alias and the long form are both local account names and are not
reproduced here, for the same privacy reason the engine path is not.)

`require_ordinary_directory` compares the original lexical path against its
resolution precisely to catch a path that is not what it says it is, and it
correctly rejected the mismatch. **The validation was right.** The defect was
where it sat.

### Defect: the session was created outside the cleanup-owned block

`attempt_one_token_proof` created the private session directory, then
validated it, then built the child environment — all *before* the
`try`/`finally` that owns cleanup:

```python
session_dir = create_private_reference_session(...)   # side effect
require_ordinary_directory(session_dir, ...)          # raises here
environment = ...
try:
    ...
finally:
    ...  # never reached
```

So the validation failure escaped past every teardown path, leaving one empty
`odysseus-colibri-stage2-ref-*` directory under the long-form user Temp
directory. Worse, the CLI then classified the escaped `ColibriStage2Failure`
as a side-effect-free pre-launch rejection and emitted `cleanup_complete:
true` — cleanup was owed and had not occurred.

**The leaked directory is empty, has been independently identified, and is
left for guarded manual cleanup. This correction does not delete it.**

### Correction

1. **Deterministic session parent.** When no explicit parent is supplied, the
   private-session parent is derived as the ordinary sibling
   `<converted-model-dir>/../runtime-temp` and validated with the same full
   lexical-ancestor, ordinary-directory and non-reparse guarantees before
   anything is created inside it. It must already exist; it is never
   provisioned silently. `TEMP`/`TMP` are never consulted as authority, and
   there is deliberately no CLI option — this is a derived location, not a
   configurable one. Deriving it from an already-canonical, already-proven
   path removes the whole class of alias mismatch rather than trying to
   normalise every spelling a shell might supply. The child's `TEMP`/`TMP`
   still point only at the created private session.

2. **Cleanup owned from the first side effect.** `session_dir` starts absent;
   session creation, validation, environment construction, reference writing
   and all subsequent work now sit inside one cleanup-owned structure, and the
   directory is only ever read through an `is not None` guard. Bounded removal
   is attempted on every exit path once the directory may exist — session
   validation failure, environment failure, reference-write failure, reference
   identity failure, pre-launch failure, and native execution failure. An
   unexpected exception after session creation is caught too: escaping would
   both leak the directory and let the caller misreport the run.

3. **Truthful classification.** The result and the CLI document now carry
   `session_created` and `reference_session_removed`, distinct from
   `reference_removed` (a session can be created and removed without a
   reference file ever being written, which is exactly what happened).
   `cleanup_complete` is false whenever removal failed and is never
   manufactured from "no process was launched". A rejection document states
   `session_created: false` only where that is established, and `null` for an
   unexpected internal failure that could have occurred anywhere.

4. **Evidence schema bumped to
   `colibri-stage2-olmoe-token-evidence-v3`.** Those two new fields change the
   record shape, so reusing the v2 identifier would make it ambiguous. The
   first attempt's capture was genuinely a v2 document and stays labelled v2 —
   it is never relabelled to imply it carried session-lifecycle evidence it
   did not have.

A Windows regression drives the real thing: a long-named directory whose 8.3
alias Windows itself generates, asserted to resolve to a different spelling
and to be genuinely rejected by `require_ordinary_directory`, with
`TEMP`/`TMP` pointed at it. The run ignores the environment, selects the
`runtime-temp` sibling, leaks nothing under either spelling, and — on a forced
post-create validation failure — creates no process while still removing the
directory it made.


## Files changed

| File | Change |
| --- | --- |
| `python/odysseus_desktop_backend/services/colibri_stage2_common.py` | manifest schema → v2; token-run evidence schema, `measured`/`unavailable` states, closed exit-category vocabulary |
| `python/odysseus_desktop_backend/services/colibri_stage2_manifest.py` | converter kind + full converter identity binding; cap/bits/prompt/expected-token fields; the one reviewed registry entry; `require_wellformed_registry` |
| `python/odysseus_desktop_backend/services/colibri_stage2_runner.py` | `build_token_command`; reference/token-contract verification; full directory-contents check; strict output parsing wired to the independent oracle; re-attributed latency evidence; whole-Job-Object teardown proof; `attempt_one_token_proof` / `run_one_token_proof` split |
| `python/odysseus_desktop_backend/services/colibri_stage2_engine_output.py` | new — strict pinned-dialect parser and the independent token comparison |
| `python/odysseus_desktop_backend/services/colibri_stage2_job_probe.py` | new — checked Job Object active-process-count query and the bounded zero-member teardown proof |
| `python/odysseus_desktop_backend/services/colibri_stage2_token_cli.py` | new — closed developer CLI emitting one JSON document, never a traceback |
| `python/odysseus_desktop_backend/services/colibri_stage2_conversion.py` | public `peak_job_memory_bytes` so the runner reuses the reviewed job-memory query |
| `python/tests/test_colibri_stage2_reference_session.py` | new — deterministic session parent, real 8.3-alias regression, cleanup ownership on every exit path |
| `python/tests/test_colibri_stage2_registry.py` | new — the reviewed entry, reference determinism, closed boundary, cannot authorize anything else |
| `python/tests/test_colibri_stage2_engine_output.py` | new — dialect parsing, independent oracle, every rejection shape, timing bounds |
| `python/tests/test_colibri_stage2_token_cli.py` | new — exit codes, closed document, privacy, no traceback |
| `python/tests/test_colibri_stage2_job_teardown.py` | new — proof-loop unit tests plus bounded Windows integration with a real grandchild writer |
| `python/tests/test_colibri_stage2_manifest.py` | converter-kind and token-contract coverage; registry-shape coverage |
| `python/tests/test_colibri_stage2_runner.py` | real engine dialect fixtures; command grammar, directory contents, ledger non-authority, independent oracle, evidence, latency attribution, memory, Job Object proof, timeout/tree-cleanup, privacy |
| `projects/odysseus/COLIBRI_STAGE2_REAL_TOKEN_RESULT.md` | this document |

## Proposed next real token run

**Never yet executed successfully. The one attempt so far was rejected
pre-launch (above). Requires explicit human approval and an interactive
terminal.**

The native command the engine will execute is:

    olmoe.exe 8 8 <private-session>\olmoe-stage2-one-token-ref.json

with `SNAP=D:\Colibri\converted`, and `TEMP`/`TMP` pointing at that same
private session directory — which now lives under the deterministic
`D:\Colibri\runtime-temp` sibling rather than anywhere derived from the
caller's `TEMP` spelling. The reference file is written by this process from
embedded token arrays immediately before launch and deleted afterwards, so
its path cannot be supplied or reused.

`D:\Colibri\runtime-temp` must already exist and be an ordinary, non-reparse
directory; it is validated, never created, by this code.

The exact operator command, run from `python/` in an interactive terminal, is:

```
python -m odysseus_desktop_backend.services.colibri_stage2_token_cli --engine "<REVIEWED_ENGINE_PATH>" --converted-model-dir "D:\Colibri\converted" --approve
```

`<REVIEWED_ENGINE_PATH>` is a **privacy placeholder, not an open question.**
The reviewed engine's location was independently resolved before the first
attempt: two byte-identical reviewed `olmoe.exe` copies exist from the
two-build determinism check, the first attempted command used the build-a
copy, and its exact size (704,275 bytes) and SHA-256
(`d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d`) matched
the registry entry.

The absolute local path is deliberately omitted from tracked evidence because
it contains a local account name, and this repository's Stage 2 evidence rule
is that no username or local path is ever persisted. Substitute the resolved
path at the prompt.

Nothing about the command's safety depends on that path being right: an engine
at the wrong location, or a different engine at the right one, is rejected on
digest before any process is created.

Only those two paths are supplied. Every identity, argument, and expectation
comes from the reviewed registry entry. The command:

- refuses to run without `--approve` and an interactive stdin/stdout;
- fails closed before creating a process if the engine, config, or any shard
  does not match byte-for-byte, if the converted directory holds anything
  unexpected, or if any path is a reparse point;
- prints exactly one closed JSON document — success or failure — and never a
  traceback, a raw engine stream, a local path, an environment value, a
  username, or the prompt;
- exits `0` only for a token parsed from the engine's own output and
  independently verified against the registry.
