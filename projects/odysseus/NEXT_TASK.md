# Next Task: Bound Sidecar Shutdown

Priority: **P0 release blocker**  
Primary owner: **Codex**  
Gate: `GATE.md` section 2

## Objective

Make Tauri shutdown complete within a defined deadline even when the Python
sidecar remains alive but never replies to `app.shutdown`.

## Scope

- Ensure the shutdown RPC cannot block the cleanup deadline indefinitely.
- Preserve graceful shutdown when Python responds normally.
- On timeout, kill and reap only the owned child process.
- Persist concise lifecycle phase/result logs without private payloads.
- Keep behavior safe when shutdown is called more than once.

## Likely files

- `src-tauri/src/lib.rs`
- `python/rpc_server.py` only if the shutdown contract itself needs adjustment

## Required tests

- Responsive child exits normally.
- Silent/hung child is killed and reaped within the bound.
- Already-exited child cleanup succeeds.
- Repeated shutdown is harmless.
- No private RPC content appears in lifecycle logs.
- `cargo test --manifest-path src-tauri/Cargo.toml`
- `cargo check --manifest-path src-tauri/Cargo.toml`

## Acceptance evidence

- Patch limited to lifecycle behavior and tests.
- Test transcript with measured upper bound.
- Log sample for graceful and forced paths.
- Exact commit SHA recorded after review.

## Stop conditions

- Do not begin forced-death recovery/UI work in this task.
- Do not change app features, branding, versions or release artifacts.
- Stop and report if a bounded design requires killing unrelated processes.
