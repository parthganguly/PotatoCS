# RFC: Colibrì as an Optional "Deep Local" Inference Provider

Status: Phase 0 deliverable of the Colibrì deep-local integration spike
(`research/colibri-deep-local-spike`). Nothing in this RFC changes v0.4 scope.

Author: Claude Code (Fable), 2026-07-16.

Upstream audited: `https://github.com/JustVugg/colibri` at commit
**`550ddcba83afd27a892dba92c587bfcc1d30f020`** (shallow clone, 2026-07-16).

PotatoCs baseline audited: branch `research/colibri-deep-local-spike`, forked
from `e8a36451` ("audit: record v0.4 P3 hardware resource baseline").
Note: the cancellable heavy-job service and jobs RPC surface (`d1c42848`,
`7dc1971a` on `feat/v0.4-indexing-control`) are **not** on this branch; they
are unmerged v0.4 work. This spike must not depend on them, but the Deep
Local job system (Phase 3, deferred) should be built on that subsystem once
it lands rather than inventing a parallel one.

---

## 1. Corrections to the brief (upstream reality check)

The brief instructed an aggressive audit. These claims in
`COLIBRI_DEEP_LOCAL_SPIKE.md` are outdated or imprecise at the audited
commit:

1. **Tools are now supported upstream.** The brief says "Tools, images,
   audio, stop sequences, logprobs, and some penalties are intentionally
   unsupported and return explicit errors." As of `550ddcb`,
   `openai_server.py` fully renders OpenAI `tools`/`functions` and
   `tool_choice` into GLM-5.2's native tool-call template and parses
   `<tool_call>` output back into OpenAI `tool_calls` (streaming and
   non-streaming). Still explicit 400 errors: `stop`, `logprobs`,
   `frequency_penalty`/`presence_penalty`, `seed`, non-text
   `response_format`, `n != 1`, and non-text message content parts
   (`unsupported_content_type` — so images/audio remain unsupported).
   The spike adapter stays text-only regardless; this only matters for the
   error taxonomy ("tools" is not a permanent server-side error).
2. **`max_tokens` above the server cap is clamped, not rejected** (server
   `--max-tokens`, default 1024). The adapter cannot assume its
   output-token limit was honored; it must read `usage` and
   `finish_reason: "length"`.
3. **Mid-generation cancellation exists upstream but is indirect.** The
   server polls the client socket; when it sees a disconnect it sends
   `CANCEL <request_id>` to the engine on stdin, and the engine
   acknowledges. However: (a) the disconnect poll only runs when the next
   engine DATA event arrives — during a minutes-long cold prefill no data
   flows and nothing is cancelled; (b) the client that closed the
   connection can never receive confirmation. So "closed HTTP connection"
   still must not be reported as completed cancellation (§8).
4. **`plan` and `doctor` use different version keys.** Doctor JSON is
   `{"schema_version": 1, ...}`; plan JSON is `{"version": 2, ...}`. A
   wrapper keying only on `schema_version` mis-handles plan output.
5. **`coli plan --json` does not emit JSON on failure.** `cmd_plan` calls
   `sys.exit("cannot create resource plan: ...")` — exit 1 with a plain
   string on stderr. Only `doctor` guarantees a JSON report on every exit
   path (including exit 2, via a synthetic `config.arguments` check).
6. **`coli` is a Python script, not a binary.** On Windows the documented
   invocation is `python coli doctor --json`. A subprocess wrapper that
   exec's the configured path directly will fail on Windows; it must
   support interpreter-launched scripts (§10).
7. **Doctor exit codes confirmed**: 0 ok/warning, 1 error, 2 invalid CLI
   values (which still prints a valid JSON report). Check IDs at this
   commit: `model.path`, `model.config`, `model.tokenizer`,
   `storage.persistence`, `engine.binary`, `accelerator.cuda`,
   `model.shards`, `storage.disk`, `memory.ram`, `placement.plan`, plus
   synthetic `config.arguments` on exit 2.
8. **Request body limit is 4 MB** (`MAX_BODY = 4 << 20`). Evidence packets
   must be bounded below this (§7).
9. **Thinking output is inline, not structured.** With
   `enable_thinking`/`reasoning_effort`, the non-streaming response
   content begins with the reasoning text and a `</think>` marker (the
   prompt ends with `<|assistant|><think>`); there is no separate
   `reasoning_content` field in the non-streaming body. (Streaming uses
   `reasoning_content` deltas only as keepalive pings.) An adapter that
   enables thinking must split on the first `</think>` itself.
10. **`/health` is unauthenticated**; `/v1/models` and completions require
    the Bearer key when `COLI_API_KEY` is set. `/health` exposes scheduler
    counters (`active/queued/capacity/max_queue/admitted/completed/
    rejected/timed_out/cancelled`), `kv_slots`, and optional `tiers`/
    `hwinfo`. Queue wait is returned as header `x-colibri-queue-wait-ms`.
11. **The `model` field must byte-match the server's `--model-id`**
    (default `glm-5.2-colibri`) or the request 404s with
    `model_not_found`. Model identity is per-server-config, not
    discoverable from weights — the adapter must use the id from
    `GET /v1/models`, never a hardcoded name.
12. Brief file reference `V04_HARDWARE_RESOURCE_AUDIT.md` does not exist;
    the actual file is `projects/odysseus/V04_HARDWARE_AUDIT.md`.
13. **Licensing**: Colibrì code is Apache-2.0 (LICENSE in repo). README
    states GLM-5.2 weights are released by Z.ai under MIT. The
    pre-converted community mirrors (`mateogrgic/GLM-5.2-colibri-int4-…`,
    `jlnsrk/GLM-5.2-colibri-int4`) carry their own HF model-card terms and
    were **not** audited in this spike; the licensing gate in the brief
    remains open and blocks any packaging/auto-download work (§11).

Confirmed as stated: Apache-2.0 code license; native Windows 11 via
MinGW-w64 (community-validated, issues #113/#128); `plan`/`doctor` are
read-only and never load tensors; text-only OpenAI-compatible API with SSE,
usage counts, queue reporting, `reasoning_effort`/`enable_thinking`; GLM-5.2
int4 ≈ 370 GB disk / ~20 GB peak RAM / 0.03–0.1 tok/s cold on ordinary SSDs
(warm/pinned setups measured 0.3–2 tok/s; community table in README);
one generation at a time with a bounded FIFO queue (`--max-queue`, default
8; `--queue-timeout`, default 300 s; 429 with `queue_full`/`queue_timeout`
codes and `Retry-After` header).

## 2. Every current `ModelService` consumer (call-site map)

`ModelService` (`python/odysseus_desktop_backend/services/model_service.py`,
717 lines) is constructed once in `rpc_server.py:81` and re-constructed
ad-hoc in two services. Full consumer map at this commit:

| Consumer | Methods used | Provider-neutral? |
|---|---|---|
| `rpc_server.py:135-139` RPC `models.detect_ollama`, `models.list`, `models.capabilities`, `models.inspect`, `models.refresh_capabilities` | `detect_ollama`, `capabilities`, `inspect`, `refresh_capabilities` | **No** — names, payload shapes (`/api/tags`, `/api/show` fields) and the `runtime_status`/`model_capabilities` tables are Ollama-shaped |
| `rpc_server.py:232-238` `diagnostics.get` | `detect_ollama`, `ps`, `capabilities` | No — `ps` wraps Ollama `/api/ps` |
| `ChatService._chat_model_detailed` (`chat_service.py:2138-2189`) | `chat_detailed`, `chat` (duck-typed fallback for test fakes) | **Yes in shape** — takes model + messages + options + thinking + timeout, returns the normalized dict from `structured_chat_response` (which embeds Ollama `raw`, `done_reason`, ns-durations) |
| `ChatService._vision_status` (`chat_service.py:1398`) | `inspect` | No — Ollama capability probe |
| `ChatService._generate_multimodal_reply` (`chat_service.py:1804`) | `chat_vision_history_detailed` | No — Ollama base64 `images` message format |
| `OCRService` (`ocr_service.py:166,222`) + `LocalVLMTextExtractor` | `capabilities`, `chat_vision_detailed` | No (vision) |
| `VisionService` (`vision_service.py:96,289,293`) | `inspect`, `chat_vision_detailed` | No (vision) |
| `CampaignService` (`campaign_service.py:67-69`) — constructs its own `ModelService(self.db)` | `detect_ollama`, `ps` | No |
| `EvalService` / `EvalModelService` (`eval_service.py:146-201,883,901`) — **subclasses `ModelService`**, overrides `chat`/`chat_detailed` calling `super()` | `chat`, `chat_detailed`, `ps` | Chat shape yes; subclassing pins the class hierarchy |
| `vision_benchmarks/runner.py:150,159` | `detect_ollama`, `inspect` | No |
| Tests (`test_rpc_server.py`, `test_trace_privacy_sentinel.py`, many others) | duck-typed fakes implementing subsets (`detect_ollama`, `ps`, `chat`, `chat_detailed`) | The fakes define the de-facto public contract |

Two structural facts constrain any refactor:

- **`EvalModelService` subclasses `ModelService` and calls `super()`** —
  the facade's method signatures and semantics are inheritance API, not
  just call API. Moving transport behind an internal provider object is
  safe; renaming/splitting the facade is not.
- **`ChatService._chat_model_detailed` inspects
  `type(self.models) is ModelService or detailed_impl is not
  ModelService.chat_detailed`** (`chat_service.py:2153-2154`) to decide
  whether `chat_detailed` is available on a fake. Any change to
  `ModelService`'s class identity breaks this duck-typing dance.

**Conclusion:** the only genuinely provider-neutral surface today is
"chat: model + messages + options → normalized result dict + errors".
Detection, capabilities, ps, and vision are Ollama-specific in both name
and shape, and several are persisted to Ollama-shaped tables
(`runtime_status`, `model_capabilities`).

A further, decisive runtime fact: **the sidecar dispatch loop is
synchronous and single-threaded** (`rpc_server.py:693-711` — one request is
fully handled before the next line of stdin is read). Any RPC that blocks
for minutes blocks *every* RPC: chat, readiness polling, health pings, the
degraded-banner heartbeat. This alone decides §5 and §6.

## 3. General provider abstraction vs narrow adapter

**Decision: narrow, internal seam — not a general provider registry.**

- Keep `ModelService` exactly as the facade every consumer sees. Public
  method names, signatures, return shapes, DB writes, log lines: unchanged.
- Extract the Ollama HTTP transport + detection internals into an internal
  `providers/ollama.py` used by `ModelService` by delegation (Phase 1).
  The extraction is behavior-preserving; existing tests plus new contract
  tests prove it.
- Add `providers/base.py` with small typed result dataclasses and a shared
  error taxonomy (§9) used by both providers.
- Implement `providers/colibri.py` as a sibling provider that **no existing
  service calls**. It is consumed only by the new, flag-gated `deep_local.*`
  RPC handlers (§7). `ChatService`, `OCRService`, `VisionService`,
  `CampaignService`, `EvalService` are untouched.

Why not a general abstraction now: there is exactly one alternate backend,
its consumer set is disjoint from Ollama's (jobs, not interactive chat), and
the Ollama-shaped persistence tables mean a "provider-neutral"
`detect`/`capabilities` would be a fiction until a second interactive
backend actually exists. The seam gives us the reusable pieces (transport,
normalized results, errors) without betting the v0.4 chat path on an
abstraction with one real implementation. What *is* deliberately reusable
for llama.cpp-server/MLX/NPU later: the OpenAI-compatible HTTP client, the
normalized `ProviderChatResult`/`ProviderStatus` types, the error taxonomy,
and the subprocess JSON wrapper pattern (§12).

## 4. Provider and model identity; profile compatibility

Today `sessions.model` and `settings.default_model` hold bare Ollama tags
(`"llama3.2"`, `"qwen2.5:3b"`). Rules adopted:

1. **A bare model string means Ollama, forever.** No migration of existing
   rows, ever. Old profiles keep working by definition.
2. **Deep Local never writes to `sessions.model`.** Deep Local work is a
   job (§6) with its own explicit `provider` (`"colibri"`), `endpoint`
   fingerprint, and `model_id` fields recorded per job. Provider identity
   in chat sessions is therefore out of scope until a second *interactive*
   provider exists.
3. If a prefixed form is ever needed for interactive chat, the reserved
   syntax is `provider:model` with bare strings defaulting to `ollama` —
   but this RFC explicitly rejects introducing it now (`"llama3.2:latest"`
   already contains `:`; parsing would need a provider allow-list and
   touches title/display code in `display_model_name`; zero benefit until
   an interactive second provider exists).

**DB changes for the spike: none.** Settings are a KV store
(`settings_service.py`); the spike adds only new keys (defaults applied in
code, absent = disabled): `deep_local_enabled` (bool, default `false`),
`deep_local_endpoint` (default `http://127.0.0.1:8000`),
`deep_local_cli_path` (default empty = plan/doctor unavailable),
`deep_local_timeout_seconds` (default 3600). API key comes only from the
environment (`ODYSSEUS_COLIBRI_API_KEY`), never from the DB (§8).
The deferred job table design is sketched in §6 for the later phase.

## 5. Does Colibrì belong in synchronous chat? **No.**

Four independent reasons, any one sufficient:

1. **Sidecar architecture.** JSON-RPC dispatch is single-threaded
   (`rpc_server.py` main loop). A Colibrì `chat.send` at 0.05–1 tok/s with
   a 500-token answer blocks the entire application — including the
   readiness UI and the degraded-state heartbeat — for minutes to hours.
2. **Timeout semantics.** `chat.send` uses
   `INTERACTIVE_CHAT_TIMEOUT_SECONDS = 120`. The honest fix is not a
   30-minute timeout (rejected, §13.3): the UI, progress events, retry
   semantics, and the user's mental model are all built around an
   interactive reply.
3. **Queueing.** The Colibrì server runs one generation at a time; a second
   chat turn would 429 or queue behind an hours-long first turn. Interactive
   chat cannot surface that honestly.
4. **Restart honesty.** v0.3's hard-won recovery rules (interrupted runs
   marked on startup) have no equivalent for an in-flight synchronous chat
   over stdio; a sidecar restart would silently orphan hours of work.

Interactive chat stays Ollama-only. Colibrì is reachable only through the
Deep Local surface.

## 6. Persisted job system vs extended timeouts

**Decision: the product shape is a persisted Deep Local job system; the
spike builds the provider layer it needs but not the job system itself.**

The job system is the right target because a Deep Local answer is
valuable *because* it is slow: it must survive app restart, be listable,
cancellable-before-start, and honest about interruption. Extending
`chat.send` timeouts is rejected (§13.3).

Deferred design (for the post-spike phase, to be built on the heavy-job
service from `feat/v0.4-indexing-control` once merged):

- Table `deep_local_jobs`: `id`, `created_at`, `updated_at`, `state`,
  `provider` (`"colibri"`), `endpoint`, `model_id`, `question`,
  `evidence_packet_json` (bounded, source ids + quoted snippets only),
  `result_text`, `usage_json` (tokens, queue-wait ms, elapsed ms),
  `warnings_json`, `error_category`, `error_message` (redacted),
  `attempt_count`.
- States exactly as the brief lists: `queued`, `checking_runtime`,
  `waiting_for_provider`, `running`, `completed`, `failed`,
  `cancel_requested`, `cancelled_before_start`, `interrupted`.
- Startup repair: any job in `checking_runtime`/`waiting_for_provider`/
  `running`/`cancel_requested` at boot → `interrupted`, with retry offered.
  (Same pattern as `recover_interrupted_runs` for benchmarks.)
- A single background worker thread executes at most one job at a time
  (matching the server's one-generation reality), with the RPC loop staying
  responsive. `deep_local.submit/get/list/cancel/retry` as in the brief.

Why the spike stops short of this: the job system's hard parts
(worker-thread lifecycle inside the sidecar, restart repair, cancellation
races) are exactly what the in-flight jobs subsystem on
`feat/v0.4-indexing-control` implements for OCR/indexing. Building a second
one now on a research branch would fork that design while it is still
being reviewed — the definition of irresponsible scope. The spike instead
proves the layer below it (§7), which is required no matter which job
runner hosts it.

## 7. The spike's vertical proof (smallest responsible slice)

What the spike implements (matches the acceptance criteria in the brief):

1. **Phase 1 — provider seam, zero behavior change** (§3).
2. **Phase 2 — `ColibriProvider`** against a user-managed server:
   `GET /health` detection (unauthenticated), `GET /v1/models` listing
   (authenticated), non-streaming `POST /v1/chat/completions`; maps
   `temperature`, `top_p`, output-token limit
   (`max_tokens`, expecting clamping); maps PotatoCs thinking modes
   `off→(omitted)`, `on→enable_thinking: true` + client-side `</think>`
   split, `auto→off` for the spike (deliberate: reasoning multiplies
   generation time on a 0.1 tok/s backend; "auto" must not silently choose
   hours); normalizes usage/elapsed/queue-wait/provider identity/warnings;
   explicit `unsupported_feature` errors for vision/multimodal/tool
   requests from our side; 429 → `queue_saturated`/`queue_timeout` with
   retry guidance; separate long-job timeout setting; loopback-only
   endpoint enforcement; Authorization header redaction everywhere.
3. **Plan/doctor subprocess wrapper** (§10) with fixture-driven tests and
   plain-language readiness mapping.
4. **Flag-gated RPC surface**, no UI:
   - `deep_local.status` → `{enabled, endpoint, reachable, healthy,
     queue: {...}, models: [...], error, error_category}`
   - `deep_local.plan` → wrapped `coli plan --json` + plain-language rows
   - `deep_local.doctor` → wrapped `coli doctor --json` + overall state:
     `runnable | runnable_slow | unsafe | incompatible | unavailable`
   - `deep_local.complete_once` → one bounded text-only completion through
     the adapter (developer-only vertical proof; documented as blocking the
     sidecar for its duration, default `max_tokens` small, requires
     `deep_local_enabled`).
   All methods return a structured error (not an exception dump) when
   `deep_local_enabled` is false or the endpoint is non-loopback.
5. **Fake Colibrì server** (loopback `http.server` in tests) covering:
   health ok/fail, models list, normal completion, thinking content,
   malformed JSON, empty body, 401/403, 404 wrong model, 429 queue-full and
   queue-timeout with `Retry-After`, connection refused, request timeout,
   secret redaction, unsupported multimodal rejection. The pytest egress
   guard (`test_no_egress.py`) already enforces loopback-only in CI.

Data flow for a future Deep Local RAG job (target shape; spike proves the
provider steps marked ▲):

```
User picks Sources ──► existing OCR / index / retrieval (Ollama-era code, unchanged)
                              │
                              ▼
                    evidence packet builder
                    (source ids + bounded quoted snippets, ≤ ~64 KB,
                     well under the server's 4 MB body cap)
                              │
        job persisted (queued) ── sidecar restart ⇒ interrupted + retry
                              │
                              ▼
              ▲ doctor/plan gate: runnable? slow? unsafe?
                              │
                              ▼
              ▲ ColibriProvider.chat_once(...)  ──►  coli serve (127.0.0.1:8000)
                 │   non-streaming, long timeout        one generation at a time,
                 │   429 ⇒ waiting_for_provider         FIFO queue
                              │
                              ▼
              ▲ normalized result: text, usage, queue-wait, elapsed,
                 provider+model identity, warnings
                              │
                              ▼
              job completed: result + sources + timings stored; trace
              excludes prompts/paths/keys (same allow-list discipline as
              build_operation_trace)
```

Component diagram (spike scope solid, deferred dashed):

```
React/Tauri UI ──JSON-RPC stdio──► rpc_server.py
                                     │
        ┌────────────────────────────┼──────────────────────────────┐
        │                            │                              │
   ChatService ── ModelService (facade, unchanged API)         deep_local.* handlers (new, flag-gated)
   OCR/Vision/     │                                                │
   Campaign/Eval   ├─ providers/ollama.py  (moved transport)        ├─ providers/colibri.py  (new)
   (unchanged)     └─ providers/base.py    (shared result types,    └─ colibri_cli.py (plan/doctor
                                            error taxonomy)             subprocess wrapper)
                                                                        │
                                              ┌─────────────────────────┴───┐
                                              ▼                             ▼
                                     coli serve (user-managed,     coli plan/doctor --json
                                     127.0.0.1:8000)               (argv arrays, bounded timeout)
   [dashed / deferred]: deep_local_jobs table + worker thread + submit/get/list/cancel/retry,
   built on the heavy-job service after feat/v0.4-indexing-control merges.
```

## 8. Cancellation, restart, privacy, and API-key semantics

**Cancellation honesty.** Upstream reality (§1.3): queued requests can be
cancelled for real (the scheduler removes the ticket before admission);
running generations are cancelled best-effort — the server notices client
disconnect only when engine data flows, then sends `CANCEL` to the engine;
the disconnecting client gets no confirmation. Therefore:

- `cancelled_before_start` may be claimed only when the job was cancelled
  before the HTTP request was sent, or the request provably never left the
  queue (429/queue removal).
- After the request is in flight, cancellation is `cancel_requested` →
  terminal `interrupted` ("PotatoCs stopped waiting; the Colibrì server may
  continue working on this for some time"), never "cancelled" as a success
  claim. UI copy must say the engine may still be busy and the queue may
  stay occupied.
- The spike's `deep_local.complete_once` is synchronous and supports no
  mid-flight cancel; that limitation is stated in its result docstring and
  is one reason it is developer-only.

**Restart.** Spike: nothing persists, nothing to repair. Job phase: startup
repair marks in-flight jobs `interrupted` and offers retry (§6), mirroring
`EvalService.recover_interrupted_runs`.

**API keys.** Source: environment variable `ODYSSEUS_COLIBRI_API_KEY` only
(brief's choice for the spike). Never written to settings/DB/traces/logs;
never passed on a command line (plan/doctor need no key — they are local
subprocesses); the provider builds the `Authorization` header at request
time and every error path formats exceptions through a redactor that
replaces the key value with `***` before any string leaves the provider.
A dedicated test asserts the sentinel key never appears in logs, RPC error
payloads, or stored state.

**Privacy threat model.**

| Threat | Control |
|---|---|
| Prompt/RAG text leaks into logs or traces | Provider logs only counts/durations/categories; reuse the `build_operation_trace` allow-list discipline; trace-privacy sentinel test extended to the adapter |
| API key leaks via error strings (urllib embeds URLs/headers in some exceptions) | Central redaction wrapper on every exception → user-facing message path |
| Non-loopback endpoint exfiltrates documents | Spike refuses non-loopback endpoints outright (parse host, require `127.0.0.1`/`::1`/`localhost`); a later opt-in with explicit warning is a product decision, not a default |
| Malicious/compromised local server returns oversized or non-JSON bodies | Response size cap (16 MB read limit), content-type check, JSON parse with `malformed_response` category; the server is treated as a local privileged service, not trusted |
| Silent model-registry contact | The adapter performs zero downloads; no Hugging Face URLs anywhere in runtime code; pytest egress guard blocks non-loopback in tests |
| Support bundles include model paths / directories | `deep_local.doctor` output stores paths only in the RPC response (user-initiated), never in persisted diagnostics; job records (later) store source *ids*, not paths |
| Replay of non-idempotent ops | Spike adds no non-idempotent RPC; job `submit` (later) takes a client-supplied request id like `artifacts.analyze` |

## 9. Error taxonomy

Extends the existing `ModelServiceError.category` convention (strings the
UI already knows how to keep jargon-free):

| Category | Trigger | User-facing meaning |
|---|---|---|
| `connection_failure` | TCP refused / DNS / server down | "Colibrì server is not running at 127.0.0.1:8000." |
| `timeout` | Request exceeded the Deep Local timeout | "The job ran longer than the configured limit." |
| `auth_failure` | HTTP 401/403 (`invalid_api_key`) | "The server requires an API key; set ODYSSEUS_COLIBRI_API_KEY." |
| `invalid_model` | HTTP 404 `model_not_found` (also wrong-path 404) | "The server does not offer this model id." |
| `queue_saturated` | HTTP 429 code `queue_full` | "The server is busy; try again later." (Retry-After honored) |
| `queue_timeout` | HTTP 429 code `queue_timeout` | "Waited too long for a free slot." |
| `unsupported_feature` | Our own guard (vision/tools/multimodal request) or server 400 `unsupported_*` | "Deep Local is text-only." |
| `malformed_response` | Non-JSON / wrong shape / oversized | "The server returned something unreadable." |
| `empty_response` | 200 with no content | "The server returned an empty answer." |
| `incompatible_server` | Unknown plan/doctor schema version; non-OpenAI body shapes | "This Colibrì version is not supported yet." |
| `server_error` | HTTP 5xx (`engine_error`, `scheduler_closed`) | "The Colibrì server hit an internal problem." |
| `disabled` | `deep_local_enabled` is false / endpoint rejected | "Deep Local is not enabled." |

Raw upstream messages are preserved in a `detail` field for developer logs
(redacted), never as the primary user string.

## 10. Plan/doctor subprocess wrapper

- Invocation: argv arrays only. Configured `deep_local_cli_path` is
  canonicalized (`Path.resolve()`), must exist and be a file. If it ends in
  `.py` or is extensionless-with-python-shebang (the upstream `coli`
  layout), invoke as `[sys.executable, path, subcommand, "--json", ...]`;
  `.exe`/`.bat` are executed directly. No shell, ever.
- Model path (if provided) is canonicalized and passed via `--model`;
  no environment secrets; `COLI_API_KEY` explicitly stripped from the
  child environment (plan/doctor don't need it).
- Bounded timeout (default 30 s — plan/doctor read only safetensors
  headers; README-order-of-magnitude is seconds), `stdout` parsed as JSON,
  `stderr` kept only in developer logs after redaction.
- Version gating: doctor accepts `schema_version == 1`; plan accepts
  `version == 2`. Anything else → `incompatible_server` with the message
  naming the seen and supported versions ("fail safely with an actionable
  compatibility message").
- Exit codes mapped exactly: doctor 0 → status from JSON (`ok`/`warning`),
  1 → `error` (JSON still parsed for checks), 2 → `invalid_arguments`
  (JSON parsed; synthetic `config.arguments` check surfaced); plan 0 →
  parse JSON, nonzero → `plan_failed` with redacted stderr line.
- Plain-language translation (never upgrading a warning to a green claim):
  - storage: model bytes vs `tiers.disk.available_bytes`;
  - RAM: `tiers.ram.budget_bytes` vs `available_bytes`, plus
    `cache_slots_per_layer` ("safe reserve" = budget already includes the
    12% headroom upstream applies);
  - VRAM tier: `tiers.vram.budget_bytes` / `expert_capacity` / devices;
  - engine/CUDA: `engine.binary`, `accelerator.cuda` checks verbatimly
    summarized;
  - model dir validity: `model.path`/`model.config`/`model.tokenizer`/
    `model.shards`;
  - overall: `runnable` (ok, no cold-expert warning), `runnable_slow`
    (ok/warning with `cold expert misses may reach disk` warning — always
    presented as "may be extremely slow"), `unsafe` (RAM fail), 
    `incompatible` (schema/engine fail), `unavailable` (no CLI configured).
- Fixtures: real captured JSON from this audit committed under
  `python/tests/fixtures/colibri/` + a tiny fake `coli` script for
  subprocess-level tests. No tensor loading anywhere.

## 11. Windows packaging / licensing gate (later phase, listed as required)

If PotatoCs ever manages the runtime (explicitly out of spike scope):
MinGW-w64-built `glm.exe` redistribution (Apache-2.0 notices; winlibs GCC
runtime licensing), optional `coli_cuda.dll` (MSVC+nvcc build; CUDA EULA
redistribution terms for cudart), code-signing both binaries with the
PotatoCs cert, Python-runtime hosting for `coli` (script, not exe),
`THIRD_PARTY_NOTICES.md` additions, GLM weight license (MIT per upstream) +
community-mirror terms audit, installer copy that never implies the 370 GB
model is included, and an explicit-download-consent flow. None of this
blocks the spike, which only detects a user-provided installation.

## 12. Reuse for future backends

Deliberately reusable: `providers/base.py` result types
(status/model-list/chat-result/usage/errors), the OpenAI-compatible HTTP
client in `providers/colibri.py` (llama.cpp `llama-server`, vLLM, LM Studio
and MLX servers speak the same protocol — the Colibrì-specific parts are
isolated to queue-header parsing, `</think>` handling, and error-code
mapping), the redaction helper, and the subprocess JSON wrapper pattern
(usable for `llama-bench`-style readiness probes and NPU runtime doctors).
Not reusable by design: Ollama detection/capabilities/persistence, which
stay Ollama-shaped.

## 13. Rejected approaches (all seven from the brief, with reasons)

1. **Call Colibrì directly from React/Tauri.** Bypasses the sidecar's
   privacy/trace discipline, egress tests, and settings validation; puts
   API keys and evidence packets in the webview; duplicates error taxonomy
   in TS; CORS conveniences upstream (`tauri://localhost` is in the default
   allow-list!) make this temptingly easy and architecturally wrong.
   Rejected.
2. **Replace Ollama globally.** Destroys the v0.4 product (interactive
   potato-class chat at seconds-latency); Colibrì is text-only (no vision
   path for OCR/artifacts); 370 GB + 0.1 tok/s is the opposite of the
   ordinary-computer path. Rejected.
3. **30-minute (or hours) timeout on ordinary `chat.send`.** Blocks the
   single-threaded RPC loop and thus the whole UI; breaks the interactive
   timeout contract tests; still dishonest at the p99 (jobs can exceed any
   fixed timeout); provides no restart story. Rejected.
4. **Download GLM-5.2 from the PotatoCs installer.** 370 GB without
   consent; violates "no silent registry contact" and the licensing gate;
   installer size/duration absurdity; mirrors unaudited. Rejected.
5. **Report a closed HTTP connection as successful cancellation.** Upstream
   provably continues generating until the next data event triggers the
   disconnect poll (and during cold prefill that can be many minutes);
   claiming success lies about queue occupancy and thermal/disk load.
   Rejected — semantics in §8 instead.
6. **Expose raw Colibrì JSON/errors to nontechnical users.** Violates the
   v0.4 A4 "noob-safe error copy" principle; upstream messages name
   safetensors headers, KV slots, CUDA linkage. Rejected — taxonomy in §9
   with plain-language mapping in §10.
7. **Mix Colibrì work into the v0.4 acceptance gate.** v0.4 is Potato Mode
   and first-run simplicity; Deep Local is experimental and
   storage-rich-machine-only. This branch stays a research branch; no
   installer, release-asset, or `main` changes; the v0.4 gate documents are
   untouched. Rejected.

## 14. Staged delivery plan

| Stage | Content | Gate |
|---|---|---|
| 0 (this doc) | RFC | committed on research branch |
| 1 | Provider seam, zero behavior change; contract tests | full pytest suite green, no UI/RPC diff |
| 2 | `ColibriProvider` + fake-server test matrix; plan/doctor wrapper + fixtures; flag-gated `deep_local.status/plan/doctor/complete_once` | new tests green; egress + trace-privacy sentinels green |
| 3 (spike report) | `COLIBRI_SPIKE_RESULT.md` with go/no-go | required commands from the brief all pass |
| 4 (post-spike, needs decision) | Persisted job system on the merged heavy-job service; hidden experimental UI | separate RFC addendum + product sign-off |
| 5 (much later) | Managed runtime/process ownership, packaging, licensing notices | licensing gate (§11) closed first |

## 15. Go/no-go inputs the spike must produce

Measured: provider detection latency, plan/doctor wall time, fake-server
completion normalization correctness, error-category coverage; if a real
Colibrì server is available on the dev machine, one real
health+models+small-completion trace with timings (not required — no full
model download). Distinctions preserved in the report: technically runnable
≠ completes a prompt ≠ correct answer ≠ sourced answer ≠ interactive.
