# Harness Token Rules

## Read order

1. `STATUS.md`
2. `GATE.md`
3. `NEXT_TASK.md`
4. `EVIDENCE_INDEX.md` only for needed evidence
5. `ROADMAP.md` only for scope/version decisions

## Working rules

- Read the live file before trusting a harness summary.
- Recheck branch, HEAD and worktree at the start and end of every task.
- Work on only the objective in `NEXT_TASK.md` unless the user changes scope.
- Treat `GATE.md` open items as facts to prove, not prompts to widen scope.
- Preserve privacy, abstention, local-first and no-cloud guardrails.
- Never infer implementation from upstream history, acknowledgments or file names.

## Output discipline

- Report outcome first, then blockers and exact evidence paths.
- Prefer SHA, command, test name, path and hash over narrative.
- Link/name artifacts; do not paste logs, diffs, reports or full test output.
- Quote only the minimal failing line needed to identify a blocker.
- Do not repeat unchanged roadmap or status sections in task handoffs.
- Record skips and untested claims explicitly.

## File budgets

- `STATUS.md`: 150 lines maximum.
- `GATE.md`: 150 lines maximum.
- `ROADMAP.md`: 200 lines maximum.
- `NEXT_TASK.md`: 80 lines maximum.
- `EVIDENCE_INDEX.md`: 150 lines maximum.
- `TOKEN_RULES.md`: 80 lines maximum.

## Update discipline

- Update `STATUS.md` only when live state changes.
- Check a `GATE.md` item only with evidence tied to one commit/artifact.
- Replace `NEXT_TASK.md` only after its acceptance evidence exists.
- Add evidence pointers, not copied evidence, to `EVIDENCE_INDEX.md`.
- Keep future features frozen until the current release gate is green.
- Do not modify app source, commit, push or publish unless explicitly requested.
