// Contract tests for the degraded-backend banner logic. There is no App.tsx
// render test because the project has no DOM test runner (no vitest/jest or
// @testing-library); adding one just for this banner would introduce new dev
// dependencies to a narrow patch. The banner component renders directly from
// backendBannerState, so these contract tests plus the Rust lifecycle tests
// cover the behavior; a render harness can be added when a broader frontend
// test setup lands.
import assert from "node:assert/strict";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/shell/backendStatus.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const source = result.outputFiles[0].text;
const module = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

// Degraded-state rendering: banner is visible with the fixed actionable copy.
const degraded = module.backendBannerState(true, false);
assert.equal(degraded.visible, true);
assert.equal(
  degraded.message,
  "Backend unavailable. Local AI features may not work until the backend reconnects."
);
assert.equal(degraded.retryLabel, "Retry backend");
assert.equal(degraded.retryDisabled, false);

// Privacy: the banner copy is a fixed string with no payloads, paths or traces.
for (const fragment of ["\\", "/", "{", "}", "Traceback", "C:", "jsonrpc"]) {
  assert.equal(
    degraded.message.includes(fragment),
    false,
    `banner copy must not contain "${fragment}"`
  );
}

// Retry in flight: action is disabled and relabeled, message unchanged.
const retrying = module.backendBannerState(true, true);
assert.equal(retrying.visible, true);
assert.equal(retrying.retryLabel, "Retrying...");
assert.equal(retrying.retryDisabled, true);
assert.equal(retrying.message, degraded.message);

// Recovery/reset: once the backend is no longer degraded the banner is gone.
const recovered = module.backendBannerState(false, false);
assert.equal(recovered.visible, false);

// Event guard accepts only the fixed-shape shell event.
assert.equal(
  module.isBackendDegradedEvent({ __odysseus_backend_degraded__: true, degraded: true }),
  true
);
assert.equal(
  module.isBackendDegradedEvent({ __odysseus_backend_degraded__: true, degraded: false }),
  true
);
assert.equal(module.isBackendDegradedEvent({ degraded: true }), false);
assert.equal(module.isBackendDegradedEvent(null), false);
assert.equal(module.isBackendDegradedEvent("backend_degraded"), false);
assert.equal(
  module.isBackendDegradedEvent({ __odysseus_backend_degraded__: true, degraded: "yes" }),
  false
);

console.log("backend-status-tests-ok");
