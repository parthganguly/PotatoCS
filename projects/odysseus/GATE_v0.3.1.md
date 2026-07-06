# v0.3.1 Patch Release Gate

State: **RED — installer/build/publish verification pending**

Scope: patch release only — degraded-backend UI (PR #5), checksum-record
build safety (PR #6), and docs shipped since v0.3.0. No v0.4 features.

## 1. Scope

- [x] Patch only: no agents/tools/research/memory/skills/Cookbook/compare
      or new vision backends; no v0.4 features included.
- [x] Only PRs #5, #6, #7 and post-release docs are in `v0.3.0..main`.

## 2. Version sources report 0.3.1

- [x] `package.json`
- [x] `package-lock.json` (both root entries)
- [x] `src-tauri/Cargo.toml`
- [x] `src-tauri/Cargo.lock` (`odysseus-desktop` package entry)
- [x] `src-tauri/tauri.conf.json`
- [x] `python/odysseus_desktop_backend/__init__.py` (`__version__`)

## 3. Automated tests (run on release/v0.3.1-prep)

- [x] `npm run test:backend-status` — backend-status-tests-ok
- [x] `npm run test:progress` — chat-progress-tests-ok
- [x] `cargo check --manifest-path src-tauri/Cargo.toml` — clean
- [x] `cargo test --manifest-path src-tauri/Cargo.toml` — 24 passed, 0 failed, 4 ignored
- [x] `npm run build:frontend` — built
- [x] `git diff --check` — clean

## 4. Live proof

- [x] Degraded-UI live smoke result exists and passed:
      `projects/odysseus/V031_DEGRADED_UI_SMOKE_RESULT.md` (issue #1
      closed after live smoke).
- [x] Checksum record survives `npm run build:frontend` (verify
      `docs/releases/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`
      still present and unmodified after the build).

## 5. Installer and publish (blocked — not started)

- [ ] Installer built from a recorded candidate SHA on this branch/main.
- [ ] Installer SHA-256 calculated and recorded (checksum file + proof
      report, following the v0.3.0 pattern).
- [ ] Installed app reports version 0.3.1.
- [ ] GitHub Release `v0.3.1` asset uploaded and downloaded-asset hash
      independently verified.

## Exit criteria

The gate turns GREEN only when every box above is checked. Until then,
v0.3.0 remains the latest published release; do not tag or publish v0.3.1.
