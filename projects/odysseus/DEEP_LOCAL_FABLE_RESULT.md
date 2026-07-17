# Deep Local (experimental) — Fable Implementation Result

Status: backend complete on `feat/deep-local-fable`; first review round
(PR #33, 2026-07-17) addressed via scope reduction. Awaiting re-review.
Nothing here changes `main`, the installer, release assets, or the v0.4
acceptance gate. Deep Local remains default-off and has **no UI surface**.

Date: 2026-07-16, updated 2026-07-17. Author: Claude Code (Fable).

## Commits

- **Base:** `e44e7742` ("feat: add cancellable import jobs UI", tip of
  `feat/v0.4-indexing-control`).
- **Build commits** (base → review head `1eedf792`):
  `66130a5d` brief → `683f040f` RFC → `2983cf1d` provider seam →
  `166b375d` Colibrì adapter → `f6f4a3bf` plan/doctor wrapper →
  `23d0b79b` spike result → `2c98caea` fixture reconcile →
  `24f47db1` cancellable transport → `3805df8c` persisted job backend →
  `cec5f93a` recovery/cancellation/privacy tests → `223e9dee` real-upstream
  E2E proof → `757f81a3` experimental UI → `1eedf792` report.
- **Review-response commits** (2026-07-17):
  `70dabe56` remove premature UI → `f5f8cbad` remove `complete_once` from
  production RPC → `2f8e7cb9` privacy-safe CLI failures → `6be3c2bf`
  startup-repair history/category → this report update (final head).
- **Upstream Colibrì audited/tested:** `https://github.com/JustVugg/colibri`
  at `main` = **`54cfe5632446ad333ca81c44c6a6c71ffec8a01d`** (2026-07-16;
  35 commits past the spike's `550ddcba`). Re-verified: plan JSON `version: 2`,
  doctor `schema_version: 1`, identical check IDs — the adapter contracts
  hold unchanged. Upstream code is Apache-2.0; it is never vendored.

## Review findings (PR #33, first round) and resolutions

| # | Finding | Resolution |
|---|---|---|
| 1 | `DeepLocalPanel` submitted a bare question with no evidence while the job prompt instructs answering only from provided source excerpts — the visible feature either abstains or answers ungrounded, violating the sourced-answer contract | **Scope reduction** (`70dabe56`): panel, App wiring, frontend RPC helpers, UI test script, and fixture entries removed. UI is deferred until a separate PR can select Sources, perform bounded retrieval, and submit a real evidence packet. |
| 2 | Panel rendered `String(error)` from the RPC boundary, which can leak raw diagnostics past the fixed-copy privacy model | Removed with the panel (`70dabe56`); any future UI must map thrown failures to fixed copy with a hostile-error sentinel test. |
| 3 | `colibri_cli` logged the first raw stderr line and `run_plan` returned it as RPC-visible `detail`; upstream CLI errors routinely embed CLI/model directories | Fixed (`2f8e7cb9`): stderr is never logged and never leaves the module (presence/byte-count only); launch OSError logs error type only; plan failure is a fixed `plan_failed` code with fixed copy and no `detail`. Hostile sentinel tests (fake model path, CLI path, username, API key via environment dump) prove nothing appears in logs or RPC-visible results. |
| 4 | `deep_local.complete_once` remained a public synchronous RPC able to block the single-threaded sidecar for the configured timeout (default one hour) | Removed from the production method map and from `DeepLocalService` entirely (`f5f8cbad`). The persisted job RPCs are the only generation path; a regression test pins that the facade has no synchronous generation surface. |
| 5 (non-blocking) | Result report understated GitHub's changed-file/line stats | This report now carries the post-review three-dot diff stats (below), verified against GitHub after push. |
| 6 (non-blocking) | Startup repair left `state_history_json` ending on the pre-crash state | Fixed (`6be3c2bf`): repair appends an `interrupted` history entry and sets `error_category='interrupted'`, matching the worker's own interruption finalization; history sequences pinned by tests. |
| 7 (non-blocking) | No GitHub Actions/status checks on the head; all evidence is local | Acknowledged; unchanged. All evidence in this report is from local runs, commands and outputs quoted exactly. |

## Imported vs rewritten research work (Phase 0 reconcile)

All five research commits from `research/colibri-deep-local-spike` were
verified against the heavy-job base before import:

| Research component | Keep | Rewrite | Drop | Reason |
|---|---:|---:|---:|---|
| `COLIBRI_PROVIDER_RFC.md` (`f4ac5dbd`) | ✔ | | | Architecture decisions unchanged; §6 deferred-job design is exactly what Phase 2 implements |
| Provider seam (`a260ed57`) | ✔ | | | `model_service.py` untouched between the branches; cherry-picked clean |
| Colibrì adapter + `deep_local.*` facade (`90e5175e`) | ✔ | | | Only `rpc_server.py`/fixture context conflicts; auto-merged, tests green |
| plan/doctor wrapper + fixtures (`28d70ec1`) | ✔ | | | Version keys re-verified against upstream `54cfe563`; fixtures still accurate |
| `COLIBRI_SPIKE_RESULT.md` (`4f6bcff1`) | ✔ | | | Historical record of the spike; kept verbatim |
| `deep_local.complete_once` | | | ✔ | Initially kept as a developer-only vertical proof; **dropped after review** — a public synchronous RPC that can freeze the sidecar has no place once the job system exists (`f5f8cbad`) |
| IPC golden fixture | | ✔ | | Base commit `e44e7742` itself failed `test_frontend_call_inventory…` (reproduced in an isolated worktree: jobs UI switched `documents.import` → `jobs.*` without updating the fixture); fixed deliberately in `2c98caea` |

## Architecture

```
React/Tauri UI ── JSON-RPC stdio ──► rpc_server.py (single-threaded dispatch)
   │                                        │
   ├─ JobsPanel (imports/OCR)               ├─ JobService (imports/OCR worker)
   │                                        ├─ DeepLocalJobService (NEW)
   │  (no Deep Local UI in this PR —        │    single daemon worker, FIFO,
   │   deferred until a separate PR can     │    persisted deep_local_jobs table
   │   select Sources, perform bounded      │        │
   │   retrieval, and submit a real         │        ▼
   │   evidence packet)                     │  ColibriProvider (text-only,
   │                                        │  loopback-only, redacted keys)
   │                                        │    ├─ urllib path (status/models)
   │                                        │    └─ cancellable http.client path
   │                                        │       + RequestCancelHandle
   │                                        │        │
   │                                        │        ▼
   │                                        │  coli serve (user-managed,
   │                                        │  127.0.0.1, one generation at
   │                                        │  a time, bounded FIFO queue)
   │                                        └─ colibri_cli.py (plan/doctor,
   │                                           argv-only, shell-free, 30 s cap,
   │                                           stderr never logged or returned)
```

- **One heavy-job substrate, two worker instances.** `DeepLocalJobService`
  reuses the v0.4 heavy-job discipline (`cancellation.py` cooperative
  primitive, single daemon worker, FIFO, fixed message codes, bounded
  queues, legal-transition state machine) rather than forking a second
  framework. It is a separate worker *instance* because an hours-long
  generation must never queue document imports behind it, and one worker
  per service enforces `coli serve`'s one-generation-at-a-time reality.
- **Everyday Local untouched.** `ModelService`'s public surface is
  byte-identical behind the provider seam (pinned by contract tests
  including instance-level monkeypatch compatibility and error-class
  identity). Interactive chat never routes to Colibrì.
- **No synchronous generation surface.** `DeepLocalService` is a read-only
  status/plan/doctor facade; generation happens exclusively through the
  persisted job RPCs (regression-tested).
- **Cancellable transport.** `RequestCancelHandle` lets the RPC thread
  abandon the worker's in-flight request. Windows subtlety proven by test:
  `shutdown(SHUT_RDWR)` does not unblock a threaded `recv()` and `close()`
  cannot release the OS handle while the response reader holds a
  `makefile()` io-ref — the handle must `detach()` the fd and
  `closesocket()` it. `auto_open = 0` guarantees a cancelled connection can
  never silently reconnect.

## Migrations and compatibility

- **New table `deep_local_jobs`** (additive `CREATE TABLE IF NOT EXISTS`,
  same pattern as the `documents.is_staging` column: no `SCHEMA_VERSION`
  bump, old profiles unaffected, downgrade-safe because v0.3/v0.4 code
  never reads it).
- **New settings keys** (KV store, absent = disabled): `deep_local_enabled`
  (default off), `deep_local_endpoint` (default `http://127.0.0.1:8000`),
  `deep_local_timeout_seconds` (default 3600), `deep_local_queue_wait_seconds`
  (default 600), `deep_local_cli_path`, `deep_local_model_path`.
- **No changes** to `sessions.model`, chat pipeline, OCR/vision paths,
  installer, or release assets. Bare model strings still mean Ollama.
  No frontend behavior changes: `src/` is untouched relative to the base.

## RPC contracts

New methods (all in the IPC golden fixture):

- `deep_local.submit {question, evidence?, model?, request_id?,
  max_output_tokens?, temperature?, top_p?, thinking?}` →
  `{ok, job, duplicate}` or structured `{ok: false, error_category, error}`
  when disabled/non-loopback. `request_id` makes submit idempotent
  (no-replay rule, same as `artifacts.analyze`).
- `deep_local.get {job_id}` → full snapshot **plus** content
  (question/evidence/result_text/thinking_text). User-initiated read of the
  user's own data.
- `deep_local.list {limit?}` → content-free snapshots only
  (`question_chars`, `evidence_count`, `result_chars`, usage, states).
- `deep_local.cancel {job_id}` → idempotent; semantics below.
- `deep_local.retry {job_id}` → explicit clone of a terminal
  non-completed job (`attempt_count`+1, `retry_of`); a live retry of the
  same origin is returned instead of stacking a duplicate; completed jobs
  refuse retry.

Read-only methods kept from the spike: `deep_local.status`,
`deep_local.plan`, `deep_local.doctor`. **`deep_local.complete_once` was
removed after review** — the persisted job RPCs are the only generation
path.

## State machine

States: `queued`, `checking_runtime`, `waiting_for_provider`, `running`,
`cancel_requested`, `completed`, `failed`, `cancelled_before_start`,
`interrupted` (last four + `completed` terminal). Legal transitions:

```
queued ──► checking_runtime ──► running ──► completed | failed
   │             │        └► waiting_for_provider ◄─┘ (429 backoff, bounded
   │             │                   │                 by queue-wait deadline)
   │             │                   └► running
   └────────────┴──── any non-terminal ──► cancel_requested
cancel_requested ──► cancelled_before_start (request never left the process)
                 ──► interrupted            (stopped waiting mid-flight)
                 ──► completed | failed     (result won the race — never undone)
running ──► interrupted                     (also: startup repair)
```

Honesty rules implemented and tested:

- `cancelled_before_start` is claimed **only** when the HTTP request
  provably never left the process (pre-request safe points).
- In-flight cancel closes our socket and terminates as
  `interrupted`/`stopped_waiting`; the server may keep computing. An HTTP
  disconnect is **never** reported as an engine-side cancellation
  guarantee.
- Startup repair (`SidecarApp.__init__`) marks every non-terminal persisted
  row `interrupted`/`interrupted_by_restart` with
  `error_category='interrupted'`, **appends the `interrupted` transition to
  `state_history_json`** so the persisted audit trail matches the repaired
  state, and offers retry.
- A cancel landing after completion never un-reports finished work.

## Files changed (base `e44e7742` → final head)

GitHub-reported PR diff: **28 changed files, +6,306/−67** (verified against
the GitHub compare API after push; the UI files added and removed within
the branch no longer appear in the three-dot diff). New runtime code:

- `python/odysseus_desktop_backend/services/providers/{__init__,base,ollama,colibri}.py`
- `python/odysseus_desktop_backend/services/{colibri_cli,deep_local_service,deep_local_jobs}.py`
- `python/odysseus_desktop_backend/storage.py` (+`deep_local_jobs` table)
- `python/rpc_server.py` (+8 `deep_local.*` methods, startup repair, shutdown)
- Tests/fixtures: `test_provider_seam.py`, `test_colibri_provider.py`,
  `test_colibri_cli.py`, `test_deep_local_jobs.py`,
  `test_deep_local_job_rpc.py`, `test_deep_local_e2e_upstream.py`,
  `fixtures/colibri/*` (incl. `stub_engine.py`, `make_fixture_model.py`)
- Docs: spike brief, RFC, spike result, this report.
- `src/` and `scripts/` are unchanged relative to the base (UI removed
  in review round; `package.json` carries no Deep Local script).

## Tests run and exact results (post-review head)

| Command | Result |
|---|---|
| `python -m pytest python\tests` | **504 passed, 7 skipped, 0 failed** (the 7 skips are the E2E module without its env var) |
| `ODYSSEUS_COLIBRI_UPSTREAM=<clone> python -m pytest python\tests` | **511 passed, 0 failed** |
| `npm run test:backend-status` | pass (`backend-status-tests-ok`) |
| `npm run test:progress` | pass (`chat-progress-tests-ok`) |
| `npm run test:readiness` | pass (`readiness row mapping tests passed`) |
| `npm run test:jobs-ui` | pass (99 assertions) |
| `npm run build:frontend` | pass (7.7 s; pre-existing >500 kB chunk warning) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | pass |
| `cargo test --manifest-path src-tauri\Cargo.toml` | pass (24 passed, 4 ignored) |
| `git diff --check` | clean |

Focused Deep Local suites (collected counts): provider seam 12, Colibrì
provider 43 (incl. 4 cancel-handle, 1 no-sync-surface regression),
plan/doctor CLI 18 (incl. 3 hostile-sentinel), persisted jobs 29, job RPC 7,
real-upstream E2E 7 — **116 focused tests**. Pre-existing failure reproduced
on the exact base: `2c98caea` documents the golden-fixture failure on
pristine `e44e7742` (isolated worktree, 1 failed / 4 passed) and fixes it
deliberately.

## Real vs fake inference evidence

Three tiers, from most deterministic to most real:

1. **Fake HTTP server** (`test_colibri_provider.py`, `test_deep_local_jobs.py`,
   `test_deep_local_job_rpc.py`): loopback `http.server` mirroring upstream's
   error objects, queue codes, and `x-colibri-queue-wait-ms` header. Covers
   health/models/completion/thinking/malformed/empty/401/404/429/refused/
   timeout/redaction/multimodal-rejection and all job semantics.
2. **Real upstream `coli serve`** (`test_deep_local_e2e_upstream.py`,
   env-gated): the actual `openai_server.py` HTTP surface at `54cfe563` —
   real request validation, queue scheduler, auth layer, disconnect-poll →
   `CANCEL` → engine path — driven through a **stub engine subprocess**
   that speaks the engine's stdio protocol (READY sentinel + STAT line,
   SUBMIT/DATA/DONE/ERROR/CANCEL) and a tiny GLM-shaped model directory
   with valid safetensors headers built in pure Python. Real `coli plan
   --json` and `coli doctor --json` run against the real upstream scripts
   (doctor honestly fails `engine.binary` on this machine and the wrapper
   maps it to a non-runnable overall state). 7 passed.
3. **Not run — exact blocker recorded:** upstream's own 313 M bench fixture
   (`c/tools/make_glm_bench_model.py`) requires `torch` + `transformers`
   (`GlmMoeDsaConfig`) to generate, and running it requires the MinGW-w64
   built `glm.exe` engine. This machine (Ryzen 5 4600H, 15.4 GB RAM,
   Windows 11) has **no C toolchain** (`gcc`/`make`/`cmake` absent) and no
   torch install. Real *token generation by the real engine* therefore
   remains unmeasured, exactly as the 370 GB-model path does. Everything
   above the engine process boundary is proven against real upstream code.

What is proven vs not: technically integrable ✔ (real upstream server,
real plan/doctor); completes a prompt through the real engine ✘ (engine
stubbed); correct/sourced answers on real hardware ✘ (needs a storage-rich
machine). Distinctions per the original brief remain explicit.

## Timings and resources

Measured on this machine (integration overhead, not inference):

- Full Python suite (511 tests incl. E2E): 4 m 21 s. E2E module alone:
  4.0 s including real `coli serve` startup (~1 s to healthy with the
  stub engine).
- Real `coli plan --json` wall time ≈ 0.4 s; `coli doctor --json` ≈ 0.4 s
  (fixture model; wrapper cap 30 s).
- Persisted job round trip against real upstream serve: `usage` captured
  per job — prompt/completion tokens, elapsed_ms, queue-wait ms when the
  header is present, derived tokens/s.
- In-flight cancel latency: the abandoned wait returns in <2 s (pinned by
  `test_cancel_handle_interrupts_in_flight_request`).

## Privacy and licensing status

- **Loopback only**: non-loopback endpoints are refused at submit and at
  provider construction (structured `disabled` error, not a warning).
  The pytest egress guard blocks non-loopback sockets suite-wide.
- **Keys**: environment-only (`ODYSSEUS_COLIBRI_API_KEY`), never persisted,
  never on a command line, stripped from plan/doctor child environments,
  redacted from every error string; sentinel-key tests assert absence from
  logs and payloads.
- **CLI output**: stderr is never logged and never returned; CLI failures
  use fixed error codes with fixed plain-language copy; hostile sentinel
  tests (fake model path, CLI path, username, API key) prove nothing
  leaks into logs or RPC-visible results.
- **Content**: `deep_local.list` snapshots, logs, traces, and
  `diagnostics.get` are proven (sentinel tests) to exclude questions,
  evidence snippets, answers, paths, and keys; content returns only via
  user-initiated `deep_local.get`.
- **No downloads, no registry contact, no telemetry, no cloud fallback.**
  The E2E harness clones upstream only on the dev machine and the test
  skips when no checkout is provided; nothing is vendored.
- **Licensing**: Colibrì code Apache-2.0 (re-confirmed at `54cfe563`).
  GLM-5.2 weights MIT per upstream README. Community mirrors remain
  **unaudited**; the licensing gate stays OPEN and continues to block any
  packaging, vendoring, or automated model acquisition. Detection-only
  integration requires no notices.

## Unresolved risks

1. **The real engine has never generated a token under this adapter.**
   The stub proves every layer above the engine process; a storage-rich
   machine with MinGW-w64 (or upstream's CI artifacts) is needed for the
   final tier. Recorded blocker: no C toolchain + no torch on this machine.
2. **Interrupted jobs may leave the server busy** for up to one generation;
   `waiting_for_provider` backoff mitigates, and future UI copy must say so.
3. **Upstream drift**: 35 commits landed between the spike audit and this
   build with contracts intact, but plan/doctor version keys and the engine
   stdio protocol are upstream-internal and may change; version gates fail
   safe (`incompatible_server`).
4. **No UI in this PR** (deliberate, per review): the backend is only
   reachable over RPC. The deferred UI PR must select Sources, perform
   bounded retrieval, submit a real evidence packet, and map every thrown
   RPC failure to fixed plain-language copy with hostile-error sentinels.
5. **Retention**: terminal jobs are pruned at 100; no user-facing storage
   accounting yet (v0.4.5 territory).

## Recommendation

**Keep as an experimental, default-off backend; do not merge before the
v0.4 gate closes and an independent re-review passes.** All four blocking
findings from the first review round are resolved by scope reduction (UI
and synchronous completion removed) and hardening (CLI privacy, repair
audit trail). The ordinary Ollama path is pinned unchanged by contract
tests. Next smallest responsible steps after re-review: (a) the evidence-
packet UI PR described above; (b) run the E2E module plus the real
MinGW-built engine with upstream's 313 M bench fixture on a machine with a
C toolchain, recording first real tokens-per-second and thermal data —
still without the 370 GB model.
