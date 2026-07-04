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

- [ ] Graceful shutdown has a bounded deadline.
- [ ] Hung shutdown reaches forced kill and child reap.
- [ ] Normal close leaves no Python sidecar orphan.
- [ ] Killing only the sidecar does not terminate the Tauri host.
- [ ] A safe idempotent request can restart the sidecar once.
- [ ] Profile data survives shutdown, forced death, restart and relaunch.
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
- [ ] `cargo check --manifest-path src-tauri/Cargo.toml` passes.
- [ ] `cargo test --manifest-path src-tauri/Cargo.toml` passes.

### 4. Installed package proof

- [ ] Florence/runtime/resource hygiene verification passes.
- [ ] NSIS installer is built from the recorded candidate SHA.
- [ ] Clean install launches and reports backend/OCR/Florence truthfully.
- [ ] Installed lifecycle matrix passes twice: close, relaunch, kill, recover, close.
- [ ] Installer SHA-256 is recalculated after the final build.
- [ ] Published checksum and asset filename match the actual installer.

### 5. Release truthfulness

- [ ] Package, Cargo, Tauri and Python runtime all report `0.3.0`.
- [ ] Release notes describe v0.3 as proof/hardening, not agentic capability.
- [ ] Public naming is documented as PotatoCS project / Odysseus Desktop app.
- [ ] Test counts, skips and smoke claims are generated from the candidate SHA.
- [ ] Proof report records commands, environment, SHA, hashes and unresolved skips.

## Current hard failures

- Installed sidecar shutdown/kill/recovery smoke is not green.
- Python sidecar version is `0.2.0` while app sources say `0.2.1`.
- Installer hash `D6E8A267...00EC209E` differs from checksum `5E2434D4...57E491B`.

## Gate rule

Do not call the candidate releasable while any required checkbox is open. A retry
is evidence only when its first failure, cleanup and second result are recorded.
