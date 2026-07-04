# PotatoCS Revised Roadmap

This roadmap supersedes the original version-to-feature mapping. It reflects the
live repository rather than upstream or historical Odysseus capabilities.

## Delivered foundation

### v0.2.x — Local multimodal workspace

- Images, Sources, screenshots/clipboard, OCR and image-derived RAG.
- Ollama vision and optional Florence-2 Basic.
- Model capability registry, image diagnostics and benchmark infrastructure.
- Operation Trace and answer-quality corrections.

Result: the original v0.2.0 vision and much of v0.2.1 benchmark/diagnostic scope
landed together. v0.2.1 became the public Operation Trace release.

## Current release line

### v0.3.0 — Proof and hardening

Minimum scope:

- Bounded sidecar shutdown, forced-death survival and safe recovery.
- Persisted lifecycle diagnostics.
- Trace privacy and progress-ID guarantees.
- Non-loopback test guard and local runtime evidence.
- Schema migration and IPC compatibility gates.
- RAG grounding/restart proof.
- Reproducible installed-package proof and matching checksum.
- One consistent runtime version and honest release notes.

Explicitly excluded:

- Tools, agents, MCP and shell execution.
- Research or web search.
- Memory, skills, compare and Cookbook.
- New vision backends, window capture, redesign and full identity migration.

### v0.3.1 — Stabilization only

- Regressions found after v0.3.0.
- Small packaging, diagnostics or compatibility fixes.
- No new agentic product surface.

## Future product lines

### v0.4.0 — Safe bounded tools

- Start only after the v0.3 proof gate remains stable.
- Explicit allowlists, user confirmation, cancellation and audit traces.
- No unrestricted shell; no ambient access to files, network or credentials.
- Define threat model and tool-specific proof gates before implementation.

### v0.5.0 — Local-first research

- Local document research first; web research only as explicit opt-in.
- Source capture, citations, cancellation, budgets and network disclosure.
- Do not restore historical upstream research code without a fresh audit.

### v0.6.0 — Memory and skills

- User-visible, inspectable and deletable memory.
- Scoped skills with provenance, permissions and versioning.
- No silent memory extraction or cross-profile leakage.

### v0.7.0 — Blind compare and Potato Cookbook

- Blind local model comparison with comparable settings and evidence.
- Cookbook/model-fit guidance for ordinary hardware.
- Reuse benchmark trust rules; do not present smoke data as scientific ranking.

## Deferred maintenance

- Full PotatoCS executable/profile/identifier migration.
- Window-specific screenshot capture.
- Smaller installer variants and model-pack distribution improvements.
- Larger scored multimodal campaigns.

## Promotion rule

Advance a feature line only when its predecessor has a green gate, an installed
artifact, matching hashes and a compact proof report tied to one commit SHA.
