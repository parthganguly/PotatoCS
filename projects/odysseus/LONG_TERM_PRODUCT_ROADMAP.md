# Long-Term Product Roadmap — Odysseus Desktop / PotatoCS

Status: planning only. This reconciles the original Odysseus feature
inventory with the PotatoCS thesis. It treats the original plan as a
**feature inventory to draw from**, not a spec to copy. Near-term
authority stays with `ROADMAP.md`, `V04_POTATO_MODE_SCOPE.md`, and
`V04_EXECUTION_PLAN.md`; this document governs everything after v0.4.
Era boundaries are intent, not promises — eras may split or reorder, but
the ordering constraints in §B and the constraints in §D do not move.

## A. Product thesis

PotatoCS is not trying to make a tiny model magically frontier-level. It
uses deterministic software, specialist local tools, retrieval, OCR,
capability routing, queues, diagnostics, and honest constraints to make
small local models useful on ordinary hardware. The model is the least
reliable component in the system, so the product invests in everything
around it: evidence, guardrails, recovery, and truthful copy about what
the machine in front of the user can and cannot do.

## B. Roadmap eras

### Era 0 — v0.3.x: shipped proof and hardening (done)

- Lifecycle hardening, bounded sidecar shutdown, sidecar recovery.
- Release proof discipline (gates, checksums, installed-app verification).
- Degraded-backend banner + Retry.
- v0.3.1 released, gate GREEN (`GATE_v0.3.1.md`).

### Era 1 — v0.4: Potato baseline (accepted scope, in planning)

- First-run readiness screen; no empty-chat first impression.
- Potato Mode conservative-settings preset.
- Ollama/model setup helper (no auto-downloads).
- Indexing/OCR guardrails: throttle, pause/cancel, giant-doc limits.
- Profile storage visibility and cleanup.
- Noob diagnostics / redacted support bundle.
- **Potato Proof Matrix** (`POTATO_PROOF_MATRIX.md`): proof on P1/P2-class
  hardware or documented simulated equivalent — not just a gaming laptop.

Everything after this era is conditional on the Potato baseline being
excellent. If v0.4 proof is weak, Era 2 waits.

### Era 2 — v0.5: foundations for richer capability

Infrastructure, not user-visible flash. All local-first, all low-end
aware (each component must state its cost on P0–P2 hardware).

- Artifact Service: one place where generated outputs (reports, extracts)
  live, sized, listed, and deletable — extends v0.4 storage work.
- Model Capability Registry: formalize what each installed model can do
  (vision, context, speed class) — v0.2.x groundwork exists.
- Generic Job Engine: queued, pausable, cancellable, crash-safe jobs —
  generalizes the v0.4.4 indexing guardrails.
- Hardware/resource profiler: RAM/CPU/disk sensing that feeds honest
  recommendations (the measured basis Potato Mode defaults deserve).
- Capability-aware routing: pick the model/tool per task from the
  registry + profiler, never silently upgrade to something heavier.

### Era 3 — v0.6: multimodal image/screenshot understanding

- Image/screenshot intake into Sources (partially shipped in v0.2.x).
- OCR + optional vision model per the Capability Registry.
- Region selection for "look at this part."
- Image evidence package: answers cite image regions like text snippets.
- **No continuous screen monitoring, ever** — explicit capture only.
- OCR-only mode is the potato default; VLM is opt-in and labeled heavy.
- Multimodal benchmark so image answers have the same proof discipline.

### Era 4 — v0.7: bounded tools / safe workflows

Only after the Job Engine and Capability Registry exist — tools without
queues and audit are how potatoes freeze and users get hurt.

- Small allowlist of tools (e.g., read file, list folder, convert doc).
- Workspace-scoped: tools see the profile/workspace, not the machine.
- Confirmation required for anything destructive or outward-facing.
- Every tool run leaves an audit trace (fixed-label, privacy-safe).
- **No unrestricted shell.** Not as an option, not as a flag.

### Era 5 — v0.8: local research

- Deep research over the user's private local files first — multi-step
  retrieval plans executed by the Job Engine, resumable, cancellable.
- Citations and bookkeeping done by deterministic software, not by
  trusting the model to remember sources.
- Optional web research later, behind an explicit network mode the user
  turns on per-task — never a silent fallback.

### Era 6 — v0.9: memory and skills

- Memory is explicit and user-approved: the app proposes, the user
  accepts; every memory is editable, deletable, and shows its source.
- Skills are reusable workflows (steps + tools + checks), not just saved
  prompts; they run on the Job Engine with the same audit trail.

### Era 7 — v1.x: Compare and the Potato Cookbook

- Blind model comparison on the user's own tasks and hardware.
- Hardware-aware model advisor: speed/quality/resource history per model
  on this machine; "best model for your machine" recommendations.
- Potato Cookbook: curated, tested model+settings recipes per tier.
- Model downloads remain explicit user actions only.

### Later / optional (no era assigned)

- Document editor; notes/tasks — only if they serve the evidence
  workflow, not to become an office suite.
- Basic image utilities (crop/rotate for intake).
- Generative image editing: optional heavy extension only, never core.
- Email/calendar integration: much later, if ever; huge privacy surface.
- Broader MCP: much later, and only through the Era 4 bounded-tool
  discipline.
- Mobile/PWA: not near-term; the product is a desktop app for weak
  desktops/laptops.

## C. Original Odysseus feature inventory — decision table

| Feature (original plan) | Decision | Earliest era | Why | Potato constraint |
|---|---|---|---|---|
| Chat | Keep | shipped | Core surface | Small models, honest limits |
| Document uploads / RAG | Keep | shipped | Core value path | Guardrails, cancel, storage visibility (v0.4) |
| Vision / image understanding | Adapt | Era 3 | Valuable but heavy | OCR-only default; VLM opt-in, labeled |
| Screenshots intake | Keep | Era 3 | Common noob need | Explicit capture only; no monitoring |
| Agents | Adapt | Era 4+ | "Agent" = jobs + bounded tools here | Job Engine first; audit trace; confirmations |
| Tools | Adapt | Era 4 | Useful, dangerous | Allowlist, workspace-scoped, no shell |
| Deep research | Adapt | Era 5 | Killer local feature | Local files first; web is explicit mode |
| Compare | Keep | Era 7 | Fits hardware-advisor thesis | Runs on user's machine/models only |
| Cookbook | Keep | Era 7 | Direct Potato thesis payoff | Recipes tested per tier |
| Memory | Adapt | Era 6 | Trust-sensitive | Explicit approval, editable, sourced |
| Skills | Adapt | Era 6 | Reuse beats re-prompting | Workflows on Job Engine, not prompt packs |
| Document editor | Defer | Later | Not core to evidence workflow | Only if lightweight |
| Notes/tasks | Defer | Later | Scope creep risk | Only if serving research workflow |
| Web search | Adapt | Era 5 | Breaks local-first default | Explicit per-task network mode only |
| Image editor (generative) | Defer | Later | Heavy, off-thesis | Optional extension only, never core |
| Email integration | Defer | Much later | Privacy surface too large for now | Explicit mode; likely never default |
| Calendar | Defer | Much later | Same as email | Same |
| Shell execution | **Reject** | never | Unbounded harm on noob machines | Bounded tools only (Era 4) |
| MCP (broad) | Defer | Much later | Ecosystem pull vs safety | Only via Era 4 allowlist discipline |
| Mobile/PWA | Defer | Not near-term | Different product | Desktop potatoes are the mission |
| Auth/2FA/accounts | **Reject** | never (as cloud accounts) | Local single-user app; no server | Profile stays local; no login walls |
| Theme editor | Defer | Later | Cosmetic | Never before proof/guardrail work |

## D. Non-negotiable product constraints

These hold in every era; a feature that cannot satisfy them ships
"unsupported" honestly or does not ship.

1. No cloud fallback by default.
2. No telemetry.
3. No hidden downloads.
4. No unrestricted shell.
5. No always-on screen watching.
6. No model auto-download without explicit user action.
7. No raw private data (prompts, documents, paths) in diagnostics.
8. No heavy dependency in the core potato install path.
9. Every major feature needs proof on weak hardware — measured on
   P1/P2-class machines or a documented simulated equivalent — or an
   honest "unsupported on this tier" label.

## E. Decision rule

When in doubt, ship the boring feature that makes a weak computer feel
safe, understandable, and recoverable before shipping the exciting
feature that makes demos look powerful.
