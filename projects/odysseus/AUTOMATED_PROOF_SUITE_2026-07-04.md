# Automated Proof Suite Report — 2026-07-04

## Verdict

**PASS.** All six commanded checks passed at one immutable commit with zero
test failures and zero unexpected skips.

## Candidate

- Candidate SHA: `511ab1db593ceffe786306c32c4cf9572f751655`
  (`docs: record installed lifecycle smoke pass`).
- Relationship to installed-smoke candidate: `511ab1db` is `304c6284` plus one
  harness-docs commit only; no `src-tauri/` or `python/` source differs from
  the installed-smoke candidate.
- Branch: `main` (`ahead 10` of `origin/main`); worktree clean at start and end.
- Environment: Windows 11 Home 10.0.26200, Python 3.13.12, pytest 9.0.3,
  vite 5.4.21, app version sources still `0.2.1` (unchanged by this task).

## Commands and results

| Command | Result |
|---|---|
| `python -m pytest python\tests` | PASS — 297 passed, 0 failed, 0 skipped (182.29 s) |
| `npm run test:progress` | PASS — `chat-progress-tests-ok` |
| `npm run build:frontend` | PASS — `tsc && vite build`, built in 7.97 s (chunk-size warning only, non-fatal) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | PASS |
| `cargo test --manifest-path src-tauri\Cargo.toml` | PASS — 20 passed, 0 failed, 4 ignored |
| `git diff --check` | PASS — exit 0, no output |

## Failures

None.

## Skipped/ignored tests

- Python: 0 skipped.
- Rust: 4 `ignored` tests, all intentional helper-process fixtures
  (`lifecycle_already_exited_fixture`, `lifecycle_shutdown_responsive_fixture`,
  `lifecycle_single_rpc_response_fixture`, `lifecycle_sleeping_child_fixture`)
  — same set recorded in prior evidence at `7119e40c`.

## GATE.md section 3 coverage mapping

- **Non-loopback egress guard**: active suite-wide — `python/tests/conftest.py`
  installs an autouse fixture patching `socket.socket.connect` and
  `socket.create_connection` to raise on non-loopback hosts (opt-out only via
  explicit `allow_egress` marker). `test_no_egress.py`: 6 passed.
- **Trace privacy sentinel**: `test_trace_privacy_sentinel.py`: 1 passed; plus
  `test_v021_operation_trace.py` (12 passed) covering no-path/no-payload
  persistence.
- **Progress identifier strict mode**: `test_progress.py`: 14 passed,
  including `test_filename_shaped_identifier_rejected_under_strict_trace_mode`.
- **Schema migration**: `test_migrations.py`: 4 passed —
  `test_upgrade_from_v021_reaches_fresh_schema_and_preserves_rows` (upgrade),
  `test_future_version_db_is_refused_without_down_stamp` (future-version
  refusal), `test_init_schema_is_idempotent` (idempotence),
  `test_fresh_db_stamps_current_schema_version`.
- **IPC golden fixtures**: `test_ipc_golden_fixtures.py`: 5 passed.
- **RAG grounding/retrieval/restart-persistence**:
  `test_v011_rag_reliability.py`: 3 passed (grounding/quote-first/verifier);
  `test_v014_real_retrieval.py`: 10 passed (real retrieval ranking, fallback,
  grader parity); restart persistence via
  `test_milestone2_rag_backend.py::test_import_index_search_delete_and_restart_persistence`
  (file: 12 passed).

## GATE.md section 3 checkbox status after this run

Evidence-backed at `511ab1db` (previously open):

- Full Python suite passes with the non-loopback egress guard active.
- Trace privacy sentinel sweep passes.
- Progress identifier tests pass in strict mode.
- Schema upgrade, future-version refusal and idempotence tests pass.
- IPC golden fixtures pass.
- RAG grounding/retrieval and restart-persistence tests pass.
- `npm run test:progress` passes.
- `npm run build:frontend` passes.

Already checked, re-confirmed at `511ab1db`:

- `cargo check --manifest-path src-tauri/Cargo.toml` passes.
- `cargo test --manifest-path src-tauri/Cargo.toml` passes (20/0/4 ignored).

All section 3 items now have passing evidence at this single SHA.
GATE.md checkboxes were **not** edited by this task.

## Remaining open (outside section 3)

- Section 1: scope/source checkboxes (unrecorded).
- Section 2: normal-close orphan checkbox, host spawn/health/exit logging,
  UI degraded-state checkboxes.
- Section 4: Florence/resource hygiene, OCR/Florence truthfulness on clean
  install, final-build SHA-256 recalculation, published checksum/filename
  match.
- Section 5: all items (version alignment to `0.3.0`, release notes, naming
  docs, generated counts, final proof report).
- Hard failures unchanged: Python sidecar `0.2.0` vs app `0.2.1`; installer
  hash vs checked-in checksum mismatch.

## Worktree note

`npm run build:frontend` (vite) cleared `dist/`, transiently deleting tracked
`dist/PotatoCs-Odysseus-Desktop-v0.2.1-SHA256SUMS.txt` — the same side effect
recorded in `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`. The tracked
file was restored via `git checkout --` immediately after; no checksum was
regenerated or modified.

## Git status at end

`## main...origin/main [ahead 10]`, clean; HEAD `511ab1db` unchanged;
`git diff --check` exit 0. No app source, versions, checksums or release
notes were modified. Only this report file was created.
