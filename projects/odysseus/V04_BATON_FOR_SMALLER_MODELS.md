# v0.4 Baton — Instructions for Future Models

You are probably a smaller or lower-context model (Sonnet, Codex, GPT-5.5)
continuing work planned by a stronger model. Follow this file literally.
When it conflicts with your instinct, this file wins. When it conflicts
with the maintainer's explicit instruction, the maintainer wins.

## A. Project identity

- **PotatoCS** = the project: local AI for ordinary computers.
- **Odysseus Desktop** = the shipped Windows app (Tauri + Rust supervisor +
  Python sidecar + SQLite profiles + Ollama loopback runtime).
- **v0.4 theme = Potato Mode + First-Run Runtime Simplification.**
- Do not change the theme, add scope, or resurrect the deferred
  "safe bounded tools" line unless the maintainer explicitly says so.

## B. Source of truth — read before any task

1. `README.md` — what the product is and claims.
2. `projects/odysseus/STATUS.md` — release/gate state.
3. `projects/odysseus/V04_POTATO_MODE_SCOPE.md` — accepted scope + audit
   table (statuses may predate later slices; verify against code).
4. `projects/odysseus/V04_EXECUTION_PLAN.md` — slice order, risk, and what
   each slice must not touch.
5. `projects/odysseus/GATE_v0.3.1.md` — what a closed gate looks like.
6. `projects/odysseus/RELEASE_PROOF_v0.3.1.md` — what release evidence
   looks like.

If a doc contradicts the code, trust the code and report the drift; do not
silently "fix" docs mid-task.

## C. Default workflow for every task

1. Start from a clean `main`; create one branch per task
   (`feat/...`, `docs/...`, `audit/...`).
2. One issue → one PR. If no issue exists, stop and ask the maintainer.
3. Keep the diff small. If your change is growing past the issue's stated
   file list, stop and report instead of continuing.
4. Run the standard test set (below) **before** pushing; paste real output,
   never summarize tests you did not run.
5. Never touch release assets (installers, checksum files, tags,
   `docs/releases/*SHA256SUMS*`) unless the task is explicitly a release
   task.
6. Update docs only for factual drift caused by your change.
7. Final report must include: branch name, commit SHA, exact tests run with
   results, and `git status` output showing a clean or explained worktree.

## D. Standard test command set

Run what applies to your diff; run all of them if you touched `src/`,
`src-tauri/`, or `python/`:

```powershell
npm run test:backend-status
npm run test:progress
cargo check --manifest-path src-tauri\Cargo.toml
cargo test --manifest-path src-tauri\Cargo.toml
npm run build:frontend
git diff --check
```

Python changes additionally require `python -m pytest python\tests`.
Docs-only changes require only `git diff --check`; say so explicitly in
your report.

## E. Model-safe prompt pattern

Tasks handed to you should look like this; if yours doesn't, ask for this
format:

> Implement only issue #N (<title>). Do not touch <protected areas, e.g.
> RPC lifecycle, trace privacy, release assets>. Files likely involved:
> <A>, <B>, <C>. Acceptance criteria: <observable behaviors>. Tests:
> <commands that must pass, plus new tests to write>. Stop and report if:
> the diff needs files outside the list, a test unrelated to your change
> fails, or the acceptance criteria require a design decision not written
> down.

## F. Red flags — stop immediately if you are about to

- Rewrite or reorganize code broadly ("while I'm here…").
- Add agents, tools, MCP, shell execution, or research features.
- Add any cloud fallback, remote endpoint default, or account system.
- Add telemetry, analytics, crash upload, or any automatic network call.
- Change the privacy model or weaken trace/diagnostics exclusions.
- Change version numbers, release notes, gates, or checksum files outside
  an explicit release task.
- Touch installer bundling, NSIS config, or `docs/releases/` assets
  casually.
- Claim a smoke test passed without recorded evidence (commands + output).
- Close a gate or mark a checklist item while any box remains unchecked.

Any of these means: stop, report what you were about to do and why, and
wait for the maintainer.

## G. Human decision points — never decide these yourself

- Which model(s) to recommend to users, and RAM/hardware thresholds.
- Whether the app may download models itself (currently: it may not).
- Whether to bundle anything heavy into the installer.
- Default Potato Mode settings values (context length, retrieval limit,
  batch size, OCR limits).
- Diagnostics/support-bundle redaction policy.
- Release readiness: only the maintainer declares a gate GREEN.

When you hit one, present options with trade-offs and stop. A wrong guess
here costs more than the time saved.
