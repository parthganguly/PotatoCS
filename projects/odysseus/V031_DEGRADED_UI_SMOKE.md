# v0.3.1 Degraded-Backend UI — Live Smoke Plan

Reviewer-executable plan proving PR #5 works in a live app:
forced backend failure → degraded banner → retry works or stays safely degraded → no private data leaks.

## 1. Scope

**Proves:** in a running app (not unit tests), sidecar loss surfaces the fixed degraded banner,
"Retry backend" behaves correctly on both success and failure, automatic recovery clears the
banner (`degraded: false` transition), non-idempotent requests are never replayed, and neither
UI nor `backend.log` leaks private data.

**Does not prove:** installer integrity, v0.3.0 release assets, sidecar feature correctness
(chat/OCR/RAG quality), multi-profile behavior, or crash-loop resilience beyond one cycle.

**Why issue #1 stays open:** the unit suite (24/0/4) exercises the state machine with fake
restarts; only this live smoke shows the event → banner → retry loop end-to-end in the real
app. Issue #1 closes only after every check below passes.

## 2. Preconditions

- Under test: **PR #5**, branch `review/v0.3.1-degraded-ui`, commit `10e172d7`.
- `git status` clean on that branch; `npm run tauri dev` (or a locally built app) launches.
- **Use a throwaway test profile.** The profile lives at `<AppData>/…/profiles/default`;
  back it up or point the app at a scratch data dir so real data is never at risk.
- Do **not** use real private documents or prompts — use sentinel strings like
  `SMOKE_PRIVATE_SENTINEL_123` so leaks are grep-able.
- Locate the sidecar PID (child python process of the app) before each scenario:
  `Get-CimInstance Win32_Process | Where-Object { $_.ParentProcessId -eq <app_pid> }`.
- Log under test: `<profile_dir>/logs/backend.log` (fixed labels only).

## 3. Failure scenarios

### A. Non-idempotent RPC failure (no replay)

1. Start the app, confirm no banner.
2. Send a chat-style request containing `SMOKE_PRIVATE_SENTINEL_123`; kill the sidecar PID
   (`Stop-Process -Id <pid> -Force`) while it is in flight — or kill it just before sending.
3. Expected:
   - The request **fails once and is not replayed** (no duplicate side effect / second answer).
   - Banner appears with the fixed copy:
     "Backend unavailable. Local AI features may not work until the backend reconnects."
   - No error detail, path, or payload appears anywhere in the UI.
   - `backend.log` contains `sidecar lifecycle phase=restart result=skipped_non_idempotent`
     and does **not** contain the sentinel.

### B. Safe/idempotent call — auto-recovery and degraded:false

1. Kill the sidecar, then trigger an allowlisted safe call (e.g. open the diagnostics/models
   view → `diagnostics.get`, `models.list`, `ocr.status`, `rag.health`).
2. Expected:
   - Exactly **one** restart attempt (`phase=restart result=attempted` once per loss).
   - If restart succeeds: call completes, **no banner** (or an existing banner clears —
     the `degraded: false` transition).
   - To test the failure branch, make restart impossible first (see §7), then confirm
     `result=failed` in the log and the banner appears.
3. Recovery check: from a degraded state, restore the sidecar precondition and trigger a safe
   call → banner clears **without** clicking Retry (automatic `degraded:false` event).

### C. Manual retry

1. From a degraded state (A or B-failure), click **Retry backend**.
2. Expected:
   - Success: banner disappears; a subsequent request works.
   - Failure (restart still impossible): banner stays with the same fixed copy; **no raw
     error, path, or trace** is shown; log gains
     `phase=restart result=failed context=user_retry`.
   - Button shows a retrying state and is not double-fired by rapid clicks.

## 4. Privacy checks

After all scenarios, grep the UI (screenshots/DOM) and `backend.log` for leaks. **All must be absent:**

| Must not appear | Where checked |
|---|---|
| Prompts / responses (`SMOKE_PRIVATE_SENTINEL_123`) | UI + backend.log |
| Document text | UI + backend.log |
| File paths / profile paths | UI (log may contain profile-local paths only if pre-existing) |
| Python executable path | UI + backend.log |
| RPC payloads (JSON bodies) | UI + backend.log |
| Stack traces | UI + backend.log |

`Select-String -Path <profile>/logs/backend.log -Pattern "SENTINEL|Traceback|python.exe|C:\\Users"`
must return nothing new from this smoke.

## 5. Evidence to capture

- Screenshots: banner visible (A), banner cleared (B recovery, C success), banner persisting (C failure).
- `backend.log` excerpt showing only fixed labels for each scenario.
- Test profile path used, and confirmation it was a scratch profile.
- Command outputs: sidecar PID lookup, kill commands, grep results from §4.
- Pass/fail table:

| # | Scenario | Expected | Result |
|---|---|---|---|
| A | Non-idempotent loss | no replay, banner, `skipped_non_idempotent` | |
| B1 | Safe call, restart OK | one retry, no/cleared banner | |
| B2 | Safe call, restart fails | banner, `result=failed` | |
| B3 | Auto-recovery | banner clears without Retry | |
| C1 | Manual retry success | banner clears | |
| C2 | Manual retry failure | fixed banner stays, no raw error | |
| P | Privacy grep | no leaks | |

## 6. Stop conditions — abort the smoke immediately if

- The test profile might corrupt or touch real profile data.
- Forcing a failure would require modifying source (beyond an approved test hook, §7).
- Any step would touch v0.3.0 release assets or `dist/`.
- Any change beyond docs/test hooks appears in `git status` mid-smoke.

## 7. Reviewer decision point — forcing restart failure

Killing the sidecar PID needs no source change. Making the **restart also fail** (B2/C2) may:
renaming the bundled python dir works on a built app but is clumsy in dev. If a hook is
needed, propose (do **not** implement in this PR) the smallest option, preferring the first:

1. **Dev-only env var** — e.g. `ODYSSEUS_DEV_FAIL_RESTART=1` checked only in debug builds,
   making `restart()` return `Err` with a fixed message.
2. Debug-gated command (e.g. `debug.force_degraded`) compiled out of release builds.
3. Test-fixture sidecar mode: a flag making the sidecar exit immediately after spawn.

Record which mechanism was used (or that filesystem denial sufficed) in the evidence.

---
*Plan only — no source changes, no installer rebuild, no release-asset changes, PR #5 unmerged,
issue #1 stays open until every row in §5 is Pass.*
