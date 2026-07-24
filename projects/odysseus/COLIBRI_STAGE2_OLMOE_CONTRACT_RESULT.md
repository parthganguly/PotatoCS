# Colibrì Stage 2A — OLMoE Scaffold Contract & Result

Status: **scaffold only** — deterministic build/manifest/runner plumbing is
implemented and tested with synthetic fixtures; no download, no conversion,
and no real `olmoe.exe` launch has been performed. This document is not a
claim of real language generation.

Date: 2026-07-24.

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
stays empty. Similarly, `colibri_stage2_conversion.REVIEWED_SOURCE_SHARD_SHA256`
is empty, so any approved-mode download attempt fails closed with
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
   separately reviewed, non-empty `REVIEWED_SOURCE_SHARD_SHA256`.

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
- **`REVIEWED_SOURCE_SHARD_SHA256` and `REVIEWED_OLMOE_MODEL_REGISTRY`
  remain the two hard gates.** Both are empty by design in this commit and
  must be populated only by dedicated, separately reviewed commits with
  real, non-truncated, officially published hashes — never a
  caller-supplied override.
