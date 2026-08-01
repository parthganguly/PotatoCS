# Colibrì Stage 2A — OLMoE Scaffold Contract & Result

Status: **scaffold only** — deterministic build/manifest/runner plumbing is
implemented and tested with synthetic fixtures; no download, no conversion,
and no real `olmoe.exe` launch has been performed. This document is not a
claim of real language generation.

Date: 2026-07-24 (corrected same day: four execution-blocker fixes, then a
second pass recording real build evidence and closing further
pre-download conversion defects, then a third pass closing the four
remaining pre-download defects below, then this reviewed source manifest
commit). Amended 2026-07-29 with the memory/resume fix below, after a
real converter crash on the 16 GiB target host.

## Correction pass 5 — memory-bounded conversion and safe resume (2026-07-29)

### The observed failure

On the real target host (Windows 11, Ryzen 5 4600H, 15.42 GiB RAM, Python
3.13.12, torch 2.8.0+cpu, safetensors 0.5.3), source shard 1 downloaded
and verified successfully (`model-00001-of-00003.safetensors`,
4,997,744,872 bytes), and the converter then died inside `torch_cpu.dll`
with exception `0xc0000005` — 0.35 GiB physical RAM left and 30.6 GiB of
the 45.84 GiB pagefile in use. Source config and shard survived; the
converted and temp roots were empty.

### Root cause — two independent defects, both measured

**1. Peak memory scales with the shard, not with a budget.** The pinned
`convert_olmoe.py` calls `load_file(shard)` (whole shard resident as
tensors), accumulates every converted tensor into a second `out_tensors`
dict while the source dict is still alive, then calls `save_file`, whose
`_flatten` step makes one further full `bytes` copy of every output
tensor before anything reaches disk. Peak ≈ source + converted +
converted again.

Measured on an 850 MB expert-heavy shard of the real shape, via a Windows
Job Object across the whole process tree:

| converter | peak working set | peak commit | outcome |
| --- | --- | --- | --- |
| pinned `convert_olmoe.py` | **1784 MiB** | 1785 MiB | failed |
| bounded converter (32 MiB chunks) | **196 MiB** | 197 MiB | succeeded |
| bounded converter (8 MiB chunks) | **196 MiB** | 197 MiB | succeeded |
| `import torch` alone (baseline) | 157 MiB | 158 MiB | — |

The pinned converter peaked at ~2.1× the shard size *and had not yet
reached the `_flatten` copy* when it died. Extrapolated to the real 4.65
GiB shard that is ≥ 9.7 GiB for one shard — which is what exhausted a
15.42 GiB machine. The bounded converter's peak is flat across a 4×
change in chunk budget, and only ~39 MiB of its 196 MiB is data; the rest
is the torch runtime itself.

**2. The pinned converter cannot complete in this environment at all.**
`save_file` → `_flatten` → `_tobytes` does `import numpy`, and numpy is
not installed in the isolated Stage 2 venv. Reproduced directly:
`ModuleNotFoundError: No module named 'numpy'` raised from
`safetensors/torch.py` line 426. So even with unlimited RAM the pinned
script could not have written its output.

### The fix

`colibri_stage2_bounded_convert.py` is a standalone, memory-bounded
converter that is **equivalent, not different**. It reproduces
`quantize_row` verbatim and writes a safetensors file byte-identical to
what `safetensors.serialize_file` would produce for the same tensors,
while holding only `O(chunk_bytes)` resident:

* dense tensors are copied byte-for-byte from the source file range to
  the output file through a bounded buffer — they never enter torch;
* expert tensors are read, quantized, and written one row-chunk at a
  time, so float32 intermediates live only for that chunk;
* the only whole-shard retained state is the float32 scale vectors — 4
  bytes per expert row, ~7 MB for a 4.65 GiB shard;
* it writes bytes directly from tensor storage via `ctypes`, so it needs
  no numpy.

Row-wise reductions make chunking bit-exact: a row's scale and quantized
values depend on that row alone, and max/divide/round/clamp are exactly
determined by IEEE-754, so no chunk boundary can perturb a value.

**Equivalence evidence.** On the 850 MB real-shape shard, the bounded
output and a true reference built with `safetensors.serialize_file` from
the pinned `quantize_row` are byte-identical:
`ed660a5f579eab85409252af7bca9199b0dd33de2ce36eb5802b098315b068a7`,
427,475,384 bytes, both. Output is also byte-identical across chunk
budgets from 1 byte to 16 MiB, and across repeated runs.

Unchanged: the model, revision, Colibrì commit, cap (`8`), bit width
(`8`), the one-token oracle, tensor names/dtypes/shapes/ordering, the
`.qs` scale suffix, and the `--model`/`--out` grammar. The unmodified
upstream script is still reachable via `--converter pinned-script`.

### Safe resume

Every reuse is gated on a complete identity proof — exact pinned
basename, exact size, exact SHA-256, ordinary regular file, direct child,
non-reparse — before a byte is trusted:

* a partial file always fails the exact-size check, so it is never
  trusted, never extended in place, and never silently deleted;
* an existing converted artifact is never overwritten; without a recorded
  identity that re-verifies, the run fails closed;
* a `colibri-stage2-resume.json` ledger records each proven converted
  shard the moment it completes, so a crash on shard 2 costs neither
  shard 1's work nor shard 2's download. The ledger is a *hint*, never
  authority: entries are re-verified against the bytes on disk, so a
  tampered ledger can at worst cause redundant work;
* `--resume` relaxes exactly one check — "roots must be absent or empty"
  — and no identity proof. Without it, behaviour is unchanged.

### Failure evidence

`conversion_failed` no longer absorbs three different facts.
`conversion_timeout`, `conversion_nonzero_exit`, and
`conversion_process_crashed` are now distinct, with the real observed
`0xC0000005` (3221225477) classified as a crash rather than an ordinary
nonzero exit. Captures carry only bounded numbers — return code, elapsed
time, peak memory — and stdout/stderr remain `DEVNULL`, so no raw output,
environment value, username, or path can reach evidence.

Peak memory is read from a Windows Job Object rather than the child's
process handle. A venv `Scripts\python.exe` is a redirector stub that
runs the real interpreter as a *grandchild*, so a handle probe measures
only the stub — it reported 5 MiB for a process holding 600 MB. The job
accounts for the whole tree and stays readable after exit. (`argtypes`
must be set on the `ctypes` call; without them the struct pointer is
truncated to 32 bits and the call returns TRUE while filling nonsense.)

### Correction pass 5a — review findings (2026-07-29)

**Reviewed bounded-converter identity.** The bounded converter is launched
as a subprocess exactly like the pinned upstream script, so it needs the
same strength of proof. Path safety establishes only *where* a file is,
never *what it contains*: a working tree can be edited and a reviewed file
patched after review. `common.REVIEWED_BOUNDED_CONVERTER_IDENTITY` now pins
basename `colibri_stage2_bounded_convert.py`, size **24,033 bytes**, SHA-256
`6f8145fc71f060c75d7d04a34c96cfd58d00daa3d51f2406a6de25e167d2266b`, and
`require_reviewed_bounded_converter_identity` re-verifies it — ordinary,
direct-child, non-reparse, exact size, exact digest — immediately before
*every* launch, never cached from a previous call.

Pinning a digest exposed a real hazard first. This repository sets
`core.autocrlf=true` with no `.gitattributes`, so a fresh Windows clone
checked the file out as **24,670 CRLF bytes** while the working tree held
**24,033 LF bytes** — two different digests for the same commit, and no
single pin could match both. A `.gitattributes` rule now forces LF for this
one file; a fresh clone was re-tested and reproduces the pinned digest
exactly. A test recomputes the identity from the real file, so editing the
converter without updating the pin fails the suite rather than silently
rejecting every launch.

**Job Object accounting boundary.** `AssignProcessToJobObject`'s return
value was ignored. A job the child never joined still answers queries — with
small, plausible numbers describing an *empty job*, not the converter — so
an ignored failure turned "no measurement" into a confident wrong one.
Assignment is now checked; peak memory is queried only after a confirmed
assignment, and `ConversionRunEvidence.peak_memory_state` records
`measured` or `unavailable`. In the `unavailable` state both peaks are
`None` and no memory claim is made; the dataclass and
`build_conversion_capture` both reject the incoherent combination, so no
code path can present an unconfirmed number as whole-tree evidence. A
resumed shard that ran no converter is `unavailable` by construction.

The 1784 MiB / 196 MiB figures above were taken through a confirmed
assignment (verified against a known 600 MB allocation) and are unaffected.

### Correction pass 5b — review findings (2026-07-29)

**Converter provenance in the capture.** `build_conversion_capture` always
recorded `REVIEWED_CONVERTER_IDENTITY`, so a bounded conversion claimed the
upstream `convert_olmoe.py`'s basename, size, and SHA-256 — a false
provenance claim in exactly the runs this PR makes the default.

Each shard now carries the `converter_kind` of the adapter that actually
ran, and the identity is resolved through the closed
`common.REVIEWED_CONVERTER_IDENTITY_BY_KIND` mapping. Both real adapters fix
their kind on the class (`BoundedScriptConverter` → `bounded`,
`PinnedScriptConverter` → `pinned_script`), so an adapter can only ever
report its own identity. `build_conversion_capture` takes no converter
parameter at all: a caller supplies at most a closed *kind*, never a
basename, size, hash, or identity object, and a forged kind is rejected at
`ShardTransactionResult` construction. A shard that cannot name its
converter is rejected rather than defaulted.

The single top-level `converter_basename`/`converter_size_bytes`/
`converter_sha256` triple is replaced by a `converters` list derived from
the shards, because a resumed run may legitimately mix both — switching
from the upstream script to the bounded converter is precisely the
migration this PR enables. The resume ledger persists each artifact's
producing converter, so a resumed shard keeps the identity of whatever
actually created it, not of this run's converter. A ledger without a
converter kind, or naming an unreviewed one, fails closed. Capture schema
bumped to **v3**.

**Whole process-tree ownership.** `run_converter_child` started the venv
launcher normally, assigned it to the job afterwards, and on timeout called
`process.kill()` only. Because a venv `Scripts\python.exe` runs the real
interpreter as a *grandchild*, the actual converter survived. Reproduced
directly as a negative control: under the old path the grandchild's counter
kept advancing (41 → 70) after the launcher was killed, while it still held
files in the temporary output directory that cleanup removes next.

Ownership is now established before the converter executes anything:

1. create the process **suspended**;
2. assign it to a kill-on-close Job Object and confirm with
   `IsProcessInJob`;
3. only then resume it.

A failed assignment kills the still-suspended process and fails closed with
`job_assignment_failed` — an unowned converter never runs, since a converter
nobody can reliably kill is the thing being prevented. Because assignment
precedes the first instruction, every process the converter spawns is born
inside the job. On timeout the whole job is terminated and the call blocks
until the job reports zero remaining members before returning; if that
cannot be proven it raises `cleanup_failed` rather than letting the caller
delete a directory something may still be writing into. Kill-on-close is
the final safeguard. On POSIX the child gets its own session and the
timeout path signals the entire process group.

Measured directly: the job held **2 PIDs** (launcher + grandchild),
`TerminateJobObject` emptied it to 0, and the grandchild stopped writing.
Peak memory is still only reported when assignment and query were both
proven. The 1784 MiB / 196 MiB figures and the byte-identical equivalence
result were re-verified unchanged through the new owned-tree path.

### What this commit did not do

No model download, no real conversion, no model deletion, no registry
promotion, no inference run, and no merge. All memory measurements used
synthetic shards in a scratch directory.
**`REVIEWED_OLMOE_MODEL_REGISTRY` remains exactly empty.**

The pre-existing `D:\Colibri` source tree was **not modified**: after all
work, `model-00001-of-00003.safetensors` is still 4,997,744,872 bytes with
SHA-256 `61874210ca7c360f43f8c622cecc12441083d40190eae3b56bc9d6e1c0a30c1e`
and `config.json` still 828 bytes with SHA-256
`272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce` — both
matching the reviewed source manifest exactly. The converted and temp
roots remain empty.

## Reviewed source manifest (this PR)

`colibri_stage2_conversion.REVIEWED_SOURCE_SHARD_MANIFEST` is now a
populated, immutable `MappingProxyType` of the four reviewed
`SourceShardEntry` identities for `allenai/OLMoE-1B-7B-0125-Instruct` at
the immutable, Apache-2.0-licensed revision
`b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e`:

| file | size (bytes) | SHA-256 |
| --- | --- | --- |
| `config.json` | 828 | `272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce` |
| `model-00001-of-00003.safetensors` | 4,997,744,872 | `61874210ca7c360f43f8c622cecc12441083d40190eae3b56bc9d6e1c0a30c1e` |
| `model-00002-of-00003.safetensors` | 4,997,235,176 | `c523a43b8a17269d5fab33395048a83633f4d1d89c1958570cea738e2bbe80c9` |
| `model-00003-of-00003.safetensors` | 3,843,741,912 | `97ae01e3519c52e63a018bca96ab17a89c4cd5cab1c6d742efed0fa5c0e2bb17` |

Exact total source bytes across all four files: **13,838,722,788**.

Provenance: an `olmoe_source_manifest_capture` evidence capture (state
`unreviewed_source_manifest_capture`) confirmed the immutable revision
matched, the exact required file set matched, and no safetensor body was
requested — only `config.json` content was fetched. The three safetensor
identities (basename, exact size, SHA-256) came from that same
immutable-revision LFS metadata, not from a downloaded body. **No shard
body was downloaded, no model conversion was performed, and no inference
was performed by this commit.**

This closes the `source_model_manifest_unreviewed` hard gate: as of this
commit, `require_reviewed_source_manifest()` succeeds against the
production registry. This does not by itself download, convert, or run
anything — every other approved-execution precondition (explicit
`--approve`, interactive stdin/stdout, isolated Python environment with
torch/safetensors already installed, safe existing parents, an absent or
empty output root, at least 18 GiB free) still gates real execution, and
none of those preconditions were exercised by this commit.
**`colibri_stage2_manifest.REVIEWED_OLMOE_MODEL_REGISTRY` remains exactly
empty** — no inference becomes authorized by this source-manifest commit,
and the real one-token runner still fails closed with
`reviewed_model_manifest_unavailable`.

## Correction pass 3 — remaining pre-download defects

1. **Approved-mode preflight ordering**: `main --approve` now performs an
   explicit, side-effect-ordered preflight instead of relying on
   argument evaluation into `run_approved_conversion`: (1) reviewed
   source manifest, (2) `--approve` flag (structural), (3) interactive
   stdin+stdout, (4) isolated venv detection, (5) dependency version
   collection/validation, (6) converter path + reviewed identity, (7)
   existing-parent-directory validation (never `mkdir(parents=True)`),
   (8) source/converted roots absent-or-empty, (9) free space, (10) leaf
   directory creation, (11) adapter construction and network/process
   work. A noninteractive approved invocation now causes zero mkdir
   calls, converter reads, dependency imports, disk-space probes,
   network calls, or subprocess calls -- proven by regression.
2. **Exact converter binding**: `common.ReviewedConverterIdentity` now
   also carries `colibri_commit` (validated against
   `PINNED_COLIBRI_COMMIT`). `OlmoeModelManifest.converter_source_sha256`
   must equal `common.REVIEWED_CONVERTER_IDENTITY.sha256` exactly (not
   merely a valid 64-character hash), and the reviewed converter
   identity's own `colibri_commit` must equal the manifest's.
3. **Complete source-to-converted capture**: `ShardTransactionResult` and
   the closed capture now carry, per shard, in exactly
   `EXPECTED_SHARD_BASENAMES` order: `source_basename`,
   `source_size_bytes`, `source_sha256`, `source_verified`,
   `source_deleted`, `converted_basename`, `converted_size_bytes`,
   `converted_sha256`, `partial_cleanup_complete`,
   `temporary_output_cleanup_complete`, `elapsed_ms`. The source fields
   come from the reviewed `SourceShardEntry`, never from caller-supplied
   capture parameters. `build_conversion_capture` rejects duplicates,
   missing entries, wrong ordering, nonpositive sizes, malformed hashes,
   or any false proof boolean. `source_config_verified` and
   `source_config_moved_to_final` were added alongside the existing
   source-config identity. The capture remains
   `unreviewed_conversion_capture` and never authorizes inference.
4. **Partial and converter path safety**: the downloader now validates
   the `.partial` basename as a direct child, rejects reparse points, and
   creates it with exclusive `"xb"` (never `"wb"`) -- failing closed with
   the new `partial_already_exists` category if a stale or race-created
   partial exists, and never overwriting or deleting a partial it did not
   itself create. `require_reviewed_converter_identity` now requires an
   absolute path, validates the containing directory and original
   ancestor chain, rejects symlinks/junctions/reparse points, requires a
   regular non-reparse file, and is called again immediately before every
   subprocess creation (once per shard conversion). Argv, `shell=False`,
   and the absolute conversion timeout are unchanged.

`colibri_stage2_path_safety.py` and `colibri_stage2_runner.py` were not
modified in this pass -- the existing `require_ordinary_directory`/
`require_direct_child_path` primitives were sufficient once reused inside
the downloader and converter-identity checks.

## Correction pass 2 — real build evidence + final pre-download defects

**The two-build `olmoe.exe` verifier has been run for real** (step A of the
remaining finite sequence below, not re-run by this correction commit —
its result is recorded here and pinned in code). Pinned Colibrì commit
`72d3d37231e922a6fa9afca16e08fa45842d5eb4`, `SOURCE_DATE_EPOCH=1784223580`:
`olmoe.exe`, 704275 bytes, clean-build A and B SHA-256 both
`d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d`
(byte-identical; build durations 117029ms / 101571ms). The existing
`glm.exe`/`test_idot.exe` identities reproduced without change. **This
proves reproducible compilation only — it is not a claim about model
loading or token generation**, which remain gated behind steps B-E below.
This result is now pinned as `common.REVIEWED_ENGINE_IDENTITY`, and
`OlmoeModelManifest.__post_init__` requires its `engine_basename`/
`engine_size_bytes`/`engine_sha256` fields to equal that identity exactly
— a caller cannot authorize a different engine by supplying an arbitrary
(even well-formed) basename/size/hash. `REVIEWED_OLMOE_MODEL_REGISTRY`
itself stays empty.

**The pinned `convert_olmoe.py` converter identity is now recorded**,
computed directly from the verified local checkout
(`c/tools/convert_olmoe.py` at the pinned commit): basename
`convert_olmoe.py`, size 4469 bytes, SHA-256
`43f3ed1bad0cd89656c1a2ee17843d86ff33f670ff12c51a803f2b6361a5e168`, pinned
as `common.REVIEWED_CONVERTER_IDENTITY`. `PinnedScriptConverter` now reads
and hashes its configured script and requires an exact match against that
fixed identity before ever invoking a subprocess — never a
caller-supplied expected hash.

**The converter argv bug is fixed**: the real upstream grammar is
`--model <source> --out <output>` (confirmed by reading the pinned local
`convert_olmoe.py`'s own `argparse` definition) — `PinnedScriptConverter`
previously passed `--output`, which the real script does not accept.
`--repo` is never used (a local `--model` directory is always supplied).

**The approved CLI is now fully executable**, not a preconditions-only
check: `main --approve` validates the converter identity, safely
creates/validates the source/converted/private-temp-output roots (the
temp root is always a sibling of `--destination`, never nested inside it
or `--converted-destination`), constructs the default real adapters
(`PinnedRevisionFileDownloader`, `PinnedScriptConverter`), calls
`run_approved_conversion`, prints only the closed conversion capture on
success, and returns nonzero on any closed rejection/failure. While
`REVIEWED_SOURCE_SHARD_MANIFEST` stays empty, the manifest gate is
checked first, before any directory is created, any converter file
opened, any dependency probed, or any network/process call made — proven
by a regression that wires every one of those to explode if reached. No
further implementation commit is required once that manifest is
populated.

**Path-safety ancestor-walk-order bug fixed** in
`colibri_stage2_path_safety.require_ordinary_directory`: the previous
implementation resolved the directory *before* walking its ancestor
chain, which could silently erase a symlinked/junctioned ancestor segment
from the chain before it was ever inspected (a real escape). The
corrected version walks the *original*, lexical path — the directory
itself and every existing ancestor down to its drive/root anchor — via
`lstat` (rejecting both `stat.S_ISLNK` and the Windows
`FILE_ATTRIBUTE_REPARSE_POINT`) strictly before any resolution occurs, and
only resolves afterward, confirming the resolution lands nowhere but the
same already-approved location. `require_direct_child_path` now also
rejects both `/` and `\` unconditionally (not just the local platform's
separator), plus drive-qualification (`:`) and dot/dot-dot names.

**Atomic no-overwrite placement**: `atomic_no_replace_move` (new, in
`colibri_stage2_path_safety.py`) replaces every check-then-`os.replace`
pattern with one true no-replace primitive — on Windows, `os.rename()`
(unlike `os.replace()`) already refuses to replace an existing
destination (CPython calls `MoveFileExW` without
`MOVEFILE_REPLACE_EXISTING`); on other platforms, a hardlink-then-unlink
sequence is used instead. Applied to all three placement points: the
downloaded `.partial` file into its verified destination, the converted
temporary shard into its final directory, and the verified config into
its final directory. A destination introduced by a race immediately
before placement now survives completely untouched instead of being
silently overwritten.

## Correction pass 1 (four execution blockers)

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
stays empty. `colibri_stage2_conversion.REVIEWED_SOURCE_SHARD_MANIFEST` is
no longer empty (see "Reviewed source manifest" above); its hard gate is
satisfied, but that only permits an approved-mode attempt to pass the
manifest check — it does not perform, and this commit did not perform,
any download, conversion, or network activity.

## No download or inference performed

This implementation commit did not download model files, install
packages, convert weights, execute `olmoe.exe`, or launch Ollama. Every
test uses synthetic fixtures and a fake `LifecycleApi`; no test touches
the network, the ordinary model store, or a real Windows process.

## Remaining finite sequence

A. ~~Run the two-build `olmoe.exe` verifier~~ **Done.** The real
   deterministic engine hash is recorded above and pinned as
   `common.REVIEWED_ENGINE_IDENTITY`.

B1. ~~Source manifest evidence capture~~ **Done.** The
    `olmoe_source_manifest_capture` evidence capture confirmed the
    immutable revision, exact file set, and config-only fetch described
    above.

B2. ~~Reviewed source manifest commit~~ **Done by this PR.**
    `REVIEWED_SOURCE_SHARD_MANIFEST` now holds the four reviewed
    `SourceShardEntry` identities; `source_model_manifest_unreviewed` is
    satisfied against the production registry.

B3. **Not done.** A human must still separately approve isolated
    dependency setup (torch/safetensors in an isolated Python
    environment) and the real, sequential ~13.84 GB download/verify/
    convert/delete sequence. `run_approved_conversion` (invoked
    automatically by `main --approve` with the real default adapters) is
    the complete real path once that approval happens — no second
    implementation is required.

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
  remain the two hard gates.** `REVIEWED_SOURCE_SHARD_MANIFEST` is now
  populated by this commit (see "Reviewed source manifest" above).
  `REVIEWED_OLMOE_MODEL_REGISTRY` remains exactly empty and must still be
  populated only by a dedicated, separately reviewed commit with real,
  non-truncated, officially published basenames/sizes/hashes for the
  converted model — never a caller-supplied override.
  *(Superseded: that dedicated commit is the `research/colibri-stage2-real-token`
  unit — see `COLIBRI_STAGE2_REAL_TOKEN_RESULT.md`. The registry now holds
  exactly one reviewed entry for the disk-attested converted artifact set.)*
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
