# PotatoCS / Odysseus Status

Updated: 2026-07-04  
Authority: live repository plus the roadmap reconciliation report at HEAD below.

## Repository snapshot

- Branch: `main`, tracking `origin/main`.
- HEAD: `e9f36fbcaeb62b19fb009df78e9306cef5b0e12d`.
- App-source evidence commit: `e9f36fbc` (`fix: bound sidecar shutdown cleanup`).
- Current worktree: harness-only updates pending.
- Latest tag: `v0.2.1`; no v0.3 tag or version exists.
- Release gate: **RED — v0.3 proof incomplete**.

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

## Partial or unproved

- Window-specific screenshot capture is unsupported.
- Multimodal benchmark plumbing exists; committed real-route evidence is skipped/unscored.
- Local-first evidence is strong but is not an OS-level runtime firewall.
- Bounded sidecar cleanup has Cargo evidence; forced-death recovery remains unproved.
- Current HEAD has no single green, installed proof bundle.

## Not started

- Bounded tool execution and agent workflows.
- Local/web deep research.
- Memory and skills.
- Blind compare and Potato Cookbook.

## Active blockers

1. Killing the sidecar can take down the Tauri host.
2. Safe idempotent restart after forced sidecar death is unproved.
3. Spawn, exit and recovery failures lack sufficient persisted logging.
4. Current installer SHA-256 does not match the checked-in checksum.
5. Runtime version sources disagree.
6. Full installed/package proof has not passed at one immutable commit.

## Freeze

Until `GATE.md` is green, do not add agents, tools, research, memory, skills,
compare/Cookbook, new vision backends, window capture, full internal rebranding,
or unrelated UI/answer-quality work.
