import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
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

// Results-only retrieval must render as retrieval, never as verified answer support.
// Persisted message metadata is durable and can be malformed, legacy or hand-edited, so
// the component is rendered for real here rather than asserted against its source text.
{
  const rendered = await build({
    stdin: {
      contents: [
        'import { createElement } from "react";',
        'import { renderToStaticMarkup } from "react-dom/server";',
        'import { SearchResultsOnlyCard } from "./src/features/search/SearchResultsOnlyCard";',
        'export function render(results) {',
        '  return renderToStaticMarkup(createElement(SearchResultsOnlyCard, { results }));',
        '}'
      ].join(String.fromCharCode(10)),
      resolveDir: process.cwd(),
      loader: "ts"
    },
    bundle: true,
    format: "cjs",
    platform: "node",
    write: false,
    // react and react-dom/server stay external so the real installed copies load; only
    // the component and its helper are bundled.
    external: ["react", "react-dom/server"],
    define: { "process.env.NODE_ENV": '"production"' }
  });
  const cardModule = { exports: {} };
  new Function("require", "module", "exports", rendered.outputFiles[0].text)(
    createRequire(import.meta.url), cardModule, cardModule.exports
  );
  const render = (results) => cardModule.exports.render(results);
  const HEADING = "Retrieved passages — not verified answer support";
  const valid = {
    passage_id: "p1",
    source_id: "doc-1",
    title: "Municipal Heat Pump Program",
    source_origin: "web",
    canonical_url: "https://example.com/heat-pumps",
    fetched_at: 1,
    provenance_kind: "exact_text",
    page_number: null,
    text: "The city approved 240 heat-pump rebates."
  };

  // 1-2. Malformed top-level payloads render nothing and never throw.
  for (const payload of [
    undefined,
    null,
    "results_only",
    42,
    {},
    { outcome: "results_only" },
    { outcome: "results_only", passages: null },
    { outcome: "results_only", passages: "nope" },
    { outcome: "results_only", passages: {} },
    { outcome: "results_only", passages: [] },
    { outcome: "results_only", passages: [null] },
    { outcome: "results_only", passages: [null, undefined, 7, "row", [], {}] },
    { outcome: "answer", passages: [valid] }
  ]) {
    assert.equal(render(payload), "", `expected no output for ${JSON.stringify(payload) ?? "undefined"}`);
  }

  // 3-4. Invalid rows are ignored while the valid row still renders.
  const mixed = render({
    outcome: "results_only",
    passages: [
      null,
      "row",
      { passage_id: "no-text" },
      { text: "no identity" },
      { passage_id: 5, text: "wrong id type" },
      valid
    ]
  });
  assert.equal(mixed.includes(valid.text), true);
  assert.equal(mixed.includes("no identity"), false);
  assert.equal(mixed.includes("wrong id type"), false);
  assert.equal((mixed.match(/<article/g) || []).length, 1);

  // 5. The distinction between retrieval and verified support survives rendering.
  assert.equal(mixed.includes(HEADING), true);
  assert.equal(mixed.includes("not verified"), true);
  assert.equal(mixed.includes("citation"), true, "copy explains that there are no citation numbers");
  assert.equal(/\[\d+\]/.test(mixed), false, "no clickable citation numbers");
  assert.equal(mixed.includes("verified_exact"), false);

  // 6. Valid source links render normally; unusable ones simply do not.
  assert.equal(mixed.includes('href="https://example.com/heat-pumps"'), true);
  const badUrl = render({
    outcome: "results_only",
    passages: [{ ...valid, canonical_url: "javascript:alert(1)" }, { ...valid, passage_id: "p2", canonical_url: 12 }]
  });
  assert.equal(badUrl.includes(valid.text), true);
  assert.equal(badUrl.includes("javascript:"), false);
  assert.equal(badUrl.includes("<a "), false);

  // Wrong primitive types in optional fields degrade instead of throwing.
  const loose = render({
    outcome: "results_only",
    passages: [{ ...valid, title: 9, fetched_at: "soon", page_number: "three", source_origin: null }]
  });
  assert.equal(loose.includes("Untitled source"), true);
  assert.equal(loose.includes("local Source"), true);
  assert.equal(loose.includes("Fetched"), false);
  assert.equal(loose.includes("Page"), false);

  const appSourceForCard = readFileSync("src/App.tsx", "utf8");
  assert.equal(appSourceForCard.includes("<SearchResultsOnlyCard results={message.metadata.search_results} />"), true);
  // The component must use the same helper this test exercises.
  const cardSource = readFileSync("src/features/search/SearchResultsOnlyCard.tsx", "utf8");
  assert.equal(cardSource.includes("resultsOnlyPassages(results)"), true);
  assert.equal(model.resultsOnlyPassages({ outcome: "results_only", passages: [null, valid] }).length, 1);
}

console.log("search-ui-tests-ok");
