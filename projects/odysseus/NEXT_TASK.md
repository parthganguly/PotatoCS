# Next Task: Make Forced Sidecar Death Recoverable and Observable

Priority: **P0 release blocker**  
Primary owner: **Codex**  
Gate: `GATE.md` section 2

## Objective

Make forced sidecar death recoverable and observable while keeping the Tauri
host alive and avoiding replay of unsafe operations.

## Scope

- Detect that the owned Python child exited or its RPC pipe closed.
- Keep the Tauri host running after sidecar death.
- Persist fixed, privacy-safe exit and recovery phase/result logs.
- Allow one restart for an explicitly safe idempotent request.
- Retry that safe request at most once after a successful restart.
- Surface restart failure as an error without terminating the host.

## Likely files

- `src-tauri/src/lib.rs`
- Focused Rust lifecycle tests in the same file

## Required tests

- Killing only the fixture sidecar does not terminate the test host process.
- A safe idempotent request triggers one restart and one retry.
- A non-idempotent request is never replayed after a lost response.
- Restart failure is returned and logged without private payloads.
- Repeated failure cannot create an unbounded restart loop.
- `cargo test --manifest-path src-tauri/Cargo.toml`
- `cargo check --manifest-path src-tauri/Cargo.toml`
- `git diff --check`

## Acceptance evidence

- Patch limited to forced-death detection, bounded recovery, logging and tests.
- Test transcript proves host survival and one-restart maximum.
- Log samples cover exit, restart success and restart failure.
- Exact reviewed commit SHA is recorded in `EVIDENCE_INDEX.md`.

## Stop conditions

- Do not add or redesign recovery UI.
- Do not change app features, branding, versions or installer artifacts.
- Do not alter shutdown classification unless a focused regression requires it.
- Stop if recovery would replay a non-idempotent request.
