# Deep Local (experimental) — Fable Implementation Result

Status: complete on `feat/deep-local-fable`, awaiting independent review.
Nothing here changes `main`, the installer, release assets, or the v0.4
acceptance gate. Deep Local remains default-off and hidden.

Date: 2026-07-16. Author: Claude Code (Fable).

## Commits

- **Base:** `e44e7742` ("feat: add cancellable import jobs UI", tip of
  `feat/v0.4-indexing-control`).
- **Branch commits** (base → final):
  `66130a5d` brief → `683f040f` RFC → `2983cf1d` provider seam →
  `166b375d` Colibrì adapter → `f6f4a3bf` plan/doctor wrapper →
  `23d0b79b` spike result → `2c98caea` fixture reconcile →
  `24f47db1` cancellable transport → `3805df8c` persisted job backend →
  `cec5f93a` recovery/cancellation/privacy tests → `223e9dee` real-upstream
  E2E proof → `757f81a3` experimental UI → this report.
- **Upstream Colibrì audited/tested:** `https://github.com/JustVugg/colibri`
  at `main` = **`54cfe5632446ad333ca81c44c6a6c71ffec8a01d`** (2026-07-16;
  35 commits past the spike's `550ddcba`). Re-verified: plan JSON `version: 2`,
  doctor `schema_version: 1`, identical check IDs — the adapter contracts
  hold unchanged. Upstream code is Apache-2.0; it is never vendored.

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
| `deep_local.complete_once` semantics | | ✔ | | Kept as developer-only vertical proof, but the persisted job system (new) is the product surface; the UI never calls it |
| IPC golden fixture | | ✔ | | Base commit `e44e7742` itself failed `test_frontend_call_inventory…` (reproduced in an isolated worktree: jobs UI switched `documents.import` → `jobs.*` without updating the fixture); fixed deliberately in `2c98caea` |

Nothing was dropped.

## Architecture

```
React/Tauri UI ── JSON-RPC stdio ──► rpc_server.py (single-threaded dispatch)
   │                                        │
   ├─ JobsPanel (imports/OCR)               ├─ JobService (imports/OCR worker)
   └─ DeepLocalPanel (hidden behind         ├─ DeepLocalJobService (NEW)
      deep_local_enabled)                   │    single daemon worker, FIFO,
                                            │    persisted deep_local_jobs table
                                            │        │
                                            │        ▼
                                            │  ColibriProvider (text-only,
                                            │  loopback-only, redacted keys)
                                            │    ├─ urllib path (status/models)
                                            │    └─ cancellable http.client path
                                            │       + RequestCancelHandle
                                            │        │
                                            │        ▼
                                            │  coli serve (user-managed,
                                            │  127.0.0.1, one generation at
                                            │  a time, bounded FIFO queue)
                                            └─ colibri_cli.py (plan/doctor,
                                               argv-only, shell-free, 30 s cap)
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

Pre-existing flag-gated methods kept from the spike: `deep_local.status`,
`deep_local.plan`, `deep_local.doctor`, `deep_local.complete_once`
(developer-only, documented as blocking).

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
  `interrupted`/`stopped_waiting`; copy states the server may keep
  computing. An HTTP disconnect is **never** reported as an engine-side
  cancellation guarantee.
- Startup repair (`SidecarApp.__init__`) marks every non-terminal persisted
  row `interrupted`/`interrupted_by_restart` and offers retry.
- A cancel landing after completion never un-reports finished work.

## Files changed (base `e44e7742` → final)

33 files, +6,448 / −67. New runtime code:

- `python/odysseus_desktop_backend/services/providers/{__init__,base,ollama,colibri}.py`
- `python/odysseus_desktop_backend/services/{colibri_cli,deep_local_service,deep_local_jobs}.py`
- `python/odysseus_desktop_backend/storage.py` (+`deep_local_jobs` table)
- `python/rpc_server.py` (+9 `deep_local.*` methods, startup repair, shutdown)
- `src/features/deepLocal/{deepLocalModel.ts,DeepLocalPanel.tsx}`, `src/tauri.ts`, `src/App.tsx`
- Tests/fixtures: `test_provider_seam.py`, `test_colibri_provider.py`,
  `test_colibri_cli.py`, `test_deep_local_jobs.py`,
  `test_deep_local_job_rpc.py`, `test_deep_local_e2e_upstream.py`,
  `fixtures/colibri/*` (incl. `stub_engine.py`, `make_fixture_model.py`),
  `scripts/test-deep-local-ui.mjs`
- Docs: spike brief, RFC, spike result, this report.

## Tests run and exact results

| Command | Result |
|---|---|
| `python -m pytest python\tests` | **502 passed, 0 failed** (E2E module skips without env var) |
| `ODYSSEUS_COLIBRI_UPSTREAM=<clone> python -m pytest python\tests` | **509 passed, 0 failed** |
| `npm run test:backend-status` | pass (`backend-status-tests-ok`) |
| `npm run test:progress` | pass (`chat-progress-tests-ok`) |
| `npm run test:readiness` | pass (`readiness row mapping tests passed`) |
| `npm run test:jobs-ui` | pass (99 assertions) |
| `npm run test:deep-local-ui` | pass (71 assertions) |
| `npm run build:frontend` | pass (6.8 s; pre-existing >500 kB chunk warning) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | pass (2 m 00 s) |
| `cargo test --manifest-path src-tauri\Cargo.toml` | pass (24 passed, 4 ignored) |
| `git diff --check` | clean |

Focused Deep Local suites: provider seam 12, Colibrì provider 44 (incl. 4
cancel-handle), plan/doctor CLI 16, persisted jobs 29, job RPC 7, real-
upstream E2E 7 — **107 focused tests(+8 UI-model scripts' 170 assertions)**.
Pre-existing failure reproduced on the exact base: `2c98caea` documents the
golden-fixture failure on pristine `e44e7742` (isolated worktree, 1 failed /
4 passed) and fixes it deliberately.

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
   maps it to a non-runnable overall state). 7 passed in 3.3 s.
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

- Full Python suite 502 tests: 4 m 38 s. E2E module alone: 3.3 s including
  real `coli serve` startup (~1 s to healthy with the stub engine).
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
   the UI copy says so, but a user could interpret a busy follow-up job as
   a bug. `waiting_for_provider` backoff mitigates.
3. **Upstream drift**: 35 commits landed between the spike audit and this
   build with contracts intact, but plan/doctor version keys and the engine
   stdio protocol are upstream-internal and may change; version gates fail
   safe (`incompatible_server`).
4. **`deep_local.complete_once`** still blocks the RPC loop; it remains
   developer-only and is a candidate for removal once the job system is the
   only consumer.
5. **Retention**: terminal jobs are pruned at 100; no user-facing storage
   accounting yet (v0.4.5 territory).

## Recommendation

**Keep as an experimental, default-off branch feature; do not merge before
the v0.4 gate closes and an independent review passes.** The backend is
fully proven against real upstream server code, the ordinary Ollama path is
pinned unchanged by contract tests, and the UI is invisible without a
maintainer-set flag. The next smallest responsible step after review: run
the E2E module plus the real MinGW-built engine with upstream's 313 M bench
fixture on a machine with a C toolchain, recording first real
tokens-per-second and thermal data — still without the 370 GB model.
