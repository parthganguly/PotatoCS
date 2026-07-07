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
{
  const rows = readinessRows(inputs());
  assert.equal(rows.length, 8);
  for (const id of ["app_shell", "backend", "ollama", "chat_model", "document_search", "lexical_fallback", "ocr"]) {
    assert.equal(rowById(rows, id).state, "ready", `${id} should be ready`);
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
  assert.equal(rowById(rows, "chat_model").state, "unavailable");
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
  assert.equal(hasCriticalReadinessGap(rows), true);
}

// Ollama present but no chat model: pull guidance, critical gap.
{
  const rows = readinessRows(inputs({ ollama: ollamaStatus({ models: [], conversation_models: [] }) }));
  const chatModel = rowById(rows, "chat_model");
  assert.equal(chatModel.state, "missing");
  assert.match(chatModel.explanation, /No chat model found/i);
  assert.match(chatModel.nextStep, /pull a small model/i);
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
  const withoutVision = readinessRows(inputs());
  assert.equal(rowById(withoutVision, "vision").state, "heavy");
  assert.match(rowById(withoutVision, "vision").explanation, /optional and may be slow on weak computers/i);
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
    for (const text of [item.label, item.stateLabel, item.explanation, item.nextStep]) {
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

// Checking: probing rows show the checking state.
{
  const rows = readinessRows(inputs({ checking: true }));
  for (const id of ["ollama", "chat_model", "document_search", "lexical_fallback", "ocr", "vision"]) {
    assert.equal(rowById(rows, id).state, "checking", `${id} should be checking`);
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
