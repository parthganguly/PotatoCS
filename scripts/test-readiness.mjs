// Contract tests for the first-run readiness row mapping. Follows the same
// pattern as test-backend-status.mjs: the mapper is a pure module, so these
// tests cover the status->row contract without a DOM test runner.
import assert from "node:assert/strict";
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

function inputs(overrides = {}) {
  return {
    checking: false,
    backendDegraded: false,
    ollama: ollamaStatus(),
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

console.log("readiness row mapping tests passed");
