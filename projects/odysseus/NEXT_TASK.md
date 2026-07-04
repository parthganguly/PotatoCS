# Next Task: Close Remaining v0.3 Release Gate Sections

Priority: **P0 release blocker**
Primary owner: **Human**, with Codex-assisted implementation
Gate: `GATE.md` sections 1, 3, 4 (remaining items) and 5

## Objective

The installed lifecycle matrix now passes twice at candidate `304c6284`
(source fix `7119e40c`), closing `GATE.md` section 2's host-survival
checkbox and section 4's two-run matrix checkbox — see
`projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md` and
`EVIDENCE_INDEX.md`. The release gate remains **RED**: version alignment,
checksum match, the automated proof suite (section 3) and release
truthfulness (section 5) are still open.

## Prerequisite

- Read `GATE.md` in full for the current checkbox state.
- Read `STATUS.md` "Active blockers" for the remaining concrete gaps:
  installer SHA-256 vs. checked-in checksum mismatch, and Python sidecar
  version (`0.2.0`) vs. app sources (`0.2.1`) disagreement.

## Procedure

1. Decide and record the target release version alignment across
   `package.json`, `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`, and
   the Python sidecar `__version__`.
2. Run the full automated proof suite listed in `GATE.md` section 3
   (Python tests with egress guard, trace privacy sentinel, progress tests,
   schema/IPC fixtures, RAG tests, `npm run test:progress`,
   `npm run build:frontend`) and record pass/fail per item.
3. Rebuild the installer from the final aligned-version candidate and
   recalculate/publish a matching SHA-256 checksum file.
4. Draft release notes describing v0.3 as proof/hardening only.
5. Assemble the final proof report tying candidate SHA, commands,
   environment, hashes and any unresolved skips together.

## Stop conditions

- Do not modify branding beyond documented PotatoCS/Odysseus Desktop naming.
- Do not mark the full release gate green until every `GATE.md` checkbox in
  sections 1, 3, 4 and 5 has commit/artifact-backed evidence.
- Do not re-run the installed lifecycle smoke again unless a source change
  invalidates the existing `304c6284` re-run evidence.
