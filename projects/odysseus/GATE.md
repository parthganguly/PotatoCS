# v0.3.0 Release Proof Gate

State: **RED — publish-time asset verification only** (all other boxes
evidence-backed or deferred; green only after the uploaded asset is verified).
Purpose: ship a proof/hardening release without adding product scope.

## Required pass conditions

### 1. Scope and source

Audit at candidate `e8702c50` (`git log/diff v0.2.1..e8702c50`: 22 commits,
35 files — lifecycle/proof fixes, proof-test hardening, version alignment,
release/harness docs, and one exception noted below).

- [x] Candidate is based on one recorded commit SHA
      (`e8702c50`, `RELEASE_PROOF_v0.3.0.md`).
- [x] Diff contains only lifecycle/proof fixes, version alignment and release docs,
      plus one documented exception: `e1c8c774` (pre-freeze answer-quality
      correction), now recorded in `docs/releases/v0.3.0.md` as tuning only —
      no new capability, no scope expansion.
- [x] No tools, agents, research, memory, skills, compare/Cookbook or
      redesign anywhere in the audited range.
- [x] Worktree is clean after candidate artifacts are produced
      (clean at build start/end and after `e335705f`).

### 2. Sidecar lifecycle

- [x] Graceful shutdown has a bounded deadline (`e9f36fbc`).
- [x] Hung shutdown reaches forced kill and child reap (`e9f36fbc`).
- [x] Normal close leaves no Python sidecar orphan.
      Zero orphans after every normal close in both smoke re-run runs
      (`304c6284` re-run report) and after both final-installer launches
      (`RELEASE_PROOF_v0.3.0.md`, clean backend shutdowns, 0 orphans).
- [x] Killing only the sidecar does not terminate the Tauri host: survives
      idle and startup `health.ping` kills across two installed runs
      (`304c6284`, `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`).
- [x] Rust forced-death fixture detects exit and keeps the test host alive (`bd635ea2`).
- [x] A safe idempotent request can restart/retry the sidecar once (`bd635ea2`).
- [x] Non-idempotent requests are not replayed after sidecar loss (`bd635ea2`).
- [x] Profile data survives shutdown, forced death, restart and relaunch
      (`app.db` 92,561,408 bytes after both installed re-run matrices,
      across uninstall/reinstall and two forced kills per run).
- [x] Lifecycle unit logs record fixed-label exit/restart/retry outcomes (`bd635ea2`).
- [x] Lifecycle logs/proof records fixed-label shutdown, exit, restart,
      retry and forced-kill outcomes without private payloads
      (`e9f36fbc`, `bd635ea2` unit logs; installed `context=startup_health`
      logs in the smoke re-run; trace privacy sentinel sweep).

Deferred beyond v0.3: spawn/restart degraded-state UI requires product/UI
work outside the v0.3 proof freeze; moved to v0.3.1/v0.4. Not counted
against this gate.

### 3. Automated proof

All items evidence-backed at `511ab1db` (no source drift from `304c6284`);
see `projects/odysseus/AUTOMATED_PROOF_SUITE_2026-07-04.md` (Python 297/0/0
with the autouse non-loopback egress guard active suite-wide).

- [x] Full Python suite passes with the non-loopback egress guard active.
- [x] Trace privacy sentinel sweep passes.
- [x] Progress identifier tests pass in strict mode
      (`test_filename_shaped_identifier_rejected_under_strict_trace_mode`).
- [x] Schema upgrade, future-version refusal and idempotence tests pass.
- [x] IPC golden fixtures pass.
- [x] RAG grounding/retrieval and restart-persistence tests pass.
- [x] `npm run test:progress` passes.
- [x] `npm run build:frontend` passes (chunk-size warning only).
- [x] `cargo check --manifest-path src-tauri/Cargo.toml` passes
      (`bd635ea2`; re-confirmed at `511ab1db`).
- [x] `cargo test --manifest-path src-tauri/Cargo.toml` passes
      (`bd635ea2`; re-confirmed at `511ab1db`: 20 passed, 0 failed,
      4 ignored helper fixtures).

### 4. Installed package proof

- [x] Florence/runtime/resource hygiene verification passes.
      Core variant at `e8702c50`: embedded runtime and sidecar verified;
      installer hygiene all 12 counters 0, including Florence/model/torch
      absence (`RELEASE_PROOF_v0.3.0.md`).
- [x] NSIS installer is built from the recorded candidate SHA.
      Final build: candidate `e8702c50`, clean worktree,
      `Odysseus Desktop_0.3.0_x64-setup.exe`, size `32,375,003` bytes
      (`e335705f`, `projects/odysseus/RELEASE_PROOF_v0.3.0.md`). The earlier
      matrix installer at `304c6284` is recorded in the smoke re-run report.
- [x] Clean install launches and reports backend/OCR/Florence truthfully.
      Final installer: sidecar `version=0.3.0`, OCR `available=True`
      (tesseract/mutool), Ollama reported honestly, Florence absent and not
      claimed (`RELEASE_PROOF_v0.3.0.md`).
- [x] Installed lifecycle matrix passes twice: close, relaunch, kill, recover, close.
      Both runs passed against candidate `304c6284` (installer SHA-256
      `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`), see
      `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`.
- [x] Installer SHA-256 is recalculated after the final build.
      `0D759D2560919A5F8B657D8D9C245D965FD770745C01749F1D77DF022426FFB4`
      at `e8702c50` (`RELEASE_PROOF_v0.3.0.md`).
- [ ] Published checksum and asset filename match the actual installer.
      Repo-artifact checksum is prepared and matches locally:
      `dist/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt` (`e335705f`)
      carries the installer hash above with asset name
      `PotatoCs-Odysseus-Desktop-v0.3.0-Windows-x64-setup.exe`. Publish-time
      asset verification remains pending.

### 5. Release truthfulness

- [x] Package, Cargo, Tauri and Python runtime all report `0.3.0`.
      All six version sources aligned at `5171fdf4`
      (`VERSION_ALIGNMENT_2026-07-04.md`); installed runtime verified at the
      final installer — registry, exe metadata, installed sidecar source and
      live `backend.log` all report `0.3.0` (`RELEASE_PROOF_v0.3.0.md`).
- [x] Release notes describe v0.3 as proof/hardening, not agentic capability.
      `docs/releases/v0.3.0.md` (`e335705f`).
- [x] Public naming is documented as PotatoCS project / Odysseus Desktop app.
      `docs/releases/v0.3.0.md`, including the historical `PotatoCs` asset
      spelling.
- [x] Test counts, skips and smoke claims are generated from the candidate SHA.
      Python 297/0/0 re-run at `e8702c50`; cargo 20/0/4 at source-identical
      `5171fdf4`; smoke claims at `304c6284` with recorded no-source-drift
      lineage (`RELEASE_PROOF_v0.3.0.md`).
- [x] Proof report records commands, environment, SHA, hashes and unresolved skips.
      `projects/odysseus/RELEASE_PROOF_v0.3.0.md` (`e335705f`), candidate
      `e8702c50`, including test counts and unresolved skips.

## Current hard failures

- None. (Version disagreement resolved at `5171fdf4`; installer/checksum
  mismatch resolved at `e335705f`.) Open checkboxes above still block release.

## Recorded evidence

- `e9f36fbc` bounds shutdown cleanup (cargo 14/0/3 ignored); unit-level only.
- `bd635ea2` forced-death fixture recovery: exit detection, one restart/retry
  max, no unsafe replay, fixed-label logs (cargo 18/0/4); unit-level only.
- `c2cc4d16` first installed smoke, **FAIL** on startup `health.ping` kill
  (candidate `1682dd14`): `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`.
  Retained as the gate-rule first-failure record; superseded by the re-run.
- `7119e40c` fixes startup `health.ping` sidecar death at source level;
  Rust fixtures prove host survival (recovered and retry-also-fails cases),
  fixed-label logs, no payload content. Cargo test 20/0/4 ignored; cargo
  check and `git diff --check` passed.
- `304c6284` installed smoke re-run, **PASS** both full runs (install, close,
  relaunch, idle and startup-`health.ping` kills with auto-restart, orphan
  checks): `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`, installer
  `04A1C2BD...C3C7DCC`. Proves installed lifecycle/recovery only.
- `e335705f` final v0.3.0 installer, matching checksum, release notes and
  proof report (candidate `e8702c50`): `RELEASE_PROOF_v0.3.0.md`.

## Gate rule

Do not call the candidate releasable while any required checkbox is open. A retry
is evidence only when its first failure, cleanup and second result are recorded.
