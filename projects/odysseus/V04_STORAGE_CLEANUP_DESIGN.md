# v0.4.5 Storage Visibility and Cleanup — Design (Issue #17)

Status: engineering design for GitHub Issue #17, written before implementation
(Phase 0 of the execution contract). Base commit: `3bbe4451` on
`feat/v0.4-storage-cleanup`. This document refines
`V04_ESSENTIAL_SEMANTICS.md` §D; where this design deliberately deviates from
§D (RPC naming, preview), the deviation is recorded in §12 and §D is amended
in the same PR.

## 1. Profile directory inventory (discovered from code)

Profile root: `<app_data>/profiles/default/` (`src-tauri/src/lib.rs:989`,
identifier `dev.odysseus.desktop`, never changed by this work).

| Path | Writer | Contents |
|---|---|---|
| `profile.json` | Rust host (`lib.rs:994`) | Profile identity record |
| `app.db`, `app.db-wal`, `app.db-shm` | `storage.py:24` (SQLite WAL) | All rows: documents, pages, OCR pages, chunks, embedding cache, artifacts, sessions, messages, benchmarks, settings |
| `logs/backend.log` (+ `.1`–`.3`) | `logging_config.py:13-37` (RotatingFileHandler, 1 MB × 4) | Backend log; **known to contain import paths** |
| `logs/shell.log` | Rust host `append_shell_log` | Host log |
| `files/documents/<uuid>.<ext>` | `document_service.py:43-68` | App-owned copies of imported documents |
| `files/documents/<uuid>.artifact.txt` | `artifact_service.py:658` | Generated text documents for indexed artifact derivations (`is_internal=1` rows) |
| `files/artifacts/originals/` | `artifact_service.py:112` | App-owned copies of imported images |
| `files/artifacts/{normalized,vision,ocr,thumbnails,crops}/` | `artifact_service.py:54-71` | Derived images (regenerable derivations, rows in `artifact_derivations`) |
| `files/artifacts/captures/` | Rust host `capture_output_path` (`lib.rs:930`) | Raw capture PNGs written before artifact import; the import then copies into `originals/` — capture files are referenced by no DB row afterwards |
| `models/<florence-pack-id>/` | User/installer (`florence2_service.py:459`) | Optional Florence-2 model pack. **Never touched** |
| `florence2-smoke-test.png` | `florence2_service.py:348` | Tiny generated smoke-test image at profile root |

Not in the profile: OCR render temp dirs (`tempfile.TemporaryDirectory`,
system temp, `ocr_service.py:443`), campaign reports (default
`~/Downloads/...`, `report_service.py:1628`, or a user-chosen
`output_folder`). Reports are user-facing exports outside the profile and are
**out of scope for accounting and cleanup**; §D's "reports" category is
amended accordingly (§12).

## 2. Ownership and regenerability classification

| Category | Owner | Regenerable? | Cleanup-eligible? |
|---|---|---|---|
| `app.db` + WAL/SHM | app | No (user data) | Never as files; only allow-listed row deletion |
| `files/documents/<id>.<ext>` referenced by a live (`is_deleted=0`) document | app | No (source may be gone) | Only via Source deletion |
| `files/documents` files referenced by **no** live document (orphans, incl. legacy soft-deleted copies) | app | n/a | Yes — orphan cleanup |
| `files/artifacts/originals` referenced by live artifact | app | No | Only via artifact deletion |
| `files/artifacts/{normalized,vision,ocr,thumbnails,crops}` referenced by a live derivation row | app | Yes, but regeneration costs compute | Not in v0.4 (kept; deleted with their artifact) |
| `files/artifacts/**` files referenced by no live artifact/derivation row | app | n/a | Yes — orphan cleanup |
| `files/artifacts/captures/*` | app (host) | No, but already copied into `originals/` on import | Yes — orphan cleanup (only when unreferenced) |
| `embedding_cache` rows with no matching live chunk | app | Yes (re-embed) | Yes — cache cleanup |
| Rows of soft-deleted (`is_deleted=1`) documents/chunks from pre-v0.4 deletes | app | n/a | Yes — legacy purge (rows + files), tombstone kept only where chat references exist |
| `logs/*` | app | n/a (diagnostic) | No (bounded at 4 MB by rotation; §D records log rotation out of scope) |
| `models/**` | user/installer | No (large download) | **Never** |
| `profile.json` | host | No | **Never** |
| `florence2-smoke-test.png` | app | Yes | No (96×64 px, trivial; avoids a special case) |
| Unknown files under the profile | unknown | unknown | **Never deleted**; counted as `other` in accounting |

## 3. Everything associated with a Source (document kind)

DB rows: `documents` (1), `document_pages` (CASCADE), `ocr_pages` (CASCADE),
`rag_chunks` (CASCADE), `message_documents` (RESTRICT — blocks row deletion
while chat history references it), `conversation_attachments` (no FK; readers
skip missing sources via KeyError), `artifact_rag_documents` (CASCADE; only
for internal artifact-derived documents), `embedding_cache` (shared by
content hash — **not** per-source), benchmark rows (historical ids in JSON
columns — never rewritten).

Filesystem: `files/documents/<id>.<ext>` (`stored_path`). The user's original
external file (`source_path`) is recorded but **never** owned or touched.

For an artifact Source: `artifacts` (1), `artifact_derivations` (CASCADE),
`message_artifacts` (RESTRICT), `artifact_analysis_runs` (RESTRICT),
`artifact_rag_documents` (CASCADE) plus its internal documents; files:
original + derivation files under `files/artifacts/**`.

## 4. Current user-delete flow (defect being fixed)

`sources.delete` / `documents.delete` → `RAGService.delete_document`
(`rag_service.py:164`): soft-deletes chunks (`is_deleted=1`,
`vector_store.py:116`) and soft-deletes the document row
(`document_service.py:593`). **No file is removed; no row is removed; no
byte is reclaimed.** `artifacts.delete` (`artifact_service.py:735`) removes
the original and derivation files (path-confined) but soft-deletes the
artifact row and, via `unindex`, soft-deletes internal documents leaving
their `.artifact.txt` copies orphaned on disk.

## 5. Current hard-purge flow and its safety properties

`DocumentService.purge_document` (`document_service.py:487`, PR #32): hard
`DELETE` of the documents row (CASCADE removes pages/OCR/chunks) plus
`_remove_owned_file` (`document_service.py:560`) which fails closed: refuses
symlinks (`is_symlink`), resolves strictly, requires the resolved path to be
a **direct child** of `<profile>/files/documents` and a regular file
(junction/reparse and traversal escapes resolve elsewhere and fail the parent
check), reports `(file_removed, bytes_reclaimed, file_missing)` honestly, and
never raises past an OSError. Used today only for staged-import rollback and
startup repair. This is the foundation the user-facing delete builds on.

## 6. Failure-atomic Source deletion sequence (new)

`delete_document` becomes (implemented in `DocumentService`, called by
`RAGService.delete_document` for compatibility with both RPC entry points):

1. **Lookup.** Row missing → fixed already-gone result (idempotent):
   `{deleted: true, already_deleted: true, ...zeroed counts}`. Row with
   `is_deleted=1` → same shape, zeroed reclaim, plus a re-attempt of orphan
   file removal (safe, path-validated) so retry-after-locked-file works.
2. **Active-job gate** (RPC layer, §8): cancel-first bounded wait, else fixed
   `source_busy` failure. The delete itself never runs concurrently with a
   job that owns the document.
3. **Measure.** Path-validate `stored_path` (same rules as
   `_remove_owned_file`); record file size if valid and present.
4. **DB purge, one transaction.** Count then hard-`DELETE` `rag_chunks`,
   `document_pages`, `ocr_pages` for the id. Then:
   - no `message_documents` reference → `DELETE FROM documents` (CASCADE
     already emptied);
   - referenced → tombstone: `is_deleted=1, status='deleted',
     index_status='deleted'` (chat history keeps rendering
     "attachment deleted", existing behavior).
   Commit. A DB failure here rolls back the transaction: no partial row
   state, no file touched yet, delete remains retryable, result is an error
   (never a false success).
5. **File removal, after commit.** `_remove_owned_file(stored_path)`. Failure
   (lock, unsafe path) is reported honestly (`file_removed: false`,
   `bytes_reclaimed: 0` for the file); the file is now an orphan that
   `storage.cleanup` reclaims later. Rationale (§D): rows are unambiguous app
   data; a locked file must not resurrect the source.
6. **Result** (exact shape):
   `{deleted, already_deleted, document_id, tombstoned, deleted_chunks,
   deleted_pages, deleted_ocr_pages, file_removed, file_missing,
   bytes_reclaimed}` — `bytes_reclaimed` counts only bytes verified removed
   in this call.

Artifact deletion is verified and completed to match: hard-delete derivation
rows + artifact row when no `message_artifacts`/`artifact_analysis_runs`
reference exists (else tombstone), keep the existing confined file removal,
and route `unindex` through the new hard document delete so `.artifact.txt`
copies and their rows are actually reclaimed. Analysis-run rows RESTRICT like
message links; an artifact with analysis history tombstones.

`embedding_cache` rows are never deleted per-source (shared by content hash);
they are reclaimed by cache cleanup (§9).

## 7. Path safety policy (mandatory, fail closed)

Unchanged from `_remove_owned_file` and extended to every new file
enumeration/removal:

- Resolve the owning app directory once with `resolve(strict=True)`.
- Refuse any candidate that `is_symlink()` or carries
  `FILE_ATTRIBUTE_REPARSE_POINT` (`os.lstat().st_file_attributes` on
  Windows — covers junctions); scanning **never** descends into such entries.
- Resolve the candidate strictly and require it inside the owning directory
  (deletion: direct child of the category dir; scanning: `relative_to`
  profile root).
- Any check failure → skip/refuse with a fixed log label
  (`reason=symlink|outside_profile|os_error`), never a path, and honest
  reporting (`file_removed: false` / `skipped_count += 1`).
- Inaccessible entries (PermissionError/OSError) are **skipped and counted**,
  never treated as zero-sized silently.
- No deletion API accepts an arbitrary path. The user's original
  `source_path` has no code path that deletes it.

## 8. Active-job interaction policy

- **Source deletion**: per §D, the RPC layer (`rpc_server.py`, which owns
  `self.jobs`) checks `JobService.active_document_ids()`. If the document has
  an active/queued job: request `jobs.cancel`, wait bounded
  (`DELETE_CANCEL_WAIT_SECONDS = 5`, structural constant, polling the job
  state) for a terminal state; on timeout fail with fixed code
  `source_busy` (retryable, nothing mutated). Import jobs whose staged
  document is invisible cannot be user-deleted anyway (not listed); the gate
  still covers OCR jobs on committed documents and any race.
- **Cleanup**: refused outright with fixed code `cleanup_busy` while
  `JobService.has_active_jobs()` — cleanup enumerates whole directories and
  must not race a staging import between file-copy and row-insert. Additional
  belt-and-braces: orphan candidates must be older than
  `ORPHAN_MIN_AGE_MS = 10 minutes` (mtime), so a file mid-import is never a
  candidate even if a job starts between the check and the unlink.
- **storage.status / cleanup preview**: read-only, allowed during jobs; no
  global lock is introduced anywhere.

## 9. Cleanup allow-list (explicit; everything else excluded)

`storage.cleanup` executes exactly three fixed categories; preview
enumerates candidates with the same code path:

1. **`embedding_cache`** — DELETE rows whose `(content_hash,
   embedding_model)` matches no live chunk
   (`rag_chunks.is_deleted=0`, join on `embedding_hash`/`embedding_model`).
   Reported as rows + vector-blob bytes with the fixed honesty note: space
   becomes reusable *inside* the database file; `VACUUM` is never run
   automatically.
2. **`orphan_files`** — files (never directories, never recursion through
   links) directly inside the app-owned data dirs — `files/documents`,
   `files/artifacts/{originals,normalized,vision,ocr,thumbnails,crops,captures}`
   — that are referenced by no live row (`documents.stored_path` with
   `is_deleted=0`; `artifacts.stored_path` / `artifact_derivations.stored_path`
   of non-deleted artifacts), pass §7 validation, and are older than
   `ORPHAN_MIN_AGE_MS`. Reported as count + bytes verified removed.
3. **`legacy_deleted_sources`** — rows with `is_deleted=1` (documents and
   artifacts): purge derived rows (chunks/pages/OCR pages/derivations) and
   owned files; hard-delete the row itself only when no
   `message_documents`/`message_artifacts`/`artifact_analysis_runs`
   reference exists (else the tombstone row stays, now with zero derived
   data). This converts pre-v0.4 soft deletes into the new semantics.

Explicit exclusions (tested): `app.db*`, `profile.json`, `logs/**`,
`models/**`, `florence2-smoke-test.png`, any unknown file or subdirectory,
any symlink/junction target, anything outside the profile, any file
referenced by a live row, `files/documents`/`files/artifacts` directories
themselves. There is no "delete everything in directory X" operation.

## 10. Storage accounting method (`storage.status`)

- One `os.scandir` walk from the profile root, iterative, never following
  symlinks/junctions (reparse points are counted as skipped entries and not
  descended into), never leaving the root, using `os.lstat` sizes.
- Category attribution by top-level location: `database` (`app.db`,
  `app.db-wal`, `app.db-shm` — WAL/SHM explicitly included and reported),
  `documents` (`files/documents`), `images` (`files/artifacts`), `logs`
  (`logs`), `models` (`models`), `other` (everything else under the root,
  including `profile.json`, the smoke-test image, and unknown files).
  Categories are disjoint by construction; `total_bytes` is their sum, so
  totals reconcile exactly; `skipped_count` reports entries that could not
  be measured (not counted as zero — reported as unmeasured).
- `free_disk_bytes` via `shutil.disk_usage(profile_root).free`;
  `low_disk = free < LOW_DISK_THRESHOLD_BYTES` (1 GiB, structural constant
  per §D — below one gigabyte an import/OCR run or WAL growth can plausibly
  fill the volume; not a tuned Potato Mode value).
- Response additionally carries `profile_dir` (the single allowed path,
  shown only in the explicit user-initiated storage view), source-count
  aggregates from existing queries, and no file names or child paths.
- Deterministic: same tree → same numbers; tested against `os.walk` sums on
  fixture profiles.

## 11. RPC contracts and frontend copy

| Method | Kind | In `can_restart_and_retry`? |
|---|---|---|
| `storage.status` | read | yes |
| `storage.cleanup_preview` | read | yes |
| `storage.cleanup` | mutation (idempotent by content, still never replayed) | **no** |
| `sources.delete` / `documents.delete` | mutation (idempotent result shape) | **no** (unchanged) |

`storage.cleanup` takes no arguments in v0.4 (all three categories, fixed
scope); preview and cleanup share the candidate-enumeration functions so the
preview provably matches execution on a quiescent profile. Failures use fixed
codes only: `cleanup_busy`, `source_busy`. Frontend copy is fixed-vocabulary
(`storageModel.ts`, mirroring `jobModel.ts`); `String(error)` /
`readError(err)` is never rendered for storage/deletion surfaces. Source
delete confirmation copy: “Delete "<name>"? This removes the app's local copy
and its search index. Your original file outside this app is not touched.”

UI surface: a new `Storage` view (sidebar), showing profile location, total
usage, category breakdown, free space, plain-language low-disk line, Refresh,
Clean up (preview → explicit confirm → exact reclaimed bytes + skipped
counts). No multi-profile management, relocation, auto-cleanup, or telemetry.

## 12. Downgrade, migration, privacy, no-replay

- **Schema**: no new columns or tables; tombstones reuse `is_deleted`.
  `SCHEMA_VERSION` stays 10; downgrade-safe (older builds already understand
  `is_deleted=1` rows and missing rows).
- **Semantics-contract amendments** (applied to `V04_ESSENTIAL_SEMANTICS.md`
  §D in this PR): (a) cleanup is exposed as `storage.cleanup_preview` +
  `storage.cleanup` instead of `storage.cleanup_cache`/`storage.cleanup_orphans`
  — one mutating endpoint with preview parity is smaller and previewable by
  construction; (b) the storage report's "reports, support bundles" category
  is dropped — reports live outside the profile (`~/Downloads`), support
  bundles don't exist until Issue #18; (c) cleanup adds the narrowly-scoped
  `legacy_deleted_sources` category (§9.3) to honor "legacy soft-deleted rows
  must be considered"; (d) reclaimable estimates live in the preview RPC, not
  `storage.status`.
- **Privacy**: no log line, progress event, job payload, or RPC error from
  the new code carries paths, file names, titles, or content — fixed labels
  and counts only. The profile root path appears only in the
  `storage.status` result for the explicit storage view (and was already
  exposed via `health.ping`/`diagnostics.get`). Sentinel tests plant hostile
  file/title strings and assert absence from logs and payloads.
- **No-replay**: mutations stay off the host allowlist; a Rust test asserts
  the two new reads are present and `storage.cleanup` is not.
