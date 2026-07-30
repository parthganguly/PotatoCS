# Colibrì Stage 2 — reviewed converted-model registry and closed token proof

Branch `research/colibri-stage2-real-token`, based on
`f5bdf99ec1fe3f3b7141897e73a9d3746798c4fd`.

This unit promotes the independently disk-attested converted OLMoE artifact
set into the reviewed registry, binds it immutably to every identity the real
one-token run depends on, and closes the evidence boundary for the run that
has **not yet been performed**.

**No real `olmoe.exe` was launched. No token was generated. Nothing under
`D:\Colibri` was read, modified, moved, deleted, or hashed by this work. No
download occurred. Nothing was merged or marked ready.**

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

### 3. The closed evidence boundary (`OneTokenRunResult`)

The result carries only closed structured fields:

- `identities`: model repository/revision, Colibrì commit, engine SHA-256,
  converter kind and SHA-256, config SHA-256, all three shard SHA-256s,
  reference SHA-256, cap, bits. Hashes and pinned identifiers only.
- `expected_token_id` and `generated_token_id`. The engine never prints a
  token id — it prints its own comparison against the reviewed reference —
  so the generated id is reported only after a confirmed `1/1` against a
  reference whose digest was verified to encode exactly one expected id.
- `exit_category`: closed vocabulary — `clean_exit`, `nonzero_exit`,
  `timed_out`, `not_observed`.
- `latency.startup_latency_ms`: resume → first observed output byte. The
  engine's only reviewed output is its match line, emitted after loading the
  model and generating the token, so this is a combined
  model-load-plus-generation figure and an **upper bound** on model load
  alone. No finer decomposition is observable from this engine's output and
  none is invented.
- `latency.one_token_latency_ms`: resume → observed process exit;
  end-to-end for the run that produces and verifies exactly one token.
- `peak_tree_memory_bytes`: whole-tree peak from the owning Job Object,
  reusing the one reviewed `QueryInformationJobObject` path already used for
  bounded conversion.
- `cleanup_complete`, `orphan_free`, `reference_removed`.
- `evidence_schema_version`, `evidence_sha256`.

Every optional measurement carries its own `*_state` (`measured` /
`unavailable`), so a missing number is `None` plus an explicit state and can
never be misread as `0 ms` or `0 bytes`.

Never retained: stdout/stderr content (bounded stream bytes are parsed in
memory and discarded), any filesystem path, the child environment, the
username, or the prompt token sequence.

`evidence_sha256` now binds the model revision, Colibrì commit, engine,
converter kind and digest, config, all three shards, the reference, cap,
bits, and the expected token id, alongside the exact `1/1` line — so it
changes if any pinned identity or argument changes.

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
- Orphan evidence is now distinct from cleanup uncertainty: a surviving
  descendant raises `orphan_detected`; a probe that could not answer raises
  `cleanup_failed`. `orphan_free` is asserted only when the probe positively
  answered "none". Both fail closed.

## Files changed

| File | Change |
| --- | --- |
| `python/odysseus_desktop_backend/services/colibri_stage2_common.py` | manifest schema → v2; token-run evidence schema, `measured`/`unavailable` states, closed exit-category vocabulary |
| `python/odysseus_desktop_backend/services/colibri_stage2_manifest.py` | converter kind + full converter identity binding; cap/bits/prompt/expected-token fields; the one reviewed registry entry; `require_wellformed_registry` |
| `python/odysseus_desktop_backend/services/colibri_stage2_runner.py` | `build_token_command`; reference/token-contract verification; full directory-contents check; identity, latency, exit-category and whole-tree-memory evidence; orphan/cleanup separation |
| `python/odysseus_desktop_backend/services/colibri_stage2_conversion.py` | public `peak_job_memory_bytes` so the runner reuses the reviewed job-memory query |
| `python/tests/test_colibri_stage2_registry.py` | new — the reviewed entry, reference determinism, closed boundary, cannot authorize anything else |
| `python/tests/test_colibri_stage2_manifest.py` | converter-kind and token-contract coverage; registry-shape coverage |
| `python/tests/test_colibri_stage2_runner.py` | command grammar, directory contents, ledger non-authority, token oracle, evidence, latency, memory, timeout/tree-cleanup, privacy |
| `projects/odysseus/COLIBRI_STAGE2_REAL_TOKEN_RESULT.md` | this document |

## Proposed first real token run

**Not executed. Requires explicit human approval and an interactive
terminal.**

The native command the engine will execute is:

    olmoe.exe 8 8 <private-session>\olmoe-stage2-one-token-ref.json

with `SNAP=D:\Colibri\converted` and `TEMP`/`TMP` pointing at that same
private session directory. The reference file is written by this process from
embedded token arrays immediately before launch and deleted afterwards, so
its path cannot be supplied or reused.

The operator command that produces it, run from `python/` in an interactive
terminal, is:

```
python -c "from pathlib import Path; from odysseus_desktop_backend.runtime_bench.isolated_server import WindowsLifecycleApi; from odysseus_desktop_backend.services.colibri_stage2_runner import run_one_token_proof; print(run_one_token_proof(olmoe_exe=Path(r'<reviewed-engine-dir>\olmoe.exe'), converted_model_dir=Path(r'D:\Colibri\converted'), api=WindowsLifecycleApi(), approved=True))"
```

`<reviewed-engine-dir>` is the directory holding the `olmoe.exe` whose
SHA-256 is `d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d`.
Only those two paths are supplied; every identity, argument, and expectation
comes from the reviewed registry entry. The run fails closed before creating
a process if the engine, config, or any shard does not match byte-for-byte,
if the directory holds anything unexpected, if any path is a reparse point,
or if `approved=True` is not paired with an interactive stdin/stdout.
