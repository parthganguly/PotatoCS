// Contract tests for the import-job state model and controller. As with the
// readiness/backend-status tests, esbuild bundles a pure module and node:assert
// verifies the contract without adding a DOM test framework.
import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/jobs/jobModel.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
const {
  ImportJobController,
  JOB_POLL_INTERVAL_MS,
  canCancelJob,
  isTerminalJobState,
  jobFailureCopy,
  jobPollUnavailableCopy,
  jobSubmissionFailureCopy,
  jobView
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

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

function snapshot(state = "queued", overrides = {}) {
  return {
    job_id: "job-1",
    kind: "import",
    state,
    message_code: "",
    scope: "library",
    document_id: "",
    artifact_id: "",
    created_at: 1,
    started_at: null,
    finished_at: null,
    elapsed_ms: 0,
    queue_position: 1,
    ...overrides
  };
}

function scheduler() {
  let nextId = 1;
  const tasks = new Map();
  return {
    tasks,
    set(callback, delayMs) {
      const id = nextId++;
      tasks.set(id, { callback, delayMs });
      return id;
    },
    clear(id) {
      tasks.delete(id);
    },
    runNext() {
      const entry = tasks.entries().next().value;
      if (!entry) return false;
      const [id, task] = entry;
      tasks.delete(id);
      task.callback();
      return true;
    }
  };
}

async function flush() {
  await Promise.resolve();
  await Promise.resolve();
}

// Every backend state has exact fixed copy; unknown input fails closed and is
// never echoed back.
const states = {
  queued: ["Queued", "Waiting for earlier imports to finish."],
  preflighting: ["Preflighting", "Checking this document before import."],
  running: ["Running", "Importing and indexing this document."],
  cancel_requested: ["Cancelling...", "Waiting for the job to reach a safe stopping point."],
  cancelled: ["Cancelled", "The import was cancelled and no partial source is visible."],
  failed: ["Failed", "The document could not be imported."],
  completed: ["Completed", "The document was imported successfully."]
};
for (const [state, [label, explanation]] of Object.entries(states)) {
  const view = jobView(state);
  equal(view.state, state);
  equal(view.label, label);
  equal(view.explanation, explanation);
}
const privateSentinel = "C:\\Users\\private\\SECRET.pdf Traceback {jsonrpc}";
const unknown = jobView(privateSentinel);
equal(unknown.state, "unknown");
equal(unknown.label, "Status unavailable");
equal(unknown.explanation.includes(privateSentinel), false);

// Every approved JOB_MESSAGE_CODES value maps to fixed copy. Unknown/private
// input gets the generic failure and no mapper echoes raw input.
const codes = [
  "cancelled_by_user",
  "job_failed",
  "file_not_found",
  "unsupported_file_type",
  "document_not_found",
  "document_busy",
  "ocr_unavailable",
  "ocr_no_text",
  "ocr_page_too_large",
  "ocr_too_many_pages"
];
for (const code of codes) {
  const copy = jobFailureCopy(code);
  ok(copy.length > 0);
  equal(copy === code, false, "fixed copy must not echo a backend code");
}
for (const mapperCopy of [
  jobFailureCopy(privateSentinel),
  jobSubmissionFailureCopy(privateSentinel),
  jobPollUnavailableCopy(true)
]) {
  equal(mapperCopy.includes(privateSentinel), false);
  equal(/[{}]|Traceback|C:\\|jsonrpc/.test(mapperCopy), false);
}

equal(JOB_POLL_INTERVAL_MS, 2000, "polling cadence is conservative and not sub-second");
for (const state of ["queued", "preflighting", "running"]) equal(canCancelJob(state), true);
for (const state of ["cancel_requested", "cancelled", "completed", "failed", "interrupted", "unknown"]) {
  equal(canCancelJob(state), false, `${state} must not offer cancellation`);
}
equal(canCancelJob("running", true), false, "in-flight cancel guard disables synchronously");

// Duplicate clicks: the local guard is rendered before the cancel promise
// resolves, and exactly one cancel RPC is sent.
{
  const clock = scheduler();
  const cancelGate = deferred();
  let cancelCalls = 0;
  let latest = [];
  const api = {
    submitImport: async () => [snapshot("running")],
    get: async () => snapshot("running"),
    cancel: () => { cancelCalls += 1; return cancelGate.promise; }
  };
  const controller = new ImportJobController(api, () => {}, clock);
  controller.subscribe((jobs) => { latest = jobs; });
  await controller.submit(["private-path.pdf"], "library", "library");
  const first = controller.cancel("job-1");
  const second = controller.cancel("job-1");
  equal(cancelCalls, 1);
  equal(latest[0].cancelSubmitting, true);
  equal(canCancelJob(latest[0].localState, latest[0].cancelSubmitting), false);
  cancelGate.resolve(snapshot("cancel_requested"));
  await Promise.all([first, second]);
  equal(latest[0].localState, "cancel_requested");
}

// No overlapping request: the next timer is scheduled only after the current
// get resolves. Completed stops polling and emits one refresh/no-resubmit intent.
{
  const clock = scheduler();
  const pollGate = deferred();
  let getCalls = 0;
  let submitCalls = 0;
  const intents = [];
  const api = {
    submitImport: async () => { submitCalls += 1; return [snapshot("queued")]; },
    get: () => { getCalls += 1; return pollGate.promise; },
    cancel: async () => snapshot("cancel_requested")
  };
  const controller = new ImportJobController(api, (intent) => intents.push(intent), clock);
  controller.subscribe(() => {});
  await controller.submit(["one.pdf"], "library", "library");
  equal(submitCalls, 1, "real controller import path submits once");
  equal(clock.tasks.size, 1, "one timer per active job");
  equal([...clock.tasks.values()][0].delayMs, 2000);
  clock.runNext();
  equal(getCalls, 1);
  equal(clock.tasks.size, 0, "no timer overlaps an in-flight poll");
  pollGate.resolve(snapshot("completed", { document_id: "document-1" }));
  await flush();
  equal(clock.tasks.size, 0, "completed stops polling");
  equal(intents.length, 1);
  equal(intents[0].refreshSources, true);
  equal(intents[0].resubmit, false);
  equal(submitCalls, 1, "terminal handling never replays submission");
}

// Each other terminal state stops. Cancelled refreshes; failed does not invent
// a refresh/resubmit intent.
for (const terminal of ["cancelled", "failed"]) {
  const clock = scheduler();
  const intents = [];
  const controller = new ImportJobController({
    submitImport: async () => [snapshot("queued")],
    get: async () => snapshot(terminal, { message_code: terminal === "failed" ? "job_failed" : "cancelled_by_user" }),
    cancel: async () => snapshot("cancel_requested")
  }, (intent) => intents.push(intent), clock);
  controller.subscribe(() => {});
  await controller.submit(["one.pdf"], "library", "library");
  clock.runNext();
  await flush();
  equal(clock.tasks.size, 0, `${terminal} stops polling`);
  equal(intents.length, terminal === "cancelled" ? 1 : 0);
}

// Unmount and removal both clear pending timers.
for (const action of ["unmount", "remove"]) {
  const clock = scheduler();
  const controller = new ImportJobController({
    submitImport: async () => [snapshot("queued")],
    get: async () => snapshot("running"),
    cancel: async () => snapshot("cancel_requested")
  }, () => {}, clock);
  controller.subscribe(() => {});
  await controller.submit(["one.pdf"], "library", "library");
  equal(clock.tasks.size, 1);
  if (action === "unmount") controller.unmount(); else controller.remove("job-1");
  equal(clock.tasks.size, 0, `${action} stops polling`);
}

// An in-flight request cannot recreate its timer after unmount.
{
  const clock = scheduler();
  const pollGate = deferred();
  const controller = new ImportJobController({
    submitImport: async () => [snapshot("running")],
    get: () => pollGate.promise,
    cancel: async () => snapshot("cancel_requested")
  }, () => {}, clock);
  controller.subscribe(() => {});
  await controller.submit(["one.pdf"], "library", "library");
  clock.runNext();
  controller.unmount();
  pollGate.resolve(snapshot("running"));
  await flush();
  equal(clock.tasks.size, 0, "an in-flight poll stays stopped after unmount");
}

// A missing in-memory record after restart becomes the fixed interrupted view,
// refreshes Sources, and never resubmits. Its raw KeyError text is not rendered.
{
  const clock = scheduler();
  const intents = [];
  let submitCalls = 0;
  let latest = [];
  const controller = new ImportJobController({
    submitImport: async () => { submitCalls += 1; return [snapshot("running")]; },
    get: async () => { throw new Error(`'job not found: job-1' ${privateSentinel}`); },
    cancel: async () => snapshot("cancel_requested")
  }, (intent) => intents.push(intent), clock);
  controller.subscribe((jobs) => { latest = jobs; });
  await controller.submit(["one.pdf"], "library", "library");
  clock.runNext();
  await flush();
  equal(latest[0].localState, "interrupted");
  equal(jobView(latest[0].localState).explanation.includes(privateSentinel), false);
  equal(clock.tasks.size, 0);
  equal(intents.length, 1);
  equal(intents[0].refreshSources, true);
  equal(intents[0].resubmit, false);
  equal(submitCalls, 1);
}

// Temporary degradation uses fixed copy and resumes with one timer.
{
  const clock = scheduler();
  let latest = [];
  const controller = new ImportJobController({
    submitImport: async () => [snapshot("running")],
    get: async () => { throw new Error(privateSentinel); },
    cancel: async () => snapshot("cancel_requested")
  }, () => {}, clock);
  controller.subscribe((jobs) => { latest = jobs; });
  await controller.submit(["one.pdf"], "library", "library");
  clock.runNext();
  await flush();
  equal(latest[0].pollUnavailable, true);
  equal(clock.tasks.size, 1);
  equal(jobPollUnavailableCopy(true).includes(privateSentinel), false);
}

for (const terminal of ["completed", "cancelled", "failed", "interrupted", "unknown"]) {
  equal(isTerminalJobState(terminal), true);
}

console.log(`jobs-ui-tests-ok assertions=${assertionCount}`);
