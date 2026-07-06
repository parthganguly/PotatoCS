# v0.3.1 Release Proof Report — 2026-07-06

## Candidate

- Candidate SHA: `971c01021da2fd3c471b7bec53b9c37f079642d7`
  (`release: prepare v0.3.1 patch gate (#8)`, squash-merge of
  `release/v0.3.1-prep` into `main`; worktree clean at build start).
- Source lineage: candidate = `v0.3.0` + PRs #5 (degraded-backend UI,
  `0721cfb6`), #6 (checksum record out of `dist/`, `458f7a8b`),
  #7 (handoff map, docs) + post-release docs + the #8 version bump.
  The only source change since v0.3.0 is PR #5.
- Environment: Windows 11 Home 10.0.26200; embedded runtime Python 3.12.8
  (downloaded at build time from python.org); vite 5.4.21; cargo release
  profile; NSIS via Tauri CLI.

## Installer and checksum

- Build command: `npm run tauri:build:core` (core variant, no Florence
  resources), exit 0.
- Installer: `src-tauri\target\release\bundle\nsis\Odysseus Desktop_0.3.1_x64-setup.exe`
- Size: `32,370,775` bytes.
- SHA-256: `F130D92BB1974035EAA28089B06AC06EF65F251FA072FF9E28595180211C111D`
- Checksum file: `docs/releases/PotatoCs-Odysseus-Desktop-v0.3.1-SHA256SUMS.txt`
- Expected published asset name:
  `PotatoCs-Odysseus-Desktop-v0.3.1-Windows-x64-setup.exe`
  (publishing requires renaming/copying the installer to that asset name).
- Resource hygiene (`verify-installer-resource-hygiene.ps1 -Variant Core`):
  PASS — all counters 0 (`evals`, `benchmarks`, `reports`, `private_temp`,
  `login_data`, `cookies`, `sqlite`, `db`, `florence`,
  `model_safetensors`, `torch`, `transformers`).

## Automated tests (run on the identical source at PR #8 branch)

Run 2026-07-06 on `release/v0.3.1-prep` (source-identical to the candidate
squash-merge; recorded in `GATE_v0.3.1.md` §3):

- `npm run test:backend-status` — `backend-status-tests-ok`.
- `npm run test:progress` — `chat-progress-tests-ok`.
- `cargo check` — clean; `cargo test` — 24 passed, 0 failed, 4 ignored
  (intentional helper-process fixtures).
- `npm run build:frontend` — built; the v0.3.0 checksum record in
  `docs/releases/` survived the build (issue #4 fix confirmed).
- `git diff --check` — clean.
- Live degraded-UI smoke: PASS, recorded pre-merge in
  `V031_DEGRADED_UI_SMOKE_RESULT.md` (issue #1 closed).

## Installed version verification (2026-07-06)

Silent uninstall of 0.3.0 (exit 0), silent install of the new 0.3.1
installer (exit 0), then:

- Registry `HKCU\...\Uninstall\Odysseus Desktop` `DisplayVersion`: `0.3.1`.
- `odysseus-desktop.exe` FileVersion/ProductVersion: `0.3.1`;
  ProductName `Odysseus Desktop`.
- Installed sidecar `python\odysseus_desktop_backend\__init__.py`:
  `__version__ = "0.3.1"`.
- Runtime log (`backend.log`): `JSON-RPC sidecar starting version=0.3.1`,
  launched from the install dir
  (`AppData\Local\Odysseus Desktop\python-runtime\python.exe`).
- Backend status on launch: OCR `available=True engine=tesseract
  renderer=mutool`; Ollama detection logged honestly
  (`installed=False reachable=True models=6`); `florence_model_dir=unset`.
- Degraded-UI patch presence: the frontend bundle built by this installer
  build (`dist/assets/*.js`, embedded into the exe) contains the
  `Retry backend` action string; the last `src/` change at the candidate is
  the PR #5 commit `0721cfb6`. A live degraded-path re-run was not done on
  the installed app; the pre-merge live smoke stands.
- Graceful close (`CloseMainWindow`): host exited, `backend shutdown`
  logged, **0 orphan sidecar processes** (the only surviving `python.exe`
  was the unrelated system Python 3.13 with a live non-app parent).

## Caveats

- A stale `evals\` directory from a pre-v0.3.0 install (dated 2026-06-15)
  remains in the install dir; the NSIS uninstaller does not remove
  directories it does not own. The v0.3.1 installer itself ships none
  (`hygiene_evals=0`).
- The full installed lifecycle matrix (idle kill, kill during startup
  `health.ping`) was not re-run for 0.3.1; the v0.3.0 two-run PASS plus the
  pre-merge v0.3.1 degraded-UI live smoke cover the changed paths.
- The full Python suite (297 tests) was not re-run at the candidate; no
  Python source changed since v0.3.0 except `__version__`.
- GitHub Release upload and downloaded-asset hash verification remain
  pending; gate stays RED until then.

## Command log (summary)

1. `gh pr merge 8 --squash` → merge SHA `971c0102…`; main synced, clean.
2. `npm run tauri:build:core` → exit 0; runtime verify + hygiene PASS.
3. `Get-FileHash -Algorithm SHA256` → `F130D92B…111D`; size 32,370,775.
4. Wrote `docs/releases/PotatoCs-Odysseus-Desktop-v0.3.1-SHA256SUMS.txt`.
5. `uninstall.exe /S` (0.3.0); installer `/S`; registry/exe/sidecar checks.
6. Launch, log inspection, graceful close, orphan check → 0.
