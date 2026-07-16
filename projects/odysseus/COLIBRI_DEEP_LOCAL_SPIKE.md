# Colibrì Deep Local Integration Spike

Status: research branch only

Branch: `research/colibri-deep-local-spike`

Owner for implementation: Claude Code using the strongest available reasoning model (Fable)

## Mission

Determine, design, and minimally prove how PotatoCs can use Colibrì as an optional local inference backend for very large streamed Mixture-of-Experts models without weakening the existing ordinary-computer path.

This is a major research feature. Do not treat it as a quick endpoint swap and do not merge an unfinished experiment into `main`.

The intended product concept is:

- **Everyday Local**: current Ollama-backed small models for interactive chat, OCR, RAG, and ordinary machines.
- **Deep Local (experimental)**: Colibrì-backed, text-only, slow, high-value local jobs using very large open models on storage-rich machines.

The goal is not to make Colibrì the default runtime. The goal is to understand whether PotatoCs can give ordinary users a safe, inspectable way to use this emerging class of runtime when their hardware permits it.

## Authoritative upstream facts to verify before coding

Read the current Colibrì repository and do not rely on this brief as a substitute for upstream documentation:

- Repository: `https://github.com/JustVugg/colibri`
- License: Apache-2.0 for the Colibrì codebase; separately audit model-weight licenses and redistribution terms.
- Native Windows 11 support exists through MinGW-w64.
- `coli plan --json` reports a bounded Disk/RAM/VRAM placement plan without loading model tensors.
- `coli doctor --json` performs read-only readiness checks with stable check IDs and documented exit codes.
- `coli serve` exposes a text-only OpenAI-compatible local API.
- Implemented API includes `GET /health`, `GET /v1/models`, `POST /v1/chat/completions`, SSE streaming, usage counts, queue reporting, and optional reasoning controls.
- The reference GLM-5.2 int4 setup is roughly 370 GB on disk, around 20 GB peak RAM, and can be extremely slow under cold SSD streaming.
- The server executes one generation at a time unless using isolated KV slots; concurrent requests queue.
- Tools, images, audio, stop sequences, logprobs, and some penalties are intentionally unsupported and return explicit errors.

Record the exact upstream commit SHA used for the spike.

## PotatoCs facts to preserve

Read these files first:

- `README.md`
- `projects/odysseus/V04_POTATO_MODE_SCOPE.md`
- `projects/odysseus/V04_EXECUTION_PLAN.md`
- `projects/odysseus/V04_HARDWARE_RESOURCE_AUDIT.md`
- `python/odysseus_desktop_backend/services/model_service.py`
- `python/odysseus_desktop_backend/services/chat_service.py`
- `python/odysseus_desktop_backend/services/settings_service.py`
- `python/rpc_server.py`
- `src/tauri.ts`
- relevant model, chat, trace, privacy, and RPC tests

Current architecture:

- Tauri/Rust supervises a bundled Python sidecar.
- The UI communicates with the sidecar over JSON-RPC stdio.
- `ModelService` currently contains Ollama-specific detection, capabilities, chat, vision, HTTP, timeout, and error behavior.
- `ChatService` owns sessions, RAG prompting, evidence packets, verification, answer metadata, and traces.
- Existing user documents and chats have no automatic cloud fallback.
- v0.4 is focused on Potato Mode and first-run simplicity; this branch must not derail or silently expand that release scope.

## Required working method

Do not begin with a broad refactor.

Work in four explicit phases and commit each phase separately. Stop and write down blockers rather than hiding them.

### Phase 0 — Architecture and risk audit

Produce `projects/odysseus/COLIBRI_PROVIDER_RFC.md` before implementing the adapter.

The RFC must answer:

1. Every current `ModelService` call site and which methods are truly provider-neutral.
2. Whether a general provider abstraction is justified now, or whether a narrow Colibrì adapter is safer for the first spike.
3. How existing session model strings will identify both provider and model without breaking old profiles.
4. Whether Colibrì belongs in synchronous chat at all, given responses may take minutes or hours.
5. Whether the feature needs a persisted background-job subsystem rather than extending `chat.send` timeouts.
6. How cancellation can be represented honestly when an HTTP connection can close while the model process may continue generating.
7. How Colibrì process ownership should work:
   - user-managed external server for the first spike;
   - PotatoCs-managed process only in a later phase;
   - never assume a 370 GB model is bundled.
8. How API keys are handled without leaking into logs, traces, diagnostics, or support bundles.
9. How `coli plan --json` and `coli doctor --json` map to PotatoCs readiness language.
10. How to test everything without downloading the full model.
11. What changes are required for Windows packaging, code signing, MinGW, CUDA DLLs, and licensing if management is added later.
12. Which parts are reusable for future backends such as llama.cpp servers, MLX, or NPU runtimes.

The RFC must include:

- proposed component diagram;
- data-flow diagram for a Deep Local RAG job;
- proposed RPC contracts;
- proposed database changes and migration strategy, if any;
- error taxonomy;
- privacy threat model;
- cancellation and restart semantics;
- staged delivery plan;
- explicit reasons to reject at least two tempting but bad approaches.

Bad approaches that must be considered and probably rejected:

- calling Colibrì directly from React/Tauri;
- replacing Ollama globally;
- adding a 30-minute timeout to ordinary `chat.send`;
- downloading GLM-5.2 from the PotatoCs installer;
- reporting a closed HTTP connection as successful cancellation;
- exposing raw Colibrì JSON/errors to nontechnical users;
- mixing Colibrì work into the v0.4 acceptance gate.

### Phase 1 — Provider seam with zero behavior change

Only after the RFC is complete, create the smallest provider seam that preserves current Ollama behavior.

Preferred direction, subject to the RFC:

- Keep `ModelService` as the facade used by `ChatService` and other services.
- Extract provider-specific transport and detection behind a typed internal interface.
- Implement `OllamaProvider` first by moving existing behavior without changing public results.
- Add normalized provider result types for:
  - status;
  - model listing;
  - model capabilities;
  - chat result;
  - usage/timing metadata;
  - provider errors.
- Preserve existing RPC methods such as `models.detect_ollama` for compatibility.
- Do not rename database fields or session models unless the migration is clearly justified and tested.

Required proof:

- existing Python tests pass unchanged where possible;
- new contract tests prove the facade returns the same Ollama-shaped behavior as before;
- no UI behavior changes;
- no new network paths;
- no cloud fallback;
- operation traces still exclude prompts, documents, paths, and secrets.

### Phase 2 — Colibrì external-server adapter

Implement a text-only provider against a user-managed Colibrì server.

Initial scope:

- configurable loopback endpoint, default `http://127.0.0.1:8000`;
- optional API key sourced from an environment variable for the spike;
- `GET /health` detection;
- `GET /v1/models` listing;
- non-streaming `POST /v1/chat/completions` first;
- map `temperature`, `top_p`, and output-token limits;
- map PotatoCs thinking modes to Colibrì `reasoning_effort` or `enable_thinking` only when supported;
- normalize usage, elapsed time, queue wait, provider/model identity, and warnings;
- explicit errors for unsupported vision, tool, and multimodal requests;
- explicit handling for HTTP 429 queue saturation/timeouts;
- configurable long-job timeout separate from ordinary interactive chat;
- no Colibrì process spawning yet;
- no model download or conversion flow;
- no installer changes.

Use a fake local OpenAI-compatible test server to cover:

- health success/failure;
- models listing;
- normal completion;
- reasoning content or thinking metadata;
- malformed response;
- empty response;
- 401/403 authentication failure;
- 404 wrong endpoint;
- 429 queue saturation and retry guidance;
- connection refusal;
- timeout;
- secret redaction;
- unsupported multimodal calls.

Do not require the 370 GB model for automated tests.

### Phase 3 — Deep Local job spike

Do not route Colibrì through normal interactive chat unless Phase 0 proves it is safe.

Preferred product shape:

1. User selects one or more local Sources.
2. PotatoCs performs existing OCR, indexing, and retrieval locally.
3. PotatoCs builds a compact evidence packet with source identifiers and bounded context.
4. User explicitly starts a **Deep Local** job.
5. The job is persisted before inference begins.
6. Colibrì performs slow text-only synthesis.
7. PotatoCs stores result, sources, timings, provider identity, queue wait, and warnings.
8. Restarted PotatoCs marks interrupted work honestly and offers retry.

Design a minimal persisted job model with states such as:

- `queued`
- `checking_runtime`
- `waiting_for_provider`
- `running`
- `completed`
- `failed`
- `cancel_requested`
- `cancelled_before_start`
- `interrupted`

Do not claim hard cancellation after generation has started unless it is actually guaranteed by Colibrì.

Possible RPC surface, subject to the RFC:

- `deep_local.providers`
- `deep_local.plan`
- `deep_local.doctor`
- `deep_local.submit`
- `deep_local.get`
- `deep_local.list`
- `deep_local.cancel`
- `deep_local.retry`

The first UI may be developer-only or hidden behind an experimental flag. A normal v0.4 user must not see a confusing 370 GB setup path.

## Plan/Doctor integration

A major value of Colibrì is not only inference; it is its machine-readable readiness system.

For the research spike, add a safe subprocess wrapper that can invoke a user-configured Colibrì CLI path:

- `coli plan --json`
- `coli doctor --json`

Requirements:

- argument arrays only; never shell-string interpolation;
- configured executable and model paths validated and canonicalized;
- bounded subprocess timeout;
- stdout parsed as JSON;
- stderr retained only in privacy-safe developer logs;
- API keys never passed on a command line;
- no tensor loading during readiness checks;
- exit codes mapped exactly according to upstream documentation;
- unknown JSON versions fail safely with an actionable compatibility message.

Translate output into plain language, for example:

- storage required and storage available;
- estimated RAM peak and safe reserve;
- selected VRAM tier;
- engine/CUDA availability;
- model directory validity;
- overall state: runnable, runnable but extremely slow, unsafe, or incompatible.

Never convert a warning into a green claim that the user will receive interactive performance.

## UX principles

Use the label **Deep Local (experimental)**.

The UX must communicate:

- it is optional;
- it is text-only initially;
- it may need hundreds of gigabytes of SSD storage;
- it may take minutes or hours;
- it is not a replacement for Everyday Local chat;
- cold SSD streaming can be extremely slow and thermally demanding;
- the model is not included with PotatoCs;
- no download starts without explicit confirmation in any later implementation;
- no cloud fallback occurs.

Suggested capability levels:

- **Everyday Local** — small resident model, interactive.
- **Deep Local available** — Colibrì detected and model passes doctor.
- **Deep Local possible but constrained** — runnable with severe latency or storage warnings.
- **Deep Local unavailable** — missing runtime/model or unsafe plan.

## Privacy and security requirements

- Default endpoint must remain loopback.
- Warn before accepting a non-loopback endpoint.
- Do not log or trace API keys.
- Do not include raw prompts, RAG context, retrieved text, documents, paths, or model directories in support bundles.
- Do not silently contact Hugging Face or any model registry.
- Do not add telemetry.
- Treat a Colibrì server as a local privileged service, not as a trusted cloud API.
- Validate response sizes and content types.
- Bound queueing and background worker resources.
- Preserve current no-replay rules for non-idempotent RPC operations.

## Licensing and distribution gate

Before any packaging or automated model acquisition:

1. Confirm Colibrì code license and required notices.
2. Confirm GLM model-weight license, permitted uses, and redistribution constraints.
3. Confirm licenses for any pre-converted community mirror.
4. Confirm whether PotatoCs may link, bundle, invoke, or merely detect the runtime.
5. Add notices only after the audit is complete.

The spike should prefer detecting a user-provided installation. Do not vendor Colibrì yet.

## Performance and truthfulness

Collect separate metrics for:

- plan/doctor duration;
- server detection;
- server cold start if later managed;
- request queue wait;
- prefill latency;
- first-token latency when streaming is added;
- total generation time;
- completion tokens;
- tokens per second;
- peak PotatoCs sidecar memory;
- Colibrì process memory;
- disk throughput and thermal warning where available;
- evidence retrieval time;
- total Deep Local job time.

Keep these concepts distinct:

- technically runnable;
- completes a test prompt;
- produces a correct answer;
- produces a sourced answer;
- is usable interactively.

A sourced answer is not automatically correct.

## Acceptance criteria for the research spike

The branch is successful when all of the following are true:

1. `COLIBRI_PROVIDER_RFC.md` maps the current architecture and makes a clear go/no-go recommendation.
2. Existing Ollama behavior remains green behind the provider seam.
3. A fake Colibrì server passes provider contract tests.
4. PotatoCs can detect a real or mocked Colibrì server and list its model.
5. PotatoCs can send one text-only completion through the adapter and normalize the result.
6. Colibrì failures are categorized and shown without raw jargon or secret leakage.
7. `coli plan --json` and `coli doctor --json` are wrapped and parsed through tests using fixtures or a tiny upstream test model.
8. No full GLM download is required for tests.
9. No installer, release asset, or main-branch behavior changes.
10. The final spike report states whether the next step should be:
    - stop;
    - keep as a developer adapter;
    - build the persisted Deep Local job system;
    - pursue managed runtime setup later.

## Required final report

Create `projects/odysseus/COLIBRI_SPIKE_RESULT.md` containing:

- upstream Colibrì commit tested;
- files changed;
- architectural decision;
- tests run and exact results;
- real hardware used;
- whether a real Colibrì model was used;
- measured latency/resource data;
- known limitations;
- privacy and licensing status;
- merge recommendation;
- next smallest responsible task.

## Commands to run before declaring success

At minimum:

```powershell
python -m pytest python\tests
npm run test:progress
npm run build:frontend
cargo check --manifest-path src-tauri\Cargo.toml
```

Add focused provider, fake-server, plan/doctor, migration, privacy, and interrupted-job tests as the implementation requires.

## Instructions to Claude Code / Fable

You are the senior systems architect and implementer for this spike.

Do not merely agree with the proposed design. Audit it aggressively. Read both codebases and correct this brief wherever upstream reality or PotatoCs architecture contradicts it.

Prioritize:

1. correctness;
2. honest resource behavior;
3. preservation of the ordinary Ollama path;
4. testability without a giant model;
5. privacy;
6. graceful failure;
7. the smallest vertical proof.

Do not optimize for maximum code produced. Optimize for a trustworthy architectural decision and a minimal end-to-end proof.

Begin by writing the RFC. Do not implement the provider until the RFC has mapped every relevant call site and identified the synchronous-chat versus persisted-job decision.