import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/chat/chatProgress.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

const event = {
  __odysseus_progress__: true,
  operation_id: "chat-1",
  session_id: "session-1",
  message_id: "message-1",
  artifact_id: null,
  source_id: null,
  stage: "florence_load",
  label: "Loading Florence...",
  status: "running",
  started_at: 1_000,
  elapsed_ms: 4_000,
  progress_current: null,
  progress_total: null,
  cache_status: "miss",
  detail: "private OCR text must not render"
};

assert.equal(module.isOperationProgressEvent(event), true);
const bubble = module.progressBubbleText(event, 6_000);
assert.equal(bubble, "Loading Florence... 5s");
assert.equal(bubble.includes(String(event.detail)), false);
assert.equal(module.fallbackProgressLabel(2_999), null);
assert.equal(module.fallbackProgressLabel(3_000), "Working... this may take a while");
assert.match(module.fallbackProgressLabel(120_000), /Taking longer than expected/);

let handler;
let unsubscribeCount = 0;
const dispose = module.createProgressSubscription(
  async (nextHandler) => {
    handler = nextHandler;
    return () => { unsubscribeCount += 1; };
  },
  (received) => assert.equal(received.operation_id, "chat-1")
);
await Promise.resolve();
handler(event);
dispose();
assert.equal(unsubscribeCount, 1);

console.log("chat-progress-tests-ok");
