# v0.4 Model Recommendation Decision

Status: **APPROVED** (embedding model and chat-model table). Decided by
Fable per `V04_PARETO_VALUE_MAP.md` §5 ("model recommendations" are a
Fable/human-only decision). UI wiring is a separate follow-up change —
this document approves copy and commands only.

Context: Issue 5 (#15, Ollama/model setup helper, shipped as PR #25) needs
copyable `ollama pull` commands. §6 of the Pareto map allows
copyable-command UI only after the recommendation is approved. Hardware
tiers follow `POTATO_PROOF_MATRIX.md` §A.

## 1. Approved — embedding model (unchanged)

| Role | Model | Command | Why |
|---|---|---|---|
| Embedding | `nomic-embed-text` | `ollama pull nomic-embed-text` | Already the backend's wired default (`DEFAULT_EMBEDDING_MODEL`, `python/odysseus_desktop_backend/services/embedding_service.py:19`); it is what `rag.health` reports when semantic search is active. |

## 2. Approved — chat model table

Selection criteria: smallest mainstream instruct models with predictable
behavior; download and RAM cost stated up front; no gaming-laptop
assumptions; Ollama model names only.

| Tier | Hardware | Level | Model | Command | Approx. download | Why | Caveat |
|---|---|---|---|---|---|---|---|
| P0 | Survival machine, 4 GB RAM | Smallest usable | `llama3.2:1b` | `ollama pull llama3.2:1b` | ~1.3 GB | The smallest mainstream instruct model; already exercised by this repo's benchmark routes (`benchmarks/vision_common_sense/routes.json`). | **Best effort.** 4 GB may still be too little once the app, browser, and OS share RAM; expect slow answers or swapping. Honest "may be too weak" copy is required wherever this tier is described. |
| P1/P2 | Weak ordinary laptop / office PC, 8–16 GB RAM | **Recommended potato default** | `llama3.2:3b` | `ollama pull llama3.2:3b` | ~2.0 GB | Conservative step up in the same family; fits comfortably in 8 GB alongside the app; predictable instruction-following. | Quality is modest; that is the point. Do not suggest anything larger by default on this tier. |
| P3 | Old gaming laptop, 16 GB + low-end dGPU | Better quality if hardware allows | `qwen3:8b` | `ollama pull qwen3:8b` | ~5 GB | Noticeably better answers when RAM/dGPU headroom exists; also present in benchmark routes. | Optional upgrade only — never the default suggestion; large download; can still be slow on CPU-only machines. |

## 3. What is approved for the UI

- **One default command** in readiness setup guidance:
  `ollama pull llama3.2:3b` (the P1/P2 potato default). The app cannot
  sense RAM until the hardware audit (#20), so tier-specific commands in
  the UI are premature; one conservative default serves the thesis.
- Wording: the UI says **"try this first"**, not "recommended" — no
  potato-proof runs (Issue 11) have validated it yet.
- Fixed copy may mention, in words only, that very weak (4 GB) machines
  can try the smaller `llama3.2:1b` instead; only the default command
  gets a copy button.
- Commands remain copy-only text; the Re-check button remains the refresh
  path.

## 4. Not approved

- No automatic `ollama pull`; no hidden downloads of any kind.
- No bundling models with the installer.
- No vision model recommendation yet (optional/heavy stays command-less).
- No Potato Mode defaults until the hardware audit (#20) — this table
  approves setup-guidance copy only, not preset values.
- No endpoint changes; loopback default stays.

## 5. Follow-up implementation

Tracked as a separate small change (Issue: "feat: wire approved
chat-model command into readiness guidance"): update
`CHAT_MODEL_GUIDANCE` in `src/features/readiness/setupGuidance.ts` with
the approved default command and "try this first" copy, and update the
`scripts/test-readiness.mjs` assertions that currently enforce a
command-less chat guidance. Nothing else changes.
