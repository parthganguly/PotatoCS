# PotatoCS / Odysseus Status

Updated: 2026-07-04  
Authority: live repository plus the roadmap reconciliation report at HEAD below.

## Repository snapshot

- Branch: `main`, tracking `origin/main`.
- Last committed baseline before this harness update: `c2cc4d16d2f33735b34ef92e0a1b720567434404`.
- Shutdown evidence: `e9f36fbc` (`fix: bound sidecar shutdown cleanup`).
- Recovery evidence: `bd635ea2` (`fix: recover from forced sidecar death`).
- Current worktree: harness-only updates pending.
- Latest tag: `v0.2.1`; no v0.3 tag or version exists.
- Release gate: **RED — v0.3 proof incomplete**.
- Installed lifecycle smoke: **FAIL** — `c2cc4d16` records
  `projects/odysseus/INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md` against
  candidate `1682dd14cdee9a3c145e3c6c034e5ebd54c2eced`, installer SHA-256
  `BE31DEF76A0A3EA60FAED198AC70FE0D4A9015EA2D1AEBD6D5835478D23C5F00`. Clean
  install, normal close, relaunch and idle-sidecar-kill-survives-host all
  passed; killing the sidecar during startup `health.ping` terminated the
  host and no fixed-label recovery logs were recorded for that path.

## Version and naming state

- `package.json`, `src-tauri/Cargo.toml`, `src-tauri/tauri.conf.json`: `0.2.1`.
- Python sidecar `__version__`: `0.2.0`.
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

## Partial or unproved

- Window-specific screenshot capture is unsupported.
- Multimodal benchmark plumbing exists; committed real-route evidence is skipped/unscored.
- Local-first evidence is strong but is not an OS-level runtime firewall.
- Shutdown/recovery has Rust fixture evidence; installed behavior remains unproved.
- Current HEAD has no single green, installed proof bundle.

## Not started

- Bounded tool execution and agent workflows.
- Local/web deep research.
- Memory and skills.
- Blind compare and Potato Cookbook.

## Active blockers

1. Installed-app host survival and recovery after sidecar kill are unproved;
   worse, the startup `health.ping` path is proved failing (`c2cc4d16`).
2. Profile survival across installed kill/restart/relaunch is unproved.
3. Current installer SHA-256 does not match the checked-in checksum.
4. Runtime version sources disagree.
5. Full installed/package proof has not passed at one immutable commit.

## Freeze

Until `GATE.md` is green, do not add agents, tools, research, memory, skills,
compare/Cookbook, new vision backends, window capture, full internal rebranding,
or unrelated UI/answer-quality work.
