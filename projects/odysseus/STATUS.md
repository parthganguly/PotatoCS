# PotatoCS / Odysseus Status

Updated: 2026-07-05.
Authority: live repository plus the roadmap reconciliation report at HEAD below.

## Repository snapshot

- Branch: `main`, tracking `origin/main`.
- Last committed baseline: `7119e40c59dfb401be400242dee2f0fffde95fff`.
- Shutdown evidence: `e9f36fbc` (`fix: bound sidecar shutdown cleanup`).
- Recovery evidence: `bd635ea2` (`fix: recover from forced sidecar death`).
- Startup health-ping fix: `7119e40c` (`fix: make startup sidecar health.ping
  failure non-fatal`) — source-level only, see `GATE.md` section 2.
- Current worktree: clean at `e335705f` after final v0.3.0 artifact commit.
- Final v0.3.0 installer: built from candidate `e8702c50`
  (`Odysseus Desktop_0.3.0_x64-setup.exe`, SHA-256 `0D759D25...26FFB4`,
  32,375,003 bytes); matching checksum file, release notes and proof report
  committed at `e335705f` — see
  `projects/odysseus/RELEASE_PROOF_v0.3.0.md`. Installed runtime verified
  reporting `0.3.0`. Not published.
- Version alignment: **DONE** at `5171fdf4` — all version sources report
  `0.3.0`; see `projects/odysseus/VERSION_ALIGNMENT_2026-07-04.md`.
- Automated proof suite: **PASS** at `511ab1db` —
  `projects/odysseus/AUTOMATED_PROOF_SUITE_2026-07-04.md` (Python 297/0/0,
  progress tests, frontend build, cargo check/test, `git diff --check`);
  closes `GATE.md` section 3.
- Latest tag: `v0.2.1`; no v0.3 tag or version exists.
- Release gate: **RED — publish-time asset verification only**; all other
  boxes evidence-backed or explicitly deferred (see `GATE.md`).
- Installed lifecycle smoke: **PASS** —
  `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04_RERUN.md`
  records two complete passing runs against candidate
  `304c6284d8d0638e48171e9e181384ae364182ee` (includes source fix
  `7119e40c`), installer SHA-256
  `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`. Clean
  install, normal close, relaunch, idle sidecar kill, sidecar kill during
  startup `health.ping`, and final orphan check all passed twice; the host
  now survives and auto-recovers from a sidecar kill during startup
  `health.ping`, with fixed-label recovery logs present.

## Version and naming state

- `package.json`, `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`,
  Python sidecar `__version__`: all `0.3.0` (aligned at `5171fdf4`).
- Public brand: `PotatoCs`; requested canonical spelling: `PotatoCS`.
- Shipped app identity: `Odysseus Desktop` / `odysseus-desktop`.
- Profile identifier: `dev.odysseus.desktop`.
- Treat this as “PotatoCS project, Odysseus Desktop app” until a migration is planned.

## Implemented

- Local chat, document import, RAG, OCR, persistence and reports.
- PNG/JPEG/WebP artifacts, Sources facade and session/library attachments.
- Full-screen/region capture and clipboard image import.
- Ollama vision with capability inspection through `/api/show`.
- Optional packaged Florence-2 Basic support.
- Image diagnostics, image eval UI and visual-common-sense benchmark harness.
- Per-answer Operation Trace with timing, routing, source IDs and warnings.
- Trace privacy sanitization, private-sentinel sweep and strict progress IDs.
- Test egress guard, proxy stripping and offline Florence flags.
- Schema migration gates and frontend/Rust/Python IPC golden fixtures.
- Bounded graceful shutdown and forced kill/reap for a hung owned sidecar.
- Forced-death detection with one safe idempotent restart/retry maximum.
- Non-idempotent RPCs are not replayed after sidecar loss.
- Fixed-label exit/restart/retry lifecycle logs.
- Startup `health.ping` sidecar death is non-fatal to the setup hook,
  proved at both source (`7119e40c`) and installed levels (re-run smoke).

## Partial or unproved

- Window-specific screenshot capture is unsupported.
- Multimodal benchmark plumbing exists; committed real-route evidence is skipped/unscored.
- Local-first evidence is strong but is not an OS-level runtime firewall.
- Shutdown/recovery Rust fixture evidence is now backed by a passing
  installed-level re-verify for the startup `health.ping` path.
- The release gate is still not green: remaining open checkboxes are listed
  under Active blockers; version alignment, checksum match, automated proof
  suite and the final installer/proof bundle are closed.

## Not started

- Bounded tool execution and agent workflows.
- Local/web deep research.
- Memory and skills.
- Blind compare and Potato Cookbook.

## Active blockers

1. Publish-time only: the single open `GATE.md` checkbox is published
   checksum/asset verification. The repo-artifact checksum matches locally;
   the uploaded asset hash/filename must be verified at release time.
   All other gate boxes are evidence-backed or explicitly deferred
   (spawn/restart degraded-state UI → v0.3.1/v0.4); the `e1c8c774`
   pre-freeze answer-quality exception is documented in
   `docs/releases/v0.3.0.md`.

## Freeze

Until `GATE.md` is green, do not add agents, tools, research, memory, skills,
compare/Cookbook, new vision backends, window capture, full internal rebranding,
or unrelated UI/answer-quality work.
