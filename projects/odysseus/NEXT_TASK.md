# Next Task: Build Final 0.3.0 Installer, Regenerate Checksum, Produce Release Proof Report

Priority: **P0 release blocker**
Primary owner: **Human**, with Codex-assisted implementation
Gate: `GATE.md` sections 1, 2 (remaining), 4 (remaining) and 5 (remaining)

## Objective

Build the final v0.3.0 installer from the aligned candidate, regenerate a
matching SHA-256 checksum, and produce the release proof report. Version
alignment closed at `5171fdf4`
(`projects/odysseus/VERSION_ALIGNMENT_2026-07-04.md`); automated proof suite
closed at `511ab1db`
(`projects/odysseus/AUTOMATED_PROOF_SUITE_2026-07-04.md`). The gate remains
**RED**: final installer, checksum match, hygiene checks and release
truthfulness are open.

## Prerequisite

- Read `GATE.md` in full for current checkbox state.
- Existing `v0.2.1` installer and checksum artifacts are version-stale and
  mismatched; do not reuse them as evidence.

## Procedure

1. Record the final candidate SHA; confirm clean worktree.
2. Build the installer from that SHA (`npm run tauri:build:core` or the
   release script); restore the transiently deleted checksum file if the
   build removes it, then replace it deliberately in step 3.
3. Recalculate SHA-256 of the new installer; write the matching checksum
   file with the correct `v0.3.0` asset filename.
4. Run Florence/runtime/resource hygiene verification scripts
   (`GATE.md` section 4).
5. Verify the installed app reports `0.3.0` (package/Cargo/Tauri/Python) and
   backend/OCR/Florence truthfully on clean install.
6. Draft release notes describing v0.3 as proof/hardening only; document
   PotatoCS project / Odysseus Desktop naming.
7. Assemble the final proof report tying candidate SHA, commands,
   environment, hashes, test counts and unresolved skips together.

## Stop conditions

- Do not mark the full gate green until every open checkbox in sections
  1, 2, 4 and 5 has commit/artifact-backed evidence.
- Do not re-run the installed lifecycle smoke unless a source change
  invalidates the `304c6284` re-run evidence.
- Do not add product scope, branding changes or new capabilities.
