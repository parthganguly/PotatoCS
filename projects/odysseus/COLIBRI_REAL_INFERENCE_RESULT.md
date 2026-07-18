# Colibrì Compiled-Engine Proof Result

Status: **passed** — compiled-engine proof only; not real language generation.

Date: 2026-07-18.

## Result

The pinned official Colibrì source compiled natively on Windows with the
MSYS2 UCRT64 toolchain. Its dependency-free integer-dot oracle compiled the
real `glm.c` engine core and matched the scalar reference bit-for-bit on both
kernel and batched-driver checks.

- Upstream commit: `72d3d37231e922a6fa9afca16e08fa45842d5eb4`
- License: Apache-2.0
- Compiler: MSYS2 UCRT64 GCC 16.1.0 (`x86_64-w64-mingw32`)
- Build tool: GNU Make 4.4.1
- Build configuration: CPU-only, static MinGW link, `ARCH=x86-64-v3`, AVX2
- Native engine: `glm.exe`, 1,088,573 bytes
- Engine SHA-256:
  `451b779b545fd6cf6cf5bcae302dab411492ab9cb2bb88c0c3d6cfc8d1efffd0`
- Oracle executable: `test_idot.exe`, 1,112,517 bytes
- Oracle executable SHA-256:
  `b794a6360cc7e2bce6c2166276812e5fe2eee59521264700d87210f7774bd13c`
- Official fixture: `test_idot.c`, 4,994 bytes
- Fixture SHA-256:
  `5c80caf2fa4a3f22f1497e0eacacf9025d28d5c2ece191cc4a0e966c049768dc`
- Clean build wall time: 32,627 ms
- Oracle wall time: 209 ms
- Oracle peak working set sampled: 5,816,320 bytes
- Exit status: 0
- Result category: `passed`
- Stdout: 68 bytes; stderr: 0 bytes

Expected and observed criteria:

```text
idot kernel exactness (avx2): ok
idot driver exactness (avx2): ok
```

The compiler emitted two existing warning classes: MinGW ignores an MSVC
link pragma that the Makefile already replaces with `-lpsapi`, and GCC warns
about a potentially truncated diagnostic buffer in `expert_load`. Neither
warning affected linkage or the oracle result.

## Downloads and storage

- Git object packs downloaded for the filtered current clone and old-commit
  comparison: exactly 1,594,955 bytes.
- Git pack/index/promisor/reverse-index files stored: 1,617,378 bytes.
- Checked-out upstream files observed: 3.94 MiB.
- Model weights downloaded: **0 bytes**.
- Pip/npm/cargo dependency downloads initiated by this phase: **0 bytes**.
  Existing Python packages, an identical-lockfile Node dependency tree, and
  the existing Cargo cache were used.

The upstream checkout, binaries, and build outputs are outside tracked source.
No binary, fixture copy, model file, or upstream source is committed.

## PotatoCS integration

`colibri_native_proof.py` is a developer-only module with no production import,
RPC, UI, or settings surface. It:

- accepts only explicit executable and fixture paths;
- executes a one-element argv with `shell=False` and a 30-second timeout;
- passes only four allow-listed Windows process variables to the child;
- pins the official fixture SHA-256 and exact correctness markers;
- returns fixed categories, hashes, byte counts, and timing only;
- never returns or logs paths, child stdout, child stderr, or exception text.

The real pytest uses `ODYSSEUS_COLIBRI_PROOF_EXECUTABLE` and
`ODYSSEUS_COLIBRI_PROOF_FIXTURE`. With neither set it skips cleanly. A partially
configured environment fails rather than silently weakening the proof.

## Validation

- Focused harness without paths: 5 passed, 1 skipped.
- Focused harness with real paths: 6 passed, 0 skipped.
- Full Python suite without real paths: 700 passed, 9 skipped, 0 failed
  (709 collected).
- `test:backend-status`: passed (`backend-status-tests-ok`).
- `test:progress`: passed (`chat-progress-tests-ok`).
- `test:readiness`: passed (`readiness row mapping tests passed`).
- `test:jobs-ui`: passed, 99 assertions.
- Frontend production build: passed, 1,614 modules transformed.
- `cargo check`: passed.
- `cargo test`: 24 passed, 4 ignored helper fixtures, 0 failed.
- `git diff --check`: passed.

## Claim boundary and Stage 2 blockers

This is the first real native compiled-engine/toolchain proof on the target
machine. It is **not** a model-forward, token-generation, quality, performance,
or production-routing proof. No OLMoE or GLM-5.2 weights were downloaded.

Stage 2 OLMoE remains blocked on independent review of this change plus the
license/provenance, storage, dependency, download, conversion, reference-oracle,
privacy, cancellation, timeout, and cleanup gates listed in the brief. No Stage
2 work is authorized by this result.
