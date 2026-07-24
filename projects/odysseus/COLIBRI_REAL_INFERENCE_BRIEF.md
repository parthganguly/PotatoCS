# Colibrì Compiled-Engine Proof Brief

Status: implemented Phase 0/1 proof for
`research/colibri-olmoe-real-inference`.

Date: 2026-07-19.

## Scope and claim boundary

This phase proves that the pinned official Colibrì native CPU source builds
reproducibly with the target Windows toolchain and that its dependency-free
integer-dot oracle selects and validates the exact AVX2 kernel and driver. It is
a compiled engine-core proof, not model inference or language generation.

The proof is bound to three separate regular files before any child process is
created:

| Role | Exact name | Expected SHA-256 |
| --- | --- | --- |
| Engine executable | `glm.exe` | `e9b4157fc2356c5fe7b348a826c37dc9b1dbf219b14bc0bd58388e7ff6af690c` |
| Oracle executable | `test_idot.exe` | `d41b8de17cebc44d5ba82a42c0eccc27179eddde2509dffcfe4ffbc475cfd0a5` |
| Source fixture | `test_idot.c` | `5c80caf2fa4a3f22f1497e0eacacf9025d28d5c2ece191cc4a0e966c049768dc` |

`test_idot.c` includes the real `glm.c` engine core and compares the selected
SIMD integer-dot kernels and batched drivers against scalar reference
implementations bit-for-bit. The only accepted stdout is the exact 68-byte
Windows CRLF sequence:

```text
idot kernel exactness (avx2): ok
idot driver exactness (avx2): ok
```

Scalar, fake, AVX-512, mismatched kernel/driver labels, LF-only text, and any
extra output are rejected. Exit status must be 0 and stderr must be empty.

## Non-goals

- No OLMoE or GLM-5.2 weights, model forward pass, token generation, or quality
  claim.
- No converter, downloader, runtime server, production RPC, UI, routing, or
  settings change.
- No automatic compiler invocation from normal PotatoCS tests.
- No CUDA/MSVC work, packaging, upstream vendoring, or committed binary.
- No changes to PR #33 or PR #35, and no merge.

Upstream documents a stronger random-weight model oracle whose teacher-forcing
criterion is `32/32 positions`. It requires Torch, Transformers, and
Safetensors, which are outside this phase and were not installed.

## Pinned upstream and toolchain

- Repository: `https://github.com/JustVugg/colibri`
- Commit: `72d3d37231e922a6fa9afca16e08fa45842d5eb4`
- Commit epoch: `1784223580`
- License: Apache-2.0
- Platform: Windows 11 Home, 64-bit
- CPU ISA used by this proof: AVX2
- Compiler: MSYS2 UCRT64 GCC 16.1.0, target `x86_64-w64-mingw32`
- Build tool: GNU Make 4.4.1
- Configuration: `ARCH=x86-64-v3`, CPU-only, static MinGW link

MSYS2 is invoked through `C:\msys64\msys2_shell.cmd` with `-ucrt64`. The
global Windows PATH, registry, and profiles are not modified.

## Reproducibility gate

Two independent clean directories must be checked out at the pinned commit and
built with:

```text
SOURCE_DATE_EPOCH=1784223580
make glm.exe ARCH=x86-64-v3
make tests/test_idot.exe ARCH=x86-64-v3
```

The epoch is derived from `git show -s --format=%ct` for the pinned commit. It
stabilizes the PE COFF timestamp and checksum; GNU ld's
`--no-insert-timestamp` is not required and is not part of the accepted recipe.
The two `glm.exe` files and two `test_idot.exe` files must have identical sizes
and SHA-256 values. Both oracles must independently produce the exact result
above with exit 0 and zero stderr. A practical strings inspection must find no
embedded absolute build-root path.

The developer-only verifier performs that two-build gate and is never run by
normal tests:

```powershell
& .\scripts\verify-colibri-native-repro.ps1 `
  -SourceRoot $env:ODYSSEUS_COLIBRI_SOURCE_ROOT `
  -BuildRoot $env:ODYSSEUS_COLIBRI_REPRO_ROOT `
  -Msys2Root 'C:\msys64'
```

Both `build-a` and `build-b` must be absent before invocation. The script does
not delete or overwrite them, download source or models, install dependencies,
or alter the global PATH.

## Harness security and privacy contract

The environment-gated harness accepts exactly:

- `ODYSSEUS_COLIBRI_PROOF_ENGINE`
- `ODYSSEUS_COLIBRI_PROOF_ORACLE`
- `ODYSSEUS_COLIBRI_PROOF_FIXTURE`

It resolves all three regular files, requires all three exact basenames, and
hashes all three before launch. Any unrecognized oracle hash is rejected before
the subprocess function is called, even if an unrelated program could print the
expected output. Only the recognized oracle is launched, with an explicit argv,
`shell=False`, and a four-variable child environment allow-list.

Stdout and stderr are read concurrently into separate bounded captures. At
most 4096 bytes per stream are retained. Timeout or either stream overflowing
causes termination, followed by kill if necessary. The result exposes only
fixed categories, fixed detail text, hashes, exit code, elapsed milliseconds,
bounded retained byte counts, and the fixed `avx2` identity. It never exposes
paths or raw child output and never logs child output.

Fixed result categories are:

- `passed`
- `invalid_engine`, `invalid_oracle`, `invalid_fixture`
- `engine_hash_mismatch`, `oracle_hash_mismatch`, `fixture_hash_mismatch`
- `launch_failed`, `timeout`, `output_overflow`
- `nonzero_exit`, `stderr_present`, `output_mismatch`

With no proof paths configured, the developer E2E pytest skips. Partial
configuration fails. Normal application and test execution has no external
toolchain dependency.

## Running the bound proof

After a successful reproducibility run, select either clean build and set the
three session-only inputs:

```powershell
$env:ODYSSEUS_COLIBRI_PROOF_ENGINE = `
  "$env:ODYSSEUS_COLIBRI_REPRO_ROOT\build-a\c\glm.exe"
$env:ODYSSEUS_COLIBRI_PROOF_ORACLE = `
  "$env:ODYSSEUS_COLIBRI_REPRO_ROOT\build-a\c\tests\test_idot.exe"
$env:ODYSSEUS_COLIBRI_PROOF_FIXTURE = `
  "$env:ODYSSEUS_COLIBRI_REPRO_ROOT\build-a\c\tests\test_idot.c"
python -m pytest python\tests\test_colibri_native_proof.py
```

No path, binary, fixture copy, upstream checkout, model file, secret, or raw
external output may be committed.

## Stage 2 OLMoE prerequisites

Stage 2 remains blocked until independent review accepts this compiled proof.
A later OLMoE phase still requires separate model/license and provenance
review, explicit checkpoint-download approval, sufficient storage, an audited
conversion plan and hashes, human-installed isolated dependencies, an agreed
reference oracle, and privacy/cancellation/timeout/cleanup plans. Nothing in
this proof authorizes those downloads or changes.
