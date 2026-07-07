# Next Task: Start v0.4 — Potato Mode + First-Run Runtime Simplification

Priority: **P1 planning-to-execution**
Primary owner: maintainer, with model-assisted implementation
Predecessors: v0.3.0 and v0.3.1 released; `GATE.md` and `GATE_v0.3.1.md` both GREEN.

## Objective

Execute the accepted v0.4 scope (issue #3, closed):
**Potato Mode + First-Run Runtime Simplification**. The prior task in this
file — build the v0.3.0 installer and proof — completed at `e335705f`
(`RELEASE_PROOF_v0.3.0.md`); v0.3.1 followed and completed at `971c0102`
(`RELEASE_PROOF_v0.3.1.md`).

## Prerequisite reading

- `projects/odysseus/V04_POTATO_MODE_SCOPE.md` — accepted scope and audit.
- `projects/odysseus/V04_EXECUTION_PLAN.md` — slice-by-slice execution order.
- `projects/odysseus/V04_ISSUE_BREAKDOWN.md` — draft issue backlog.
- `projects/odysseus/V04_BATON_FOR_SMALLER_MODELS.md` — rules for
  lower-context models continuing this work.

## Procedure

1. Merge the v0.4 planning baton PR (docs only).
2. Open GitHub issues from `V04_ISSUE_BREAKDOWN.md` **only with maintainer
   approval**, in dependency order.
3. Work each issue as one branch / one PR with a small diff, per
   `V04_BATON_FOR_SMALLER_MODELS.md`.
4. Do not build or publish any installer until the v0.4 release-gate task.

## Stop conditions

- No agents, tools, research, memory, skills, Cookbook, compare, or new
  vision backends in v0.4.
- No app source changes without an accepted issue.
- No release assets touched outside an explicit release task.
