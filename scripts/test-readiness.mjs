// Contract tests for the first-run readiness row mapping. Follows the same
// pattern as test-backend-status.mjs: the mapper is a pure module, so these
// tests cover the status->row contract without a DOM test runner.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/readiness/readinessRows.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const { readinessRows, hasCriticalReadinessGap, firstRunReadinessNeeded } = module;

function ollamaStatus(overrides = {}) {
  return {
    name: "ollama",
    installed: true,
    reachable: true,
    endpoint: "http://127.0.0.1:11434",
    version: "0.5.0",
    models: ["llama3.2:latest"],
    conversation_models: [
      {
        tag: "llama3.2",
        canonical_tag: "llama3.2",
        display_name: "Llama 3.2",
        role: "chat",
        installed: true,
        stale: false,
        exact_tags: ["llama3.2:latest"],
        tooltip: ""
      }
    ],
    error: "",
    updated_at: 0,
    ...overrides
  };
}

function ragHealth(overrides = {}) {
  return {
    ok: true,
    embedding: {
      backend: "semantic",
      provider: "ollama",
      model: "nomic-embed-text",
      cache_key: "ollama:nomic-embed-text",
      semantic: true,
      dimensions: 768,
      message: "Semantic retrieval active: nomic-embed-text",
      ...(overrides.embedding ?? {})
    }
  };
}

function ocrStatus(overrides = {}) {
  return {
    available: true,
    engine_name: "tesseract",
    renderer: "pdftoppm",
    message: "OCR is available.",
    dependencies: {},
    ...overrides
  };
}

function capability(model, overrides = {}) {
  return {
    model,
    digest: "sha256:test",
    size: 0,
    family: "llama",
    parameter_size: "3B",
    quantization_level: "Q4_K_M",
    context_length: 8192,
    capabilities: ["completion"],
    text_generation: "yes",
    vision: "no",
    embedding: "no",
    tools: "unknown",
    thinking: "unknown",
    raw: {},
    inspected_at: 0,
    error: "",
    ...overrides
  };
}

function inputs(overrides = {}) {
  return {
    checking: false,
    backendDegraded: false,
    ollama: ollamaStatus(),
    modelCapabilities: [],
    ragHealth: ragHealth(),
    ocrStatus: ocrStatus(),
    ...overrides
  };
}

function rowById(rows, id) {
  const found = rows.find((item) => item.id === id);
  assert.ok(found, `expected a row with id ${id}`);
  return found;
}

// All ready: every row is ready except vision, which is always optional/heavy.
// Ready rows carry no setup guidance.
{
  const rows = readinessRows(inputs());
  assert.equal(rows.length, 8);
  for (const id of ["app_shell", "backend", "ollama", "chat_model", "document_search", "lexical_fallback", "ocr"]) {
    const item = rowById(rows, id);
    assert.equal(item.state, "ready", `${id} should be ready`);
    assert.equal(item.guidance, undefined, `${id} must not carry guidance when ready`);
  }
  assert.equal(rowById(rows, "vision").state, "heavy");
  assert.equal(hasCriticalReadinessGap(rows), false);
}

// Ollama missing entirely: runtime row says install + re-check, chat model is
// gated on the runtime, and the gap is critical.
{
  const rows = readinessRows(
    inputs({ ollama: ollamaStatus({ installed: false, reachable: false, models: [], conversation_models: [] }) })
  );
  const runtime = rowById(rows, "ollama");
  assert.equal(runtime.state, "missing");
  assert.match(runtime.explanation, /runtime not found/i);
  assert.match(runtime.nextStep, /Install Ollama.*Re-check/i);
  assert.ok(runtime.guidance, "missing runtime must carry install guidance");
  assert.match(runtime.guidance.steps.join(" "), /download ollama/i);
  assert.equal(runtime.guidance.command.text, "https://ollama.com/download");
  const gated = rowById(rows, "chat_model");
  assert.equal(gated.state, "unavailable");
  assert.equal(gated.guidance, undefined, "runtime-gated chat row must not show pull guidance");
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Ollama installed but not running: distinct copy, still a critical gap.
{
  const rows = readinessRows(
    inputs({ ollama: ollamaStatus({ installed: true, reachable: false, models: [], conversation_models: [] }) })
  );
  const runtime = rowById(rows, "ollama");
  assert.equal(runtime.state, "missing");
  assert.match(runtime.nextStep, /Start Ollama/i);
  assert.ok(runtime.guidance, "installed-not-running must carry start guidance");
  assert.equal(runtime.guidance.command.text, "ollama serve");
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Ollama present but no chat model: pull guidance, critical gap.
{
  const rows = readinessRows(inputs({ ollama: ollamaStatus({ models: [], conversation_models: [] }) }));
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "missing");
  assert.match(chatModel.explanation, /No chat model found/i);
  assert.match(chatModel.nextStep, /pull a small model/i);
  // Guidance carries the approved v0.4 default command
  // (V04_MODEL_RECOMMENDATION_DECISION.md §3): "try this first", not
  // "recommended", plus a words-only mention of the smaller 1b alternative
  // for very weak machines with no second copy button.
  assert.ok(chatModel.guidance, "missing chat model must carry guidance");
  assert.equal(chatModel.guidance.command.text, "ollama pull llama3.2:3b");
  assert.match(chatModel.guidance.steps.join(" "), /try this first/i);
  assert.doesNotMatch(chatModel.guidance.steps.join(" "), /recommended/i);
  assert.match(chatModel.guidance.steps.join(" "), /llama3\.2:1b/);
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Embedding missing, lexical fallback active: search is degraded but honest,
// fallback row is ready, and nothing critical blocks the user.
{
  const rows = readinessRows(
    inputs({
      ragHealth: ragHealth({
        embedding: { backend: "lexical", semantic: false, model: "", message: "Lexical fallback active." }
      })
    })
  );
  const search = rowById(rows, "document_search");
  assert.equal(search.state, "degraded");
  assert.match(search.explanation, /basic keyword search.*embedding model/i);
  // The embedding command is the one approved model name: the backend's own
  // wired default (embedding_service.py DEFAULT_EMBEDDING_MODEL).
  assert.ok(search.guidance, "degraded search must carry embedding guidance");
  assert.equal(search.guidance.command.text, "ollama pull nomic-embed-text");
  assert.match(search.guidance.steps.join(" "), /optional/i);
  const fallback = rowById(rows, "lexical_fallback");
  assert.equal(fallback.state, "ready");
  assert.match(fallback.explanation, /keyword matching/i);
  assert.equal(hasCriticalReadinessGap(rows), false);
}

// OCR unavailable: plain copy, not a critical gap.
{
  const rows = readinessRows(inputs({ ocrStatus: ocrStatus({ available: false, message: "not available" }) }));
  const ocr = rowById(rows, "ocr");
  assert.equal(ocr.state, "unavailable");
  assert.match(ocr.explanation, /Tesseract or a PDF renderer/i);
  // Basic pointer only — no OCR install wizard and no command yet.
  assert.ok(ocr.guidance, "unavailable OCR must carry basic guidance");
  assert.equal(ocr.guidance.command, undefined, "no OCR install command yet");
  assert.equal(hasCriticalReadinessGap(rows), false);
}

// Backend degraded: error row that defers to the existing banner; critical.
{
  const rows = readinessRows(inputs({ backendDegraded: true }));
  const backend = rowById(rows, "backend");
  assert.equal(backend.state, "error");
  assert.match(backend.nextStep, /Retry backend/);
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Vision is always optional/heavy: with or without a vision model it never
// becomes a blocking state, and the copy warns about weak computers.
{
  const withVision = readinessRows(
    inputs({
      ollama: ollamaStatus({
        conversation_models: [
          { tag: "llama3.2", canonical_tag: "llama3.2", display_name: "", role: "chat", installed: true, stale: false, exact_tags: [], tooltip: "" },
          { tag: "qwen3-vl", canonical_tag: "qwen3-vl", display_name: "", role: "vision", installed: true, stale: false, exact_tags: [], tooltip: "" }
        ]
      })
    })
  );
  assert.equal(rowById(withVision, "vision").state, "heavy");
  assert.equal(rowById(withVision, "vision").guidance, undefined, "installed vision model needs no guidance");
  const withoutVision = readinessRows(inputs());
  const visionRow = rowById(withoutVision, "vision");
  assert.equal(visionRow.state, "heavy");
  assert.match(visionRow.explanation, /optional and may be slow on weak computers/i);
  // Guidance labels vision as optional/heavy and never offers a pull command.
  assert.ok(visionRow.guidance, "vision without a model carries optional/heavy guidance");
  assert.equal(visionRow.guidance.command, undefined, "vision guidance must never offer a download command");
  assert.match(visionRow.guidance.steps.join(" "), /optional and heavy/i);
  assert.equal(hasCriticalReadinessGap(withVision), false);
}

// Privacy: raw error strings, paths and payload fragments from the status
// objects must never surface in any row field.
{
  const hostile = 'Traceback (most recent call last): C:\\Users\\secret\\file.py {"jsonrpc": "2.0"} /etc/passwd';
  const rows = readinessRows(
    inputs({
      ollama: ollamaStatus({ reachable: true, error: hostile, version: hostile }),
      ragHealth: ragHealth({ embedding: { semantic: false, message: hostile, model: hostile } }),
      ocrStatus: ocrStatus({ available: false, message: hostile })
    })
  );
  for (const item of rows) {
    const guidanceTexts = item.guidance
      ? [...item.guidance.steps, ...(item.guidance.command ? [item.guidance.command.label, item.guidance.command.text] : [])]
      : [];
    for (const text of [item.label, item.stateLabel, item.explanation, item.nextStep, ...guidanceTexts]) {
      for (const fragment of ["Traceback", "C:", "\\", "{", "}", "jsonrpc", "/etc/passwd"]) {
        assert.equal(
          text.includes(fragment),
          false,
          `row ${item.id} copy must not contain "${fragment}"`
        );
      }
    }
  }
}

// Null statuses (fetch failed): rows report a fixed error state, never crash.
{
  const rows = readinessRows(inputs({ ollama: null, ragHealth: null, ocrStatus: null }));
  assert.equal(rowById(rows, "ollama").state, "error");
  assert.equal(rowById(rows, "document_search").state, "error");
  assert.equal(rowById(rows, "ocr").state, "error");
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Checking: probing rows show the checking state and no guidance.
{
  const rows = readinessRows(inputs({ checking: true }));
  for (const id of ["ollama", "chat_model", "document_search", "lexical_fallback", "ocr", "vision"]) {
    const item = rowById(rows, id);
    assert.equal(item.state, "checking", `${id} should be checking`);
    assert.equal(item.guidance, undefined, `${id} must not show guidance while checking`);
  }
}

// First-run trigger: fresh profile always shows readiness, even when all is
// ready; an established profile shows it only for critical gaps.
{
  const allReady = readinessRows(inputs());
  assert.equal(firstRunReadinessNeeded({ sessionCount: 0, userDocumentCount: 0, rows: allReady }), true);
  assert.equal(firstRunReadinessNeeded({ sessionCount: 3, userDocumentCount: 2, rows: allReady }), false);
  const noRuntime = readinessRows(
    inputs({ ollama: ollamaStatus({ installed: false, reachable: false, models: [], conversation_models: [] }) })
  );
  assert.equal(firstRunReadinessNeeded({ sessionCount: 3, userDocumentCount: 2, rows: noRuntime }), true);
  const onlyOptionalMissing = readinessRows(inputs({ ocrStatus: ocrStatus({ available: false }) }));
  assert.equal(
    firstRunReadinessNeeded({ sessionCount: 3, userDocumentCount: 2, rows: onlyOptionalMissing }),
    false,
    "optional gaps alone must not force the readiness view"
  );
}

// ---------- Chat catalog classification (capability hand-off) fixtures ----------

function catalogItem(tag, role, overrides = {}) {
  return {
    tag,
    canonical_tag: tag,
    display_name: "",
    role,
    installed: true,
    stale: false,
    exact_tags: [tag],
    tooltip: "",
    ...overrides
  };
}

// text_generation defaults to "yes" on capability(): the only affirmative chat signal.
function embedCapability(model) {
  return capability(model, {
    capabilities: ["embedding"],
    text_generation: "no",
    embedding: "yes",
    family: "nomic-bert"
  });
}

function failedCapability(model) {
  return capability(model, {
    capabilities: [],
    text_generation: "error",
    vision: "error",
    embedding: "error",
    tools: "error",
    thinking: "error",
    error: "inspection failed"
  });
}

// Baseline first-run shape: detect_ollama's catalog was built before capability
// inspection finished, so both installed Llama tags are role "unknown". The
// capabilities call then confirms text generation. Readiness must use the
// confirmed capabilities and report Ready instead of "No chat model found".
{
  const ollama = ollamaStatus({
    models: ["llama3.2:3b", "llama3.2:latest", "nomic-embed-text:latest"],
    conversation_models: [
      catalogItem("llama3.2:3b", "unknown", { exact_tags: ["llama3.2:3b"] }),
      catalogItem("llama3.2:latest", "unknown", { exact_tags: ["llama3.2:latest"] })
    ]
  });
  const rows = readinessRows(
    inputs({
      ollama,
      modelCapabilities: [
        capability("llama3.2:3b"),
        capability("llama3.2:latest"),
        embedCapability("nomic-embed-text:latest")
      ]
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "ready");
  assert.equal(chatModel.guidance, undefined);
  assert.equal(hasCriticalReadinessGap(rows), false);
}

// Unknown capabilities are neither confirmed usable nor confirmed absent: no
// Ready, no "No chat model found", and no download guidance.
{
  const rows = readinessRows(
    inputs({ ollama: ollamaStatus({ models: ["mystery:latest"], conversation_models: [] }) })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "degraded");
  assert.doesNotMatch(chatModel.explanation, /No chat model found/i);
  assert.match(chatModel.nextStep, /Re-check/i);
  assert.equal(chatModel.guidance, undefined, "unconfirmed discovery must not encourage a download");
  assert.equal(hasCriticalReadinessGap(rows), false);
}

// Capability inspection errors are also unconfirmed, not absence.
{
  const rows = readinessRows(
    inputs({
      ollama: ollamaStatus({ models: ["mystery:latest"], conversation_models: [] }),
      modelCapabilities: [failedCapability("mystery:latest")]
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "degraded");
  assert.equal(chatModel.guidance, undefined);
}

// A reachable runtime whose /api/tags call failed is an inventory read failure,
// not an empty inventory: no pull guidance, no empty claim.
{
  const ollama = ollamaStatus({
    reachable: true,
    error: "connection reset while reading tags",
    models: [],
    conversation_models: []
  });
  const rows = readinessRows(inputs({ ollama }));
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "error");
  assert.match(chatModel.explanation, /could not read the installed model list/i);
  assert.doesNotMatch(chatModel.explanation, /No chat model found/i);
  assert.equal(chatModel.guidance, undefined);
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Empty inventory and incompatible inventory both stay actionable, but the
// incompatibility is never described as "nothing installed".
{
  const empty = readinessRows(inputs({ ollama: ollamaStatus({ models: [], conversation_models: [] }) }));
  const emptyChat = rowById(empty, "chat_model");
  assert.match(emptyChat.explanation, /No chat model found/i);
  assert.equal(emptyChat.guidance.command.text, "ollama pull llama3.2:3b");

  const embedderOnly = readinessRows(
    inputs({
      ollama: ollamaStatus({ models: ["nomic-embed-text:latest"], conversation_models: [] }),
      modelCapabilities: [embedCapability("nomic-embed-text:latest")]
    })
  );
  const incompatible = rowById(embedderOnly, "chat_model");
  assert.equal(incompatible.state, "missing");
  assert.match(incompatible.explanation, /cannot answer chat questions/i);
  assert.doesNotMatch(incompatible.explanation, /No chat model found/i);
  assert.equal(incompatible.guidance.command.text, "ollama pull llama3.2:3b");
}

// Stale or uninstalled catalog entries never satisfy the legacy "installed
// models" fallback: they cannot make an empty runtime look stocked.
{
  const rows = readinessRows(
    inputs({
      ollama: ollamaStatus({
        models: [],
        conversation_models: [
          catalogItem("llama3.2:3b", "chat", { installed: false }),
          catalogItem("qwen3-vl", "vision", { stale: true })
        ]
      })
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "missing");
  assert.match(chatModel.explanation, /No chat model found/i, "stale or uninstalled entries must not count");
}

// A vision+text model is selectable for chat when capabilities confirm both;
// the vision row confirmation stays on the detect catalog's role, as before
// (baseline semantics restored — no capability-based vision change here).
{
  const ollama = ollamaStatus({
    models: ["qwen3-vl:latest"],
    conversation_models: [catalogItem("qwen3-vl:latest", "vision")]
  });
  const rows = readinessRows(
    inputs({
      ollama,
      modelCapabilities: [capability("qwen3-vl:latest", { vision: "yes", capabilities: ["completion", "vision"] })]
    })
  );
  assert.equal(rowById(rows, "chat_model").state, "ready");
  assert.equal(rowById(rows, "vision").state, "heavy");
  assert.equal(rowById(rows, "vision").guidance, undefined, "a confirmed vision model needs no guidance");
}

// Manual refresh semantics: the same installed tag re-inspected with changed
// capabilities (replacement under the same tag), or recovering from a
// transient inspection error, changes the row.
{
  const ollama = ollamaStatus({ models: ["llama3.2:3b"], conversation_models: [] });
  const ready = readinessRows(inputs({ ollama, modelCapabilities: [capability("llama3.2:3b")] }));
  assert.equal(rowById(ready, "chat_model").state, "ready");

  const replaced = readinessRows(
    inputs({
      ollama,
      modelCapabilities: [embedCapability("llama3.2:3b")]
    })
  );
  assert.equal(rowById(replaced, "chat_model").state, "missing");

  const transient = readinessRows(
    inputs({ ollama, modelCapabilities: [failedCapability("llama3.2:3b")] })
  );
  assert.equal(rowById(transient, "chat_model").state, "degraded");
  const recovered = readinessRows(inputs({ ollama, modelCapabilities: [capability("llama3.2:3b")] }));
  assert.equal(rowById(recovered, "chat_model").state, "ready");
}

// ---------- Private runtime fixtures ----------
// The private_runtime state describes only the pinned experimental candidate.
// Ordinary model availability is expressed by the runtime's own installed tags
// and their capabilities, and readiness must treat those independently:

const QWEN = "qwen3:4b-instruct-2507-q4_K_M";
const PRIVATE_RUNTIME_BASE = {
  endpoint: "http://127.0.0.1:54380",
  experimental_model: QWEN,
  expected_digest: "expected",
  installed_digest: "expected",
  state: "ready"
};

function privateOllama(runtimeOverrides = {}, ollamaOverrides = {}) {
  return {
    ...ollamaStatus({
      endpoint: PRIVATE_RUNTIME_BASE.endpoint,
      models: [],
      conversation_models: [],
      ...ollamaOverrides
    }),
    private_runtime: { ...PRIVATE_RUNTIME_BASE, ...runtimeOverrides }
  };
}

// Digest-verified and capability-confirmed: Ready.
{
  const rows = readinessRows(
    inputs({ ollama: privateOllama({}, { models: [QWEN] }), modelCapabilities: [capability(QWEN)] })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "ready");
  assert.equal(chatModel.guidance, undefined);
}

// Digest-ready but no capability confirmation: not counted as ready. The
// existing explicit per-conversation selection in the model picker is named.
{
  const rows = readinessRows(inputs({ ollama: privateOllama({}, { models: [QWEN] }) }));
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "degraded");
  assert.match(chatModel.explanation, /matches its expected version/i);
  assert.doesNotMatch(chatModel.explanation, /No chat model found/i);
  assert.match(chatModel.nextStep, /model picker/i);
  assert.equal(chatModel.guidance, undefined);
}

// Experiment absent, no models installed: generic private-runtime chat setup.
// No ambient command, and nothing implies the experimental model is required.
{
  const rows = readinessRows(inputs({ ollama: privateOllama({ state: "missing", installed_digest: "" }) }));
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "missing");
  assert.match(chatModel.explanation, /private runtime.*has no chat model/i);
  assert.doesNotMatch(chatModel.explanation, /experimental/i);
  assert.ok(chatModel.guidance, "private missing must carry setup guidance");
  assert.equal(chatModel.guidance.command, undefined, "private missing must not offer `ollama pull`");
  assert.doesNotMatch(chatModel.guidance.steps.join(" "), /ollama pull|experimental/i);
}

// Real desktop shape: experiment still absent and an embedding model is the
// only installed tag. Same generic setup; the experimental model is not the
// communicated requirement.
{
  const rows = readinessRows(
    inputs({
      ollama: privateOllama({ state: "missing", installed_digest: "" }, { models: ["nomic-embed-text:latest"] }),
      modelCapabilities: [embedCapability("nomic-embed-text:latest")]
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "missing");
  assert.doesNotMatch(chatModel.explanation, /experimental|must be installed/i);
  assert.ok(chatModel.guidance);
  assert.equal(chatModel.guidance.command, undefined);
  assert.doesNotMatch(chatModel.guidance.steps.join(" "), /ollama pull|experimental/i);
}

// A non-experimental installed tag whose inspection failed stays unconfirmed
// even under a private runtime: re-check copy, no download, no experimental
// requirement.
{
  const rows = readinessRows(
    inputs({
      ollama: privateOllama({ state: "missing", installed_digest: "" }, { models: ["llama3.2:3b"] }),
      modelCapabilities: [failedCapability("llama3.2:3b")]
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "degraded");
  assert.doesNotMatch(chatModel.explanation, /experimental/i);
  assert.match(chatModel.nextStep, /Re-check/i);
  assert.equal(chatModel.guidance, undefined, "failed inspection must not encourage a download");
}

// The experimental candidate does not block ordinary models: a confirmed
// compatible chat tag counts normally even while the experiment is missing or
// mismatched.
{
  const missingButLlamaChat = readinessRows(
    inputs({
      ollama: privateOllama({ state: "missing", installed_digest: "" }, { models: ["llama3.2:latest"] }),
      modelCapabilities: [capability("llama3.2:latest")]
    })
  );
  assert.equal(rowById(missingButLlamaChat, "chat_model").state, "ready");

  const mismatchButLlamaChat = readinessRows(
    inputs({
      ollama: privateOllama({ state: "digest_mismatch", installed_digest: "other" }, { models: ["llama3.2:latest"] }),
      modelCapabilities: [capability("llama3.2:latest")]
    })
  );
  assert.equal(rowById(mismatchButLlamaChat, "chat_model").state, "ready");
}

// Digest mismatch with no other usable model: a distinct, honest error. No
// ambient command.
{
  const rows = readinessRows(
    inputs({
      ollama: privateOllama({ state: "digest_mismatch", installed_digest: "other" }, { models: [QWEN] }),
      modelCapabilities: [capability(QWEN)]
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "error");
  assert.match(chatModel.explanation, /does not match the expected version/i);
  assert.equal(chatModel.guidance.command, undefined);
  assert.doesNotMatch(chatModel.guidance.steps.join(" "), /ollama pull/i);
}

// Offline private runtime: chat is gated on the runtime, guidance has no
// ambient commands.
{
  const rows = readinessRows(
    inputs({
      ollama: privateOllama({ state: "unreachable", installed_digest: "" }, { reachable: false })
    })
  );
  assert.equal(rowById(rows, "ollama").guidance.command, undefined, "private runtime start guidance is copy-only");
  assert.doesNotMatch(rowById(rows, "ollama").guidance.steps.join(" "), /ollama serve/i);
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "unavailable");
  assert.match(chatModel.explanation, /private AI runtime/i);
  assert.equal(chatModel.guidance, undefined);
}

// TCP-reachable private runtime whose /api/tags read failed: the runtime state
// is "unreachable", but this is a check failure, not an offline runtime and not
// an empty inventory.
{
  const rows = readinessRows(
    inputs({
      ollama: privateOllama({ state: "unreachable", installed_digest: "" }, {
        reachable: true,
        error: "connection reset while reading tags"
      })
    })
  );
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "error");
  assert.match(chatModel.explanation, /could not read the installed model list/i);
  assert.doesNotMatch(chatModel.explanation, /No chat model found|not running|not reachable/i);
  assert.equal(chatModel.guidance, undefined);
}

// ---------- App wiring ----------

// App wiring: startup and re-check must pass the refreshed capability data into
// the same readiness mapping.
{
  const app = readFileSync("src/App.tsx", "utf8");
  const readinessCalls = app.match(/readinessRows\(\{[\s\S]*?\}\)/g) ?? [];
  assert.equal(readinessCalls.length, 2);
  for (const call of readinessCalls) assert.match(call, /modelCapabilities/);

  const recheck = app.slice(
    app.indexOf("async function recheckReadiness"),
    app.indexOf("async function refreshDocuments")
  );
  // Fresh inspection feeds detect_ollama before rows are recomputed.
  assert.match(recheck, /refreshModelCapabilities\(\)[\s\S]{0,200}models\.detect_ollama/);
  // Each status call fails on its own null instead of leaving old data.
  assert.match(recheck, /\.catch\(\(\) => null\)/);
  // A failed model check clears the model result so rows show fixed retry errors.
  assert.match(recheck, /setModelCapabilities\(\[\]\)/);
  assert.match(recheck, /setOllama\(null\)/);
  // A failed engine status must not keep the old engine-ready row.
  assert.match(recheck, /setBackendDegraded\(true\)/);

  // Boot and sidebar refresh re-inspect capabilities so a model replaced under
  // the same tag is reflected.
  assert.equal(app.match(/listModelCapabilities\(/g), null);
  assert.match(app, /refreshModelCapabilities\(\),[\s\S]{0,80}listImageEvalSuite/);
}

console.log("readiness row mapping tests passed");
