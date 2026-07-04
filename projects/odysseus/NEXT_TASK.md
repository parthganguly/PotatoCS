# Next Task: Installed-App Lifecycle Smoke Procedure and Proof

Priority: **P0 release blocker**  
Primary owner: **Human**, with Codex-assisted evidence capture
Gate: `GATE.md` sections 2 and 4

## Objective

Prove graceful shutdown and forced sidecar recovery in the installed Windows app
without changing source or treating unit fixtures as installed-app evidence.

## Prerequisite

- Use an installer with recorded commit provenance including `bd635ea2`.
- Record installer path and SHA-256 before installation.
- If provenance is missing, stop; do not rebuild or infer it.

## Procedure

1. Record clean starting process tree and selected test profile path.
2. Install and launch; confirm backend readiness and record host/sidecar PIDs.
3. Close normally; prove host and owned sidecar both exit with no orphan.
4. Relaunch; kill only the recorded sidecar PID, never a process-name tree.
5. Prove the Tauri host remains alive.
6. Trigger one safe diagnostics request; prove one sidecar restart and one retry.
7. Confirm fixed-label exit/restart/retry logs contain no private payloads.
8. Confirm selected profile data remains readable after recovery and relaunch.
9. Close normally and repeat the lifecycle matrix once.

## Required evidence

- Installer path, SHA-256 and source commit provenance.
- Timestamped host/sidecar PID snapshots for both runs.
- Sanitized lifecycle log excerpts for graceful, kill, restart and retry outcomes.
- Profile continuity result without copying private profile content.
- Pass/fail matrix with first failures and cleanup retained.

## Acceptance

- Two complete installed lifecycle runs pass at one installer hash.
- No orphan process, host termination, restart loop or unsafe replay occurs.
- Evidence is indexed by exact path and hash in `EVIDENCE_INDEX.md`.

## Stop conditions

- Do not modify app source, branding, versions or release artifacts.
- Do not rebuild, publish, rename or replace an installer without user authority.
- Do not mark package, checksum, version or release proof complete in this task.
- Stop if killing the sidecar also terminates the host; preserve logs and PIDs.
