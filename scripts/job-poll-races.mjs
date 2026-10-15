import assert from "node:assert/strict";

// Settlement races for the shared JobPollController, run by both the Search and
// chat UI tests against the real bundled controller.
export async function checkJobPollRaces(JobPollController, kind, pollFailureCode) {
  const snapshot = (id, state) => ({
    job_id: id, kind, state, message_code: state === "cancelled" ? "cancelled_by_user" : "", scope: "chat",
    document_id: "", artifact_id: "", session_id: "session-1", message_id: "", run_id: "",
    created_at: 1, started_at: 2, finished_at: null, elapsed_ms: 0, queue_position: null
  });
  const submitted = (job) => ({ job, session: { id: "session-1" } });
  const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return { promise, resolve }; };
  const flush = async () => { for (let i = 0; i < 12; i += 1) await Promise.resolve(); };
  const clock = () => {
    let nextId = 1;
    const tasks = new Map();
    return {
      tasks,
      set(callback) { const id = nextId++; tasks.set(id, callback); return id; },
      clear(id) { tasks.delete(id); },
      async runNext() { const [id, callback] = tasks.entries().next().value; tasks.delete(id); callback(); await flush(); }
    };
  };
  const make = (api, timers) => {
    const settled = [];
    const shown = [];
    const controller = new JobPollController(api, (job) => { settled.push(`${job.job_id}:${job.state}`); }, timers, pollFailureCode);
    controller.subscribe((job) => shown.push(job));
    return { controller, settled, shown };
  };

  // A cancel answered with a terminal snapshot settles exactly once and stops polling.
  for (const terminal of ["completed", "cancelled", "failed"]) {
    const timers = clock();
    const { controller, settled, shown } = make({
      submit: async () => submitted(snapshot("A", "running")),
      get: async () => snapshot("A", terminal),
      cancel: async () => snapshot("A", terminal)
    }, timers);
    await controller.submit({});
    await controller.cancel();
    assert.deepEqual(settled, [`A:${terminal}`], `${kind}: terminal cancel (${terminal}) must settle`);
    assert.equal(shown.at(-1).state, terminal);
    assert.equal(timers.tasks.size, 0);
  }

  // A terminal cancel and a terminal poll racing each other still settle once.
  {
    const timers = clock();
    const get = deferred();
    const { controller, settled } = make({
      submit: async () => submitted(snapshot("A", "running")),
      get: () => get.promise,
      cancel: async () => snapshot("A", "cancelled")
    }, timers);
    await controller.submit({});
    const polling = timers.runNext();
    await controller.cancel();
    get.resolve(snapshot("A", "completed"));
    await polling;
    await flush();
    assert.deepEqual(settled, ["A:cancelled"], `${kind}: one settlement per job`);
  }

  // Sol's stale cancel: A's late cancel response must not replace active job B.
  {
    const timers = clock();
    const cancelA = deferred();
    let current = "A";
    const { controller, settled, shown } = make({
      submit: async () => submitted(snapshot(current, "running")),
      get: async (id) => snapshot(id, "completed"),
      cancel: () => cancelA.promise
    }, timers);
    await controller.submit({});
    const cancelling = controller.cancel();
    await timers.runNext(); // A completes by polling and settles
    current = "B";
    await controller.submit({});
    cancelA.resolve(snapshot("A", "cancel_requested"));
    await cancelling;
    assert.equal(shown.at(-1).job_id, "B", `${kind}: stale cancel replaced the active job`);
    assert.equal(shown.at(-1).state, "running");
    assert.equal(timers.tasks.size, 1, "B keeps its poll timer");
    await timers.runNext();
    assert.deepEqual(settled, ["A:completed", "B:completed"]);
  }

  // A late poll response for A after B was submitted is ignored.
  {
    const timers = clock();
    const getA = deferred();
    let current = "A";
    const { controller, settled, shown } = make({
      submit: async () => submitted(snapshot(current, "running")),
      get: (id) => (id === "A" ? getA.promise : Promise.resolve(snapshot(id, "running"))),
      cancel: async (id) => snapshot(id, "cancel_requested")
    }, timers);
    await controller.submit({});
    const polling = timers.runNext();
    controller.clear();
    current = "B";
    await controller.submit({});
    getA.resolve(snapshot("A", "completed"));
    await polling;
    await flush();
    assert.equal(shown.at(-1).job_id, "B", `${kind}: stale poll replaced the active job`);
    assert.deepEqual(settled, []);
  }

  // Two submits before React commits busy: the second is refused synchronously.
  {
    const timers = clock();
    const gate = deferred();
    let submits = 0;
    const { controller } = make({
      submit: async () => { submits += 1; await gate.promise; return submitted(snapshot("A", "running")); },
      get: async () => snapshot("A", "running"),
      cancel: async () => snapshot("A", "cancel_requested")
    }, timers);
    const first = controller.submit({});
    await assert.rejects(controller.submit({}), /already being submitted/);
    gate.resolve();
    await first;
    assert.equal(submits, 1, `${kind}: duplicate submission`);
  }

  // A new submission hides the previous terminal job before the new id is known.
  {
    const timers = clock();
    const gate = deferred();
    let call = 0;
    const { controller, shown } = make({
      submit: async () => { call += 1; if (call === 2) await gate.promise; return submitted(snapshot(call === 1 ? "A" : "B", "running")); },
      get: async (id) => snapshot(id, "completed"),
      cancel: async (id) => snapshot(id, "cancel_requested")
    }, timers);
    await controller.submit({});
    await timers.runNext();
    assert.equal(shown.at(-1).state, "completed");
    const pending = controller.submit({});
    assert.equal(shown.at(-1), null, `${kind}: a new submission still shows the previous job`);
    gate.resolve();
    await pending;
    assert.equal(shown.at(-1).job_id, "B");
  }
}
