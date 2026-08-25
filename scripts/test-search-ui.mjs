import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/search/searchModel.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const model = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

for (const state of ["queued", "preflighting", "running", "cancel_requested"]) {
  assert.equal(model.isActiveSearchState(state), true);
}
for (const state of ["completed", "failed", "cancelled", "private-payload"]) {
  assert.equal(model.isActiveSearchState(state), false);
}
assert.equal(model.canCancelSearch("running"), true);
assert.equal(model.canCancelSearch("cancel_requested"), false);
assert.equal(model.searchJobLabel("queued"), "Search queued");
assert.equal(model.searchJobLabel("cancel_requested"), "Cancelling Search…");
assert.equal(
  model.SEARCH_PRIVACY_DISCLOSURE,
  "Search sends your question and public search-query reformulations to Brave Search. Content from local Sources is never included."
);
assert.equal(model.shouldShowChatProgress(true, "completed"), true);
assert.equal(model.shouldShowChatProgress(true, "failed"), true);
assert.equal(model.shouldShowChatProgress(true, "running"), false);
assert.equal(model.shouldShowChatProgress(false, "completed"), false);
const chatHeaderSource = readFileSync("src/features/chat/ChatHeader.tsx", "utf8");
assert.match(chatHeaderSource, /\{SEARCH_PRIVACY_DISCLOSURE\}/);
assert.match(chatHeaderSource, /aria-describedby="web-search-privacy-disclosure"/);
const appSource = readFileSync("src/App.tsx", "utf8");
assert.match(appSource, /shouldShowChatProgress\(props\.busy, props\.searchJob\?\.state\)/);

const approvedCodes = [
  "search_provider_unconfigured",
  "search_provider_failed",
  "search_no_evidence",
  "search_budget_exhausted",
  "model_unavailable",
  "search_failed",
  "search_poll_unavailable"
];
for (const code of approvedCodes) {
  const copy = model.searchFailureCopy(code);
  assert.equal(copy.length > 0, true);
  assert.equal(copy.includes(code), false);
}
const sentinel = "C:\\Users\\private\\SECRET Traceback {jsonrpc}";
assert.equal(model.searchFailureCopy(sentinel), model.searchFailureCopy("search_failed"));
assert.equal(model.searchFailureCopy(sentinel).includes(sentinel), false);

function snapshot(state = "running", overrides = {}) {
  return {
    job_id: "search-job-1",
    kind: "search",
    state,
    message_code: "",
    scope: "chat",
    document_id: "",
    artifact_id: "",
    session_id: "session-1",
    message_id: "",
    run_id: "",
    created_at: 1,
    started_at: 2,
    finished_at: null,
    elapsed_ms: 0,
    queue_position: null,
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
    clear(id) { tasks.delete(id); },
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
  await Promise.resolve();
}

const submitResult = (job) => ({ job, session: { id: "session-1", title: "New chat", model: "fixture", created_at: 1, updated_at: 1, last_message_at: null } });

// One transient get failure schedules bounded backoff, then recovery reaches
// terminal state and releases the UI through onSettled.
{
  const clock = scheduler();
  let gets = 0;
  let busy = true;
  const settled = [];
  const controller = new model.SearchJobController({
    submit: async () => submitResult(snapshot("running")),
    get: async () => {
      gets += 1;
      if (gets === 1) throw new Error("transient private RPC failure");
      return snapshot("completed", { message_id: "message-1", finished_at: 10 });
    },
    cancel: async () => snapshot("cancel_requested")
  }, (job) => { busy = false; settled.push(job); }, clock);
  controller.subscribe(() => {});
  await controller.submit({ query: "public", model: "fixture" });
  clock.runNext();
  await flush();
  assert.equal(clock.tasks.size, 1);
  assert.equal([...clock.tasks.values()][0].delayMs, model.searchPollDelay(1));
  clock.runNext();
  await flush();
  assert.equal(gets, 2);
  assert.equal(settled[0].state, "completed");
  assert.equal(busy, false);
  assert.equal(clock.tasks.size, 0);
}

// Persistent get failures terminate after the fixed cap, expose fixed copy,
// and release busy state instead of polling forever.
{
  const clock = scheduler();
  let gets = 0;
  let busy = true;
  let latest = null;
  const controller = new model.SearchJobController({
    submit: async () => submitResult(snapshot("running")),
    get: async () => { gets += 1; throw new Error("persistent private RPC failure"); },
    cancel: async () => snapshot("cancel_requested")
  }, () => { busy = false; }, clock);
  controller.subscribe((job) => { latest = job; });
  await controller.submit({ query: "public", model: "fixture" });
  for (let index = 0; index < model.SEARCH_POLL_MAX_FAILURES; index += 1) {
    assert.equal(clock.runNext(), true);
    await flush();
  }
  assert.equal(gets, model.SEARCH_POLL_MAX_FAILURES);
  assert.equal(latest.state, "failed");
  assert.equal(latest.message_code, "search_poll_unavailable");
  assert.equal(model.searchFailureCopy(latest.message_code).includes("persistent private"), false);
  assert.equal(busy, false);
  assert.equal(clock.tasks.size, 0);
}

console.log("search-ui-tests-ok");
