# v0.3.1 Degraded-Backend UI — Live Smoke Result

Executed 2026-07-06 against the plan in `V031_DEGRADED_UI_SMOKE.md`.

## Under test

- Branch `review/v0.3.1-degraded-ui`, commit `73e2aa18` (PR #5 implementation
  `10e172d7` + smoke plan `99b0e2a5`, merged with post-#6/#7 `main`).
- Windows 11, debug build (`cargo run --no-default-features`), production-built
  frontend (`npm run build:frontend`) served at `http://127.0.0.1:1420` via
  `vite preview`. The `tauri dev` server could not serve module transforms on
  this machine (Vite dep-optimizer hang, environment issue unrelated to the PR),
  so the same debug shell binary was pointed at the built frontend instead —
  identical Rust shell code path, identical UI bundle to release.
- **Scratch profile:** the real `%APPDATA%\dev.odysseus.desktop` (489 files,
  ~335 MB) was renamed aside before the smoke and restored intact afterwards
  (489 files verified). The app created a fresh `profiles/default`.
- Sidecar python controlled via debug-only `ODYSSEUS_PYTHON` pointing at a
  junction to `python-runtime-core`; `python` removed from the app's `PATH`.
  **Forcing restart failure needed no source change and no test hook:**
  deleting the junction (and temporarily renaming
  `target/debug/python-runtime`) made every `locate_python` fallback fail —
  the §7 decision point was satisfied by filesystem denial.
- UI driven and screenshotted through WebView2 CDP
  (`WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS=--remote-debugging-port=9333`);
  real DOM clicks on the real window.

## Pre-smoke checks (all pass)

`npm run test:backend-status` ok · `npm run test:progress` ok ·
`cargo check` ok · `cargo test` 24/0/4 · `npm run build:frontend` exit 0 ·
`git diff --check` clean · worktree clean.

## Scenarios

Sidecar kills used `Stop-Process -Id <pid> -Force` on the python child of the
app process. Prompt used the sentinel `SMOKE_PRIVATE_SENTINEL_123`, no real data.

### A. Non-idempotent RPC failure (no replay) — PASS

Typed the sentinel prompt into the real composer, killed sidecar PID 23244,
submitted. Result: request failed once, **no replay** (log shows no restart
attempt, no second answer, no session created); banner appeared with the exact
fixed copy; log gained only:

```
sidecar lifecycle phase=exit result=detected event=child_exited
sidecar lifecycle phase=restart result=skipped_non_idempotent
```

The chat surface additionally showed the request's failure message
("Python sidecar exited before handling chat.send (status: exit code:
0xffffffff)") — this is the pre-existing generic chat error banner, not part
of PR #5's degraded UI; it contains no prompt text, path, payload, or trace.

### B1. Safe call, restart OK — PASS

Killed sidecar 29492, opened Diagnostics (allowlisted calls). Exactly one
restart cycle, call completed, **no banner** at any point:

```
phase=exit result=detected event=child_exited
phase=restart result=attempted
phase=restart result=succeeded
phase=retry result=succeeded
```

### B2. Safe call, restart fails — PASS

Removed the runtime junction + hid `target/debug/python-runtime` (PATH already
python-free), killed sidecar 4808, triggered safe calls. One restart attempt,
spawn failed, degraded banner appeared with fixed copy:

```
phase=restart result=attempted
sidecar launch executable=python ...
phase=restart result=failed
```

`app_status` confirmed `backend_ready:false, backend_degraded:true`.

### C2. Manual retry failure — PASS

From the B2 degraded state, clicked **Retry backend**. Banner persisted with
the same fixed copy; no raw error, path, or trace appeared in the UI (the
retry handler swallows the error by construction); log:

```
phase=restart result=attempted context=user_retry
phase=restart result=failed context=user_retry
```

### B3. Auto-recovery, degraded:false without Retry — PASS

Restored the junction and `target/debug/python-runtime`, clicked the sidebar
"Refresh Ollama" (allowlisted `models.detect_ollama`) — did **not** click
Retry. Banner cleared automatically; `app_status` returned
`backend_ready:true, backend_degraded:false`; log:

```
phase=exit result=detected event=not_running
phase=restart result=attempted
phase=restart result=succeeded
phase=retry result=succeeded
```

### C1. Manual retry success — PASS

Earlier, from the Scenario A degraded state (junction intact), clicked
**Retry backend**: banner cleared, subsequent safe calls (Diagnostics data,
Ollama refresh, `app_status`) worked; log shows
`phase=restart result=attempted/succeeded context=user_retry`.

### Rapid-click behavior — PASS with a noted limitation

Two **same-tick synthetic** clicks (CDP `.click(); .click();`) fired two retry
invocations (two `context=user_retry` attempted/succeeded pairs). Both
completed safely and serially under the backend mutex; exactly one sidecar
survived, no orphans. The button's disabled/"Retrying..." state renders on the
next React frame (~16 ms), so human-timescale double-clicks are blocked; only
same-tick programmatic clicks bypass it. On this machine a failing retry
resolves in <120 ms, so the "Retrying..." label was not observable; the state
is covered by the unit-tested `backendBannerState` (retrying → label swap +
disabled).

## Privacy checks — PASS

- `Select-String backend.log -Pattern 'SENTINEL|Traceback'` → **no matches**.
- `python.exe` / `C:\Users` in `backend.log` appear **only** in the
  pre-existing `sidecar launch executable=…` / sidecar startup/shutdown lines
  (format unchanged by PR #5; present in v0.3.0). Every PR-added lifecycle
  line is fixed-label only.
- UI DOM grep: `SMOKE_PRIVATE_SENTINEL_123` absent from the rendered UI
  (it existed only as the user's own text in the composer).
- The Diagnostics page displays profile/db/log/python paths — that is the
  pre-existing, deliberate diagnostics runtime panel, not degraded-UI output.
- Exact banner text observed (both scenarios):
  `Backend unavailable. Local AI features may not work until the backend
  reconnects.` with button `Retry backend`.

## Pass/fail table

| # | Scenario | Expected | Result |
|---|---|---|---|
| A | Non-idempotent loss | no replay, banner, `skipped_non_idempotent` | **PASS** |
| B1 | Safe call, restart OK | one retry, no/cleared banner | **PASS** |
| B2 | Safe call, restart fails | banner, `result=failed` | **PASS** |
| B3 | Auto-recovery | banner clears without Retry | **PASS** |
| C1 | Manual retry success | banner clears | **PASS** |
| C2 | Manual retry failure | fixed banner stays, no raw error | **PASS** |
| P | Privacy grep | no leaks | **PASS** |

## Evidence artifacts

Screenshots and logs captured in the session scratchpad (`smoke/` —
`shot-1-baseline-no-banner.png`, `shot-2-scenarioA-banner.png`,
`shot-3-C1-after-retry.png`, `shot-5-B2-banner-degraded.png`,
`shot-6-B3-recovered.png`, `backend.log.baseline`, `backend.log.final`).
Screenshots show: clean baseline, banner + failed chat (A), banner cleared
(C1), banner over Diagnostics (B2/C2), recovered Diagnostics (B3).

## Limitations / notes

- Run against the debug shell + production frontend bundle (see "Under test");
  the release-installer build was not rebuilt (out of scope per plan §6).
- One extra `context=user_retry attempted/failed` pair appears in the log
  during B2 (~7 s after the failure) that the harness did not issue — the app
  window was visible on the desktop and the banner button is the only code
  path that emits it, so it is attributed to a manual click. Its behavior
  matches C2 exactly (failed, banner persisted) and was reproduced
  deliberately afterwards.
- "Retrying..." in-flight label not visually captured (failure resolves in
  <120 ms here); covered by unit tests.
- Crash-loop resilience beyond one cycle, installer integrity, sidecar feature
  quality: out of scope per plan §1.

## Merge recommendation

**PR #5 is merge-ready.** Every row in §5 of the smoke plan passes; issue #1's
live-smoke condition (forced failure → banner → retry recovers / stays safely
degraded → no private data leaks) is proven end-to-end in a live app.
