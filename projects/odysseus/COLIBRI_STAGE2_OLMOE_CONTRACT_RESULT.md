# Colibrì Stage 2A — OLMoE Scaffold Contract & Result

Status: **scaffold only** — deterministic build/manifest/runner plumbing is
implemented and tested with synthetic fixtures; no download, no conversion,
and no real `olmoe.exe` launch has been performed. This document is not a
claim of real language generation.

Date: 2026-07-24 (corrected same day: four execution-blocker fixes below).

## Correction pass (four execution blockers)

This revision fixes four concrete execution blockers found before any real
run could be attempted, without changing the selected model, pinned
revisions, token oracle, cap/bits, provider behavior, Deep Local jobs, UI,
routing, Ollama, installer, or packaging:

1. **Native build process tree** (`scripts/verify-colibri-native-repro.ps1`):
   the build's stdout/stderr are now redirected and bounded (64 KiB);
   `SOURCE_DATE_EPOCH` is supplied only to the child process's own
   `ProcessStartInfo.EnvironmentVariables` block and the caller's actual
   PowerShell environment is never touched; a timeout or output overflow now
   kills the *complete* process tree (`Process.Kill($true)` where the
   overload exists, else `taskkill /PID /T /F`), waits bounded for the
   launcher to confirm exit, and verifies no descendant process remains;
   final output is one `ConvertTo-Json`-serialized object containing no
   paths or raw build output.
2. **Closed source manifest**: `REVIEWED_SOURCE_SHARD_SHA256` (a bare
   basename→hash dict) is replaced by `REVIEWED_SOURCE_SHARD_MANIFEST`, a
   `MappingProxyType` of immutable `SourceShardEntry` records (exact
   basename, exact positive size, lowercase SHA-256). Still empty in this
   commit — `require_reviewed_source_manifest` still fails closed with
   `source_model_manifest_unreviewed` until all four required source files
   have a real, reviewed entry.
3. **Path safety**: a new shared module,
   `colibri_stage2_path_safety.py` (`require_ordinary_directory`,
   `require_direct_child_path`), proves every directory touched by the
   conversion orchestrator and the real one-token runner is an ordinary,
   non-reparse directory all the way down to its drive/root anchor, and
   that every prospective file path is a direct, non-reparse child of an
   approved directory — checked before any filesystem write, network call,
   converter call, or process creation.
4. **Real approved conversion orchestrator**: the CLI no longer
   hard-codes `isolated_python_env_ready=False` /
   `dependencies_installed=False`. `_default_isolated_python_env_ready`
   and `_default_dependency_versions` perform real (but side-effect-free)
   detection. `run_approved_conversion` is the complete, real, executable
   sequence (download config once → per-shard download/convert/verify/
   move/delete, in order → move config into the final directory exactly
   once → build the conversion capture) with default real adapters
   (`PinnedRevisionFileDownloader`, `PinnedScriptConverter`). It remains
   structurally unreachable while `REVIEWED_SOURCE_SHARD_MANIFEST` is
   empty, via the same `source_model_manifest_unreviewed` gate as before.

The runner also gained: TEMP/TMP set to the run's own private reference
session directory (never the caller's general temp dir); a resource probe
invocation moved to before process/job handles are closed; a truthful
per-category mapping from PR #40's `IsolatedServerFailure` categories
instead of one blanket `process_create_failed`; and an `evidence_sha256`
now bound to the reviewed engine, config, all three shard identities, the
reference identity, and the exact `1/1` evidence line together.

`ALLOWED_CONVERSION_DEPENDENCY_NAMES` was trimmed to `{python, torch,
safetensors}` — Transformers is not a required conversion dependency and
is no longer listed as an allowed one.

## Decision

**GO** for `allenai/OLMoE-1B-7B-0125-Instruct`, immutable revision
`b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e`, license Apache-2.0.

## Pinned commits

- Colibrì upstream commit: `72d3d37231e922a6fa9afca16e08fa45842d5eb4`
- Build recipe: `SOURCE_DATE_EPOCH=1784223580 make olmoe.exe ARCH=x86-64-v3`
  (same toolchain, same pin as the reviewed `glm.exe`/`test_idot.exe` proof
  in PR #36)
- Model repository: `allenai/OLMoE-1B-7B-0125-Instruct`
- Model revision: `b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e`

## 0924 → 0125 deviation

The upstream OLMoE release history includes an earlier `0924` checkpoint.
This proof deliberately pins `0125-Instruct` instead: it is the reviewed,
instruction-tuned revision selected for the one-token proof, and every
manifest/runner/dry-run surface states this explicitly rather than silently
assuming the newest or the original release.

## Cap / bits

The native invocation is fixed at `olmoe.exe 8 8 <derived-ref-path>` — cap
`8`, bits `8`. Both are compile-time constants (`CAP_ARGUMENT`,
`BITS_ARGUMENT` in `colibri_stage2_common.py`), not caller-supplied values.

## Why no startup-line deadline is claimed

The unmodified C engine may buffer stdout when attached to a pipe, so the
short "resident weights loaded" line is not a reliable observable
milestone under `CreateProcess`+pipe redirection. The runner therefore uses
one absolute total deadline (900 seconds) covering setup, generation, and
draining — not a separate startup-line deadline — because the pinned
engine does not stream a token before completing generation.

## Source and converted hash capture sequence

Per shard, transactionally:

1. download one exact file from the immutable revision;
2. verify basename, exact size, and SHA-256;
3. run the unmodified pinned `convert_olmoe.py` with `--model`;
4. verify the expected converted output shard exists as a regular file;
5. hash and record it;
6. only then delete the corresponding source shard;
7. verify the deletion;
8. proceed to the next shard.

A failure at any point before step 5 retains the source shard untouched
(`colibri_stage2_conversion.run_shard_transaction`, tested in
`test_colibri_stage2_conversion.py`). An existing converted shard is never
overwritten. The resulting evidence is captured in a closed,
privacy-safe shape (`build_conversion_capture`) — basenames, sizes,
hashes, dependency versions, and elapsed times only, no paths, usernames,
environment values, or raw tool output — and is always stamped
`state: "unreviewed_conversion_capture"`. It cannot itself authorize a
real run: it has no field in common with `OlmoeModelManifest`'s pinned
identity fields, and constructing a manifest from a capture's fields fails.

## Current registry-empty state

`colibri_stage2_manifest.REVIEWED_OLMOE_MODEL_REGISTRY` is an immutable,
empty `MappingProxyType({})` in this commit, because the converted model
files a manifest would describe do not exist yet. The real one-token
runner (`colibri_stage2_runner.run_one_token_proof`) checks this registry
first, before opening any file or creating any process, and fails closed
with the fixed category `reviewed_model_manifest_unavailable` while it
stays empty. Similarly, `colibri_stage2_conversion.REVIEWED_SOURCE_SHARD_MANIFEST`
is empty, so any approved-mode download attempt — including the real
`run_approved_conversion` orchestrator — fails closed with
`source_model_manifest_unreviewed` before any network activity.

## No download or inference performed

This implementation commit did not download model files, install
packages, convert weights, execute `olmoe.exe`, or launch Ollama. Every
test uses synthetic fixtures and a fake `LifecycleApi`; no test touches
the network, the ordinary model store, or a real Windows process.

## Remaining finite sequence

A. Run the two-build `olmoe.exe` verifier (extended
   `scripts/verify-colibri-native-repro.ps1`) to produce the real
   deterministic engine hash.

B. A human approves dependency setup and the sequential, transactional
   download/verify/convert/delete sequence (Part 3), which requires a
   separately reviewed, non-empty `REVIEWED_SOURCE_SHARD_MANIFEST` (exact
   basename, exact size, and SHA-256 for `config.json` and all three
   shards). Once that manifest lands, `run_approved_conversion` is the
   complete real path — no second implementation is required.

C. Review the resulting conversion capture (privacy-safe, hashes/sizes
   only).

D. Add one tiny, separately reviewed commit that populates exactly one
   `OlmoeModelManifest` entry in `REVIEWED_OLMOE_MODEL_REGISTRY`, keyed by
   the pinned model revision.

E. Run exactly one real token via `run_one_token_proof`, requiring
   explicit interactive approval, and confirm `Matching tokens: 1/1` for
   token id `7785`.

F. Integrate or freeze Colibrì based on the result.

## Remaining concerns

- **Resource evidence (CPU/memory/disk-read) is not implemented.** The
  runner's `resource_probe` hook defaults to `None`, so `ResourceEvidence`
  always reports `state="unavailable"` in this commit. Wiring a bounded
  `QueryInformationJobObject`/`GetProcessTimes` probe is deferred rather
  than adding new, unreviewed ctypes surface beyond the PR #40 primitives
  this task asked to reuse.
- **Converted shard naming is not pinned.** `OlmoeModelManifest` requires
  exactly three converted shard basenames but does not assume a specific
  naming convention, since the task did not specify one; the reviewing
  commit (step D above) must supply real names alongside real hashes.
- **`REVIEWED_SOURCE_SHARD_MANIFEST` and `REVIEWED_OLMOE_MODEL_REGISTRY`
  remain the two hard gates.** Both are empty by design in this commit and
  must be populated only by dedicated, separately reviewed commits with
  real, non-truncated, officially published basenames/sizes/hashes — never
  a caller-supplied override.
- **The default real adapters
  (`PinnedRevisionFileDownloader`/`PinnedScriptConverter`) are untested
  against a real network or a real `convert_olmoe.py`.** They are
  structurally reviewable (pinned repository/revision only, `.partial`
  staging, exact size+hash verification before atomic rename, `shell=False`
  explicit argv) but exercised in tests only via monkeypatched
  `urllib.request.urlopen`/`subprocess.run` — never a live call. Real
  end-to-end exercise happens only at step B above, under explicit human
  approval.
- **`PinnedScriptConverter` accepts the pinned converter script's path as
  configuration, not a hardcoded location**, since this codebase does not
  pin a Colibrì checkout path; it validates the script's basename
  (`convert_olmoe.py`) before invoking it, but the reviewing commit at
  step D is responsible for recording the converter's own SHA-256 in the
  manifest/capture.
