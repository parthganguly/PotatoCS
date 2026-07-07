# v0.4 Model Recommendation Decision

Status: **embedding model approved; chat model recommendation PROPOSED,
pending human approval — not wired into the UI.**

Context: Issue 5 (#15, Ollama/model setup helper) needs copyable
`ollama pull` commands. Per `V04_PARETO_VALUE_MAP.md` §5, "model
recommendations (which model, per tier)" are a Fable/human-only decision,
and §6 allows copyable-command UI only **after** the recommendation is
approved. This file records what is approved today and what is still a
proposal.

## 1. Approved today

| Role | Model | Command | Why it counts as approved |
|---|---|---|---|
| Embedding | `nomic-embed-text` | `ollama pull nomic-embed-text` | It is already the backend's wired default (`DEFAULT_EMBEDDING_MODEL`, `python/odysseus_desktop_backend/services/embedding_service.py:19`) and the model `rag.health` reports when semantic search is active. Recommending anything else would contradict what the app actually uses. |

This is the **only** model name the setup-guidance UI
(`src/features/readiness/setupGuidance.ts`) is allowed to show.

## 2. Proposed, NOT approved — chat model per hardware tier

Proposal criteria: smallest models with acceptable instruction-following;
download and RAM cost stated up front; tiers follow `POTATO_PROOF_MATRIX.md`.
**Do not wire any of these names into the UI until a maintainer approves
this table and the Potato Proof smoke (Issue 11) has exercised the chosen
model on P1/P2 hardware.**

| Tier | RAM | Proposed model | Approx. download | Rationale |
|---|---|---|---|---|
| P0 (very weak) | ≤ 4 GB | `llama3.2:1b` | ~1.3 GB | Smallest mainstream instruct model; already exercised by this repo's benchmark routes (`benchmarks/vision_common_sense/routes.json`). |
| P1 (weak) | ~8 GB | `llama3.2:3b` | ~2.0 GB | Conservative step up; same family, predictable behavior. |
| P2 (modest) | ~16 GB | `qwen3:8b` (or stay on `llama3.2:3b`) | ~5 GB | Only if the user asks for more quality; never the default suggestion. |

Open questions a human must settle before approval:

1. One single default for the UI, or per-tier suggestions? (Per-tier needs
   the Issue 10 hardware audit first — the app cannot sense RAM today.)
2. Quantization/tag pinning (e.g. `:1b` vs an explicit quant tag)?
3. Does the recommendation change the Potato Mode preset (Issue 4)?

## 3. Consequence for the setup helper (#15)

- Embedding gap → copyable `ollama pull nomic-embed-text`. Shipped.
- Chat-model gap → guidance says a small model is needed and that the
  specific recommendation is still being decided; **no copyable command**.
  Enforced by test: `scripts/test-readiness.mjs` asserts the chat-model
  guidance has no command.
- When this table is approved, wiring the approved name into
  `CHAT_MODEL_GUIDANCE` in `setupGuidance.ts` (plus the matching test
  update) is the entire remaining change.
