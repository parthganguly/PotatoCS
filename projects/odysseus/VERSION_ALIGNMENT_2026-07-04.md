# Version Alignment to 0.3.0 — 2026-07-04

## Verdict

**PASS.** All runtime/source version declarations now read `0.3.0`; builds
and targeted tests pass; diff contains only version lines.

## Base

- Base SHA (HEAD, uncommitted worktree changes on top):
  `7b1e80ca` (`docs: record automated proof suite pass`).
- Branch: `main` (`ahead 11` of `origin/main`).

## Changes (6 files, 7 lines)

| File | Before | After |
|---|---|---|
| `package.json` | `0.2.1` | `0.3.0` |
| `package-lock.json` (root + `packages[""]`) | `0.2.0` (stale) | `0.3.0` |
| `src-tauri/Cargo.toml` | `0.2.1` | `0.3.0` |
| `src-tauri/Cargo.lock` (`odysseus-desktop` entry) | `0.2.1` | `0.3.0` (updated by `cargo check`) |
| `src-tauri/tauri.conf.json` | `0.2.1` | `0.3.0` |
| `python/odysseus_desktop_backend/__init__.py` | `0.2.0` | `0.3.0` |

Notes:

- `package-lock.json` was already stale (`0.2.0` vs `package.json` `0.2.1`);
  both root entries now match `0.3.0`.
- `src/features/shell/AppSidebar.tsx` and
  `python/odysseus_desktop_backend/services/chat_service.py` (the other
  version-bearing files in `EVIDENCE_INDEX.md`) contain no hardcoded version
  string — verified by grep, unmodified.
- No branding, profile identifier (`dev.odysseus.desktop`), behavior, release
  notes or checksum changes. Installer not rebuilt.

## Verification commands

| Command | Result |
|---|---|
| `npm run build:frontend` | PASS — `odysseus-desktop@0.3.0`, built in 10.08 s (chunk-size warning only) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | PASS — compiled `odysseus-desktop v0.3.0` |
| `cargo test --manifest-path src-tauri\Cargo.toml` | PASS — 20 passed, 0 failed, 4 ignored helper fixtures |
| `python -m pytest python\tests\test_migrations.py python\tests\test_ipc_golden_fixtures.py` | PASS — 9 passed |
| `git diff --check` | PASS — exit 0 |

## Worktree note

`npm run build:frontend` again transiently deleted tracked
`dist/PotatoCs-Odysseus-Desktop-v0.2.1-SHA256SUMS.txt` (known side effect);
restored via `git checkout --` before the remaining checks. No checksum was
regenerated or modified.

## Remaining for GATE.md section 5

This closes only the version-alignment half of the first section 5 item at
source level. Still open: installed runtime proof that all four report
`0.3.0`, release notes, naming docs, generated counts and the final proof
report. The installer/checksum mismatch (section 4) is unchanged and the
existing `v0.2.1` installer/checksum artifacts are now version-stale by
design until the final rebuild.

## Git status at report time

`## main...origin/main [ahead 11]`; modified (uncommitted): the six version
files above plus this report (untracked). Not committed — awaiting
instruction.
