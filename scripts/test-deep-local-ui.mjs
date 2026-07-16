// Contract tests for the Deep Local (experimental) view model. Same pattern
// as the jobs/readiness scripts: esbuild bundles the pure module and
// node:assert verifies the contract without a DOM test framework.
import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/deepLocal/deepLocalModel.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const {
  DEEP_LOCAL_DISCLAIMER,
  DEEP_LOCAL_POLL_INTERVAL_MS,
  deepLocalErrorCategoryCopy,
  deepLocalFailureCopy,
  deepLocalJobView,
  formatDeepLocalElapsed
} = module;

let assertionCount = 0;
function equal(actual, expected, message) {
  assertionCount += 1;
  assert.equal(actual, expected, message);
}
function ok(value, message) {
  assertionCount += 1;
  assert.ok(value, message);
}

// The disclaimer must state every mandatory fact about the experiment.
ok(DEEP_LOCAL_DISCLAIMER.includes("optional"), "disclaimer says optional");
ok(DEEP_LOCAL_DISCLAIMER.includes("text-only"), "disclaimer says text-only");
ok(DEEP_LOCAL_DISCLAIMER.includes("minutes to hours"), "disclaimer says minutes to hours");
ok(DEEP_LOCAL_DISCLAIMER.includes("no model is included"), "disclaimer says no model included");
ok(DEEP_LOCAL_DISCLAIMER.includes("not Everyday Local chat"), "disclaimer distinguishes Everyday Local");
ok(DEEP_LOCAL_DISCLAIMER.includes("never downloads"), "disclaimer rules out automatic download");
ok(DEEP_LOCAL_DISCLAIMER.includes("no cloud fallback"), "disclaimer rules out cloud fallback");
ok(DEEP_LOCAL_DISCLAIMER.includes("127.0.0.1"), "disclaimer states loopback-only");

// Every state maps to plain-language copy with correct affordances.
const nonTerminalCancellable = ["queued", "checking_runtime", "waiting_for_provider", "running"];
for (const state of nonTerminalCancellable) {
  const view = deepLocalJobView(state, "");
  equal(view.terminal, false, `${state} is not terminal`);
  equal(view.cancellable, true, `${state} is cancellable`);
  equal(view.retryable, false, `${state} is not retryable`);
  ok(view.label.length > 0 && view.explanation.length > 0, `${state} has copy`);
}

const cancelRequested = deepLocalJobView("cancel_requested", "");
equal(cancelRequested.terminal, false, "cancel_requested is not terminal");
equal(cancelRequested.cancellable, false, "cancel_requested is not re-cancellable");

const completed = deepLocalJobView("completed", "");
equal(completed.terminal, true, "completed is terminal");
equal(completed.retryable, false, "completed is not retryable");

for (const state of ["failed", "cancelled_before_start", "interrupted"]) {
  const view = deepLocalJobView(state, "");
  equal(view.terminal, true, `${state} is terminal`);
  equal(view.cancellable, false, `${state} is not cancellable`);
  equal(view.retryable, true, `${state} is retryable`);
}

// Honest cancellation copy: running must warn that cancel only stops the wait.
ok(
  deepLocalJobView("running", "").explanation.includes("stops the wait"),
  "running copy says cancel only stops the wait"
);
ok(
  deepLocalJobView("interrupted", "stopped_waiting").explanation.includes("may have kept working"),
  "interrupted copy never claims the engine stopped"
);
ok(
  deepLocalJobView("interrupted", "interrupted_by_restart").explanation.includes("closed while"),
  "restart interruption is explained"
);

// Unknown states fail safe: terminal, no affordances.
const unknown = deepLocalJobView("something_new", "");
equal(unknown.terminal, true, "unknown state is treated as terminal");
equal(unknown.cancellable, false, "unknown state is not cancellable");
equal(unknown.retryable, false, "unknown state is not retryable");

// Error-category copy is jargon-free and total.
for (const category of [
  "disabled",
  "connection_failure",
  "auth_failure",
  "invalid_model",
  "queue_saturated",
  "queue_timeout",
  "timeout",
  "unsupported_feature",
  "interrupted",
  "totally_new_category"
]) {
  const copy = deepLocalErrorCategoryCopy(category);
  ok(typeof copy === "string" && copy.length > 0, `category ${category} has copy`);
  ok(!copy.includes("HTTP") && !copy.includes("429"), `category ${category} copy is jargon-free`);
}
equal(deepLocalErrorCategoryCopy(""), "", "empty category maps to empty copy");
ok(deepLocalFailureCopy("deep_local_failed").includes("retry"), "failure copy offers retry");

// Elapsed formatting.
equal(formatDeepLocalElapsed(0), "0s", "zero elapsed");
equal(formatDeepLocalElapsed(59_000), "59s", "seconds only");
equal(formatDeepLocalElapsed(61_000), "1m 1s", "minutes and seconds");
equal(formatDeepLocalElapsed(3_720_000), "1h 2m", "hours and minutes");
equal(formatDeepLocalElapsed(-5), "0s", "negative clamps to zero");

ok(Number.isInteger(DEEP_LOCAL_POLL_INTERVAL_MS) && DEEP_LOCAL_POLL_INTERVAL_MS >= 1000, "poll interval bounded");

console.log(`deep-local-ui-tests-ok (${assertionCount} assertions)`);
