# Installed-App Lifecycle Smoke Re-run — 2026-07-04 (Fixed Candidate)

## Verdict

**PASS.** Both full lifecycle-matrix runs passed, including sidecar kill during
the startup `health.ping` request, which the host now survives via one
restart/retry recovery.

## Candidate and installer

- Candidate SHA: `304c6284d8d0638e48171e9e181384ae364182ee`
  (`docs: record startup health.ping non-fatal fix evidence`).
- Source fix SHA: `7119e40c59dfb401be400242dee2f0fffde95fff`
  (`fix: make startup sidecar health.ping failure non-fatal`), included in
  candidate history; `304c6284` adds harness docs only.
- Branch at start: `main` (ahead of `origin/main` by 9 commits); worktree clean.
- Build command: `npm run tauri:build:core` (same as prior smoke).
- Build completed: 2026-07-04 20:15 +05:30; exit `0`.
- Installer: `C:\Users\Parth Ganguly\Documents\Codex\odysseus-desktop\src-tauri\target\release\bundle\nsis\Odysseus Desktop_0.2.1_x64-setup.exe`
- Size: `32,362,413` bytes.
- SHA-256: `04A1C2BD317FBB14BB52EADE5DC8A2E6F3BB289E9C88B1636A3B60193C3C7DCC`
- Note: the build script transiently deleted `dist/PotatoCs-Odysseus-Desktop-v0.2.1-SHA256SUMS.txt`
  as a side effect; it was restored via `git checkout` immediately after the
  build and before any install/test steps. Worktree was clean at build start
  and end.
- Git status after build and after both runs: clean, `304c6284` HEAD unchanged.

## Commands and PIDs

Uninstall: `uninstall.exe /S`. Install: installer above with `/S`. Launches
used `Start-Process` on the installed `odysseus-desktop.exe`. Normal closes
used `.CloseMainWindow()` with a 20-second bounded wait. Sidecar kills used
`Stop-Process -Id <owned-python-pid> -Force`. Startup-window kills polled
`Get-CimInstance Win32_Process` for the child `python.exe` immediately after
host launch and killed it as soon as detected (~1.0–1.3 s after host start,
before OCR/model detection log lines appear — i.e. during the startup
`health.ping` window). Process checks used `Get-CimInstance Win32_Process`;
installer hash used `Get-FileHash -Algorithm SHA256`.

### Run 1

| Time (+05:30) | Host PID | Sidecar PID | Observation |
|---|---:|---:|---|
| 20:33:03–20:33:12 | — | — | Silent uninstall, exit 0. |
| 20:33:40–20:33:49 | — | — | Silent install of new candidate installer, exit 0. |
| 20:33:57 | 32128 | 7372 | Clean install launched. |
| 20:34:17–20:34:19 | 32128 | 7372 | Normal close; both exited, no orphan. |
| 20:34:30 | 31152 | 268 | Relaunch succeeded. |
| 20:34:50–20:34:55 | 31152 | 268 (killed) | Idle sidecar killed; host survived; no auto-restart observed in idle window (expected — restart applies to startup/RPC paths). |
| 20:35:14–20:35:17 | — | — | Normal close; no orphan. |
| 20:35:34 | 2884 | 30480 (killed @1304 ms) | Relaunch; sidecar killed during startup `health.ping` window. |
| 20:35:41–20:35:57 | 2884 (alive) | 23720 (new) | Host survived; sidecar auto-restarted; new sidecar fully started. |
| 20:36:21–20:36:24 | — | — | Final close; no orphan. |

Fixed-label log excerpt (`backend.log`, no RPC payload/private content):

```
1783177535 WARN odysseus_desktop.shell - sidecar lifecycle phase=exit result=detected event=stdout_closed context=startup_health
1783177535 WARN odysseus_desktop.shell - sidecar lifecycle phase=restart result=attempted context=startup_health
1783177535 WARN odysseus_desktop.shell - sidecar lifecycle phase=restart result=succeeded context=startup_health
1783177541 WARN odysseus_desktop.shell - sidecar lifecycle phase=retry result=succeeded context=startup_health
```

Profile continuity: `%APPDATA%\dev.odysseus.desktop\profiles\default\app.db`
present at `92,561,408` bytes after run 1 (same size recorded in the prior
failing smoke, `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`).

### Run 2

| Time (+05:30) | Host PID | Sidecar PID | Observation |
|---|---:|---:|---|
| 20:36:37–20:36:48 | — | — | Silent uninstall then silent reinstall of same installer, both exit 0. |
| 20:37:01 | 7092 | 33040 | Clean install launched. |
| 20:38:10–20:38:13 | 7092 | 33040 | Normal close; no orphan. |
| 20:38:23 | 26812 | 7968 | Relaunch succeeded. |
| 20:38:40–20:38:45 | 26812 | 7968 (killed) | Idle sidecar killed; host survived. |
| 20:39:09–20:39:12 | — | — | Normal close; no orphan. |
| 20:39:29 | 3292 | 31024 (killed @998 ms) | Relaunch; sidecar killed during startup `health.ping` window. |
| 20:39:36–20:39:44 | 3292 (alive) | 13608 (new) | Host survived; sidecar auto-restarted; new sidecar fully started. |
| 21:28:08–21:28:12 | — | — | Final close; no orphan. |

Fixed-label log excerpt (`backend.log`, no RPC payload/private content):

```
1783177770 WARN odysseus_desktop.shell - sidecar lifecycle phase=exit result=detected event=stdout_closed context=startup_health
1783177770 WARN odysseus_desktop.shell - sidecar lifecycle phase=restart result=attempted context=startup_health
1783177770 WARN odysseus_desktop.shell - sidecar lifecycle phase=restart result=succeeded context=startup_health
1783177776 WARN odysseus_desktop.shell - sidecar lifecycle phase=retry result=succeeded context=startup_health
```

Profile continuity: `app.db` present at `92,561,408` bytes after run 2 —
identical size to run 1 and to the prior failing smoke, across uninstall,
reinstall, four launches/closes and two forced sidecar kills per run.

## Acceptance matrix (both runs)

| Requirement | Run 1 | Run 2 |
|---|---|---|
| Candidate SHA recorded | PASS | PASS |
| Installer built from candidate SHA | PASS | PASS |
| Clean install launches | PASS | PASS |
| Normal close leaves no orphan | PASS | PASS |
| Relaunch works | PASS | PASS |
| Idle sidecar kill never terminates host | PASS | PASS |
| Sidecar kill during startup `health.ping` never terminates host | **PASS** | **PASS** |
| Safe restart/retry recovers the sidecar once | PASS | PASS |
| Final close/cleanup leaves no orphan | PASS | PASS |
| Fixed-label lifecycle events present for the crash path, no payload | PASS | PASS |
| Profile `app.db` survives across runs | PASS (92,561,408 bytes) | PASS (92,561,408 bytes) |
| Two complete runs with same installer hash | PASS | PASS |

Git state before and after this report: `## main...origin/main [ahead 9]`, clean.

## Scope note

No source under `src-tauri/` or `python/` was modified during this task. Only
this report and the linked harness files (`STATUS.md`, `GATE.md`,
`EVIDENCE_INDEX.md`, `NEXT_TASK.md`) were changed. Branding, versions, release
notes, checksum files and installer naming were not modified; the release
gate is not marked green — other `GATE.md` sections remain open.
