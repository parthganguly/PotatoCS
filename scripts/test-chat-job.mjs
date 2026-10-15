import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { build, transform } from "esbuild";
import { checkJobPollRaces } from "./job-poll-races.mjs";

// Bundle the real modules: chat job copy/labels, the shared poll controller and
// the rendered progress bubble. react stays external so the installed copy loads.
const bundled = await build({
  stdin: {
    contents: [
      'import { createElement } from "react";',
      'import { renderToStaticMarkup } from "react-dom/server";',
      'import { ChatProgressBar } from "./src/features/chat/ChatProgressBar";',
      'export * from "./src/features/chat/chatJob";',
      'export { JobPollController } from "./src/features/search/searchModel";',
      "export function render(props) { return renderToStaticMarkup(createElement(ChatProgressBar, props)); }"
    ].join("\n"),
    resolveDir: process.cwd(),
    loader: "ts"
  },
  bundle: true,
  format: "cjs",
  platform: "node",
  write: false,
  external: ["react", "react-dom/server"],
  define: { "process.env.NODE_ENV": '"production"' }
});
const loaded = { exports: {} };
new Function("require", "module", "exports", bundled.outputFiles[0].text)(createRequire(import.meta.url), loaded, loaded.exports);
const chat = loaded.exports;

const JOB_ID = "7c9e6679-7425-40de-944b-e07fc1f90ae7";
const job = (state, overrides = {}) => ({
  job_id: JOB_ID, kind: "chat", state, message_code: "", scope: "chat", document_id: "", artifact_id: "",
  session_id: "session-1", message_id: "", run_id: "", created_at: 1, started_at: 2, finished_at: null,
  elapsed_ms: 0, queue_position: null, ...overrides
});
const progress = (stage, label, operationId, status = "running") => ({
  __odysseus_progress__: true, operation_id: operationId, session_id: null, message_id: null, artifact_id: null,
  source_id: null, stage, label, status, started_at: 1_000, elapsed_ms: 0, progress_current: null,
  progress_total: null, cache_status: null
});
const OWN = `operation-${JOB_ID}`;

// Failure copy: fixed, names the model, distinguishes the stages, never fabricates.
const sentinel = "C:\\Users\\private\\Traceback timeout: Ollama request exceeded 120s";
// Only the backend's confirmed codes may say that no answer was generated.
const confirmed = ["model_prepare_timeout", "model_prepare_failed", "model_generation_timeout", "model_generation_failed", "chat_turn_timeout", "cancelled_by_user"];
for (const code of confirmed) {
  const copy = chat.chatJobFailureCopy(code, "Qwen3 4B");
  assert.match(copy, /No answer was saved\. The question stays in this conversation and can be retried\./);
  assert.equal(copy.includes(code), false);
  assert.equal(copy.includes("composer"), false, "a failed turn never invites a resend from the composer");
  assert.equal(chat.isConfirmedChatOutcome(code), true);
}
for (const code of [chat.CHAT_POLL_FAILURE_CODE, "job_failed", "some_future_code"]) {
  const copy = chat.chatJobFailureCopy(code, "Qwen3 4B");
  assert.equal(copy.includes("No answer was saved"), false, `${code} cannot establish that no answer exists`);
  assert.match(copy, /Whether an answer was saved is unknown\. Nothing was resent; use Check status/);
  assert.equal(chat.isConfirmedChatOutcome(code), false, `${code} needs a status check`);
}
assert.match(chat.chatJobFailureCopy("chat_turn_timeout", "x"), /5-minute limit/);
assert.equal(chat.CHAT_RESULT_UNAVAILABLE_COPY.includes("No answer was saved"), false);
assert.match(chat.CHAT_RESULT_UNAVAILABLE_COPY, /generated and saved/);
assert.match(chat.chatJobFailureCopy("model_prepare_timeout", "Qwen3 4B"), /^Qwen3 4B did not finish loading/);
assert.match(chat.chatJobFailureCopy("model_generation_timeout", "Qwen3 4B"), /^Qwen3 4B loaded but did not finish the answer/);
assert.match(chat.chatJobFailureCopy(chat.CHAT_POLL_FAILURE_CODE, "x"), /Nothing was resent/);
assert.equal(chat.chatJobFailureCopy(sentinel, "x"), chat.chatJobFailureCopy("job_failed", "x"));

// Labels and cancellability per job state.
assert.equal(chat.chatJobStatusLabel("queued"), "Waiting for other jobs to finish…");
assert.equal(chat.chatJobStatusLabel("cancel_requested"), "Cancelling…");
assert.equal(chat.chatJobStatusLabel("running"), null);
assert.equal(chat.chatJobStatusLabel("running", true), "Time limit reached — stopping…");
assert.equal(chat.chatJobStatusLabel("cancel_requested", true), "Time limit reached — stopping…");
assert.equal(chat.chatJobStatusLabel("failed", true), null);

// Attempt outcomes: fixed copy, never raw backend text.
assert.equal(chat.chatAttemptLabel({ state: "failed", outcome_code: "model_prepare_timeout" }), "Failed — the model did not finish loading within 120 s");
assert.equal(chat.chatAttemptLabel({ state: "timed_out", outcome_code: "chat_turn_timeout" }), "Timed out — the 5-minute limit was reached");
assert.equal(chat.chatAttemptLabel({ state: "interrupted", outcome_code: "backend_restarted" }), "Stopped — the backend stopped before an answer was saved");
assert.equal(chat.chatAttemptLabel({ state: "failed", outcome_code: sentinel }), "Failed — an unexpected error");
assert.equal(chat.chatTurnStatusLabel({ answered: false, latest_state: "running", latest_outcome_code: "" }), null);
assert.equal(chat.chatTurnStatusLabel({ answered: true, latest_state: "completed", latest_outcome_code: "" }), null);
assert.equal(chat.chatTurnStatusLabel({ answered: false, latest_state: "cancelled", latest_outcome_code: "cancelled_by_user" }), "Not answered. Cancelled.");
for (const state of ["queued", "preflighting", "running"]) assert.equal(chat.canCancelChatJob(state), true);
for (const state of ["cancel_requested", "completed", "failed", "cancelled"]) assert.equal(chat.canCancelChatJob(state), false);

// Image turns stay on chat.send.
assert.equal(chat.canSendAsChatJob([{ backend_kind: "document" }], []), true);
assert.equal(chat.canSendAsChatJob([{ backend_kind: "artifact" }], []), false);
assert.equal(chat.canSendAsChatJob([], [{ backend_kind: "artifact" }]), false);

// Rendered lifecycle.
{
  // Preparation is shown as preparation, with Cancel, never as generation.
  const html = chat.render({ event: progress("model_prepare", "Preparing local model...", OWN), fallbackLabel: null, now: 31_000, job: job("running"), onCancel: () => {} });
  assert.match(html, /Preparing local model\.\.\. 30s/);
  assert.equal(html.includes("Generating"), false);
  assert.match(html, />Cancel</);
}
{
  const html = chat.render({ event: progress("answer_generation", "Generating answer...", OWN), fallbackLabel: null, now: 2_000, job: job("running"), onCancel: () => {} });
  assert.match(html, /Generating answer\.\.\. 1s/);
}
{
  // While the submit is in flight there is no job id: an earlier operation's "Done" is not shown.
  const html = chat.render({ event: progress("done", "Done", "operation-11111111-2222-4333-8444-555555555555", "completed"), fallbackLabel: null, now: 2_000, job: null, awaitingJob: true, onCancel: () => {} });
  assert.equal(html.includes("Done"), false);
  assert.match(html, /Starting…/);
  assert.match(html, /animate-spin/);
}
{
  // A stale "done" from an earlier import is not this chat's completion.
  const html = chat.render({ event: progress("done", "Done", "operation-11111111-2222-4333-8444-555555555555", "completed"), fallbackLabel: null, now: 2_000, job: job("running"), onCancel: () => {} });
  assert.equal(html.includes("Done"), false);
  assert.match(html, /Working…/);
  assert.match(html, /animate-spin/);
}
{
  // The backend's own deadline passed while work is stopping: say so, offer no Cancel,
  // and show elapsed time since accepted Send rather than any estimate.
  const html = chat.render({ event: progress("answer_generation", "Generating answer...", OWN), fallbackLabel: null, now: 301_001, job: job("running", { deadline_reached: true }), onCancel: () => {} });
  assert.match(html, /Time limit reached — stopping… · 301s total/);
  assert.equal(html.includes(">Cancel<"), false);
  assert.equal(/%|remaining|ETA/i.test(html.replace(/<[^>]*>/g, "")), false);
}
{
  const html = chat.render({ event: progress("model_prepare", "Preparing local model...", OWN), fallbackLabel: null, now: 2_000, job: job("cancel_requested"), onCancel: () => {} });
  assert.match(html, /Cancelling…/);
  assert.equal(html.includes(">Cancel<"), false);
}
{
  const html = chat.render({ event: null, fallbackLabel: null, now: 0, job: job("queued", { queue_position: 1 }), onCancel: () => {} });
  assert.match(html, /Waiting for other jobs to finish…/);
  assert.match(html, />Cancel</);
}
{
  // Synchronous chat.send (image turns) keeps the old bubble without Cancel.
  const html = chat.render({ event: progress("local_vision", "Running local vision...", "operation-x"), fallbackLabel: null, now: 2_000 });
  assert.match(html, /Running local vision/);
  assert.equal(html.includes(">Cancel<"), false);
}

// Backend loss: polling gives up after the cap with the chat code, settles once,
// and never resubmits the request.
{
  const tasks = new Map();
  let nextId = 1;
  const clock = {
    set(callback, delayMs) { const id = nextId++; tasks.set(id, { callback, delayMs }); return id; },
    clear(id) { tasks.delete(id); }
  };
  let submits = 0;
  const settled = [];
  const controller = new chat.JobPollController({
    submit: async () => { submits += 1; return { job: job("running"), session: { id: "session-1" } }; },
    get: async () => { throw new Error("Python sidecar exited before responding to jobs.get"); },
    cancel: async () => job("cancel_requested")
  }, (terminal) => { settled.push(terminal); }, clock, chat.CHAT_POLL_FAILURE_CODE);
  controller.subscribe(() => {});
  await controller.submit({ message: "q" });
  while (tasks.size) {
    const [id, task] = tasks.entries().next().value;
    tasks.delete(id);
    task.callback();
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  }
  assert.equal(submits, 1);
  assert.equal(settled.length, 1);
  assert.equal(settled[0].state, "failed");
  assert.equal(settled[0].message_code, chat.CHAT_POLL_FAILURE_CODE);
}

await checkJobPollRaces(chat.JobPollController, "chat", chat.CHAT_POLL_FAILURE_CODE);

// App control flow, extracted from the real source and run with stub dependencies.
// This is a handler seam, not a WebView test.
const appSource = readFileSync("src/App.tsx", "utf8").replaceAll("\r\n", "\n");
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
{
  // Two sends from the same render (busy not yet committed) submit once.
  const start = appSource.indexOf("  async function sendMessage(event: FormEvent)");
  const handler = (await transform(appSource.slice(start, appSource.indexOf("  const hasRuntime", start)), { loader: "ts" })).code;
  let submits = 0;
  const keys = [];
  const gate = deferred();
  const noOp = () => {};
  const deps = {
    busy: false, sendingRef: { current: false }, draft: "q", pendingSources: [], conversationContext: [], ollama: { reachable: false },
    modelChoices: [], selectedSession: null, selectedSessionId: null, modelDraft: "fixture", settings: { default_model: "fixture" },
    useWebSearch: false, visionBackendDraft: "", chatMultimodalMode: "automatic", chatVisionModel: "", useRag: false,
    ragPreset: "standard", verifyRag: false, answerStyle: "precise", selectedRagDocumentId: "",
    setError: noOp, setBusy: noOp, setAwaitingJob: noOp, setDraft: noOp, setPendingUserContent: noOp, setLastChatAnalysis: noOp,
    setRetrievedChunks: noOp, setRetrievedSnippets: noOp, setDocumentEvidence: noOp, setGrounding: noOp,
    normalizeVisionBackend: () => "automatic", canSendAsChatJob: () => true, readableModelLabel: (x) => x,
    chatTurnRef: { current: null }, chatJob: { submit: async (params) => { submits += 1; keys.push(params.operation_key); await gate.promise; return { job: job("queued") }; } },
    readError: String, makeRequestId: (prefix) => `${prefix}-${submits}`, reconcileChatTurn: async () => false
  };
  const send = new Function(...Object.keys(deps), `${handler}; return sendMessage;`)(...Object.values(deps));
  const first = send({ preventDefault: noOp });
  const second = send({ preventDefault: noOp });
  gate.resolve();
  await Promise.all([first, second]);
  assert.equal(submits, 1);
  assert.equal(deps.sendingRef.current, true, "the turn stays claimed until the job settles");
  assert.equal(typeof keys[0], "string", "every submission carries an operation key");
  assert.equal(deps.chatTurnRef.current.operationKey, keys[0]);
}
{
  // A completed job whose result cannot be loaded: the answer exists, so no resend is invited.
  const start = appSource.indexOf("    async (job) => {", appSource.indexOf("const chatJob = useJobPoll"));
  const callback = appSource.slice(start, appSource.indexOf("    },\n    CHAT_POLL_FAILURE_CODE", start) + 5).trim();
  const compiled = (await transform(`const settle = ${callback}`, { loader: "ts" })).code;
  const calls = [];
  const deps = {
    chatTurnRef: { current: { message: "q", attachedSources: [], modelLabel: "fixture" } }, sendingRef: { current: true },
    applyChatResult: async () => calls.push(["applied"]), getChatJobResult: async () => { throw new Error("result lost"); },
    setSelectedSessionId: (x) => calls.push(["session", x]), loadMessages: async () => calls.push(["messages"]),
    setDraft: (x) => calls.push(["draft", x]), setError: (x) => calls.push(["error", x]),
    chatJobFailureCopy: chat.chatJobFailureCopy, CHAT_RESULT_UNAVAILABLE_COPY: chat.CHAT_RESULT_UNAVAILABLE_COPY,
    readError: String, setPendingUserContent: (x) => calls.push(["pending", x]), setBusy: (x) => calls.push(["busy", x])
  };
  const settle = new Function(...Object.keys(deps), `${compiled}; return settle;`)(...Object.values(deps));
  await settle(job("completed", { message_id: "m-1" }));
  assert.equal(calls.some(([name]) => name === "draft"), false, "a generated answer must not be offered for resend");
  assert.deepEqual(calls.filter(([name]) => name === "error"), [["error", chat.CHAT_RESULT_UNAVAILABLE_COPY]]);
  assert.ok(calls.findIndex(([name]) => name === "messages") < calls.findIndex(([name]) => name === "error"), "copy is set after loadMessages clears errors");
  assert.deepEqual(calls.slice(-1), [["busy", false]]);
  assert.equal(deps.sendingRef.current, false);
  assert.equal(deps.chatTurnRef.current, null);
}

const settleSource = (() => {
  const start = appSource.indexOf("    async (job) => {", appSource.indexOf("const chatJob = useJobPoll"));
  return appSource.slice(start, appSource.indexOf("    },\n    CHAT_POLL_FAILURE_CODE", start) + 5).trim();
})();
const compiledSettle = (await transform(`const settle = ${settleSource}`, { loader: "ts" })).code;
function settleWith(overrides) {
  const calls = [];
  const deps = {
    chatTurnRef: { current: { message: "q", attachedSources: [], modelLabel: "fixture", operationKey: "k-1", retry: false } },
    sendingRef: { current: true },
    applyChatResult: async () => calls.push(["applied"]), getChatJobResult: async () => ({}),
    setSelectedSessionId: (x) => calls.push(["session", x]), loadMessages: async () => calls.push(["messages"]),
    setDraft: (x) => calls.push(["draft", x]), setError: (x) => calls.push(["error", x]),
    chatJobFailureCopy: chat.chatJobFailureCopy, CHAT_RESULT_UNAVAILABLE_COPY: chat.CHAT_RESULT_UNAVAILABLE_COPY,
    isConfirmedChatOutcome: chat.isConfirmedChatOutcome,
    reconcileChatTurn: async (turn) => { calls.push(["reconcile", turn.operationKey]); return false; },
    readError: String, setPendingUserContent: (x) => calls.push(["pending", x]), setBusy: (x) => calls.push(["busy", x]),
    ...overrides
  };
  return { calls, deps, settle: new Function(...Object.keys(deps), `${compiledSettle}; return settle;`)(...Object.values(deps)) };
}
{
  // A confirmed failure: the question stays visible with Retry; the composer is not refilled.
  const { calls, deps, settle } = settleWith({});
  await settle(job("failed", { message_code: "model_generation_timeout" }));
  assert.equal(calls.some(([name]) => name === "draft"), false);
  assert.equal(calls.some(([name]) => name === "reconcile"), false);
  assert.match(calls.find(([name]) => name === "error")[1], /stays in this conversation/);
  assert.deepEqual(calls.slice(-1), [["busy", false]]);
  assert.equal(deps.sendingRef.current, false);
}
for (const code of [chat.CHAT_POLL_FAILURE_CODE, "job_failed"]) {
  // Lost polling or an unexpected failure: reconcile by operation key, never resend.
  const { calls, settle } = settleWith({});
  await settle(job("failed", { message_code: code }));
  assert.deepEqual(calls.filter(([name]) => name === "reconcile"), [["reconcile", "k-1"]]);
  assert.equal(calls.some(([name]) => name === "draft"), false);
  assert.deepEqual(calls.slice(-1), [["busy", false]]);
}
{
  // Reconciliation found the job still running and follows it again: busy stays claimed.
  const { calls, deps, settle } = settleWith({ reconcileChatTurn: async () => true });
  await settle(job("failed", { message_code: chat.CHAT_POLL_FAILURE_CODE }));
  assert.equal(calls.some(([name]) => name === "busy"), false);
  assert.equal(deps.sendingRef.current, true);
}

const between = (from, to) => appSource.slice(appSource.indexOf(from), appSource.indexOf(to, appSource.indexOf(from)));
const compiledReconcile = (await transform(between("  async function reconcileChatTurn(", "  // Explicit retry of the latest"), { loader: "ts" })).code;
function reconcileWith(status) {
  const calls = [];
  const deps = {
    setOutcomeCheck: (x) => calls.push(["check", x && x.checking]),
    getChatStatus: async (params) => { calls.push(["status", params.operation_key]); if (status instanceof Error) throw status; return status; },
    setDraft: (x) => calls.push(["draft", x]), setError: (x) => calls.push(["error", x]),
    setSelectedSessionId: (x) => calls.push(["session", x]), loadMessages: async (x) => calls.push(["messages", x]),
    isActiveSearchState: (state) => ["queued", "preflighting", "running", "cancel_requested"].includes(state),
    chatTurnRef: { current: null }, sendingRef: { current: false }, setBusy: (x) => calls.push(["busy", x]),
    chatJob: { attach: (j) => calls.push(["attach", j.job_id]), submit: () => calls.push(["SUBMIT"]) },
    chatTurnStatusLabel: chat.chatTurnStatusLabel
  };
  const reconcile = new Function(...Object.keys(deps), `${compiledReconcile}; return reconcileChatTurn;`)(...Object.values(deps));
  return { calls, deps, reconcile };
}
const sendTurn = { message: "my question", attachedSources: [], modelLabel: "m", operationKey: "k-9", retry: false };
const turnStatus = (overrides) => ({ message_id: "m-1", session_id: "s-1", answered: false, answer_message_id: null, current_attempt_id: "a-1",
  latest_state: "failed", latest_outcome_code: "model_prepare_timeout", attempt_count: 1, attempts: [], is_latest_turn: true, can_retry: true, can_copy: false, ...overrides });
{
  const { calls, deps, reconcile } = reconcileWith({ found: true, turn: turnStatus({ latest_state: "running" }), job: job("running") });
  assert.equal(await reconcile(sendTurn), true);
  assert.deepEqual(calls.filter(([n]) => n === "attach"), [["attach", JOB_ID]]);
  assert.equal(deps.sendingRef.current, true);
  assert.equal(deps.chatTurnRef.current, sendTurn);
  assert.equal(calls[0][0], "check", "the checking state is shown before the status request");
}
{
  const { calls, reconcile } = reconcileWith({ found: true, turn: turnStatus({}), job: null });
  assert.equal(await reconcile(sendTurn), false);
  assert.ok(calls.findIndex(([n]) => n === "messages") < calls.findIndex(([n]) => n === "error"));
  assert.match(calls.find(([n]) => n === "error")[1], /^Not answered\. Failed/);
  assert.equal(calls.some(([n]) => n === "draft" || n === "SUBMIT"), false);
}
{
  // Authoritatively never accepted: nothing was saved, so the text may come back.
  const { calls, reconcile } = reconcileWith({ found: false, turn: null, job: null });
  await reconcile(sendTurn, "another answer is still in progress in this conversation");
  assert.deepEqual(calls.filter(([n]) => n === "draft"), [["draft", "my question"]]);
  assert.deepEqual(calls.filter(([n]) => n === "error"), [["error", "another answer is still in progress in this conversation"]]);
  const retry = reconcileWith({ found: false, turn: null, job: null });
  await retry.reconcile({ ...sendTurn, retry: true });
  assert.equal(retry.calls.some(([n]) => n === "draft"), false, "a retry's question is already in the conversation");
}
{
  // The backend cannot be reached: outcome unknown, Check status offered, nothing restored or resent.
  const { calls, reconcile } = reconcileWith(new Error("Python sidecar exited"));
  assert.equal(await reconcile(sendTurn), false);
  assert.deepEqual(calls.filter(([n]) => n === "check"), [["check", true], ["check", false]]);
  assert.equal(calls.some(([n]) => n === "draft" || n === "SUBMIT" || n === "attach"), false);
}
{
  // Retry: only when the backend allows it, by message id with a fresh operation key, once.
  const compiledRetry = (await transform(between("  async function retryChatTurn(", "  async function loadConversationContext("), { loader: "ts" })).code;
  const submitted = [];
  const gate = deferred();
  const noOp = () => {};
  const deps = {
    busy: false, sendingRef: { current: false }, chatTurnRef: { current: null }, selectedSession: { model: "m" }, settings: {},
    readableModelLabel: (x) => x, makeRequestId: (prefix) => `${prefix}-${submitted.length}`,
    setBusy: noOp, setError: noOp, setAwaitingJob: noOp, loadMessages: async () => {}, readError: String,
    reconcileChatTurn: async () => false,
    chatJob: { submit: async (params) => { submitted.push(params); await gate.promise; return { job: job("queued") }; } }
  };
  const retry = new Function(...Object.keys(deps), `${compiledRetry}; return retryChatTurn;`)(...Object.values(deps));
  const message = { id: "m-1", session_id: "s-1", role: "user", content: "q", created_at: 1, turn: turnStatus({}) };
  await retry({ ...message, turn: turnStatus({ can_retry: false }) });
  assert.equal(submitted.length, 0);
  const first = retry(message);
  const second = retry(message);
  gate.resolve();
  await Promise.all([first, second]);
  assert.equal(submitted.length, 1);
  assert.deepEqual(Object.keys(submitted[0]).sort(), ["operation_key", "retry_message_id"]);
  assert.equal(submitted[0].retry_message_id, "m-1");
}
{
  // Turn status, verification notice and the unknown-outcome card, from the real App source.
  const React = createRequire(import.meta.url)("react");
  const { renderToStaticMarkup } = createRequire(import.meta.url)("react-dom/server");
  const source = between("function TurnStatus(", "function IconButton(");
  const compiled = (await transform(source, { loader: "tsx" })).code;
  const parts = new Function("React", "chatTurnStatusLabel", "chatAttemptLabel", "CHECKING_OUTCOME_LABEL",
    `${compiled}; return { TurnStatus, VerificationNotice, OutcomeCheckCard };`)(React, chat.chatTurnStatusLabel, chat.chatAttemptLabel, chat.CHECKING_OUTCOME_LABEL);
  const html = (component, props) => renderToStaticMarkup(React.createElement(component, props));
  const attempt = (n, state, code = "") => ({ attempt_id: `a-${n}`, number: n, state, outcome_code: code, created_at: 0, started_at: 1_000, finished_at: 4_000 });
  const noOp = () => {};
  const failed = html(parts.TurnStatus, { busy: false, onRetry: noOp, onCopy: noOp, message: { turn: turnStatus({ attempts: [attempt(1, "failed", "model_prepare_timeout")] }) } });
  assert.match(failed, /Not answered\. Failed — the model did not finish loading within 120 s\./);
  assert.match(failed, />Retry</);
  assert.equal(failed.includes("Copy to new message"), false);
  assert.match(failed, /Attempts \(1\)/);
  assert.match(failed, /Attempt 1: Failed — the model did not finish loading within 120 s · 3s/);
  const busyRetry = html(parts.TurnStatus, { busy: true, onRetry: noOp, onCopy: noOp, message: { turn: turnStatus({}) } });
  assert.match(busyRetry, /disabled=""[^>]*>Retry</);
  const older = html(parts.TurnStatus, { busy: false, onRetry: noOp, onCopy: noOp, message: { turn: turnStatus({ can_retry: false, can_copy: true, is_latest_turn: false }) } });
  assert.equal(older.includes(">Retry<"), false);
  assert.match(older, /Copy to new message/);
  assert.equal(html(parts.TurnStatus, { busy: false, onRetry: noOp, onCopy: noOp, message: { turn: turnStatus({ latest_state: "running", can_retry: false }) } }), "");
  const answered = html(parts.TurnStatus, { busy: false, onRetry: noOp, onCopy: noOp, message: { turn: turnStatus({ answered: true, latest_state: "completed", can_retry: false,
    attempts: [attempt(1, "interrupted", "backend_restarted"), attempt(2, "completed")] }) } });
  assert.equal(answered.includes("Not answered"), false);
  assert.match(answered, /Attempt 1: Stopped — the backend stopped before an answer was saved/);
  assert.match(answered, /Attempt 2: Answered/);
  assert.equal(html(parts.TurnStatus, { busy: false, onRetry: noOp, onCopy: noOp, message: {} }), "", "legacy messages show no invented outcome");
  assert.match(html(parts.VerificationNotice, { message: { metadata: { verification_notice: { label: "Verification incomplete", reason: "Verification ran out of time." } } } }),
    /Verification incomplete — Verification ran out of time\./);
  assert.equal(html(parts.VerificationNotice, { message: { metadata: {} } }), "");
  assert.match(html(parts.OutcomeCheckCard, { checking: true, onCheck: noOp }), /Checking whether the answer was saved…/);
  const unknown = html(parts.OutcomeCheckCard, { checking: false, onCheck: noOp });
  assert.match(unknown, /Outcome unknown/);
  assert.match(unknown, />Check status</);
  assert.equal(unknown.includes(">Retry<"), false, "an unknown outcome offers Check status, not Retry");
}
{
  // attach() follows an existing job to one settlement and never submits.
  const tasks = new Map();
  let nextId = 1;
  const clock = { set(callback) { const id = nextId++; tasks.set(id, callback); return id; }, clear(id) { tasks.delete(id); } };
  let submits = 0;
  let polls = 0;
  const settled = [];
  const controller = new chat.JobPollController({
    submit: async () => { submits += 1; return { job: job("running") }; },
    get: async () => { polls += 1; return job(polls < 2 ? "running" : "completed"); },
    cancel: async () => job("cancel_requested")
  }, (terminal) => { settled.push(terminal.state); }, clock, chat.CHAT_POLL_FAILURE_CODE);
  controller.subscribe(() => {});
  controller.attach(job("running"));
  while (tasks.size) {
    const [id, callback] = tasks.entries().next().value;
    tasks.delete(id);
    callback();
    for (let i = 0; i < 5; i += 1) await Promise.resolve();
  }
  assert.equal(submits, 0);
  assert.deepEqual(settled, ["completed"]);
}

// Wiring: text/document turns go through the job, and Enter cannot double-submit.
const app = readFileSync("src/App.tsx", "utf8");
assert.match(app, /event\.preventDefault\(\);\s*\/\/[^\n]*\n\s*if \(busy \|\| sendingRef\.current\) return;/);
assert.match(app, /awaitingJob=\{props\.awaitingJob\}/);
assert.match(app, /if \(canSendAsChatJob\(attachedSources, conversationContext\)\) \{/);
assert.match(app, /await chatJob\.submit\(params\);/);
assert.match(app, /onCancel=\{props\.onCancelChat\}/);
assert.match(readFileSync("src/tauri.ts", "utf8"), /"jobs\.submit_chat"/);
assert.match(readFileSync("src/tauri.ts", "utf8"), /"jobs\.retry_chat"/);
assert.match(readFileSync("src/tauri.ts", "utf8"), /"jobs\.chat_status"/);
assert.match(app, /params\.operation_key = turn\.operationKey;/);
assert.equal(/setDraft\(turn\.message\)/.test(settleSource), false, "settlement never refills the composer");

console.log("chat-job-tests-ok");
