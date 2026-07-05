# Evidence Index
Use paths as pointers; do not paste their full contents into harness files.

## Repository baseline
- Snapshot SHA: `946746de16e7124df6a1208085e935a0606d6552`.
- `README.md` (public claims); `docs/milestones.md` (scope/exclusions);
  `docs/releases/v0.2.1.md` (Operation Trace contract);
  `docs/v0.2.0-current-state-audit.md` (early multimodal audit).

## Versions and identity

- `package.json`, `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`,
  `python/odysseus_desktop_backend/__init__.py`.
- `src/features/shell/AppSidebar.tsx`,
  `python/odysseus_desktop_backend/services/chat_service.py`.

## Multimodal implementation

- `python/odysseus_desktop_backend/services/`: `artifact_service.py`,
  `image_preprocessing_service.py`, `ocr_service.py`, `model_service.py`,
  `vision_service.py`, `florence2_service.py`, `source_service.py`.
- `src-tauri/src/lib.rs` — capture and sidecar supervisor.
- `src/features/vision-diagnostics/ImageVisionDiagnostics.tsx`

## Benchmark evidence

- `src/features/image-evals/ImageBenchmarkPanel.tsx`
- `benchmarks/vision_common_sense/README.md`
- `python/odysseus_desktop_backend/vision_benchmarks/`
- `python/tests/test_v020_vision_common_sense_benchmark.py`
- `reports/vision_common_sense/smoke-*/report.md` — plumbing only.
- `reports/vision_common_sense/run-*/report.md` — current real route skipped/unscored.

## Trust/proof tests

- `python/tests/`: `test_v021_operation_trace.py`,
  `test_trace_privacy_sentinel.py`, `test_no_egress.py`, `test_progress.py`,
  `test_migrations.py`, `test_ipc_golden_fixtures.py`,
  `test_v011_rag_reliability.py`, `test_v014_real_retrieval.py`,
  `test_mvp_hardening_smoke.py`.
- Fixtures: `python/tests/fixtures/v021_schema.sql`,
  `python/tests/fixtures/ipc_contract.golden.json`.

## Packaging proof

- `scripts/build-release.ps1`, `scripts/build-core.ps1`
- `scripts/verify-packaged-florence.ps1`
- `scripts/verify-installer-resource-hygiene.ps1`
- `scripts/inspect-florence-installer.ps1`
- `src-tauri/target/release/bundle/nsis/Odysseus Desktop_0.3.0_x64-setup.exe`
- `dist/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`
- Historical `v0.2.1` installer/checksum mismatch
  (`D6E8A267...00EC209E` vs `5E2434D4...57E491B`) resolved at `e335705f`:
  stale checksum file deleted, matching `v0.3.0` file committed.

## Final v0.3.0 installer and proof — 2026-07-05

- Commit `e335705f` (`docs: add v0.3.0 release proof and checksum`), build
  candidate `e8702c50788d81207a5d712a5c196625f492c37f`:
  `projects/odysseus/RELEASE_PROOF_v0.3.0.md`, `docs/releases/v0.3.0.md`,
  `dist/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`.
- Installer SHA-256
  `0D759D2560919A5F8B657D8D9C245D965FD770745C01749F1D77DF022426FFB4`,
  size `32,375,003` bytes; Core hygiene PASS (all counters 0); installed
  runtime reports `0.3.0` (registry, exe metadata, sidecar source,
  `backend.log`); full Python suite re-run at candidate: 297/0/0.
- Does not prove publish-time asset match or the still-open `GATE.md`
  section 1/2 items; nothing is published.

## Installed lifecycle smoke failure (2026-07-04) — superseded

- Commit `c2cc4d16`: `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`
  (candidate `1682dd14`, installer `BE31DEF7...D23C5F00`). **FAIL** on
  startup `health.ping` kill; superseded by the passing re-run below while
  retaining the gate-rule first-failure and cleanup record.

## Installed lifecycle smoke re-run — PASS (2026-07-04)

- Commit `304c6284d8d0638e48171e9e181384ae364182ee` (includes source fix
  `7119e40c`): `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`.
- Candidate `304c6284d8d0638e48171e9e181384ae364182ee`, installer SHA-256
  `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`,
  size `32,362,413` bytes.
- Verdict **PASS**, two complete runs: clean install, normal close,
  relaunch, idle sidecar kill (host survives), sidecar kill during startup
  `health.ping` (host survives, auto-restart succeeds), final orphan check —
  all PASS both runs.
- Fixed-label recovery logs (`phase=exit/restart/retry`,
  `context=startup_health`) present for both startup-kill events, no RPC
  payload/private content. Profile `app.db` (92,561,408 bytes) survived both
  runs, matching the prior failing smoke's recorded size.
- Satisfies `GATE.md` section 4 two-run matrix and section 2 host-survival
  for the startup `health.ping` path.

## Version alignment to 0.3.0 — DONE (2026-07-04)

- Commit `5171fdf4841ac095a6cfb9fb5d8f2e5640f8a81c`
  (`chore: align version sources to 0.3.0`):
  `projects/odysseus/VERSION_ALIGNMENT_2026-07-04.md`.
- All six version sources now `0.3.0`; frontend build, cargo check/test
  (20/0/4 ignored) and migration/IPC fixture tests passed post-change.
- Closes the version half of `GATE.md` section 5's first item at source
  level. Does not prove installed runtime reporting, checksum match,
  release notes or naming docs; `v0.2.1` installer/checksum artifacts are
  now version-stale pending final rebuild.

## Automated proof suite — PASS (2026-07-04)

- Candidate `511ab1db593ceffe786306c32c4cf9572f751655` (`304c6284` plus one
  docs commit; no source drift):
  `projects/odysseus/AUTOMATED_PROOF_SUITE_2026-07-04.md`.
- Python 297 passed/0 failed/0 skipped with autouse non-loopback egress
  guard; `npm run test:progress`, `npm run build:frontend`, cargo
  check/test (20/0/4 ignored) and `git diff --check` all passed.
- Closes `GATE.md` section 3. Does not prove version alignment, checksum
  match, installed package hygiene or release truthfulness.

## Startup health-ping recovery evidence

- Commit `7119e40c` (`src-tauri/src/lib.rs` only): startup `health.ping`
  sidecar death no longer propagates a hard error out of the Tauri setup
  hook. Rust fixtures prove host survival (recovered and retry-also-fails
  cases); fixed-label `context=startup_health` logs, no payload content.
- Cargo test 20/0/4 ignored; cargo check and `git diff --check` passed.
  Installed-level proof is in the re-run entry above.

## Bounded shutdown evidence

- Commit `e9f36fbc` (`fix: bound sidecar shutdown cleanup`),
  `src-tauri/src/lib.rs` only. Proves graceful-shutdown deadline and
  hung-child forced kill/reap; fixed-label logs, no RPC payload content.
- Cargo test 14/0/3 ignored; cargo check passed. Does not prove host
  survival, installed lifecycle, package or release readiness.

## Forced sidecar recovery evidence

- Commit `bd635ea2` (`fix: recover from forced sidecar death`),
  `src-tauri/src/lib.rs` only. Proves exit detection without host
  termination; safe RPC one restart/one retry max; no non-idempotent
  replay; fixed-label exit/restart/retry logs.
- Cargo test 18/0/4 ignored; cargo check and `git diff --check` passed.
  Does not prove installed lifecycle, package, version or release readiness.

## Evidence rules

- Historical pass claims do not prove current HEAD.
- A smoke retry must retain the first failure and cleanup record.
- Stub benchmark reports prove plumbing, not model quality.
- A capability registry entry for `tools` is not a tool implementation.
- Upstream Git history or acknowledgments are not live-tree implementation evidence.
