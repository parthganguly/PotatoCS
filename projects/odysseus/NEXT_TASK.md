# Next Task: Issue #20 — Hardware/Resource Readiness Audit

Priority: **P1 measurement gate for Potato Mode defaults**
Primary owner: maintainer/auditor
Predecessors: readiness audit (PR #23), first-run readiness panel (PR #24),
and Ollama/model setup guidance (PRs #25, #27, #28) complete.

## Objective

Execute GitHub Issue #20, **“audit: hardware/resource readiness and Potato
Proof metrics.”** This is measurement only: no tuning, no Potato Mode
implementation, and no code changes. The output updates
`projects/odysseus/POTATO_PROOF_MATRIX.md` with measured numbers that can
confirm or revise its proposed §B budgets before Potato Mode defaults are
locked in backlog Issue 4 / Slice 2.

## Prerequisite reading

- `projects/odysseus/POTATO_PROOF_MATRIX.md` — proposed §B budgets and the
  destination for measured results.
- `projects/odysseus/V04_EXECUTION_PLAN.md` — slice-by-slice execution order.
- `projects/odysseus/V04_ISSUE_BREAKDOWN.md` — draft issue backlog.

## Procedure

1. Run the Issue #20 hardware/resource measurements without changing app
   behavior, defaults, or code.
2. Record the measured numbers in `POTATO_PROOF_MATRIX.md` §B with enough
   context to distinguish the measured hardware/setup from proposals.
3. Mark every §B budget clearly and visibly as **measured-pass**,
   **measured-fail**, or **still-proposed**. Never present a proposed number
   as measured.
4. Use those results as the gate for the later human/Fable decision that
   locks Potato Mode defaults in backlog Issue 4 / Slice 2.

## Stop conditions

- No tuning and no Potato Mode implementation.
- No code changes; Issue #20 changes measurement documentation only.
- Do not lock defaults while any relevant §B budget is still proposed or
  lacks a clear measured-pass/measured-fail disposition.
- No installer or release asset changes.
