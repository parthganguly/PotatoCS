import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/chat/OperationTrace.tsx"],
  bundle: true,
  format: "esm",
  platform: "node",
  jsx: "automatic",
  write: false
});
const trace = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString("base64")}`);

function base(overrides = {}) {
  return {
    schema_version: 1,
    timing: {},
    models: {},
    pipeline: { rag_enabled: false, verifier_enabled: false },
    tokens: {},
    sources: { retrieved_chunk_ids: [], retrieved_document_ids: [], retrieved_page_numbers: [] },
    warnings: [],
    model_trace: { thinking_returned: false, thinking_char_count: 0, thinking_truncated: false },
    ...overrides
  };
}

// Legacy traces keep showing the answering model, or a dash when none was recorded.
assert.equal(trace.finalAnswerLabel(base({ models: { final_answer_model: "llama3.2:latest" } })), "llama3.2:latest");
assert.equal(trace.finalAnswerLabel(base()), "—");

// A local conversion never shows the session's selected model as its answerer.
const local = base({
  models: { final_answer_model: "llama3.2:latest" },
  capability: { name: "time_conversion", engine: "local_time_conversion", outcome: "converted", time_zone_data: "tzdata 2026.2" }
});
assert.equal(trace.finalAnswerLabel(local), "Local time conversion (no model)");
assert.deepEqual(trace.capabilityRows(local.capability), [
  ["Engine", "Local time conversion (no model)"],
  ["Outcome", "converted"],
  ["Time-zone data", "tzdata 2026.2"]
]);
assert.deepEqual(trace.capabilityRows({ name: "time_conversion", engine: "local_time_conversion", outcome: "clarified" })[2], [
  "Time-zone data",
  "—"
]);
assert.equal(trace.finalAnswerLabel(base({ capability: { name: "x", engine: "other_engine" } })), "Local engine: other_engine");

console.log("operation trace engine labels ok");
