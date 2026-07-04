# Next Task: Make Startup Sidecar Health Failure Non-Fatal

Priority: **P0 release blocker**
Primary owner: **Human**, with Codex-assisted implementation
Gate: `GATE.md` sections 2 and 4

## Objective

Fix the setup-hook panic recorded in
`INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md`: killing the owned sidecar while
the startup `health.ping` request is in flight currently terminates the Tauri
host (`Failed to setup app: error encountered during setup hook: Python
sidecar exited before responding to health.ping`). Startup health-check
failure must be handled the same way the already-fixed forced-death and
bounded-shutdown paths are (`bd635ea2`, `e9f36fbc`): recorded, recoverable,
and non-fatal to the host process.

## Prerequisite

- Candidate baseline for this fix is `1682dd14cdee9a3c145e3c6c034e5ebd54c2eced`,
  the SHA tested in the failing smoke run.
- Read `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md` in full before changing code.

## Procedure

1. Locate the setup hook that calls `health.ping` during startup and currently
   propagates a hard error out of the hook.
2. Change the failure path so a sidecar death/non-response during startup
   health check does not abort app setup; apply the same restart/retry
   discipline already proven for the running-state forced-death case.
3. Emit fixed-label lifecycle logs for the startup failure path (exit,
   restart attempted/succeeded/failed, retry result) with no RPC payload
   content, matching the existing bounded-shutdown/forced-recovery log shape.
4. Add or extend a Rust fixture that kills the sidecar during startup
   `health.ping` and proves the host process survives.
5. Run `cargo test` and `cargo check` in `src-tauri`; record pass/fail counts.

## Required evidence

- Changed path(s), scoped to source needed for this fix.
- Fixture proving host survives a sidecar kill during startup `health.ping`.
- Fixed-label log excerpts for the startup failure path.
- `cargo test` and `cargo check` results.

## Acceptance

- Killing the owned sidecar during startup `health.ping` no longer terminates
  the host process in the Rust fixture.
- Fixed-label logs exist for the startup failure path.
- Evidence is indexed by exact path and hash in `EVIDENCE_INDEX.md`.

## Stop conditions

- Do not mark the installed package proof green.
- Do not mark the two-run installed lifecycle matrix green.
- Do not modify branding, versions or release artifacts.
- Do not rebuild, publish, rename or replace an installer without user authority.
- This task proves the source fix only; the installed-app lifecycle smoke in
  `INSTALLED_APP_LIFECYCLE_SMOKE_2026-07-04.md` must be re-run separately
  against a new installer built from the fixed commit.
