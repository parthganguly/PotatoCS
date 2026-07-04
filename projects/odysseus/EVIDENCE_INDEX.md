# Evidence Index

Use paths as pointers; do not paste their full contents into harness files.

## Repository baseline

- Snapshot SHA: `946746de16e7124df6a1208085e935a0606d6552`.
- `README.md` — public capability, privacy, limitation and old smoke claims.
- `docs/milestones.md` — Sources/vision scope and explicit agent/tool exclusions.
- `docs/releases/v0.2.1.md` — Operation Trace release contract.
- `docs/v0.2.0-current-state-audit.md` — early multimodal implementation audit.

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

Current mismatch:

- Installer: `D6E8A267021366E347A0BD27E0E3796E30E70C6180EE790973D54CDC00EC209E`
- Checksum file: `5E2434D429A6E4132853791854839820E87FD0FED023C648A2C41D93F57E491B`

## Installed lifecycle evidence

- `%APPDATA%/dev.odysseus.desktop/profiles/default/logs/backend.log`
- July 4 lines near 2431–2437 show shutdown followed by launch records without
  corresponding Python startup or persisted recovery failure.
- Interactive observation: killing the sidecar can terminate the Tauri host.

## Installed lifecycle smoke failure (2026-07-04)

- Commit: `c2cc4d16d2f33735b34ef92e0a1b720567434404`.
- Subject: `docs: record installed lifecycle smoke failure`.
- Report path: `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`.
- Candidate SHA tested: `1682dd14cdee9a3c145e3c6c034e5ebd54c2eced`.
- Installer SHA-256: `BE31DEF76A0A3EA60FAED198AC70FE0D4A9015EA2D1AEBD6D5835478D23C5F00`.
- Verdict: **FAIL**.
- Clean install launched: PASS.
- Normal close: PASS.
- Relaunch: PASS.
- Idle sidecar kill: PASS — did not terminate host.
- Sidecar kill during startup `health.ping`: **FAIL** — terminated host.
- Fixed-label recovery logs for the crash path: **absent**.
- Final orphan check: PASS.
- Does not prove installed package, version, checksum or release readiness,
  and does not satisfy the required two-run installed lifecycle matrix.

## Bounded shutdown evidence

- Commit: `e9f36fbcaeb62b19fb009df78e9306cef5b0e12d`.
- Subject: `fix: bound sidecar shutdown cleanup`.
- Changed path: `src-tauri/src/lib.rs` only.
- Proves: graceful shutdown deadline; hung-child forced kill and reap.
- Cargo test: 14 passed, 0 failed, 3 ignored helper fixtures.
- Cargo check: passed.
- Lifecycle logs use fixed request/cleanup results without RPC payload content.
- Does not prove host survival after external kill, restart, installed lifecycle,
  package integrity, version alignment or release readiness.

## Forced sidecar recovery evidence

- Commit: `bd635ea2d5a99415923fe97fc60861587077e35e`.
- Subject: `fix: recover from forced sidecar death`.
- Changed path: `src-tauri/src/lib.rs` only.
- Rust fixture proves sidecar exit detection without terminating the test host.
- Safe allowlisted RPC: one restart and one retry maximum.
- Non-idempotent RPC: no restart/replay after sidecar loss.
- Fixed-label logs cover exit, restart attempted/succeeded/failed and retry result.
- Cargo test: 18 passed, 0 failed, 4 ignored helper fixtures.
- Cargo check and `git diff --check`: passed.
- Does not prove installed lifecycle, installed profile continuity, package integrity,
  version alignment, checksum correctness or release readiness.

## Evidence rules

- Historical pass claims do not prove current HEAD.
- A smoke retry must retain the first failure and cleanup record.
- Stub benchmark reports prove plumbing, not model quality.
- A capability registry entry for `tools` is not a tool implementation.
- Upstream Git history or acknowledgments are not live-tree implementation evidence.
