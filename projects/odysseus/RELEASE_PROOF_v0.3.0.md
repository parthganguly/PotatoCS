# v0.3.0 Release Proof Report — 2026-07-05

## Candidate

- Candidate SHA: `e8702c50788d81207a5d712a5c196625f492c37f`
  (`docs: record v0.3.0 version alignment evidence`).
- Branch `main` (`ahead 13` of `origin/main`); worktree clean at build start.
- Source lineage: `e8702c50` = `5171fdf4` (version alignment, last source
  change) + docs. Prior evidence commits `304c6284`/`511ab1db` differ from
  this candidate in version strings and docs only; no lifecycle source drift.
- Environment: Windows 11 Home 10.0.26200; dev Python 3.13.12 / pytest 9.0.3;
  embedded runtime Python 3.12.8 (downloaded at build time from python.org);
  vite 5.4.21; cargo release profile; NSIS via Tauri CLI.

## Installer and checksum

- Build command: `npm run tauri:build:core` (core variant, no Florence
  resources), exit 0.
- Installer: `src-tauri\target\release\bundle\nsis\Odysseus Desktop_0.3.0_x64-setup.exe`
- Size: `32,375,003` bytes.
- SHA-256: `0D759D2560919A5F8B657D8D9C245D965FD770745C01749F1D77DF022426FFB4`
- Checksum file: `dist/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`,
  asset name `PotatoCs-Odysseus-Desktop-v0.3.0-Windows-x64-setup.exe`
  (existing `PotatoCs` asset convention retained; publishing requires
  renaming/copying the installer to that asset name).
- The stale, mismatched `v0.2.1` checksum file was deliberately replaced
  (deleted) by the `v0.3.0` file. `dist/` is gitignored; the checksum file
  must be committed with `git add -f`, as its predecessor was.

## Package/resource hygiene (GATE section 4)

`build-core.ps1` ran, pre- and post-build:

- `prepare-python-runtime.ps1` + `verify-python-runtime.ps1`: embedded
  Python runtime verified; JSON-RPC sidecar verified; `runtime-ok`.
- `verify-installer-resource-hygiene.ps1 -Variant Core` against the built
  installer: PASS — all counters 0 (`evals`, `benchmarks`, `reports`,
  `private_temp`, `login_data`, `cookies`, `sqlite`, `db`, `florence`,
  `model_safetensors`, `torch`, `transformers`);
  "Installer resource hygiene verified for Core."
- Florence is intentionally excluded from the core installer
  (`hygiene_florence=0`); `verify-packaged-florence.ps1` applies to the
  Florence variant only and was not run.

## Installed version verification

Silent uninstall of 0.2.1, silent install of the new 0.3.0 installer, then:

- Registry `HKCU\...\Uninstall\Odysseus Desktop` `DisplayVersion`: `0.3.0`.
- `odysseus-desktop.exe` FileVersion/ProductVersion: `0.3.0`;
  ProductName `Odysseus Desktop`.
- Installed sidecar `python\odysseus_desktop_backend\__init__.py`:
  `__version__ = "0.3.0"`.
- Runtime log (`backend.log`): `JSON-RPC sidecar starting version=0.3.0`.
- Truthful reporting on launch: OCR `available=True engine=tesseract
  renderer=mutool`; Ollama detection logged honestly
  (`installed=False reachable=True models=6`); `florence_model_dir=unset`
  (core build, no Florence claimed).
- Both launches ended in clean backend shutdowns with **0 orphan processes**
  (host exits with its launching shell in this harness; each shutdown was
  graceful). The full installed lifecycle matrix was not re-run: the
  two-run PASS at `304c6284` stands (`INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`);
  the only source change since is version strings (`5171fdf4`).

## Automated test counts at candidate source

- Full Python suite re-run at `e8702c50` on 2026-07-05:
  **297 passed, 0 failed, 0 skipped** (204.89 s), autouse non-loopback
  egress guard active suite-wide.
- Cargo test at source-identical `5171fdf4`: **20 passed, 0 failed,
  4 ignored** (intentional helper-process fixtures); cargo check passed.
- `npm run test:progress`, `npm run build:frontend`, migration and IPC
  golden-fixture tests: passed post-alignment
  (`VERSION_ALIGNMENT_2026-07-04.md`); full suite detail in
  `AUTOMATED_PROOF_SUITE_2026-07-04.md`.
- `git diff --check`: exit 0.

## Release notes and naming

- `docs/releases/v0.3.0.md` drafted: proof/hardening only, explicitly no
  agentic capability; documents PotatoCS project / Odysseus Desktop app
  naming and the `PotatoCs` historical asset spelling.

## Unresolved skips and open items

- 4 cargo `ignored` tests: intentional helper-process fixtures, not skipped
  functionality.
- Vision common-sense benchmark real-route evidence remains
  skipped/unscored (plumbing proved only) — documented in release notes.
- Florence truthfulness verified only as "absent and not claimed" for the
  core installer; packaged-Florence verification applies to the Florence
  variant, which was not built.
- GATE items outside this task remain open: section 2 host
  spawn/health/exit logging checkbox, UI degraded-state checkbox, and the
  normal-close orphan checkbox at candidate level (observed clean here but
  matrix-level evidence is at `304c6284`); section 1 recording; final
  GATE/STATUS updates and any publish step.
- Artifacts (release notes, checksum file, this report) were produced after
  `e8702c50`; committing them will move HEAD past the candidate SHA —
  record that commit as the release-docs commit, with `e8702c50` as the
  build candidate.

## Command log (summary)

1. `git rev-parse HEAD` → `e8702c50…` (clean).
2. `npm run tauri:build:core` → exit 0; runtime verify + hygiene PASS.
3. `Get-FileHash -Algorithm SHA256` on the installer → `0D759D25…26FFB4`.
4. Wrote `dist/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`.
5. `uninstall.exe /S`; installer `/S`; registry/exe/sidecar version checks.
6. Two launches with log inspection; orphan checks → 0.
7. `python -m pytest python\tests` → 297/0/0.
8. `git diff --check` → exit 0.
