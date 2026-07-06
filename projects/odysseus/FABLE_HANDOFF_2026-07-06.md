# Fable Handoff Map — 2026-07-06

For Sonnet/Codex reviewers. Docs only; no source, releases, or merges in this commit.

## 1. Current released state

- **v0.3.0 is released and published.** GitHub Release assets verified
  (installer + `PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`, installer
  SHA-256 `0D759D25…26FFB4`, downloaded-hash match recorded).
- **v0.3.0 gate is GREEN** — `projects/odysseus/GATE.md`.
- README v0.3.0 download/checksum verification docs are done; **issue #2 closed**.
- Full evidence chain: `projects/odysseus/RELEASE_PROOF_v0.3.0.md`,
  `EVIDENCE_INDEX.md`, `docs/releases/v0.3.0.md`.

## 2. Open PRs

### PR #5 — v0.3.1 degraded-backend UI
https://github.com/parthganguly/odysseus-desktop/pull/5

- Branch `review/v0.3.1-degraded-ui` → base `main`; head `99b0e2a5`.
- Two commits: `10e172d7` (implementation, amended with Sonnet must-fix
  fixes) and `99b0e2a5` (smoke plan, `V031_DEGRADED_UI_SMOKE.md`).
- **Changes:** when sidecar spawn/restart recovery is exhausted, backend sets
  degraded, emits fixed-payload `backend_degraded` event, UI shows a
  privacy-safe banner with "Retry backend"; degraded clears on recovery.
- **Tests run:** cargo test 24/0/4, cargo check, `npm run
  test:backend-status`, `npm run test:progress`, `npm run build:frontend`,
  `git diff --check` — all pass.
- **Review focus:** degraded true/false transitions (both directions, only
  on change); non-idempotent sidecar loss degrades **without replay**;
  retry semantics (one restart max, user retry reuses restart path);
  privacy-safe UI/logs (fixed copy, fixed labels, no payloads/paths/traces);
  `backend_degraded` emitted **after** the mutex guard is dropped;
  smoke plan practicality.
- **Must prove before merge:** live smoke per
  `projects/odysseus/V031_DEGRADED_UI_SMOKE.md`
  (forced failure → banner → retry recovers or stays safely degraded →
  privacy grep clean). Unit tests alone are not sufficient.

### PR #6 — build no longer deletes release checksum (issue #4)
https://github.com/parthganguly/odysseus-desktop/pull/6

- Branch `fix/build-does-not-delete-release-checksum` → base `main`;
  head `c3ed54d7`.
- **Changes:** moves the tracked checksum record out of Vite-generated
  `dist/` to `docs/releases/PotatoCs-Odysseus-Desktop-v0.3.0-SHA256SUMS.txt`
  (git 100%-similarity rename) and updates repo-path references in
  `EVIDENCE_INDEX.md` / `RELEASE_PROOF_v0.3.0.md`. No app source touched.
- **Tests run:** `npm run build:frontend` exit 0 with record surviving;
  `git diff --check` clean.
- **Review focus:** checksum content preserved byte-for-byte (still contains
  `0D759D25…26FFB4` + installer asset name); repo record path vs published
  release asset name not confused (the GitHub asset did **not** move);
  build no longer deletes the record; no release assets changed.

## 3. Open issues

- **#1** — degraded backend UI. Keep open until PR #5's live smoke passes;
  do not close from unit tests.
- **#3** — v0.4 planning. Do not implement anything from it until v0.3.1
  is resolved.
- **#4** — build deletes tracked checksum. Closes automatically when PR #6
  merges (`Closes #4` in body).

## 4. Exact next reviewer sequence

1. Review and **merge PR #6 first** — small, docs/record-only, removes the
   build footgun that deletes the checksum record.
2. Review PR #5 code (focus list above).
3. Run the PR #5 live smoke from `projects/odysseus/V031_DEGRADED_UI_SMOKE.md`
   (throwaway profile; sentinel strings; capture the evidence table).
4. If smoke passes: merge PR #5 and close issue #1 with the evidence.
5. Only then decide whether to cut a v0.3.1 patch release.
6. If cutting v0.3.1: open a fresh gate/checklist (model it on `GATE.md`).
7. Do **not** start v0.4 implementation until issue #3 is resolved.

## 5. Suggested v0.3.1 release criteria

- PR #6 merged (or consciously deferred with a note).
- PR #5 reviewed and approved.
- Live degraded-UI smoke passes; issue #1 closed.
- Version bumped to `0.3.1` in all version sources.
- Release notes written (`docs/releases/v0.3.1.md`).
- Installer rebuilt from a recorded candidate SHA.
- Installer SHA-256 calculated and recorded in a new checksum file under
  `docs/releases/` (never `dist/`).
- GitHub Release asset uploaded; downloaded hash verified to match.
- v0.3.1 gate/checklist green.

## 6. What not to do

- Do not add agents/tools/research/memory/skills/Cookbook/compare/new
  vision backends without a new gate.
- Do not merge PR #5 without the live smoke.
- Do not close issue #1 from unit tests alone.
- Do not publish v0.3.1 without installer/hash/release verification.
- Do not store tracked release records in generated `dist/`.
- Do not modify v0.3.0 release assets, retag, or rebuild the v0.3.0 installer.

## 7. Commands for reviewers

```powershell
git fetch origin

# PR #6 — checksum record move
gh pr checkout 6
npm run build:frontend        # must exit 0
git status                    # checksum record must NOT appear deleted
git diff --check

# PR #5 — degraded-backend UI
gh pr checkout 5
npm run test:backend-status
npm run test:progress
cargo check --manifest-path src-tauri/Cargo.toml
cargo test  --manifest-path src-tauri/Cargo.toml   # expect 24/0/4 ignored
npm run build:frontend
git diff --check
# then run the live smoke: projects/odysseus/V031_DEGRADED_UI_SMOKE.md
```

---
*Handoff frozen at 2026-07-06. `origin/main` head `4187b30a`; PR #5 head
`99b0e2a5`; PR #6 head `c3ed54d7`. Issues #1, #3, #4 open; issue #2 closed.*
