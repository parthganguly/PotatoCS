# Evidence Index
Use paths as pointers; do not paste their full contents into harness files.

## Repository baseline

- Snapshot SHA: `946746de16e7124df6a1208085e935a0606d6552`.
- `README.md` (public claims); `docs/milestones.md` (scope/exclusions);
  `docs/releases/v0.2.1.md` (Operation Trace contract);
  `docs/v0.2.0-current-state-audit.md` (early multimodal audit).

## Versions and identity

- `package.json`
- `src-tauri/Cargo.toml`
- `src-tauri/tauri.conf.json`
- `python/odysseus_desktop_backend/__init__.py`
- `src/features/shell/AppSidebar.tsx`
- `python/odysseus_desktop_backend/services/chat_service.py`

## Multimodal implementation

- `python/odysseus_desktop_backend/services/artifact_service.py`
- `python/odysseus_desktop_backend/services/image_preprocessing_service.py`
- `python/odysseus_desktop_backend/services/ocr_service.py`
- `python/odysseus_desktop_backend/services/model_service.py`
- `python/odysseus_desktop_backend/services/vision_service.py`
- `python/odysseus_desktop_backend/services/florence2_service.py`
- `python/odysseus_desktop_backend/services/source_service.py`
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

- `python/tests/test_v021_operation_trace.py`
- `python/tests/test_trace_privacy_sentinel.py`
- `python/tests/test_no_egress.py`
- `python/tests/test_progress.py`
- `python/tests/test_migrations.py`
- `python/tests/fixtures/v021_schema.sql`
- `python/tests/test_ipc_golden_fixtures.py`
- `python/tests/fixtures/ipc_contract.golden.json`
- `python/tests/test_v011_rag_reliability.py`
- `python/tests/test_v014_real_retrieval.py`
- `python/tests/test_mvp_hardening_smoke.py`

## Packaging proof

- `scripts/build-release.ps1`
- `scripts/verify-packaged-florence.ps1`
- `scripts/verify-installer-resource-hygiene.ps1`
- `scripts/inspect-florence-installer.ps1`
- `src-tauri/target/release/bundle/nsis/Odysseus Desktop_0.2.1_x64-setup.exe`
- `dist/PotatoCs-Odysseus-Desktop-v0.2.1-SHA256SUMS.txt`

Current mismatch (both artifacts also version-stale `v0.2.1`):
- Installer: `D6E8A267021366E347A0BD27E0E3796E30E70C6180EE790973D54CDC00EC209E`
- Checksum file: `5E2434D429A6E4132853791854839820E87FD0FED023C648A2C41D93F57E491B`

## Installed lifecycle smoke failure (2026-07-04) — superseded

- Commit `c2cc4d16`: `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`.
- Candidate `1682dd14`, installer SHA-256 `BE31DEF7...D23C5F00`.
- Verdict **FAIL**: sidecar kill during startup `health.ping` terminated the
  host, no fixed-label recovery logs; all other steps passed. Superseded by
  the passing re-run below; the report retains the first failure and cleanup
  record required by the gate's retry rule.

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
