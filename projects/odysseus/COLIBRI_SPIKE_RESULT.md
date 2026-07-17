# Colibrì Deep Local Spike — Result Report

Status: research spike complete on `research/colibri-deep-local-spike`.
Nothing here changes `main`, the installer, release assets, or the v0.4
acceptance gate.

Date: 2026-07-16. Author: Claude Code (Fable).

## Upstream tested

- Repository: `https://github.com/JustVugg/colibri`
- Commit: **`550ddcba83afd27a892dba92c587bfcc1d30f020`** (shallow clone,
  read-only audit of `openai_server.py`, `doctor.py`, `resource_plan.py`,
  `coli`, README, and the server/doctor/plan test suites).
- No Colibrì code was vendored; the spike only detects and talks to a
  user-provided installation.

## Files changed (branch total vs base `e8a36451`)

19 files, +3,197 / −66. New runtime code:

- `python/odysseus_desktop_backend/services/providers/{__init__,base,ollama,colibri}.py`
- `python/odysseus_desktop_backend/services/colibri_cli.py`
- `python/odysseus_desktop_backend/services/deep_local_service.py`
- `python/rpc_server.py` (+4 flag-gated `deep_local.*` methods)
- `python/odysseus_desktop_backend/services/model_service.py` (transport
  extraction only; public surface unchanged)

New tests/fixtures: `test_provider_seam.py`, `test_colibri_provider.py`,
`test_colibri_cli.py`, `fixtures/colibri/*` (plan/doctor JSON + fake
`coli` script), `fixtures/ipc_contract.golden.json` (+4 method names,
deliberate). Docs: `COLIBRI_PROVIDER_RFC.md`, this report.

Commits: `f4ac5dbd` (RFC) → `a260ed57` (seam) → `90e5175e` (adapter) →
`28d70ec1` (plan/doctor).

## Architectural decision (full reasoning in COLIBRI_PROVIDER_RFC.md)

- **Colibrì never enters synchronous chat.** Decisive fact: the sidecar
  JSON-RPC loop is single-threaded — a minutes-long generation blocks the
  entire app. Interactive chat stays Ollama-only.
- **Narrow internal provider seam, not a provider registry.** `ModelService`
  keeps its exact public surface (subclassing by `EvalModelService` and
  instance-level test monkeypatches both constrain it); Ollama transport
  moved verbatim behind `providers/ollama.py`; `providers/colibri.py` is a
  disjoint sibling consumed only by the flag-gated `deep_local.*` RPC
  surface.
- **The product shape is a persisted Deep Local job system, deferred.**
  It should be built on the cancellable heavy-job service currently
  pending on `feat/v0.4-indexing-control`, not forked in parallel on a
  research branch.
- **Provider identity:** bare model strings mean Ollama forever; Deep Local
  records provider/endpoint/model per job. No DB migration; the spike adds
  only settings keys (`deep_local_enabled` — default off, `deep_local_endpoint`,
  `deep_local_cli_path`, `deep_local_model_path`, `deep_local_timeout_seconds`).
- The RFC corrects several brief claims against upstream reality (tools are
  now supported upstream; `max_tokens` is clamped not rejected; plan and
  doctor use different JSON version keys; `coli plan --json` emits
  non-JSON on failure; `coli` is a Python script requiring interpreter
  launch on Windows).

## Tests run and exact results

All from the brief's required list, on this branch tip (`28d70ec1` + report):

| Command | Result |
|---|---|
| `python -m pytest python\tests` | **362 passed, 2 failed** — the 2 failures (`test_ipc_golden_fixtures.py::test_tauri_command_inventory…` and `…frontend_call_inventory…`) fail identically on the untouched branch base (verified via `git stash`); they concern Rust/frontend inventory drift (`rpc_call`) that predates this spike and is out of its scope. The Python-RPC inventory test **passes** including the new `deep_local.*` methods. |
| `npm run test:progress` | **pass** (`chat-progress-tests-ok`) |
| `npm run build:frontend` | **pass** (built in 18.66 s; standard >500 kB chunk-size warning, pre-existing) |
| `cargo check --manifest-path src-tauri\Cargo.toml` | **pass** (finished `dev` profile, 4 m 41 s) |

New focused suites: `test_provider_seam.py` 12 passed;
`test_colibri_provider.py` 33 passed; `test_colibri_cli.py` 16 passed.
Privacy/egress sentinels (`test_trace_privacy_sentinel.py`,
`test_no_egress.py`) pass with the new code present.

## Hardware and model reality

- Hardware: AMD Ryzen 5 4600H, 15.4 GB RAM, Windows 11 Home 10.0.26200
  (tier ≈ P3 laptop). Python 3.13.12, Node v21.6.2, cargo 1.96.0.
- **No real Colibrì model or server was used.** This machine cannot hold
  the ~370 GB GLM-5.2 container, and the brief forbids downloading it.
  All provider behavior was proven against a fake OpenAI-compatible
  loopback server that mirrors upstream's error objects, queue semantics,
  and headers; plan/doctor were proven against fixtures shaped from the
  audited upstream source plus a fake `coli` script.
- Consequently this spike proves **"technically integrable"**, not
  "completes a test prompt" and not "produces a correct/sourced answer" on
  real hardware. Those distinctions (per the brief) remain unmeasured
  until someone runs a real `coli serve` against this adapter.

## Measured data (fakes — integration overhead only, not inference)

- Provider `health()` first call: ~303 ms (Windows first-connection
  overhead); subsequent calls ~2–3 ms. `list_models()` ~2 ms;
  non-streaming `chat_once()` round trip ~3 ms against the fake.
- `coli doctor --json` wrapper wall time: ~131 ms; `coli plan --json`
  wrapper: ~123 ms (fake script; real plan/doctor read safetensors headers
  and should stay in seconds — the wrapper enforces a 30 s bound).
- Real-model numbers from upstream's community table (for expectation
  setting only, not measured here): 0.03–0.11 tok/s cold on ordinary SSDs;
  0.3–2.06 tok/s with warm caches, large RAM pins, or Apple-Silicon Metal.
  On potato-class machines (≤16 GB RAM) upstream's own data says the RAM
  cap, not the disk, is binding — Deep Local is genuinely a
  storage-rich-machine feature.

## Known limitations

1. `deep_local.complete_once` is synchronous and blocks the sidecar RPC
   loop for its duration; it is a developer-only vertical proof, bounded
   to 128 output tokens by default, and is not a product surface. The job
   system replaces it.
2. No streaming (SSE) support yet; long non-streaming requests depend on
   the OS not killing an idle connection. Upstream's SSE keepalive path is
   the right basis for the job-system phase.
3. Cancellation of an in-flight generation is not offered at all in the
   spike (honest choice: upstream cancellation is best-effort and
   unconfirmable from a closed connection).
4. Thinking mode `auto` maps to off by design; `on` splits `</think>`
   client-side and is only lightly exercised.
5. No UI. The surface is RPC-only and default-disabled.
6. The two pre-existing IPC golden-fixture failures on this branch base
   should be fixed wherever they were introduced (they involve a
   `rpc_call` Tauri command absent from the fixture), not on this branch.
7. `queue_wait_ms`-based tokens/s subtraction assumes the queue-wait
   header is present; absent header degrades to elapsed-time throughput.

## Privacy and licensing status

- Privacy: endpoints are hard-restricted to loopback (non-loopback is a
  structured `disabled` error, not a warning); API keys live only in
  `ODYSSEUS_COLIBRI_API_KEY`, are never persisted, never passed on a
  command line, stripped from plan/doctor child environments, and redacted
  from every error string (sentinel-key test). Responses are size-bounded
  (16 MB) and schema-checked. No downloads, no registry contact, no
  telemetry, no cloud fallback. Trace-privacy and no-egress sentinels pass.
- Licensing: Colibrì code Apache-2.0 (confirmed in repo). GLM-5.2 weights
  MIT per upstream README (Z.ai). Community pre-converted mirrors are
  **unaudited** — the licensing gate in the brief remains OPEN and blocks
  any packaging, vendoring, or automated model acquisition. Detection-only
  integration (this spike) requires no notices.

## Acceptance criteria check (brief §"Acceptance criteria")

1. RFC maps architecture + go/no-go — **done**. 2. Ollama behavior green
behind the seam — **done** (307→362 passing, contract tests). 3. Fake
server passes provider contract tests — **done** (33). 4. Detect server +
list model — **done** (`deep_local.status`). 5. One text-only completion
normalized — **done** (`deep_local.complete_once`). 6. Failures
categorized without jargon/secrets — **done** (taxonomy + redaction
tests). 7. plan/doctor wrapped and fixture-tested — **done** (16 tests).
8. No full GLM download — **done**. 9. No installer/release/main changes —
**done**. 10. Recommendation — below.

## Merge recommendation: **do not merge now; keep as a developer adapter on this branch**

Honest go/no-go: **conditional go** — the integration is architecturally
sound and cheap to keep alive, but it must not touch v0.4.

- **Do not merge into `main` before the v0.4 gate closes.** Deep Local
  serves storage-rich machines; v0.4's entire thesis is potato hardware.
  Nothing here helps a first-run noob, and any UI surface would actively
  confuse the v0.4 audience.
- **Stop is not warranted**: the seam cost is small, fully tested, and the
  Ollama path is provably unchanged; upstream is active (Windows-native
  path validated by community datapoints) and its machine-readable
  plan/doctor system maps cleanly onto PotatoCs readiness language.
- The adapter's real risk is product, not technical: on most users'
  hardware Deep Local would be `runnable_slow` at best. The doctor→
  plain-language mapping (runnable / runnable_slow / unsafe /
  incompatible / unavailable) exists precisely to keep that honest.

### Next smallest responsible task

After the heavy-job service (`feat/v0.4-indexing-control`) merges and the
v0.4 gate closes: **build `deep_local.submit/get/list/cancel/retry` as a
persisted job table on that job runner** (states and restart-repair
semantics already specified in RFC §6), validated end-to-end against the
existing fake server plus — on a real storage-rich machine — upstream's
tiny bench fixture (`make_glm_bench_model.py`, 313 M params) instead of
the full GLM container. That is the first point at which a real
"sourced Deep Local answer" measurement becomes possible without a 370 GB
download.
