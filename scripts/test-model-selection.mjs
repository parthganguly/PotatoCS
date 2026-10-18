import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/api/models.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const models = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString("base64")}`);

const QWEN = "qwen3:4b-instruct-2507-q4_K_M";

// Ordinary launch keeps its fallback to the first installed model.
assert.equal(models.selectInstalledModelOrFallback("llama3.2", [QWEN, "mistral:latest"]), QWEN);
assert.equal(models.selectInstalledModelOrFallback("llama3.2", ["llama3.2:latest", QWEN]), "llama3.2:latest");
assert.equal(models.selectInstalledModelOrFallback("llama3.2", []), "llama3.2");

// Private runtime: nothing is substituted, even when the candidate is the only choice.
assert.equal(models.selectInstalledModelOrFallback("llama3.2", [QWEN], true), "llama3.2");
assert.equal(models.selectInstalledModelOrFallback("", [QWEN], true), "");
assert.equal(models.selectInstalledModelOrFallback("llama3.2", ["llama3.2:latest", QWEN], true), "llama3.2:latest");

const runtime = {
  endpoint: "http://127.0.0.1:54380",
  experimental_model: QWEN,
  expected_digest: "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0",
  installed_digest: "0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0",
  state: "ready"
};
assert.equal(models.isExperimentalModel(QWEN, runtime), true);
assert.equal(models.isExperimentalModel("QWEN3:4b-instruct-2507-q4_K_M", runtime), true);
assert.equal(models.isExperimentalModel(QWEN, null), false);
assert.equal(models.isExperimentalModel("llama3.2", runtime), false);

assert.equal(
  models.privateRuntimeLabel(runtime, "llama3.2"),
  `Private runtime http://127.0.0.1:54380: choose ${QWEN} in this conversation to use the experimental model.`
);
assert.equal(
  models.privateRuntimeLabel(runtime, QWEN),
  `Experimental model ${QWEN} · expected digest 0edcdef34593… · installed digest matches · http://127.0.0.1:54380`
);
assert.match(models.privateRuntimeLabel({ ...runtime, state: "missing", installed_digest: "" }, QWEN), /not installed in the private runtime; sending will fail and no other model is used/);
assert.match(
  models.privateRuntimeLabel({ ...runtime, state: "digest_mismatch", installed_digest: "ffff0000aaaa1111" }, QWEN),
  /installed digest ffff0000aaaa… does not match; sending will fail/
);
assert.match(models.privateRuntimeLabel({ ...runtime, state: "unreachable" }, QWEN), /private runtime not reachable/);

// Every App selection path passes the private-runtime flag; no local fallback copy remains.
const app = readFileSync("src/App.tsx", "utf8");
// The whole call `name(...)` starting at `at`, by balancing parentheses.
function callAt(at, name) {
  let depth = 0;
  let end = at + name.length;
  do {
    if (app[end] === "(") depth += 1;
    if (app[end] === ")") depth -= 1;
    end += 1;
  } while (depth > 0);
  return app.slice(at, end);
}
const calls = [];
for (let at = app.indexOf("selectInstalledModelOrFallback("); at !== -1; at = app.indexOf("selectInstalledModelOrFallback(", at + 1)) {
  calls.push(callAt(at, "selectInstalledModelOrFallback"));
}
assert.equal(calls.length, 5);
for (const call of calls) assert.match(call, /private_runtime/, call);
assert.doesNotMatch(app, /function selectInstalledModelOrFallback/);
assert.doesNotMatch(app, /11434/);
assert.match(app, /Ollama is not reachable at \{props\.diagnostics\?\.ollama\.endpoint\}/);

// Diagnostics never picks a benchmark model in the private runtime (the actual updater is run);
// an explicit choice stays, and the ordinary launch keeps its first-installed default.
const updaterCall = callAt(app.indexOf("setBenchmarkModel((current) =>"), "setBenchmarkModel");
const benchmarkUpdater = (ollama) =>
  new Function("nextDiagnostics", `return (${updaterCall.slice("setBenchmarkModel(".length, -1)});`)({ ollama });
const privateDiagnostics = { models: [QWEN, "nomic-embed-text:latest"], private_runtime: { state: "ready" } };
assert.equal(benchmarkUpdater(privateDiagnostics)(""), "");
assert.equal(benchmarkUpdater(privateDiagnostics)("gone:latest"), "");
assert.equal(benchmarkUpdater(privateDiagnostics)(QWEN), QWEN);
assert.equal(benchmarkUpdater({ models: [QWEN, "llama3.2:latest"] })(""), QWEN);
assert.match(app, /Choose a model \(private runtime: none is selected automatically\)/);

// The header shows the candidate's identity and experimental status.
const header = readFileSync("src/features/chat/ChatHeader.tsx", "utf8");
assert.match(header, / · experimental/);
assert.match(header, /privateRuntimeLabel\(props\.privateRuntime, activeModel\)/);
assert.match(header, /data-testid="private-runtime"/);

console.log("model selection checks passed");
