# v0.4.5 Storage Visibility and Cleanup — Result (Issue #17)

Status: implementation complete on `feat/v0.4-storage-cleanup`; draft PR open
for independent review. Design authority: `V04_STORAGE_CLEANUP_DESIGN.md`
(Phase 0, committed before implementation); semantics contract
`V04_ESSENTIAL_SEMANTICS.md` §D amended in the same PR.

## Commits

- Base: `3bbe44512af8cf156bc044dc43e5a4f04a264879` (Merge PR #32)
- Head: `HEAD_SHA_PLACEHOLDER`

Checkpoints: design (`8d110540`), backend (`be6cae7b`), UI + Rust contracts
(`adadaba8`), tests (`163d1588`), report (this commit).

## Architecture and ownership inventory

Discovered from code and recorded in `V04_STORAGE_CLEANUP_DESIGN.md` §1–§3:
profile root `<app_data>/profiles/default/` holds `app.db(+wal/shm)`,
`logs/`, `files/documents/`, `files/artifacts/{originals,normalized,vision,
ocr,thumbnails,crops,captures}`, optional `models/`, `profile.json`. Campaign
reports live outside the profile (`~/Downloads`) and are excluded. Ownership
and regenerability per category are classified in the design §2; unknown
files are counted as `other` and never deleted.

## Deletion sequence and failure semantics

`DocumentService.delete_user_document` (`document_service.py`):

1. missing row → fixed idempotent already-gone shape;
2. RPC layer releases active jobs first (cancel + bounded 5 s wait) or fails
   with fixed `source_busy`, nothing mutated;
3. one DB transaction: hard-`DELETE` chunks/pages/OCR rows; hard-`DELETE`
   the documents row, or tombstone (`is_deleted=1`) only while
   `message_documents` references exist; DB failure → rollback + fixed
   `delete_failed`, never a false success;
4. after commit: `_remove_owned_file` — refuses symlinks/reparse points,
   requires resolved direct child of `files/documents`; locked/unsafe file
   reported honestly (`file_removed: false`, 0 bytes claimed) and later
   reclaimed as an orphan by cleanup;
5. result carries `deleted_chunks/pages/ocr_pages`, `file_removed`,
   `file_missing`, `bytes_reclaimed` (only verified-removed bytes).

Artifacts follow the same shape (`ArtifactService.delete`): derivation rows
hard-deleted, row tombstoned only while `message_artifacts` or
`artifact_analysis_runs` reference it, confined file removal with byte
accounting. `artifacts.unindex` internal documents route through the new
document hard delete. The user's external `source_path` has no deletion code
path anywhere.

## Cleanup allow-list and exclusions

`StorageService.cleanup` (preview shares the same enumeration code):

1. embedding-cache rows matching no live chunk (bytes reported as reusable
   inside the DB file; no automatic VACUUM);
2. orphan files directly inside the eight app-owned data directories,
   unreferenced by any live row, non-link, direct child, older than 10
   minutes;
3. legacy soft-deleted (`is_deleted=1`) documents/artifacts: derived rows +
   owned files purged; row removed only when chat/analysis holds no
   reference.

Never touched (test-proved): `app.db*`, `logs/**`, `models/**`,
`profile.json`, unknown files, subdirectories, symlink/junction targets,
anything referenced by a live row, anything outside the profile. Cleanup is
refused with fixed `cleanup_busy` while any heavy job is active.

## RPC contracts

- `storage.status` (read; in `can_restart_and_retry`): profile root, total,
  six disjoint categories (database incl. WAL/SHM), `skipped_count`,
  `free_disk_bytes`, `low_disk` (< 1 GiB structural), source counts.
- `storage.cleanup_preview` (read; in allowlist): per-category candidates +
  reclaimable byte estimates.
- `storage.cleanup` (mutation; **not** replayed): per-category results,
  `reclaimed_file_bytes`, `reclaimed_db_bytes`, `skipped_count`,
  `failed_count`. Content-idempotent (second run reports zeros).
- `sources.delete`/`documents.delete` (mutations; **not** replayed): honest
  reclaim shape above; fixed `source_busy` when a job owns the source.

IPC golden fixture updated for the three methods, plus repair of a
**pre-existing** PR #32 frontend-inventory drift (`jobs.*` calls missing,
`documents.import` stale) — reproduced failing on clean base `3bbe4451`
before the fix.

## Migrations

None. No schema change; `SCHEMA_VERSION` stays 10. Tombstones reuse
`is_deleted`. Downgrade-safe: older builds understand both tombstones and
absent rows. Legacy pre-v0.4 soft deletes are converted only by explicit
user-initiated cleanup.

## Files changed

Python: `services/storage_service.py` (new), `pathsafety.py` (new),
`document_service.py`, `artifact_service.py`, `rag_service.py`,
`job_service.py`, `rpc_server.py`, `tests/test_v045_storage_cleanup.py`
(new), `tests/fixtures/ipc_contract.golden.json`,
`tests/test_v020_image_understanding.py`.
Rust: `src-tauri/src/lib.rs` (allowlist + assertions).
Frontend: `src/features/storage/{storageModel.ts,StoragePanel.tsx}` (new),
`src/api/storage.ts` (new), `src/App.tsx`, `src/features/shell/AppSidebar.tsx`,
`src/features/sources/SourcesPage.tsx`, `scripts/test-storage-ui.mjs` (new),
`package.json`.
Docs: `V04_STORAGE_CLEANUP_DESIGN.md` (new), `V04_ESSENTIAL_SEMANTICS.md`
(§D amendment), this report.

## Tests

| Suite | Result |
|---|---|
| `python -m pytest python\tests` | 419 passed, 1 skipped (symlink fixture needs privileges; junction path runs), 409.9 s |
| `npm run test:backend-status` | pass (`backend-status-tests-ok`) |
| `npm run test:progress` | pass (`chat-progress-tests-ok`) |
| `npm run test:readiness` | pass |
| `npm run test:jobs-ui` | pass (99 assertions) |
| `npm run test:storage-ui` (new) | pass (58 assertions) |
| `npm run build:frontend` | pass |
| `cargo check` | pass (exit 0) |
| `cargo test` | 24 passed, 0 failed, 4 ignored (pre-existing ignores) — includes new allowlist assertions |
| `git diff --check` | clean |

New focused coverage (`test_v045_storage_cleanup.py`, 25 tests): size
matches `os.walk` fixtures; WAL/SHM accounting; category reconciliation;
low-disk boundary at exactly 1 GiB; inaccessible entries skipped-not-zeroed;
junction escape not followed (real `mklink /J` fixture); deterministic scan;
delete reclaims file+chunks+pages and spares the original; idempotent
repeat; chat-referenced tombstone; malicious out-of-profile and symlinked
`stored_path` refused; locked-file honesty + orphan recovery; injected DB
failure → rollback, no false success, retryable; artifact hard delete;
cache/orphan/legacy cleanup with preview parity and age guard; cleanup
exclusion proofs (DB, logs, models, unknown files, subdirectories); cleanup
refused during active jobs; `release_source` bounded cancel-first wait;
fixed `source_busy` RPC code; sentinel sweeps over payloads and logs.

## Windows-specific evidence

- Junction (`mklink /J`, no elevation) fixtures prove the scanner counts
  reparse points as skipped and never descends or deletes through them
  (`test_scan_never_follows_symlink_or_junction_escape`,
  `test_pathsafety_is_link_guard`).
- Reparse detection uses `os.lstat().st_file_attributes &
  FILE_ATTRIBUTE_REPARSE_POINT`, covering junctions that `is_symlink()`
  misses.
- Locked-file semantics (Windows sharing violations) simulated via injected
  `PermissionError`; honest failure + orphan-recovery path proved.
- Symlink fixture test skips gracefully where symlink creation needs
  privileges; the junction path (the Windows-realistic attack) runs.

## Privacy evidence

- New logs carry fixed labels and counts only (`reason=symlink`,
  `reason=outside_profile`, `reason=os_error`, `reason=db_error`, counts).
- Sentinel tests plant hostile names in file names/content and assert
  absence from all storage/delete payloads (except the single allowed
  profile-root path in `storage.status`) and from storage/documents logs.
- Frontend copy is a closed fixed vocabulary (`storageModel.ts`);
  `test:storage-ui` proves arbitrary/hostile errors collapse to generic
  fixed copy and `String(error)` never renders for storage/delete surfaces.

## Known limitations

1. `documents.import`/`sources.import` synchronous paths keep their existing
   non-atomic semantics (documented in §B of the semantics contract).
2. `bytes_reclaimed` excludes DB row bytes by design; DB space is reported
   separately as reusable-inside-file. No automatic VACUUM.
3. The per-source `size_bytes` shown in Sources comes from import-time
   measurement (existing behavior), not a re-stat.
4. A file locked at delete time leaves a temporary orphan until the next
   cleanup run — reported honestly, never counted as reclaimed.
5. The legacy-cleanup enumeration measures candidate files at preview time;
   sizes may differ at execution if the profile changed in between (counts
   remain exact; cleanup is refused while jobs are active).
6. Log rotation remains out of scope (bounded at ~4 MB by the existing
   RotatingFileHandler).

## Go/no-go recommendation

**Go for draft-PR review; no-go for merge without independent review.** All
required suites pass locally on Windows 11 (P3-class dev machine). Deletion
semantics, path-safety guards, and no-replay contracts are test-proved. The
riskiest surfaces for the reviewer: the legacy-cleanup purge breadth
(§9.3), the tombstone-reference predicate (message/analysis links), and the
`release_source` race boundary.
