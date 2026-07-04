# Next Task: Re-run Installed Lifecycle Smoke on the Fixed Candidate

Priority: **P0 release blocker**
Primary owner: **Human**, with Codex-assisted implementation
Gate: `GATE.md` sections 2 and 4

## Objective

The startup `health.ping` sidecar-death crash recorded in
`INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md` is now source-fixed at
`7119e40c59dfb401be400242dee2f0fffde95fff` (`fix: make startup sidecar
health.ping failure non-fatal`), with Rust fixture evidence only. Build a
new installer from this commit and re-run the full installed lifecycle
smoke to prove the fix holds at the installed-package level, and to
complete the required two-run matrix.

## Prerequisite

- Candidate SHA: `7119e40c59dfb401be400242dee2f0fffde95fff`.
- Read `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md` in full before starting;
  it defines the exact command/observation format to reuse.
- See `GATE.md` current hard failures and `EVIDENCE_INDEX.md` "Startup
  health-ping recovery evidence" for what is already proved and what is not.

## Procedure

1. Build a fresh installer from `7119e40c` (or later, if the user has since
   advanced HEAD) using the same build command as the prior smoke.
2. Run the full installed lifecycle matrix twice: clean install, launch,
   normal close, relaunch, idle-sidecar kill (host survives), sidecar kill
   during startup `health.ping` (host must survive this time), final orphan
   check.
3. Confirm fixed-label recovery logs are present in
   `%APPDATA%\dev.odysseus.desktop\profiles\default\logs\backend.log` for
   the startup-kill path, with no RPC payload/private content.
4. Confirm profile continuity (`app.db`) survives across both runs.
5. Record installer path, size, SHA-256, and all commands/timestamps/PIDs.

## Required evidence

- New installer SHA-256, built from the recorded candidate SHA.
- Two complete, passing lifecycle-matrix runs (or one pass + one documented
  retry with its first failure preserved per `TOKEN_RULES.md`).
- Fixed-label log excerpts for the startup-kill path from the installed app.
- Profile continuity evidence across both runs.

## Acceptance

- Both installed lifecycle matrix runs pass, including sidecar kill during
  startup `health.ping` not terminating the host.
- Evidence is indexed by exact path and hash in `EVIDENCE_INDEX.md`.
- `GATE.md` section 2 "Killing only the sidecar does not terminate the Tauri
  host" and section 4 "Installed lifecycle matrix passes twice" are checked
  only after this evidence exists.

## Stop conditions

- Do not modify branding, versions or release artifacts.
- Do not touch source in `src-tauri` or `python/` beyond what re-running the
  smoke requires (none expected).
- Do not mark the full release gate green; other `GATE.md` sections remain
  open (version alignment, checksum match, automated proof suite).
