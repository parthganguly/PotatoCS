# v0.3.0 Release Proof Gate

State: **RED**  
Purpose: ship a proof/hardening release without adding product scope.

## Required pass conditions

### 1. Scope and source

- [ ] Candidate is based on one recorded commit SHA.
- [ ] Diff contains only lifecycle/proof fixes, version alignment and release docs.
- [ ] No tools, agents, research, memory, skills, compare/Cookbook or redesign.
- [ ] Worktree is clean after candidate artifacts are produced.

### 2. Sidecar lifecycle

- [x] Graceful shutdown has a bounded deadline (`e9f36fbc`).
- [x] Hung shutdown reaches forced kill and child reap (`e9f36fbc`).
- [ ] Normal close leaves no Python sidecar orphan.
- [x] Killing only the sidecar does not terminate the Tauri host.
      Installed smoke re-run (candidate `304c6284`, installer SHA-256
      `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`) proved
      the host survives sidecar kill both idle and during startup
      `health.ping`, across two complete runs. See
      `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`.
- [x] Rust forced-death fixture detects exit and keeps the test host alive (`bd635ea2`).
- [x] A safe idempotent request can restart/retry the sidecar once (`bd635ea2`).
- [x] Non-idempotent requests are not replayed after sidecar loss (`bd635ea2`).
- [x] Profile data survives shutdown, forced death, restart and relaunch.
      Installed re-run: `app.db` was `92,561,408` bytes after both runs,
      matching the size recorded in the prior failing smoke, across
      uninstall/reinstall, four launch/close cycles and two forced sidecar
      kills per run.
- [x] Lifecycle unit logs record fixed-label exit/restart/retry outcomes (`bd635ea2`).
- [ ] Host logs record spawn, health failure, exit status, restart and forced kill.
- [ ] Spawn/restart failure reaches the UI as an actionable degraded state.

### 3. Automated proof

All items below are evidence-backed at candidate `511ab1db` (`304c6284` plus
one docs commit; no source drift) — see
`projects/odysseus/AUTOMATED_PROOF_SUITE_2026-07-04.md`. Python suite:
297 passed, 0 failed, 0 skipped, with the autouse non-loopback egress guard
active suite-wide.

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

- [ ] Florence/runtime/resource hygiene verification passes.
- [x] NSIS installer is built from the recorded candidate SHA.
      Candidate `304c6284d8d0638e48171e9e181384ae364182ee` (includes source
      fix `7119e40c`), installer SHA-256
      `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`, size
      `32,362,413` bytes. This run passed both matrix runs.
- [ ] Clean install launches and reports backend/OCR/Florence truthfully.
      Clean install and launch passed in both re-run runs; OCR/Florence
      truthfulness was not separately checked.
- [x] Installed lifecycle matrix passes twice: close, relaunch, kill, recover, close.
      Both runs passed against candidate `304c6284` (installer SHA-256
      `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`), see
      `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`.
- [ ] Installer SHA-256 is recalculated after the final build.
- [ ] Published checksum and asset filename match the actual installer.

### 5. Release truthfulness

- [x] Package, Cargo, Tauri and Python runtime all report `0.3.0`.
      Source-level alignment at `5171fdf4` (`chore: align version sources to
      0.3.0`): `package.json`, `package-lock.json`, `src-tauri/Cargo.toml`,
      `Cargo.lock`, `tauri.conf.json` and Python `__version__` all `0.3.0`;
      build/check/test/fixture verification recorded in
      `projects/odysseus/VERSION_ALIGNMENT_2026-07-04.md`.
- [ ] Release notes describe v0.3 as proof/hardening, not agentic capability.
- [ ] Public naming is documented as PotatoCS project / Odysseus Desktop app.
- [ ] Test counts, skips and smoke claims are generated from the candidate SHA.
- [ ] Proof report records commands, environment, SHA, hashes and unresolved skips.

## Current hard failures

- Installer hash `D6E8A267...00EC209E` differs from checksum `5E2434D4...57E491B`;
  both artifacts are also version-stale (`v0.2.1`) after the `0.3.0`
  alignment — a final installer rebuild and checksum regeneration are required.
- (Resolved at `5171fdf4`: Python sidecar/app version disagreement — all
  sources now `0.3.0`.)

## Recorded evidence

- `e9f36fbcaeb62b19fb009df78e9306cef5b0e12d` bounds shutdown cleanup.
- Cargo test: 14 passed, 0 failed, 3 ignored helper fixtures.
- Cargo check: passed.
- This evidence does not prove host survival, restart, installed lifecycle, package,
  version or release readiness.
- `bd635ea2d5a99415923fe97fc60861587077e35e` adds forced-death fixture recovery.
- Cargo test: 18 passed, 0 failed, 4 ignored helper fixtures.
- Cargo check: passed.
- This evidence proves unit-level exit detection, one safe restart/retry maximum,
  unsafe no-replay and fixed-label logs; it does not prove installed behavior.
- `c2cc4d16` records the first installed lifecycle smoke attempt:
  `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`.
- Candidate: `1682dd14cdee9a3c145e3c6c034e5ebd54c2eced`. Installer SHA-256:
  `BE31DEF76A0A3EA60FAED198AC70FE0D4A9015EA2D1AEBD6D5835478D23C5F00`.
- Verdict: **FAIL**. Clean install launched; normal close passed; relaunch
  passed; idle sidecar kill did not terminate the host; killing the sidecar
  during startup `health.ping` terminated the host; fixed-label recovery logs
  were absent for that crash path; final orphan check passed.
- This evidence does not prove installed package, version, checksum or
  release readiness, and does not satisfy the two-run installed lifecycle
  matrix requirement.
- `7119e40c59dfb401be400242dee2f0fffde95fff` fixes startup `health.ping`
  sidecar death at the source level.
- Cargo test: 20 passed, 0 failed, 4 ignored helper fixtures.
- Cargo check and `git diff --check`: passed.
- Rust fixtures prove the host process survives a sidecar kill during
  startup `health.ping`, both when a retry recovers and when the retry
  itself also fails; fixed-label logs record the crash path with no
  RPC payload/private content.
- This evidence proves the source fix only; installed behavior is proved by
  the re-run below.
- `304c6284` records the installed lifecycle smoke re-run:
  `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`.
- Candidate: `304c6284d8d0638e48171e9e181384ae364182ee` (includes `7119e40c`).
  Installer SHA-256: `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`.
- Verdict: **PASS**, both runs. Clean install, normal close, relaunch, idle
  sidecar kill (host survives), sidecar kill during startup `health.ping`
  (host survives, auto-restarts) and final orphan check all passed twice.
  Fixed-label recovery logs (`context=startup_health`) present for both
  startup-kill events, no RPC payload/private content. Profile `app.db`
  (92,561,408 bytes) survived both runs.
- This evidence proves installed sidecar lifecycle/recovery only; it does not
  prove version alignment, checksum match or full release readiness.

## Gate rule

Do not call the candidate releasable while any required checkbox is open. A retry
is evidence only when its first failure, cleanup and second result are recorded.
