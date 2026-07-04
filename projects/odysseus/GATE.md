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
- [ ] Killing only the sidecar does not terminate the Tauri host.
      Installed smoke `c2cc4d16` (candidate `1682dd14`) proves this **FAILS**
      for the startup `health.ping` path: killing the owned sidecar during
      that request terminated the host, and no fixed-label recovery logs
      were emitted for that crash path. Idle-sidecar kill after startup
      passed in the same run.
- [x] Rust forced-death fixture detects exit and keeps the test host alive (`bd635ea2`).
- [x] A safe idempotent request can restart/retry the sidecar once (`bd635ea2`).
- [x] Non-idempotent requests are not replayed after sidecar loss (`bd635ea2`).
- [ ] Profile data survives shutdown, forced death, restart and relaunch.
- [x] Lifecycle unit logs record fixed-label exit/restart/retry outcomes (`bd635ea2`).
- [ ] Host logs record spawn, health failure, exit status, restart and forced kill.
- [ ] Spawn/restart failure reaches the UI as an actionable degraded state.

### 3. Automated proof

- [ ] Full Python suite passes with the non-loopback egress guard active.
- [ ] Trace privacy sentinel sweep passes.
- [ ] Progress identifier tests pass in strict mode.
- [ ] Schema upgrade, future-version refusal and idempotence tests pass.
- [ ] IPC golden fixtures pass.
- [ ] RAG grounding/retrieval and restart-persistence tests pass.
- [ ] `npm run test:progress` passes.
- [ ] `npm run build:frontend` passes.
- [x] `cargo check --manifest-path src-tauri/Cargo.toml` passes (`bd635ea2`).
- [x] `cargo test --manifest-path src-tauri/Cargo.toml` passes (`bd635ea2`).

### 4. Installed package proof

- [ ] Florence/runtime/resource hygiene verification passes.
- [ ] NSIS installer is built from the recorded candidate SHA.
      Attempted at `c2cc4d16`: candidate `1682dd14cdee9a3c145e3c6c034e5ebd54c2eced`,
      installer SHA-256 `BE31DEF76A0A3EA60FAED198AC70FE0D4A9015EA2D1AEBD6D5835478D23C5F00`.
      Not counted as passing because the run this installer was tested in failed.
- [ ] Clean install launches and reports backend/OCR/Florence truthfully.
      Clean install and launch succeeded in `c2cc4d16`; OCR/Florence
      truthfulness was not separately checked in that run.
- [ ] Installed lifecycle matrix passes twice: close, relaunch, kill, recover, close.
      **NOT COMPLETE.** `c2cc4d16` records only a single, failing run: normal
      close and relaunch passed; idle sidecar kill did not terminate the host;
      killing the sidecar during startup `health.ping` did terminate the host
      with no fixed-label recovery logs for that path; final orphan check
      passed after cleanup. The required second full run was not executed.
- [ ] Installer SHA-256 is recalculated after the final build.
- [ ] Published checksum and asset filename match the actual installer.

### 5. Release truthfulness

- [ ] Package, Cargo, Tauri and Python runtime all report `0.3.0`.
- [ ] Release notes describe v0.3 as proof/hardening, not agentic capability.
- [ ] Public naming is documented as PotatoCS project / Odysseus Desktop app.
- [ ] Test counts, skips and smoke claims are generated from the candidate SHA.
- [ ] Proof report records commands, environment, SHA, hashes and unresolved skips.

## Current hard failures

- Installed sidecar shutdown/kill/recovery smoke is not green. `c2cc4d16`
  (`projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`) proves
  **FAIL**: killing the owned sidecar during the startup `health.ping`
  request terminates the installed host, with no fixed-label recovery logs
  for that crash path.
- Python sidecar version is `0.2.0` while app sources say `0.2.1`.
- Installer hash `D6E8A267...00EC209E` differs from checksum `5E2434D4...57E491B`.

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

## Gate rule

Do not call the candidate releasable while any required checkbox is open. A retry
is evidence only when its first failure, cleanup and second result are recorded.
