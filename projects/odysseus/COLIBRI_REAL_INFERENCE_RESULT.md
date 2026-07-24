# Colibrì Compiled-Engine Proof Result

Status: **passed** — reproducible compiled-engine proof only; not language
generation.

Date: 2026-07-19.

## Deterministic result

The pinned official Colibrì source compiled in two independent clean
directories on Windows using the MSYS2 UCRT64 toolchain. The two builds were
byte-identical, and both dependency-free integer-dot oracles matched the scalar
reference bit-for-bit using the exact AVX2 kernel and driver.

- Upstream commit: `72d3d37231e922a6fa9afca16e08fa45842d5eb4`
- Commit-derived `SOURCE_DATE_EPOCH`: `1784223580`
- License: Apache-2.0
- Compiler: MSYS2 UCRT64 GCC 16.1.0 (`x86_64-w64-mingw32`)
- Build tool: GNU Make 4.4.1
- Build configuration: CPU-only, static MinGW link, `ARCH=x86-64-v3`, AVX2
- Native engine: exact name `glm.exe`, 1,088,573 bytes
- Deterministic engine SHA-256:
  `e9b4157fc2356c5fe7b348a826c37dc9b1dbf219b14bc0bd58388e7ff6af690c`
- Oracle: exact name `test_idot.exe`, 1,112,517 bytes
- Deterministic oracle SHA-256:
  `d41b8de17cebc44d5ba82a42c0eccc27179eddde2509dffcfe4ffbc475cfd0a5`
- Source fixture: exact name `test_idot.c`, 4,994 bytes
- Fixture SHA-256:
  `5c80caf2fa4a3f22f1497e0eacacf9025d28d5c2ece191cc4a0e966c049768dc`
- Clean build A wall time: 26,651 ms
- Clean build B wall time: 24,524 ms
- Oracle A: exit 0, exactly 68 stdout bytes, 0 stderr bytes
- Oracle B: exit 0, exactly 68 stdout bytes, 0 stderr bytes
- Result category: `passed`

The exact observed bytes use Windows CRLF line endings:

```text
idot kernel exactness (avx2): ok
idot driver exactness (avx2): ok
```

No absolute build-root string was found in either deterministic executable.
Both PE files have the commit-derived timestamp `2026-07-16 23:09:40` and
stable checksums. The engine checksum is `0x0010b2ed`; the oracle checksum is
`0x0011a58b`.

## Reproducibility investigation

The hashes initially reviewed on 2026-07-18 were one-off build hashes:

- `glm.exe`:
  `451b779b545fd6cf6cf5bcae302dab411492ab9cb2bb88c0c3d6cfc8d1efffd0`
- `test_idot.exe`:
  `b794a6360cc7e2bce6c2166276812e5fe2eee59521264700d87210f7774bd13c`

A later clean rebuild produced different hashes, so those values were not
silently accepted. Two additional uncontrolled clean baseline builds also
differed:

| Artifact | Baseline A | Baseline B |
| --- | --- | --- |
| `glm.exe` | `d78ac65cef4a43c096d09447bf6b7049550b508eb494b3adae8a461b0a52fac2` | `f9272c97c9617266b14dfcd299a8f661c906afe845888dc9786619a3cf5db455` |
| `test_idot.exe` | `7faee96f4ac2e4abc7a4c848c83c51e9a75cf00729aa3182bc6692899f0a64cd` | `42c514e2306197541b5450fa92a1385b17aa0e3f522dbc36276e9ed35f2ce11a` |

The artifact sizes and section tables were identical. A byte comparison found
only two changed bytes in `glm.exe` and three in `test_idot.exe`, confined to
PE offsets `0x88-0x89` (COFF timestamp) and `0xd8` (checksum). Neither file had
a PE debug directory or build ID, and neither embedded the clean build root.

Setting `SOURCE_DATE_EPOCH` to the pinned commit epoch made two new clean builds
byte-identical. The deterministic developer verifier independently repeated
the same two-build result. `-Wl,--no-insert-timestamp` was therefore not needed
and the upstream link flags remain exactly `-lm -fopenmp -static -lpsapi`.

## Three-file proof binding

The developer-only harness has no production import, RPC, UI, routing, or
settings surface. Before launch it:

1. resolves three separate regular-file inputs;
2. requires `glm.exe`, `test_idot.exe`, and `test_idot.c` exactly;
3. hashes all three and compares them with the deterministic values above;
4. rejects any mismatch with fixed privacy-safe categories; and
5. launches only the recognized oracle, never the engine or an unrecognized
   executable.

The result exposes all three hashes but no paths. A hostile regression test
substitutes an unrelated oracle while mocking the exact expected result and
proves rejection occurs before the subprocess launcher is called.

## Exact output and bounded execution

The harness accepts only the exact 68-byte CRLF AVX2 output. Scalar, fake,
AVX-512, mismatched kernel/driver labels, LF-only output, and trailing output
are rejected.

Stdout and stderr use independent bounded readers with a maximum retained size
of 4096 bytes each. Timeout or overflow terminates the child and escalates to a
kill if required. No raw child output is returned or logged; only the fixed
category and bounded retained byte counts are exposed. Hostile timeout and
output-flood tests prove termination and retained-memory bounds.

## Downloads and tracked scope

- Model weights downloaded: **0 bytes**.
- Additional packages or tools installed: **none**.
- Global Windows PATH changes: **none**.
- Committed binaries, fixtures, upstream source, or private paths: **none**.
- PR #33 and PR #35 changes: **none**.

The upstream checkout, clean build directories, and executables remain outside
the tracked repository.

## Validation

- Focused harness without paths: 15 passed, 1 skipped.
- Focused harness with deterministic real paths: 16 passed, 0 skipped.
- Standalone two-clean-build reproducibility verifier: passed.
- Full Python suite: 710 passed, 9 skipped, 0 failed (719 collected).
- `test:backend-status`: passed (`backend-status-tests-ok`).
- `test:progress`: passed (`chat-progress-tests-ok`).
- `test:readiness`: passed (`readiness row mapping tests passed`).
- `test:jobs-ui`: passed (`jobs-ui-tests-ok`, 99 assertions).
- Frontend production build: passed, 1,614 modules transformed.
- `cargo check`: passed.
- `cargo test`: 24 passed, 4 ignored helper fixtures, 0 failed.
- `git diff --check`: passed.

## Claim boundary and Stage 2 blockers

This is a reproducible native compiled-engine/toolchain and AVX2 exactness
proof. It is not a model-forward, token-generation, quality, performance, or
production-routing proof. No OLMoE or GLM-5.2 weights were downloaded.

Stage 2 OLMoE remains blocked on independent review plus the license,
provenance, storage, dependency, download, conversion, reference-oracle,
privacy, cancellation, timeout, and cleanup gates listed in the brief.
