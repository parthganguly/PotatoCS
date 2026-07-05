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
      Final build: candidate `e8702c50`, clean worktree,
      `Odysseus Desktop_0.3.0_x64-setup.exe`, size `32,375,003` bytes
      (`e335705f`, `projects/odysseus/RELEASE_PROOF_v0.3.0.md`). The earlier
      matrix installer at `304c6284` is recorded in the smoke re-run report.
- [ ] Clean install launches and reports backend/OCR/Florence truthfully.
      Clean install and launch passed in both re-run runs; OCR/Florence
      truthfulness was not separately checked.
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
      Source-level alignment at `5171fdf4` (`chore: align version sources to
      0.3.0`): `package.json`, `package-lock.json`, `src-tauri/Cargo.toml`,
      `Cargo.lock`, `tauri.conf.json` and Python `__version__` all `0.3.0`;
      build/check/test/fixture verification recorded in
      `projects/odysseus/VERSION_ALIGNMENT_2026-07-04.md`. Installed runtime
      verified at the final installer: registry, exe metadata, installed
      sidecar source and live `backend.log` all report `0.3.0`
      (`RELEASE_PROOF_v0.3.0.md`).
- [x] Release notes describe v0.3 as proof/hardening, not agentic capability.
      `docs/releases/v0.3.0.md` (`e335705f`).
- [x] Public naming is documented as PotatoCS project / Odysseus Desktop app.
      `docs/releases/v0.3.0.md`, including the historical `PotatoCs` asset
      spelling.
- [ ] Test counts, skips and smoke claims are generated from the candidate SHA.
- [x] Proof report records commands, environment, SHA, hashes and unresolved skips.
      `projects/odysseus/RELEASE_PROOF_v0.3.0.md` (`e335705f`), candidate
      `e8702c50`, including test counts and unresolved skips.

## Current hard failures

- None. (Resolved at `5171fdf4`: version disagreement. Resolved at
  `e335705f`: installer/checksum mismatch — stale `v0.2.1` checksum replaced
  by the matching `v0.3.0` file.) Open checkboxes above still block release.

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
- `c2cc4d16` records the first installed smoke attempt (**FAIL**: startup
  `health.ping` kill terminated the host, no recovery logs; other steps
  passed): `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`,
  candidate `1682dd14`, installer `BE31DEF7...D23C5F00`. Retained as the
  gate-rule first-failure record; superseded by the re-run.
- `7119e40c` fixes startup `health.ping` sidecar death at source level;
  Rust fixtures prove host survival (recovered and retry-also-fails cases),
  fixed-label logs, no payload content. Cargo test 20/0/4 ignored; cargo
  check and `git diff --check` passed.
- `304c6284` records the installed smoke re-run (**PASS**, two full runs:
  install, close, relaunch, idle kill, startup `health.ping` kill with
  auto-restart, orphan checks; `app.db` 92,561,408 bytes survived):
  `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`,
  installer `04A1C2BD...C3C7DCC`. Proves installed lifecycle/recovery only.
- `e335705f` records the final v0.3.0 installer, matching checksum file,
  release notes and proof report (candidate `e8702c50`):
  `projects/odysseus/RELEASE_PROOF_v0.3.0.md`.

## Gate rule

Do not call the candidate releasable while any required checkbox is open. A retry
is evidence only when its first failure, cleanup and second result are recorded.
