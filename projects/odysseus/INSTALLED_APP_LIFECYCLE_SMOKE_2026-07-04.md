# Installed-App Lifecycle Smoke — 2026-07-04

## Verdict

**FAIL.** Stop point: killing the owned sidecar during the startup `health.ping`
request terminated the installed Tauri host via a setup-hook panic. No source fix
was attempted and the required second full run was not executed.

## Candidate and installer

- Candidate SHA: `1682dd14cdee9a3c145e3c6c034e5ebd54c2eced`
- Branch at start: `main...origin/main [ahead 5]`; worktree clean.
- Successful build: `npm run tauri:build:core`.
- Build completed: 2026-07-04 18:07:48 +05:30; exit `0`.
- Installer: `C:\Users\Parth Ganguly\Documents\Codex\odysseus-desktop\src-tauri\target\release\bundle\nsis\Odysseus Desktop_0.2.1_x64-setup.exe`
- Size: `32,359,045` bytes.
- SHA-256: `BE31DEF76A0A3EA60FAED198AC70FE0D4A9015EA2D1AEBD6D5835478D23C5F00`
- A preceding `npm run tauri:build` attempt hit its 15-minute command limit
  while bundling Florence and emitted no replacement installer; its recorded
  child build PIDs were stopped before the isolated Core build.

## Install and commands

- Silent uninstall: `uninstall.exe /S`, 18:10:58–18:12:09, exit `0`.
- Silent install: current installer with `/S`, 18:12:09–18:12:32, exit `0`.
- Launches used the installed `odysseus-desktop.exe` via `Start-Process`.
- Normal closes used `.CloseMainWindow()` and a 20-second bounded wait.
- Sidecar kills used `Stop-Process -Id <owned-python-pid> -Force`; no process-name
  or process-tree kill was used.
- Process checks used `Get-CimInstance Win32_Process`; installer hashes used
  `Get-FileHash -Algorithm SHA256`.

## Observations

| Time (+05:30) | Host PID | Sidecar PID | Observation |
|---|---:|---:|---|
| 18:12:51 | 13964 | 18036 | Clean install launched; installed paths confirmed. |
| 18:13:22 | 13964 | 18036 | Normal close exited; no host or owned Python orphan. |
| 18:13:44 | 10016 | 33584 | Relaunch succeeded. |
| 18:14:36 | 10016 | 33584 | Idle owned sidecar killed; host survived; no automatic request/restart in 20 s. |
| 18:15:48 | 10016 | — | Normal close exited; no orphan. |
| 18:16:07 | 30400 | 32520 | Relaunched with stdout/stderr capture. |
| 18:16:10 | 30400 | 32520 | Sidecar killed during startup request; host terminated. |
| 18:16:33 | — | — | Final orphan check passed. |

Profile continuity evidence: the same
`%APPDATA%\dev.odysseus.desktop\profiles\default\app.db` remained present at
`92,561,408` bytes after reinstall, relaunch, close, and failure cleanup.

## Failure evidence

Captured stderr (`%TEMP%\odysseus-installed-pass1.stderr.log`):

```text
Failed to setup app: error encountered during setup hook: Python sidecar exited before responding to health.ping
```

Installed log:
`%APPDATA%\dev.odysseus.desktop\profiles\default\logs\backend.log`.
It records the 18:13 startup and 18:13:22 shutdown plus later sidecar launches,
but the failing leg does **not** record the required fixed-label
exit/restart/retry outcomes.

## Acceptance matrix

| Requirement | Result |
|---|---|
| Candidate SHA recorded | PASS |
| Installer built from candidate SHA | PASS |
| Clean install launches | PASS |
| Normal close leaves no orphan | PASS |
| Relaunch works | PASS |
| Killing only owned sidecar never terminates host | **FAIL** during setup `health.ping` |
| Safe idempotent request restarts/retries once | **FAIL**; host panicked before recovery |
| Final close/cleanup leaves no orphan | PASS after crash cleanup check |
| Fixed-label lifecycle events present | **FAIL** for the crash path |
| Commands/timestamps/PIDs/path/hash/git state recorded | PASS |
| Two complete runs with same installer hash | NOT EXECUTED after blocking failure |

Git state before adding this report: `## main...origin/main [ahead 5]`, clean.
