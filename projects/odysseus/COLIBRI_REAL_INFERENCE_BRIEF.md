# Colibrì Compiled-Engine Proof Brief

Status: approved Phase 0/1 plan for `research/colibri-olmoe-real-inference`.

Date: 2026-07-18.

## Scope

This phase proves that the current official Colibrì native CPU toolchain works
on the target Windows machine. It pins upstream, compiles the real GLM-5.2
engine source, and runs the smallest dependency-free native engine-core oracle
that upstream runs in its own CI. PotatoCS receives only a developer-only,
environment-gated subprocess harness and pytest.

The proof target is `tests/test_idot.exe`. Its official source fixture includes
`glm.c` directly and compares the selected SIMD integer-dot kernels and batched
drivers against scalar reference implementations bit-for-bit. The expected
stdout markers are:

```text
idot kernel exactness (avx2): ok
idot driver exactness (avx2): ok
```

The parenthesized kernel identity is machine/build dependent; both fixed
prefix/suffix criteria and exit status 0 must match.

Upstream also documents a stronger 2.4 MB random-weight model oracle whose
teacher-forcing criterion is `32/32 positions`. Generating it requires Torch,
Transformers, and Safetensors, which are not present on this machine. Those
packages will not be installed in this phase. Passing the native oracle is not
represented as model-level token-generation correctness.

## Non-goals

- No OLMoE or GLM-5.2 weights, language generation, or quality claim.
- No converter run, model downloader, runtime server, production RPC, UI, or
  settings change.
- No automatic compiler invocation from PotatoCS tests.
- No CUDA/MSVC work, packaging, upstream vendoring, or committed binary.
- No changes to PR #33 or PR #35.

## Pinned upstream and reconciliation

- Repository: `https://github.com/JustVugg/colibri`
- Pinned current commit: `72d3d37231e922a6fa9afca16e08fa45842d5eb4`
- Previously audited commit: `550ddcba83afd27a892dba92c587bfcc1d30f020`
- License: Apache-2.0
- Source checkout size observed: 3.94 MiB plus 1.53 MiB of Git packs.

The current commit still supplies the contracts used by PR #33: the Python
`coli` CLI, `plan` and `doctor`, the text-only OpenAI-compatible server,
`GET /v1/models`, `GET /v1/models/{model}`, and chat/completions endpoints.
The PotatoCS adapter remains deliberately text-only, so upstream feature work
outside that subset does not invalidate it.

Compared with the previous audit, current upstream adds or strengthens native
Windows defaults, compiler-target detection, MinGW linkage, Windows CPU
inventory, environment-default tests, and CI on `main`. None of the relevant
provider or CLI files were removed. The resource-plan JSON contract remains
version 2 and doctor remains schema version 1.

## Privacy-safe machine and toolchain inventory

- OS: Windows 11 Home, build 26200, 64-bit.
- CPU: AMD Ryzen 5 4600H, x86-64, 6 physical/12 logical cores.
- ISA: AVX, AVX2, and FMA present; no AVX-VNNI claim.
- RAM: 15.4 GiB installed; free RAM is transient and is not an acceptance
  input.
- Intended build drive: more than 70 GiB free at preflight.
- Git: 2.46.0.windows.1.
- Compiler: MSYS2 UCRT64 GCC 16.1.0, target `x86_64-w64-mingw32`.
- Build tool: GNU Make 4.4.1.
- Python: CPython 3.13.12 for the PotatoCS test suite.
- Rust: rustc/cargo 1.96.0.
- CMake and Ninja: absent and not required by upstream's Makefile path.

The toolchain is invoked through a process-local UCRT64 environment. The
global Windows PATH, registry, profiles, and system configuration must not be
changed.

## Expected network and storage work

- Network: one shallow/filtering Git clone plus one shallow fetch for the old
  audited commit. Observed repository data is under 6 MiB locally.
- Build outputs: `glm.exe`, `tests/test_idot.exe`, and compiler intermediates,
  expected to remain well below 100 MiB total.
- Model downloads: exactly zero bytes.
- Tracked artifacts: Markdown, a small Python harness, and tests only.

The upstream clone and all executables stay in an out-of-tree dependency
directory. No individual download may exceed 100 MB.

## Acceptance criteria

1. `glm.exe` builds from the pinned current commit with UCRT64 GCC 16.1.0.
2. `tests/test_idot.exe` builds from the same checkout and exits 0.
3. Both exactness markers end in `: ok`; raw external stderr is not persisted.
4. The report records upstream, compiler, configuration, executable and
   fixture SHA-256 values, duration, exit status, and a fixed result category.
5. Reproduction uses only explicit, out-of-tree paths and a process-local
   UCRT64 environment.
6. No model weight, binary, cloned upstream source, username, absolute path,
   secret, or raw external stderr is committed.
7. The developer E2E test skips when its two path variables are absent.
8. Normal PotatoCS application and test execution have no external-toolchain
   dependency.

## Fixed failure categories

- `passed`: exit 0 and both exactness markers match.
- `not_configured`: required environment paths are absent; pytest skips.
- `invalid_executable`: executable path is absent, not a regular file, or not
  an `.exe`.
- `invalid_fixture`: fixture path is absent, not a regular file, or its name is
  not `test_idot.c`.
- `launch_failed`: process creation fails; no raw operating-system text leaves
  the harness.
- `timeout`: the fixed deadline expires and the child is terminated.
- `nonzero_exit`: the child completes unsuccessfully.
- `output_mismatch`: exit 0 without both fixed correctness markers.

Result details may contain only fixed copy, exit code, elapsed milliseconds,
SHA-256 digests, stdout/stderr byte counts, and the normalized kernel identity.

## Stage 2 OLMoE prerequisites

Stage 2 remains blocked until independent review accepts this compiled-engine
proof. A later OLMoE phase additionally requires:

- a separately approved model/license and provenance review;
- explicit approval for the multi-gigabyte OLMoE checkpoint download;
- substantially more free disk than this phase uses;
- an audited local conversion plan and hashes for every source artifact;
- Torch, Transformers, Safetensors, and Hugging Face tooling installed by the
  human in an isolated environment;
- an OLMoE reference oracle and acceptance criterion agreed before execution;
- privacy, cancellation, timeout, and cleanup plans for long-running inference.

No Stage 2 prerequisite authorizes an automatic download in this phase.

## Reproducibility commands

The human supplies privacy-safe session variables. `ODYSSEUS_MSYS2_ROOT`
identifies the MSYS2 installation and `ODYSSEUS_COLIBRI_DEP_ROOT` identifies an
out-of-tree dependency directory.

```powershell
$env:MSYSTEM = 'UCRT64'
$env:CHERE_INVOKING = '1'
$env:PATH = "$env:ODYSSEUS_MSYS2_ROOT\ucrt64\bin;$env:ODYSSEUS_MSYS2_ROOT\usr\bin;$env:PATH"

git clone --filter=blob:none --no-checkout --depth 1 --branch main `
  https://github.com/JustVugg/colibri.git $env:ODYSSEUS_COLIBRI_DEP_ROOT
git -C $env:ODYSSEUS_COLIBRI_DEP_ROOT checkout --detach `
  72d3d37231e922a6fa9afca16e08fa45842d5eb4

Push-Location "$env:ODYSSEUS_COLIBRI_DEP_ROOT\c"
make.exe glm.exe ARCH=x86-64-v3
make.exe tests/test_idot.exe ARCH=x86-64-v3
.\tests\test_idot.exe
Pop-Location

$env:ODYSSEUS_COLIBRI_PROOF_EXECUTABLE = `
  "$env:ODYSSEUS_COLIBRI_DEP_ROOT\c\tests\test_idot.exe"
$env:ODYSSEUS_COLIBRI_PROOF_FIXTURE = `
  "$env:ODYSSEUS_COLIBRI_DEP_ROOT\c\tests\test_idot.c"
python -m pytest python\tests\test_colibri_native_proof.py
```
