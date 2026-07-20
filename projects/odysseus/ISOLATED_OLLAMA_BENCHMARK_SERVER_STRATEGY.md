# Isolated Ollama Benchmark Server Strategy

Status: PROPOSED, revision 6 — dev-only measurement-infrastructure
design (Fable, 2026-07-19; revised same day after independent
architecture review 4731154883 on draft PR #38). No real servers or
models were run and nothing was downloaded or installed. PRs #33 /
#35 / #36 / #37 are inputs and are not modified by this document.

Parent strategy: `HARDWARE_RELATIVE_MODEL_UPLIFT_STRATEGY.md` (the
PR #37 stack). This document designs the one mechanism that strategy
needs but does not define: how to measure **server-level** Ollama
configuration — Flash Attention and KV-cache quantization above all —
without touching the user's ordinary Ollama service.

Revision 2 corrections (all incorporated below): per-session runtime
identity binding instead of any hardcoded version; deny-by-default
minimal child environment with a temporary home; a gated empty-store
first attestation launch; a disposable shadow model store as the
preferred design; privacy-safe typed artifacts (no durable PIDs,
ports, paths, or raw logs); discovery-based ordinary-service handling
with no port assumption; distribution-level ABBA/BAAB blocks with an
externally approved interference policy; and a Phase-1-only
implementation prompt.

Revision 3 Phase 1 contract corrections (incorporated below): launcher-
generated fixed environment keys are separate from the narrow caller
override allowlist; runtime versions are strictly normalized before they
are persisted or compared; startup-log version attestation is optional
and explicitly typed `unattested`; the Windows lifecycle uses the complete
closed failure vocabulary; and Phase 1 writes a standalone attestation
schema-v1 artifact, not performance schema v3. During implementation only
the dry-run CLI and synthetic fixture lifecycle may execute. The real-
launch approval flag, installed Ollama binary, ordinary Ollama service,
and real model store remain untouched.

Revision 4 Phase 1 review corrections: every child uses
`STARTUPINFOEX` with `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` so only the
declared stdio handles are inherited; startup dialects live in a closed
committed SHA-256 registry; readiness and attestation use separate clocks;
the real candidate-port race retries only after complete verified cleanup;
and the suspended process image plus executable hash are revalidated before
assignment/resume. The registry intentionally contains no entry for the
installed binary in this correction cycle. Registering that binary's
reviewed dialect is a separate prerequisite before human G-ISO-0 execution.

Revision 5 Phase 1 review corrections that remain in force: every child
receives valid stdio through an exact three-handle allowlist containing a
launcher-owned read-only Windows NUL handle plus the stdout/stderr pipe
writers; race cleanup may tolerate only the same observed foreign listener
after proving every benchmark-owned resource is gone; and G-ISO-0 requires
a bounded owned `GET /api/ps` proof of zero resident models. Revision 5's
pre-server command-version probe, its port-only first-PID listener lookup,
and its reader-thread cancellation/containment-join design were
architectural mistakes and are superseded below.

Revision 6 architecture corrections (independent review 4731154883 on
draft PR #38; all incorporated below):

1. **Command-version binding is post-readiness.** `ollama --version` is
   a client-server probe that contacts `OLLAMA_HOST` and reports both
   client and server versions (§2.7, verified-upstream). The version
   child now runs only after the isolated server's endpoint is ready
   and TCP-owned, pointed at that owned endpoint, and its output is
   parsed only through the closed SHA-256 version-output dialect
   (§2.6). No harness process may ever target port 1, a guessed port,
   or an unproven endpoint.
2. **Address-aware TCP ownership.** Listener ownership is decided from
   all relevant rows (exact loopback and wildcard on the requested
   port), with a closed owned / not-present / foreign / ambiguous /
   uncertain result model, and bind-race evidence uses a stable
   non-reusable foreign-owner identity — a held checked process handle
   plus creation time — never PID equality alone (§3.3).
3. **Genuinely bounded log I/O.** Blocking reader threads and the
   unbounded containment join are replaced by benchmark-owned
   overlapped named-pipe reads with event waits under absolute
   deadlines, checked `CancelIoEx` against the exact pending
   operation, and a fixed no-retry cleanup failure after cleanup-
   deadline exhaustion. No Python helper thread exists in the I/O
   path and no unbounded wait or join exists anywhere in the
   lifecycle (§5).

The Phase 1 lifecycle is now specified as an explicit ordered state
machine (§12). The dialect registry remains empty and no real runtime,
service, or model store was touched in this revision.

Authority notes:

- Facts below are labeled **measured** (P3 artifacts under
  `projects/odysseus/benchmarks/local-runtime/`), **verified-upstream**
  (read from official Ollama documentation or source on 2026-07-19,
  §2), or **proposed** (this design; requires implementation and/or
  human approval).
- No runtime version is an architectural fact of this design. The
  reviewed PR #37 result records Ollama **0.32.1** on the reference
  machine; upstream docs and source were read at current `main`.
  Every session binds the identity of the binary it actually ran
  (§2.6) and fails closed on disagreement; nothing below assumes any
  particular version's behavior without attestation.

---

## 1. Why an isolated server is required

`OLLAMA_FLASH_ATTENTION` and `OLLAMA_KV_CACHE_TYPE` are **server-global
environment configuration**, not per-request options (verified-upstream:
`envconfig/config.go`; the FAQ calls KV-cache quantization "currently a
global option"). The paired-arm harness on this branch
(`runtime_bench/paired.py`, schema v2) interleaves per-request option
arms against one already-running server. That design cannot measure
server-level arms at all: both arms would see whatever env the one
server was started with.

The stakes are already measured: the flash-attention + q8_0-KV
exploratory finding (10.1 → 23.5 tok/s on llama3.2:3b under VRAM
pressure) is exactly the class of result blocked from promotion because
it was taken as a single-arm before/after against a hand-restarted
server, with a missing GPU snapshot and an unexplained system-RAM dip
(`capabilities.py` `MEASURED_FINDINGS`, `LOCAL_RUNTIME_BENCHMARKS.md`).
Uplift-strategy Stage B (§9) requires re-running that arm under
paired-discipline conditions — which requires controlled, repeatable,
benchmark-owned servers.

Restarting the user's ordinary Ollama service with modified env is
ruled out on three grounds:

1. **Mutation.** On Windows the supported configuration path is
   *user-account environment variables* plus a tray-app restart
   (verified-upstream: FAQ). Changing those mutates the user's
   persistent environment — forbidden by the standing constraints
   (no hidden heavy work, no config mutation without a user-visible,
   reviewed apply phase).
2. **Ownership.** The tray app manages the ordinary server's lifecycle
   (verified-upstream: troubleshooting docs). A benchmark that stops or
   restarts it is racing an agent that may restart it mid-session.
3. **Attribution.** A shared server serves other clients; residency,
   VRAM, and keep-alive state would be contaminated by traffic the
   harness does not control.

The mechanism is therefore: **short-lived, loopback-only, benchmark-
owned `ollama serve` child processes**, one per server configuration,
using the installed binary, an isolated temporary home, and (for
inference phases) a disposable shadow view of the existing model
store — with the ordinary service left untouched.

---

## 2. Verified current Ollama behavior (sources)

All items in this section are verified-upstream on 2026-07-19 from
docs.ollama.com (FAQ, API reference, troubleshooting) and the
`ollama/ollama` source (`envconfig/config.go`, `llm/server.go`,
`llm/llama_server.go`, `cmd/cmd.go`, `server/routes.go`, repo file
listing).

### 2.1 Server environment variables (envconfig/config.go)

| Variable | Default | Relevance here |
| --- | --- | --- |
| `OLLAMA_HOST` | `127.0.0.1:11434` | per-process bind address → one isolated server per port |
| `OLLAMA_MODELS` | `%USERPROFILE%\.ollama\models` (Windows) | pointed at a per-session store root (§3.4) |
| `OLLAMA_FLASH_ATTENTION` | false/auto | arm variable |
| `OLLAMA_KV_CACHE_TYPE` | `f16` (`q8_0`, `q4_0` allowed) | arm variable; global option |
| `OLLAMA_KEEP_ALIVE` | 5 m; negative = infinite; per-request `keep_alive` overrides | session residency policy |
| `OLLAMA_NUM_PARALLEL` | 1 | pin to 1 for determinism |
| `OLLAMA_MAX_LOADED_MODELS` | 0 (auto) | pin to 1 |
| `OLLAMA_MAX_QUEUE` | 512 | pin to 1; queueing is contamination |
| `OLLAMA_LOAD_TIMEOUT` | 5 m | bounds a stalled load |
| `OLLAMA_NOPRUNE` | **false** | **critical**: a starting server prunes unreferenced blobs in its model store; every isolated child sets `OLLAMA_NOPRUNE=1`, and the shadow store (§3.4) makes even a misparsed value harmless |
| `OLLAMA_DEBUG` | INFO (0), DEBUG (1), TRACE (2) | INFO is sufficient for attestation (§2.3) |

The env package exposes `Values()`/`AsMap()` used to report the whole
effective configuration at startup — part of the attestation surface.

### 2.2 Runner subprocess model (llm/llama_server.go)

- Models are served by a **subprocess** the Ollama server spawns (on
  current upstream `main`, the upstream `llama-server` binary found
  under `lib/ollama/`; the installed line may name its runner
  differently — attested at first launch, §7). The Ollama process a
  benchmark launches is therefore a **process tree**, and cleanup must
  be tree-wide (§4).
- The runner binds **`127.0.0.1` only**, on a port obtained by
  listening on `localhost:0` (ephemeral) with a random-port fallback;
  it is started with `--no-webui` and `--offline`.
- Flash attention is passed as `--flash-attn on|off|auto`: `on` only
  when explicitly enabled *and* `ml.FlashAttentionSupported(gpus)`
  holds; `off` when disabled or unsupported; `auto` by default. A
  requested `on` can therefore be silently downgraded — which is
  precisely why requested ≠ attested (§6).
- KV-cache type is passed as `--cache-type-k <t> --cache-type-v <t>`
  when non-empty.
- The full runner command line is logged at INFO
  (`starting llama-server` with the complete `cmd`) — an attestation
  *source*, parsed in memory only; command lines are never persisted
  (§5, §6).

### 2.3 Observability surface

- Startup logs: `ollama serve` run as a child writes logs to its own
  stdout/stderr, captured directly by the parent — no shared log file
  with the ordinary service (whose tray-managed instance writes
  `%LOCALAPPDATA%\Ollama\server.log`).
- `GET /api/version` → server version.
- `GET /api/ps` → per loaded model: `name`, `model`, `size`, `digest`,
  `details` (format, family, `parameter_size`, `quantization_level`),
  `expires_at`, `size_vram`, `context_length`.
- Preload without generating: POST `/api/generate` with only `model`.
  Unload: same call with `"keep_alive": 0`.
- No API field reports flash-attention or KV-cache-type state; those
  are attestable **only** from startup/runner logs (§6).

### 2.4 Model store semantics

- The store is a directory of content-addressed blobs plus small
  manifests; serving reads blobs, while writes occur on
  pull/create/delete/push — and on **startup pruning** of unreferenced
  blobs unless `OLLAMA_NOPRUNE=1` is set (§2.1). No store-level lock is
  documented for concurrent readers. The design therefore never gives
  a child write-reachable access to the original store at all in the
  preferred path (§3.4), and verifies non-mutation by digest
  comparison rather than trusting configuration.

### 2.5 Windows configuration inheritance

Because the supported Windows configuration mechanism is user-account
environment variables, **the harness process itself inherits the
user's `OLLAMA_*` variables** — and, like any process, the user's
proxy settings and application secrets. A child environment built by
cloning the parent would leak all of it. The child environment is
therefore constructed deny-by-default (§3.2): nothing is inherited;
every variable present is individually justified.

### 2.6 Runtime identity binding (binding rule)

No version string is hardcoded anywhere in this design — not 0.32.1
(the version the reviewed PR #37 result records), not upstream `main`.
Instead, **every session binds one normalized runtime identity**:

1. resolved executable **basename** (e.g. `ollama.exe` — the basename
   only; the path is used transiently and never persisted, §5);
2. executable **SHA-256** (hashed from the resolved file before
   launch and re-hashed at suspended-image verification);
3. the strictly normalized **client executable version**, parsed from
   the owned post-readiness `ollama --version` child through the
   reviewed version-output dialect (§2.7);
4. the strictly normalized **command-reported server version**, parsed
   from that same output through the same dialect;
5. the strictly normalized `/api/version` value obtained from the
   owned child endpoint.

Values 3–5 are all required and must be equal after normalization.

**Command-version identity sequence (ordered, post-readiness).**
`ollama --version` is a client-server probe, not executable metadata
(§2.7): it contacts `OLLAMA_HOST` and reports both client and server
versions. It therefore runs only against an endpoint this harness has
already proved it owns, in this exact order:

1. resolve the executable; compute its basename and SHA-256;
2. select the reviewed **startup dialect and version-output dialect**
   for that hash from the closed committed registry (§6) — an unknown
   hash fails `attestation_dialect_unavailable` before any child
   process exists;
3. create the isolated **server** child suspended;
4. verify the suspended process image and re-hash the executable;
5. assign the kill-on-close Job Object and verify the assignment;
6. resume the server;
7. verify the owned loopback TCP endpoint under the address-aware
   ownership model (§3.3);
8. obtain a bounded `/api/version` response from the owned endpoint;
9. verify endpoint ownership again;
10. only then launch a **separately Job-owned** `ollama --version`
    child, built with the same minimal deny-by-default environment
    construction and with `OLLAMA_HOST` set to the already-owned
    server endpoint;
11. capture at most 4 KiB of combined stdout/stderr under an absolute
    deadline;
12. parse the capture through the reviewed installed-binary
    version-output dialect into the **client executable version** and
    the **command-reported server version**;
13. require both to equal the normalized `/api/version` value.

The version child retains every isolation property of the server
child: its own temporary home; its own temporary **empty** model
store; the exact NUL/stdout/stderr three-handle inherited-handle list
(§4.1); a hard absolute wall-clock deadline; suspended-image
verification and executable re-hash before resume; complete verified
Job/tree/handle/temporary-space cleanup; and no raw output
persistence. Plain `subprocess.run` remains forbidden.

**No harness-created process may ever be pointed at port 1, a guessed
port, or any endpoint whose ownership has not been proved under §3.3.**
Revision 5's pre-server version probe against an arbitrary unused
endpoint is withdrawn: §2.7 shows such a run yields no server version,
emits warning lines a strict parser must not see, and — if anything
listens on the arbitrary endpoint — contacts a socket the harness
never proved it owns.

**Fail-closed handling (closed categories, §9):**

| Condition | Category |
| --- | --- |
| missing client version — the reviewed dialect can neither find an explicit client-version line nor derive the client version by its reviewed rule | `version_output_malformed` |
| missing command-reported server version | `version_output_malformed` |
| the dialect's connection-warning marker is present (the child could not reach the owned endpoint) | `version_endpoint_ownership_failed` |
| output unparseable under the reviewed dialect (unexpected lines, invalid encoding, over-cap length) | `version_output_malformed` |
| client / command-server / API disagreement after normalization | `runtime_identity_mismatch` |
| endpoint ownership changed, ambiguous, or uncertain at the recheck immediately before the version child starts | `version_endpoint_ownership_failed` |

No universal version-output format is assumed. Which lines exist, how
the client and server versions are derived (including a dialect rule
such as "a lone server line with no client warning attests client =
server"), and the exact connection-warning marker are properties of
the installed binary, bound through the closed SHA-256 dialect
registry — which remains empty until installed-binary evidence is
independently reviewed (§6, §7).

The child server's startup-log version remains optional because log
shape is not a stable API. If the fixture-reviewed startup dialect for
the attested executable hash yields a parseable startup version, it is
normalized and must match; if it does not, the artifact records the
typed state `unattested`, and absence alone does not fail an otherwise
valid identity bind. A required disagreement, a parseable
startup-version disagreement, or a hash/basename change is
**`runtime_identity_mismatch`** and invalidates the session, fail
closed. Log-shape and flag-spelling expectations are fixture-driven per
attested identity, never assumed from upstream `main` (consistent with
this branch's fail-closed metadata rules, commits
`0ccad259`…`b5308a4a`).

### 2.7 Version-command semantics (verified-upstream)

Current upstream `cmd/cmd.go` `versionHandler`:

- builds its client via `api.ClientFromEnvironment()` — i.e.
  **`ollama --version` reads `OLLAMA_HOST`** and calls that server's
  `/api/version`;
- server reachable: prints `ollama version is <serverVersion>`, and
  adds `Warning: client version is <clientVersion>` only when the two
  differ;
- server unreachable: prints
  `Warning: could not connect to a running Ollama instance` plus
  `Warning: client version is <clientVersion>`, and still exits 0 —
  connection failure is not reflected in the exit code.

`/api/version` itself is registered for GET and HEAD and returns
`{"version": <serverVersion>}` (`server/routes.go`).

Consequences bound into this design: the version command is a
client-server identity probe, never local executable metadata; it must
not run before the isolated server is ready and owned; its exit code
proves nothing about connectivity; and its output dialect is a
property of the installed binary, bound through the SHA-256 registry
(§2.6), never assumed universal.

---

## 3. Isolation mechanism

### 3.1 What is launched

One **isolated server** = one child `ollama serve` process, spawned
from the same installed binary the ordinary service uses (identity
bound per §2.6), owned by the harness, bound to
`127.0.0.1:<benchmark port>`, alive only for the duration of one
server session (§8) or one attestation launch (§7).

Loopback-only is enforced twice: `OLLAMA_HOST=127.0.0.1:<port>` on the
child, and the existing `require_loopback_endpoint()` guard on every
client call (`paired.py` already refuses non-loopback endpoints). The
runner subprocess is loopback-only by upstream construction (§2.2).

### 3.2 Minimal explicit child environment (deny-by-default)

The child environment is **constructed from empty**, never cloned. By
construction this removes everything not explicitly listed — in
particular all inherited `OLLAMA_*`, `HTTP_PROXY`, `HTTPS_PROXY`,
`ALL_PROXY` and other proxy variants (`NO_PROXY`, lowercase forms,
`WSS_PROXY`, etc.), cloud/API tokens, and any other application
secrets the harness process happens to hold. The construction is the
guarantee; the named removals above document intent, not mechanism.

**Minimum Windows variables retained**, each with its reason:

| Variable | Value | Why it is needed |
| --- | --- | --- |
| `SystemRoot` | inherited value | required by Windows API/DLL resolution and Winsock initialization; processes malfunction without it |
| `SystemDrive` | inherited value | path resolution for system components |
| `PATH` | explicit minimal list: the Ollama install directory (and its runner `lib` subdirectory) + `%SystemRoot%\System32` | the server must locate its bundled runner/backend libraries and system/GPU driver DLLs (`nvcuda.dll` etc. live in System32); nothing else is on the path |
| `USERPROFILE` | **temporary per-session directory** | Ollama derives its home (`.ollama`: keys, default store) from the profile; a temp profile means first-run artifacts (e.g. a generated `id_ed25519`) land in disposable space, never in the user's real home |
| `HOMEDRIVE` / `HOMEPATH` (and `HOME` where relevant) | pointing into the same temporary profile | some Go/library code resolves home via these; they must agree with `USERPROFILE` |
| `TEMP` / `TMP` | **temporary per-session directory** | scratch writes stay in disposable space |
| `LOCALAPPDATA` | subdirectory of the temporary profile | any app-data writes stay disposable |

**Explicit settings** (the only other variables present):

- `NO_PROXY=127.0.0.1,localhost` — belt-and-braces against any proxy
  behavior reappearing via defaults;
- `OLLAMA_DEBUG_LOG_REQUESTS=0` — request bodies (prompts) must never
  enter logs;
- `OLLAMA_NO_CLOUD=1` — no cloud model routing;
- `OLLAMA_NOPRUNE=1` — no startup pruning (§2.1);
- the already-approved deterministic server values:
  `OLLAMA_HOST=127.0.0.1:<port>`, `OLLAMA_MODELS=<per-session store
  root, §3.4>`, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_MAX_LOADED_MODELS=1`,
  `OLLAMA_MAX_QUEUE=1`, `OLLAMA_DEBUG=0`;
- per arm, drawn from the separate closed `USER_OVERRIDE_ENV_KEYS`
  allowlist only:
  `OLLAMA_FLASH_ATTENTION`, `OLLAMA_KV_CACHE_TYPE`,
  `OLLAMA_KEEP_ALIVE` (and, if an experiment declares it,
  `OLLAMA_CONTEXT_LENGTH`).

For the version child (§2.6), `OLLAMA_HOST` is set to the already-owned
server endpoint; every other rule above applies unchanged, with the
child's own temporary profile and empty store.

The launcher defines `FIXED_INTERNAL_ENV_KEYS` as the closed set of every
minimum Windows variable and explicit fixed setting listed above. These
keys are generated exclusively by the launcher and cannot be provided or
overridden by a caller. `USER_OVERRIDE_ENV_KEYS` is the four-key narrow
experiment subset above; schema-v2 `SERVER_ENV_KEYS` remains a recording
allowlist and is not the child-process environment contract. A caller
attempting to supply a fixed key (case-insensitively on Windows), or any
key outside the user-override set, is rejected before temporary-space
creation, executable probing, socket allocation, or process creation.
The temporary profile and temp directories are deleted at teardown; their
paths are never persisted (§5). Phase 1 normally supplies no user
overrides.

### 3.3 Port ownership — address-aware, verified before first contact

- **Candidate selection.** A candidate port is chosen by probe-binding
  a fresh randomized dynamic-range loopback port, then closing the
  probe socket; the child is launched with that port. The ordinary
  service's endpoints — whatever they are (§3.5) — are excluded from
  candidate selection. No fixed port number, including 11434, is
  assumed or hardcoded as *the* ordinary port; discovery decides.
- **Address-aware listener classification.** Every ownership question
  is answered from the Windows IPv4 listener table
  (`GetExtendedTcpTable` with `TCP_TABLE_OWNER_PID_LISTENER`,
  read-only; each `MIB_TCPROW_OWNER_PID` row carries state, local
  address, local port, remote address, remote port, and owning PID,
  with the local address and port in network byte order). For the
  requested endpoint `127.0.0.1:<port>`, every row is classified as
  exactly one of:
  - **exact loopback listener** — local address `127.0.0.1`, local
    port `<port>`; relevant;
  - **wildcard listener** — local address `0.0.0.0`, local port
    `<port>`; relevant, because a wildcard bind also covers loopback;
  - **unrelated concrete-interface listener** — same port on another
    concrete local address; never relevant, and never mistaken for the
    benchmark endpoint;
  - **irrelevant row** — any other port.
  The ownership query returns **all** relevant rows with their owner
  identities — never an arbitrary first PID, and wildcard and loopback
  rows are never silently collapsed into one another.
- **Closed ownership result model.** Every ownership probe resolves to
  exactly one of:
  - **OWNED** — exactly one relevant row, and its owner is the child
    process or a member of the benchmark Job Object;
  - **NOT_PRESENT** — zero relevant rows; interpreted by lifecycle
    stage (not yet ready during readiness; closed during the closure
    check);
  - **FOREIGN** — exactly one relevant row, not benchmark-owned; its
    stable identity is captured (below);
  - **AMBIGUOUS** — more than one relevant row (including wildcard
    plus loopback coexisting, whoever owns them) →
    `port_ownership_ambiguous`, fail closed;
  - **UNCERTAIN** — the table cannot be read or an owner cannot be
    resolved with checked read-only APIs →
    `ownership_probe_unavailable`, fail closed.
  An HTTP request is sent only from the OWNED state; ownership is
  verified before the first request, re-verified before the version
  child starts (§2.6), and re-verified again before `/api/ps` (§7).
- **Stable foreign-owner identity.** Bind-race evidence never relies
  on PID equality alone — PIDs are reusable. When a FOREIGN result is
  captured during readiness:
  - *capture*: open the owner with a checked
    `PROCESS_QUERY_LIMITED_INFORMATION` handle and read its creation
    time (`GetProcessTimes`). The identity is the (PID, creation time)
    pair pinned by the **held-open handle**: while the handle is open
    the kernel process object persists and Windows cannot recycle the
    PID. If the handle or creation time cannot be obtained →
    `owner_identity_unavailable`, fail closed.
  - *comparison during race cleanup*: re-run the classification; the
    result must be exactly one relevant foreign row whose PID matches,
    whose freshly re-queried creation time is identical, and whose
    held handle is still unsignaled (a signaled handle means the
    original owner exited).
  - *PID-reuse detection*: a same-PID row with a different creation
    time, or a signaled held handle while a same-PID row exists, is a
    reused PID → `owner_identity_changed`.
  - *handle lifetime*: the identity handle is closed with a checked
    result at the end of the attempt's endpoint-closure state
    (§12 S19), whether the attempt retries or fails.
  - *privacy*: PID, address, port, handle value, and creation time are
    memory-only and never persisted.
- **Closed race/closure outcomes:**
  - **unchanged foreign identity**, plus proof that every
    benchmark-owned resource is gone (terminated tree, no descendants,
    no pending I/O, closed handles, no benchmark-owned relevant row,
    removed temporary space) → the *only* condition permitting a
    retry: one transition back to candidate endpoint selection with
    the raced port excluded, under the bounded outer attempt count;
  - **changed foreign identity** → `owner_identity_changed`, fail
    closed, no retry;
  - **PID reused by a new process** → `owner_identity_changed`, fail
    closed, no retry;
  - **owner vanished** (zero relevant rows where the captured foreign
    owner was expected) → `owner_identity_changed`: the identity can
    no longer be proven unchanged, so the attempt fails closed rather
    than retrying;
  - **multiple relevant owners** → `port_ownership_ambiguous`, fail
    closed;
  - **benchmark/job-owned listener** remaining at closure →
    `port_not_closed`;
  - **ownership probe uncertainty** → `ownership_probe_unavailable`
    (or `owner_identity_unavailable` for the identity step), fail
    closed.
- **Post-teardown closure.** After teardown the classification must
  return NOT_PRESENT for the benchmark endpoint; a benchmark-owned
  survivor is `port_not_closed` (§4). The only tolerated non-empty
  result is the unchanged proven foreign owner of the race-cleanup
  path above.
- A retry is only ever the proved bind race defined above. Job,
  identity, log, attestation, and arbitrary process failures are never
  retried. Exhaustion reports `port_bind_failed` with the bounded
  outer attempt count.

### 3.4 Model store design — disposable shadow store (preferred)

For inference phases (never for the first launch, §7), the child sees
a **disposable shadow store**, not the user's store:

1. Create a shadow root inside the session's temporary space.
2. Copy only the **small manifest files** of the models under test
   (manifests are kilobytes).
3. **Hard-link** the required content-addressed blobs into the shadow
   blob directory — after verifying support: the shadow root and the
   original store must be on the **same volume**, the filesystem must
   support hard links (NTFS), and a probe link must succeed. Nothing
   multi-gigabyte is ever copied; a hard link adds zero bytes.
4. Point `OLLAMA_MODELS` at the shadow root.
5. After child termination: delete the links and manifests, then the
   shadow root. Unlinking a hard link removes only the directory
   entry — the original store's data is untouched by construction.
   Even a worst-case child that pruned or deleted inside its store
   could only remove *shadow entries*, never original files.
6. Fingerprint the original store (manifest set + manifest digests of
   the models under test, read-only) before the first session and
   after the last; any difference is `model_store_mutation` and
   invalidates the batch — this now guards against *external* writers
   (a concurrent `ollama pull`), since the child cannot reach the
   original.
7. Neither the original store path nor the shadow path is persisted
   in any artifact (§5).

**Fail closed**: if safe links cannot be created — different volume,
non-NTFS filesystem, link probe failure, insufficient rights — the
batch refuses to start with `shadow_store_unavailable`. There is no
silent fallback to copying (disk cost) or to the shared store.

**Direct shared-store use is a separately approved fallback** (gate
G-ISO-3, §11): permitted only after the empty-store attestation launch
(§7) has attested, against the installed binary's own startup config
report, that `OLLAMA_NOPRUNE=1` is parsed and honored — and only with
the maintainer's explicit approval of that residual-risk trade.

### 3.5 The ordinary service — discovered, probed read-only, never touched

The harness never assumes where (or whether) the ordinary service is
listening:

1. **Discovery** (read-only): enumerate processes via
   `CreateToolhelp32Snapshot`; identify Ollama-family processes not
   owned by the harness job. Map their PIDs to listening loopback
   endpoints via the TCP table. This yields the set of *identified
   ordinary endpoints* — 11434 if that is where it lives, any other
   port if not, empty if the service is not running.
2. **Probing**: only identified loopback endpoints are probed, and
   only with read-only `GET /api/ps` / `GET /api/version`. No request
   is ever sent to an assumed port.
3. **Preflight**: if any ordinary endpoint reports a loaded model, the
   run refuses to start (`ordinary_service_busy`). A human may quiesce
   it (`ollama stop <model>`, or quitting the tray app) — the harness
   never does (gate G-ISO-2). An idle or absent ordinary service is
   recorded as ambient state and tolerated.
4. **Re-probe** after every session; a model appearing mid-batch marks
   the overlapping sessions `ordinary_service_activity` (invalid).
5. **Fail closed on uncertainty**: if discovery cannot establish the
   ordinary service's state — enumeration denied, a non-owned
   Ollama-family process with no readable endpoint, an endpoint that
   stops answering — the state is `ordinary_service_state_unknown`
   and the affected sessions are invalid. Unknown is never treated as
   idle.
6. The harness never stops, unloads, restarts, signals, or otherwise
   mutates any process it did not create, under any failure mode.

---

## 4. Windows process-tree ownership and cleanup

The child is a tree (`ollama serve` → runner subprocess, §2.2). Rules:

1. **Job object ownership.** Both the version child and server child
   are created suspended, assigned
   to a dedicated Windows Job Object with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, then resumed (all via ctypes;
   stdlib-only). Every process the server spawns lands in the job. The
   assignment is verified (`job_assignment_verified`) before any
   further step. If the harness exits for any reason — including being
   killed — the OS closes the job handle and the entire tree dies with
   it. No orphan survives a harness crash.
   Process creation uses `STARTUPINFOEX`,
   `PROC_THREAD_ATTRIBUTE_HANDLE_LIST`, and
   `EXTENDED_STARTUPINFO_PRESENT`; the explicit inheritance list contains
   exactly a benchmark-opened read-only Windows NUL stdin handle and the
   stdout and stderr write ends of the two benchmark-created overlapped
   pipes (§5). All three are explicitly inheritable; the parent-side
   overlapped read handles are never inheritable. The parent terminal
   stdin and every unrelated handle are absent. All three
   `STARTF_USESTDHANDLES` fields match these valid handles, and the
   parent-held child-side NUL/writer handles plus attribute
   list are closed with checked results immediately after process
   creation, so pipe EOF is observable.
2. **Graceful-first shutdown.** End of session: unload via
   `keep_alive: 0`, poll `/api/ps` until empty (bounded, reusing the
   existing 30 s cancel-poll discipline), then terminate the job.
   Record the shutdown method (`graceful`, `terminated`, `job_killed`)
   and the unload-to-exit wall time.
3. **Orphan verification.** After job termination, enumerate processes
   (read-only): any survivor whose parent chain led into the job is
   `orphaned_runner` — the batch aborts (a live orphan would
   contaminate every later session's RAM/VRAM baseline). Matching is
   by parentage and job membership, never by image name alone —
   name-based cleanup could hit the user's ordinary service and is
   forbidden.
4. **Port closure.** The address-aware classification is re-run
   (§3.3); a benchmark-owned listener remaining on the benchmark
   endpoint is `port_not_closed` and aborts the batch.
5. **Temporary-space teardown.** The session's temporary profile,
   temp dirs, and shadow store are removed; failure to remove is
   recorded (`teardown_incomplete`) but does not retroactively
   invalidate completed measurements — it blocks the *next* session
   until resolved.

---

## 5. Bounded overlapped log I/O and privacy-safe artifacts

**Selected design: benchmark-owned overlapped pipe reads under
absolute deadlines; no helper threads, no helper processes.** An
undrained pipe on Windows blocks the writer, so stdout and stderr must
be drained continuously — but revision 5's blocking reader threads
ended in an unbounded containment join, which contradicted the
hard-deadline contract. Revision 6 removes the threads entirely:

- **Pipe construction.** Each of stdout and stderr is a
  benchmark-created single-instance named pipe with a unique random
  name (`PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED |
  FILE_FLAG_FIRST_PIPE_INSTANCE`, byte mode,
  `PIPE_REJECT_REMOTE_CLIENTS`, one instance) — the standard Windows
  technique for cancellable anonymous-style pipes. The child's write
  end is opened inheritable via `CreateFileW` and is exactly the
  writer entry in the three-handle inheritance list (§4.1); the
  parent's overlapped read handle is never inheritable. The pipe name
  contains a random component, is memory-only, and is never persisted.
  Creation or connection failure is `log_io_setup_failed`.
- **Continuous draining.** One overlapped `ReadFile` is kept pending
  per stream, each with its own manual-reset event. The single
  lifecycle thread waits with `WaitForMultipleObjects` on
  {stdout event, stderr event, process handle}, with the timeout
  computed from the governing absolute deadline. A completion is
  confirmed with checked `GetOverlappedResult`, its bytes are fed to
  the bounded capture, and the next read is posted;
  `ERROR_BROKEN_PIPE` marks that stream EOF. A read completing with
  any other unrecoverable error is `log_read_failed`. No Python helper
  thread exists anywhere in the I/O path; the version child uses the
  same mechanism.
- **Caps.** Server logs: first 64 KiB + last 192 KiB retained,
  256 KiB total diagnostic cap, truncation flag; bytes beyond the cap
  are counted and discarded. Overflow before readiness is
  `startup_log_overflow`. Version child: 4 KiB combined cap; overflow
  is `version_probe_output_overflow`.
- **Absolute deadlines.** Readiness (default 30 s, measured from
  immediately before `ResumeThread` — excluding hashing,
  temporary-space creation, and suspended process/job setup), startup
  attestation (separate clock, default 10 s), version binding
  (default 10 s), and cleanup each have an absolute wall-clock
  deadline computed once; every wait in the lifecycle uses
  max(0, deadline − now). **No unbounded wait or join exists anywhere,
  including cleanup.**
- **Cancellation sequence (cleanup, in order).**
  1. terminate the Job Object and wait, bounded, on the process
     handle;
  2. drain any already-completed reads (checked
     `GetOverlappedResult`);
  3. for each still-pending read: checked `CancelIoEx(handle,
     &overlapped)` against exactly that pipe handle and operation
     (`ERROR_NOT_FOUND` — already complete — is acceptable);
  4. wait, bounded by the absolute cleanup deadline, on that
     operation's event, and confirm via `GetOverlappedResult` that the
     operation finished or aborted (`ERROR_OPERATION_ABORTED`);
  5. close pipe and event handles with checked results;
  6. close thread, process, and job handles with checked results.
- **Proof that nothing remains.** The design owns zero helper threads
  and zero helper processes, and step 4 confirms every pending
  operation individually — after it, no pending I/O references any
  buffer, and there is no reader to contain.
- **When cancellation itself fails.** A failed `CancelIoEx` is
  `io_cancellation_failed`; an operation not confirmed complete by the
  absolute cleanup deadline is `pending_io_cleanup_timeout`. Both are
  fixed cleanup failures: the attempt is **never retried**, evidence
  is **never declared complete**, the affected buffers and OVERLAPPED
  blocks stay referenced for the remaining process lifetime (never
  freed or reused, so a late completion cannot corrupt memory), and
  the CLI exits nonzero. The kill-on-close Job Object has already
  destroyed the child tree, and the design owns no helper — so no
  uncontrolled benchmark-owned helper can be left behind. There is no
  "containment" operation hiding an infinite wait.

API readiness is not attestation readiness. After ownership and
`/api/version` succeed, the separate bounded attestation clock
(default 10 s) continues draining logs until every mandatory pattern in
the reviewed dialect is observed. Process exit, overflow, and read
failure remain fatal during this interval. Deadline expiry yields
`attestation_missing` with bounded timeout metadata.

**Raw logs are never persisted.** They are parsed in memory into typed
attestation records (§6) and then discarded. Durable artifacts contain
**no** PID, no port number, no executable path, no model-store or
shadow-store or temporary-profile path, no pipe name, no raw log
excerpts, and no command lines.

Phase 1 has its own standalone closed artifact; it does not implement or
reuse performance schema v3:

- `schema_version: 1`;
- `artifact_kind: isolated_ollama_server_attestation`;
- captured UTC timestamp;
- runtime identity: executable basename, executable SHA-256, normalized
  client executable version, normalized command-reported server
  version, normalized API version, and the optional typed
  startup-version state/value/source;
- requested typed settings, using only `loopback: true` and
  `store_kind: empty_temp` markers for the generated endpoint/store;
- attested typed settings and their closed sources;
- endpoint-owner, job-assignment, and `model_residency_verified_empty` booleans;
- bounded log counts and truncation state;
- readiness duration;
- shutdown method and duration;
- orphan verification, port closure, and temporary-space teardown;
- fixed failure categories with only closed bounded numeric metadata;
- overall diagnostic evidence state.

The schema contains no PID, port, absolute path, environment secret, raw
command output, raw log, command line, prompt, or generated output.

The existing write-time redaction sentinel applies on top as
defense-in-depth; with this schema there should be nothing left for it
to catch.

---

## 6. Requested versus attested server configuration

**Requested** = the settings dict from §3.2. It proves intent, not
effect: Ollama may downgrade flash attention when the backend does not
support it (§2.2), silently normalize values, or spell flags
differently across versions.

**Attested** = what the running server demonstrably did, parsed in
memory from the capture and the API, persisted only in typed form:

Startup and version-output parsing is selected only through a closed,
committed registry keyed by executable SHA-256. Each entry carries
**both** the startup dialect (config-report and runner-line patterns)
and the **version-output dialect** (which lines exist, how the client
and command-reported server versions are derived, and the
connection-warning marker — §2.6/§2.7). Registry construction validates
that every entry's key and dialect identity match; patterns are
compiled, bounded in length, contain exactly one capture group, use
only allowlisted setting names and sources, and include mandatory
Phase 1 `noprune` and `no_cloud` markers. Caller-supplied regexes or
fixture paths are forbidden. An unknown hash is rejected before any
child process is created as `attestation_dialect_unavailable`. The
empty registry in revision 6 is intentional: it prevents the CLI from
implying that G-ISO-0 can complete before the installed binary's
dialects have been independently reviewed and committed.

| Typed field | Source | Attests |
| --- | --- | --- |
| `runtime_identity` (§2.6 normalized bind) | file hash, version command (client and command-reported server), `/api/version`, optional startup log | one specific binary served this session |
| `endpoint_owner_verified` | address-aware TCP classification, pre-first-request (§3.3) | it is *our* server |
| `model_residency_verified_empty` | bounded owned `GET /api/ps` after config attestation | the isolated empty-store server has zero resident models |
| `effective_env_report` (typed subset) | startup config report (envconfig `Values()`) | env the server parsed — incl. noprune and no-cloud |
| `flash_attention_applied` (`on`/`off`/`auto`/`unattested`) | runner launch line | flag handed to the runner |
| `kv_cache_type_applied` (type or `unattested`) | runner launch line | flag handed to the runner |
| `model_identity` / `size_vram` / `context_length` / `expires_at` | `/api/ps` after load (inference phases only) | identity, placement, effective context, residency policy |

Binding rules (all fail-closed, consistent with this branch's
metadata rules):

1. Every artifact records requested and attested side by side; each
   attested field carries `source` ∈ {`startup_log`, `runner_log`,
   `api_ps`, `api_version`, `version_command`, `file_hash`,
   `tcp_table`} or the value `unattested`.
2. `unattested` on an arm-defining field (flash attention or KV type
   for a Stage-B arm) → `attestation_missing`: the session may
   complete for diagnostic value, but its artifact is ineligible for
   comparison and can never support promotion.
3. A conflict between requested and attested (asked `on`, runner got
   `off`) → `attestation_mismatch`: the session is invalid as the
   intended arm. This is the *expected honest outcome* whenever
   hardware does not support a feature — the mechanism exists to
   surface exactly that.
4. `flash_attn=auto` observed on a baseline arm is recorded as `auto`,
   never coerced: the comparison layer must see what was actually
   applied.
5. Any required normalized version disagreement (client,
   command-reported server, or API — §2.6), or a disagreement with a
   parseable startup-log version, → `runtime_identity_mismatch`,
   session invalid. An unparseable or absent startup-log version
   remains typed `unattested` and does not by itself invalidate
   identity.

---

## 7. First launch: empty-store attestation (gated, no inference)

The first real launch of an isolated server on the dev machine is a
dedicated **attestation launch**, separately human-approved (gate
G-ISO-0, §11), and deliberately inert:

**Separate prerequisite before approval/execution:** resolve and hash the
installed binary without launching it, independently review synthetic/raw
log and version-output samples outside this implementation run, and
commit a validated dialect entry (startup **and** version-output
dialects) for that exact SHA-256. Revision 6 deliberately does not
invent or register the installed binary's dialects. Until that
prerequisite lands, the real CLI fails `attestation_dialect_unavailable`
before process creation; the approval flag alone is insufficient.

- **temporary empty model store** (a fresh empty directory as
  `OLLAMA_MODELS`) — the user's model files are not exposed in any
  form, not even via shadow links;
- **temporary home/profile** (§3.2);
- **no model load, no inference** — the only requests are
  `/api/version` and `/api/ps` (expected empty).

Immediately before `GET /api/ps`, endpoint ownership is reverified with
the address-aware classification (§3.3) against the child/Job Object.
The response read is capped at 8 KiB and must have the exact closed
shape `{"models": []}`. Only the boolean
`model_residency_verified_empty` persists; model names, digests, sizes, raw
JSON, endpoint, port, and owner identity remain memory-only. Malformed or
oversized evidence is `model_residency_probe_failed`; any entry in `models` is
`unexpected_model_residency`. A complete G-ISO-0 artifact requires the boolean
to be true.

Its sole purpose is to prove the mechanism itself, on the installed
binary, before any model is ever involved:

1. the required normalized runtime identity binds with no mismatch
   (§2.6): client version, command-reported server version, and
   `/api/version` agree, with the startup-log version either matching
   or typed `unattested`;
2. the server binds loopback-only on the assigned port;
3. address-aware ownership verification works pre-first-request;
4. the startup config report shows `OLLAMA_NOPRUNE=1` and
   `OLLAMA_NO_CLOUD=1` parsed as requested (this attestation is the
   precondition for ever considering the shared-store fallback,
   §3.4);
5. log capture stays within bounds and parses into typed records;
6. Job Object teardown leaves zero survivors (orphan scan clean);
7. the bounded owned `/api/ps` proof reports zero resident models;
8. the benchmark endpoint has no listener after teardown.

Every proof lands in a typed attestation artifact (§5 rules apply —
no paths, no PIDs, no ports, no raw logs). Only after this artifact
exists and is reviewed may any inference-phase session plan even be
proposed under G-ISO-1.

---

## 8. Server-session benchmark topology

New unit, one level above schema v2's paired arms:

- **Server session** — spawn isolated server with one attested config
  → readiness (§5) → attestation (§6) → cold load run (first
  `/api/generate` on the model; the load is inside this request) →
  n ≥ 3 warm runs per shape (existing shapes, greedy sampling, fixed
  `num_predict`, unchanged) → unload → tree shutdown (§4) →
  post-verification. Ambient snapshots (available RAM, VRAM used,
  system CPU, ordinary-service probe per §3.5) bracket every session.
- **Priming sessions** — each batch begins with **one discarded
  priming session per server configuration** (baseline *and*
  candidate), whose only purpose is to bring the model's blobs into
  the Windows file cache and exercise each config's load path once.
  Priming **order is counterbalanced** across batches/blocks (A-first
  in one, B-first in the next), so neither arm systematically enjoys
  fresher cache state. Priming artifacts are written (nothing hidden)
  with `role: cache_priming, excluded_from_comparison: true`.
- **Block** — the comparison unit. One block =
  - two baseline sessions and two candidate sessions in ABBA or BAAB
    order (block 1: A,B,B,A; block 2: B,A,A,B; further blocks
    alternate; realized order recorded per session);
  - one **block-level baseline distribution** and one **block-level
    candidate distribution** per metric per shape (each session
    contributes its runs; distributions are summarized, e.g.
    median/min/max/n, in the closed schema);
  - one **block-level difference/ratio report** computed from those
    two distributions.
  There is **no session-to-session pairing** — no "session 2 vs
  session 3" — because no such pairing is principled; the block's two
  distributions are the only comparison. First-order time drift
  cancels because each arm occupies positions summing to the same
  rank total (1+4 = 2+3).
- **Batch** — ≥ 2 blocks, plus the priming sessions and the
  original-store fingerprint bracket (§3.4).
- **Cross-block rule**: block difference reports that disagree in
  direction are reported `inconsistent_across_blocks` — never pooled
  into one result. Averaging away a contradiction is the single-arm
  mistake at a higher level.

**Interference policy is external.** RAM-swing, foreign-VRAM-growth,
system-CPU, and elapsed-gap limits are **not hardcoded**: they live in
a closed-schema policy JSON reviewed and approved under G-ISO-1
(precedent: the existing `compare --policy` interference policy input
in `__main__.py`). Sessions breaching the approved policy are marked
`ambient_drift_exceeded` (or the policy's more specific category) and
invalidated; a batch run without an approved policy attached cannot
produce comparison verdicts at all.

**Schema v3 output**: one artifact per **server-session × shape**
(v2 arm-artifact shape plus the typed server-session evidence of §5),
connected by **privacy-safe opaque session and block IDs** (random
identifiers carrying no host information), plus one **closed parent
batch manifest** artifact per batch recording: the block structure and
realized orders, priming-session IDs, the policy identity (hash) it
ran under, the store fingerprint bracket result, and the
session-validity ledger (§9 categories per session). v1 and v2
artifacts remain valid and unchanged.

### 8.1 Cold model load versus warm requests

Each session yields exactly one **process-cold** load measurement (the
first request: server has no model resident, `load` timing populated)
and n warm request measurements. Two colds must never be conflated:

- *process-cold* — new server process, model not resident. Every
  session's first run is process-cold by construction. This is the
  cold the topology can honestly measure, and the cold that matters
  for keep-alive/residency policy (measured: 13–18× per-turn reload
  cost).
- *disk-cold* — model bytes absent from the Windows file cache too.
  **This topology cannot manufacture disk-cold**; no artifact from
  this mechanism may claim it.

Warm runs measure the steady state under the session's server config —
the actual Stage-B quantity. A structural benefit: because every arm
gets a fresh server, both arms are measured with equally fresh
prompt-cache state — warm-vs-warm is symmetric by construction.

### 8.2 Windows file-cache uncertainty

After any session touches a model's blobs, subsequent reads come
partly from the file cache; there is no supported user-level,
install-nothing way to drop the Windows file cache selectively, and
rebooting between sessions is not a viable protocol. The design
handles this honestly rather than pretending control: the per-config
priming sessions (§8) put every *measured* session — both arms, all
blocks — on file-cache-warm footing, with counterbalanced priming
order so neither arm benefits first. All measured sessions are labeled
`file_cache_state: warm`; the priming sessions themselves are
`unknown` (prior user activity is unobservable). Consequence, stated
plainly: this mechanism measures process-cold loads *from a warm file
cache*; true first-boot cold start stays explicitly unmeasured.

---

## 9. Contamination and failure categories (closed vocabulary)

Additive to the existing per-run `error_category` values (which remain
for in-run errors: timeouts, HTTP errors, safety floor). Closed set —
an unrecognized condition is a validation failure, not a new ad-hoc
string. Revision 6 renames `log_reader_failed` to `log_read_failed`
(there are no reader threads) and adds the version-endpoint,
address-aware-ownership, owner-identity, and overlapped-I/O categories.

| Category | Raised when | Invalidates |
| --- | --- | --- |
| `platform_unsupported` | required Windows lifecycle APIs are unavailable | launch |
| `executable_not_found` | configured executable cannot be resolved | launch |
| `executable_identity_unavailable` | executable hash or image identity cannot be established | launch |
| `attestation_dialect_unavailable` | executable hash has no closed committed reviewed dialect entry (startup + version-output) | launch before process creation |
| `temp_space_failed` | isolated profile, scratch, or empty store cannot be created | launch |
| `port_bind_failed` | no candidate port bound after the bounded proved-race retries | session |
| `process_create_failed` | checked suspended process creation fails | session |
| `process_attribute_list_failed` | STARTUPINFOEX handle-list sizing/initialization/update fails | launch |
| `process_attribute_list_cleanup_failed` | attribute-list or post-create handle cleanup cannot be completed | launch |
| `job_create_failed` | checked Job Object creation fails | session |
| `job_limit_configuration_failed` | kill-on-close limit configuration fails | session |
| `job_assignment_failed` | assignment or assignment verification fails | session |
| `process_resume_failed` | checked primary-thread resume fails | session |
| `log_io_setup_failed` | overlapped pipe/event creation, child-end open, or initial overlapped read post fails | launch |
| `ownership_probe_unavailable` | the TCP table or an owner cannot be read with checked read-only APIs | session |
| `port_ownership_ambiguous` | more than one relevant listener row (exact loopback and/or wildcard) for the benchmark endpoint (§3.3) | session |
| `owner_identity_unavailable` | a stable foreign-owner identity (checked handle + creation time) cannot be captured | session |
| `owner_identity_changed` | the foreign owner's identity changed, its PID was reused, or the owner vanished before race cleanup proved it unchanged | session |
| `port_hijacked` | the single relevant listener is foreign pre-request without a proved bind race (§3.3) | session |
| `port_not_closed` | a benchmark-owned listener remains on the benchmark endpoint after teardown (§4.4) | batch (aborts) |
| `startup_timeout` | endpoint not answering by the readiness deadline | session |
| `startup_process_exit` | child exits before readiness | session |
| `startup_log_overflow` | capture cap hit before readiness | session |
| `log_read_failed` | an overlapped stdout/stderr read completes with an unrecoverable error | session |
| `version_probe_timeout` | the owned version child exceeds its absolute deadline | session |
| `version_probe_output_overflow` | combined version output exceeds 4 KiB | session |
| `version_probe_failed` | the owned version child exits nonzero or emits no output | session |
| `version_output_malformed` | version output lacks a dialect-required client or server version or is unparseable under the reviewed dialect (§2.6) | session |
| `version_endpoint_ownership_failed` | endpoint ownership is lost, ambiguous, or uncertain at the pre-version-child recheck, or the dialect's connection-warning marker appears | session |
| `version_probe_cleanup_failed` | version-child tree/handle/pending-I/O/temporary cleanup cannot be proven | session |
| `io_cancellation_failed` | checked `CancelIoEx` against an exact pending pipe operation fails (§5) | session (cleanup failure; no retry) |
| `pending_io_cleanup_timeout` | a pending operation is not confirmed complete by the absolute cleanup deadline (§5) | session (cleanup failure; no retry) |
| `model_residency_probe_failed` | bounded owned `/api/ps` response is unavailable, malformed, or oversized | session |
| `unexpected_model_residency` | owned isolated `/api/ps` reports one or more resident models | launch attestation |
| `runtime_identity_mismatch` | required normalized versions disagree (client / command-server / API), or a parseable startup version disagrees (§2.6) | session; batch if the binary changed mid-batch |
| `attestation_missing` | arm-defining field `unattested` (§6.2) | session for verdicts |
| `attestation_mismatch` | requested ≠ attested on arm-defining field (§6.3) | session as intended arm |
| `ordinary_service_busy` | preflight: model loaded on an identified ordinary endpoint | batch never starts |
| `ordinary_service_activity` | re-probe: model appeared mid-batch | overlapping sessions |
| `ordinary_service_state_unknown` | discovery cannot establish ordinary-service state (§3.5.5) | affected sessions (fail closed) |
| `foreign_gpu_consumer` | ambient VRAM growth beyond approved policy, not attributable to session | session |
| `ambient_drift_exceeded` | pre/post ambient swing beyond the G-ISO-1-approved policy (§8) | session |
| `shadow_store_unavailable` | safe hard links impossible (volume/filesystem/rights) (§3.4) | batch never starts |
| `model_store_mutation` | original-store fingerprint changed across batch (§3.4.6) | batch |
| `unload_failed` | `/api/ps` non-empty after bounded unload poll | session (→ terminate path) |
| `orphaned_runner` | post-shutdown scan finds survivor (§4.3) | batch (aborts) |
| `unclean_shutdown` | graceful path failed, job kill needed | recorded; session valid only if all runs completed first |
| `teardown_incomplete` | temp profile/shadow store not fully removed, or a handle close fails (§4.5) | blocks next session |
| `session_incomplete` | fewer than the declared runs completed | session |
| `block_incomplete` | any of a block's four sessions invalid | the block |
| `inconsistent_across_blocks` | block difference reports disagree in direction (§8) | pooled verdict (reported, not averaged) |

Every Windows API return value is checked. Durable diagnostics expose
only this fixed vocabulary and closed bounded numeric metadata; raw Win32
messages, paths, handles, process identifiers, creation times, addresses,
and port numbers are never persisted.

Aggregation rule, unchanged in spirit from schema v2: invalid units
are excluded *and named* in the batch manifest's validity ledger; a
batch whose every block is invalid produces no verdict, not a weak
one.

---

## 10. Resource and safety envelope

- Exactly one isolated server at a time; with the idle ordinary
  service present, the machine briefly hosts two Ollama processes but
  only one loaded model — peak RAM/VRAM matches a normal benchmark
  run plus one idle server process (small; visible in the ambient
  snapshots).
- The existing safety floor (`max(1.5 GiB, 12 % of total RAM)`, held
  by value in `paired.py`) and abort semantics apply inside every run;
  additionally the spawn itself is preceded by an ambient check — if
  available RAM is already below floor + model estimate, the session
  is refused before any process starts.
- Wall-clock budget is declared up front in the experiment definition
  (sessions × (startup + cold + n·warm + shutdown)) and approved at
  G-ISO-1. For scale (measured bases): 3b cold ≈ 10 s, 8b cold
  28–41 s, warm runs sub-minute — an ABBA+BAAB llama3.2:3b batch is
  tens of minutes, not hours.

---

## 11. Human approval gates

Consistent with `V04_BATON_FOR_SMALLER_MODELS.md` §G and the parent
strategy §13; none delegable to implementation agents:

1. **G-ISO-0 — attestation-launch approval.** Before the first real
   isolated server ever runs on the dev machine: the maintainer
   approves the empty-store, no-inference attestation launch (§7).
   Phase 1 code existing does not imply permission to run it. A closed,
   reviewed dialect entry (startup + version-output) committed for the
   resolved executable SHA-256 is a prerequisite to seeking or
   exercising this approval; unknown hashes fail before process
   creation.
2. **G-ISO-1 — session-plan + policy approval.** Before any
   inference-phase batch: the experiment definition (model, arms,
   blocks, shapes, wall-clock budget, priming plan) **and the
   external interference policy** (§8) are reviewed and approved
   together. Re-approval per experiment definition.
3. **G-ISO-2 — ordinary-service quiescence.** The harness never stops
   the tray app or unloads its models. If preflight reports
   `ordinary_service_busy`, a human quiesces it manually (or
   declines) and re-runs.
4. **G-ISO-3 — shared-store fallback.** Direct use of the user's real
   model store (bypassing the shadow store) requires: the G-ISO-0
   artifact proving NOPRUNE parsing on the installed binary, a stated
   reason the shadow store is insufficient, and explicit maintainer
   approval of the residual risk.
5. **Existing gates unchanged.** G-THRESH still precedes any Stage-B
   verdict computed from these sessions; G-PROMOTE still gates any
   planner-visible rule; G-GREEN untouched. This document adds
   measurement capability, not promotion authority.

Standing constraints re-affirmed: no downloads (the client cannot
pull), no telemetry, loopback only, no raw private data in artifacts
(§5 removes whole categories of it; the redaction sentinel remains as
defense-in-depth), no new dependencies (ctypes is stdlib).

---

## 12. Phase 1 lifecycle state machine

The Phase 1 attestation lifecycle is this explicit ordered state
machine. States execute in order; the **only** backward edge in the
entire machine is S10 → S4 on a proved candidate-port race (§3.3),
under the bounded outer attempt count. Every other failure is fatal
for the attempt and follows the cleanup rule below. "Owned resources"
lists what the launcher holds once the state completes.

| # | State | Success → | Fatal failure categories (fail closed) | Retry | Owned resources after state |
| --- | --- | --- | --- | --- | --- |
| S0 | contract validation (overrides, timeouts, attempts, exclusions) | S1 | contract error (pre-lifecycle, typed `Phase1ContractError`; no artifact category) | no | none |
| S1 | executable resolution + basename + SHA-256 | S2 | `executable_not_found`, `executable_identity_unavailable` | no | none (path/hash memory-only) |
| S2 | dialect lookup (closed SHA-256 registry: startup + version-output dialects) | S3 | `attestation_dialect_unavailable` | no | none |
| S3 | temporary-space creation (profile, LOCALAPPDATA, scratch, empty store) | S4 | `temp_space_failed` | no | session space |
| S4 | candidate endpoint selection (probe-bind fresh loopback port, close probe socket) | S5 | `port_bind_failed` on exhaustion | re-entered only via the S10 race edge | session space |
| S5 | suspended server creation (overlapped pipes + events, NUL stdin, attribute list, `CreateProcessW` suspended; child-side handles closed checked) | S6 | `process_create_failed`, `process_attribute_list_failed`, `process_attribute_list_cleanup_failed`, `log_io_setup_failed` | no | + suspended process, pipe/event handles |
| S6 | executable revalidation (image query + file re-hash) | S7 | `runtime_identity_mismatch`, `executable_identity_unavailable` | no | unchanged |
| S7 | Job Object assignment (create, kill-on-close, assign, verify) | S8 | `job_create_failed`, `job_limit_configuration_failed`, `job_assignment_failed` | no | + job |
| S8 | log-I/O setup (post initial overlapped reads on both pipes) | S9 | `log_io_setup_failed` | no | + pending overlapped reads |
| S9 | resume (`ResumeThread`, checked; readiness clock starts immediately before) | S10 | `process_resume_failed` | no | running child tree |
| S10 | endpoint ownership readiness (address-aware classification loop, log draining, absolute readiness deadline) | S11 | `startup_timeout`, `startup_process_exit`, `startup_log_overflow`, `log_read_failed`, `port_hijacked`, `port_ownership_ambiguous`, `ownership_probe_unavailable`, `owner_identity_unavailable` | **only** proved race → S4 (after full attempt cleanup S16–S20 with no failures; raced port excluded) | running child tree |
| S11 | bounded `/api/version` against the OWNED endpoint (within the readiness deadline) | S12 | same categories as S10; malformed response → `runtime_identity_mismatch` | no | running child tree |
| S12 | command-version binding (§2.6: ownership recheck → own temp space → version child suspended → image verify/re-hash → own job → resume → ≤ 4 KiB capture under absolute deadline → dialect parse → client/command-server/API equality → verified probe cleanup incl. its temp space) | S13 | `version_endpoint_ownership_failed`, `version_probe_timeout`, `version_probe_output_overflow`, `version_probe_failed`, `version_output_malformed`, `runtime_identity_mismatch`, `version_probe_cleanup_failed` | no | running child tree (version-child resources are transient inside this state and fully cleaned before exit) |
| S13 | mandatory startup attestation (reviewed dialect markers incl. `noprune`/`no_cloud`; separate absolute attestation deadline) | S14 | `attestation_missing`, `attestation_mismatch`, `startup_process_exit`, `startup_log_overflow`, `log_read_failed` | no | running child tree |
| S14 | ownership revalidation (address-aware, must be OWNED) | S15 | `port_hijacked`, `port_ownership_ambiguous`, `ownership_probe_unavailable` | no | running child tree |
| S15 | bounded `/api/ps` (8 KiB cap; exact `{"models": []}`) | S16 | `model_residency_probe_failed`, `unexpected_model_residency` | no | running child tree |
| S16 | shutdown (terminate job; bounded wait on process handle) | S17 | `unclean_shutdown` (recorded) | no | dead tree; handles; pending I/O |
| S17 | pending-I/O cancellation (§5 sequence: drain, `CancelIoEx`, bounded confirmation, close pipe/event handles) | S18 | `io_cancellation_failed`, `pending_io_cleanup_timeout` | no | process/thread/job handles |
| S18 | orphan verification (parentage/job membership, read-only snapshot) | S19 | `orphaned_runner`, `ownership_probe_unavailable` | no | process/thread/job handles |
| S19 | address-aware endpoint closure (NOT_PRESENT required; race-identity comparison when applicable; close process/thread/job and any identity handles, checked) | S20 | `port_not_closed`, `port_ownership_ambiguous`, `owner_identity_changed`, `owner_identity_unavailable`, `ownership_probe_unavailable`, `teardown_incomplete` (handle close) | no | session space only |
| S20 | temporary-space teardown | S21 | `teardown_incomplete` | no | none |
| S21 | artifact validation (closed schema; `complete` only with zero failures and every proof true) | done | internal validation error (never a weakened artifact) | no | none |

**Cleanup rule (mandatory before exit).** A fatal failure in S5–S15
does not end the attempt: the lifecycle always proceeds through
S16 → S17 → S18 → S19 → S20 → S21, recording both the primary failure
and any cleanup failures, before the launcher returns. A failure in
S0–S4 runs only the cleanup states matching resources actually
acquired (S20 → S21 once the session space exists; S21 alone before
that). Cleanup states never loop back and are themselves bounded by
the absolute cleanup deadline (§5); exhausting it yields the fixed
cleanup failure, never a retry, and never `complete` evidence.

**Race edge, restated.** S10 → S4 fires only when §3.3's proved-race
conditions hold: the candidate was free at probe time, exactly one
relevant foreign listener with a captured stable identity now owns it,
the benchmark child exited promptly, the abandoned attempt's full
cleanup chain (S16–S20) completed with **no** failures, and the
closure check found the identical unchanged foreign identity. Anything
less fails closed with the matching category. Version-child failures
(S12), job failures, identity failures, log failures, and attestation
failures never re-enter S4.

---

## 13. Implementation ladder

The work is deliberately split so that no single agent task ever holds
both "can launch servers" and "touches model files":

- **Phase 1** — isolated child-server *lifecycle and attestation*
  against synthetic fixtures: temp home/store construction, minimal
  env, job objects, bounded overlapped log I/O, typed attestation,
  post-readiness identity binding, address-aware TCP ownership,
  readiness, shutdown, orphan and endpoint-closure verification. No
  model is ever loaded; no real server is launched by code or tests.
  Phase 1 exists as draft PR #38 (branch
  `codex/isolated-ollama-attestation-phase-1`, based on
  `research/hardware-relative-uplift-benchmark`); the revisions 3–5
  build prompts are superseded, and the single active implementation
  prompt is the revision-6 correction prompt in §15.
- **Gate G-ISO-0**, then the human-executed empty-store attestation
  launch (§7) using Phase 1's CLI.
- **Phase 2** — shadow-store construction (§3.4) + the
  ordinary-service discovery preflight (§3.5), still no inference.
- **Phase 3** — server sessions, priming, ABBA/BAAB blocks, schema v3
  artifacts + batch manifest (§8), behind G-ISO-1.
- **Phase 4** — block-level comparison and `inconsistent_across_blocks`
  reporting, feeding the existing compare pipeline.

Each later phase gets its own narrow prompt only after the previous
phase's review.

The real empty-store attestation launch (§7) is **not** part of
Phase 1: it happens only after Phase 1 review and an explicit G-ISO-0
approval, executed by a human using the reviewed CLI.

---

## 14. Non-goals

- No llama.cpp server sessions (the mechanism generalizes — llama.cpp
  flags are per-server too — but that is a later extension).
- No disk-cold measurement (§8.2), no cache-flush tooling, no reboot
  protocols.
- No change to the ordinary Ollama service, its configuration, its
  autostart, or its endpoint — ever, under any failure mode; no
  assumption about where that endpoint is.
- No scheduler/planner/UI integration; evidence produced here flows
  through the existing G-THRESH/G-PROMOTE gates like any other
  artifact.
- No new model downloads, dependencies, or background services; no
  durable host-identifying data in artifacts.

---

## Conclusion

Server-level configuration is the last measured-but-unpromotable
uplift lever on the reference machine: the flash-attention + q8_0-KV
result is real enough to matter (2.3× exploratory under VRAM pressure)
and unproven enough to be unshippable. The paired-arm harness solved
drift for per-request options; this mechanism extends the same
fail-closed discipline one level up — to the server process itself —
by making each server configuration a short-lived, loopback-only,
job-owned child with a disposable home and a disposable shadow of the
model store, which binds the identity of the binary it ran against its
own proven endpoint, attests what that binary actually applied,
persists only typed privacy-safe evidence, and dies provably clean
under absolute deadlines. The ladder to get there starts deliberately
small: Phase 1 builds and tests the lifecycle against fixtures alone,
a human then launches one empty, model-less server to prove the
mechanism on the real binary, and only after that does any model
file — via shadow links, never the original store — enter the picture.

---

## 15. Revision 6 correction prompt for GPT-5.6 Sol/Codex

```
TASK: Revision-6 architecture corrections only, continuing on the
existing draft PR #38 branch codex/isolated-ollama-attestation-phase-1.
Do not create a new branch.

CONTEXT (read first):
- projects/odysseus/ISOLATED_OLLAMA_BENCHMARK_SERVER_STRATEGY.md
  revision 6 — the contract. Especially §2.6/§2.7 (post-readiness
  command-version binding), §3.3 (address-aware ownership + stable
  owner identity), §5 (overlapped bounded I/O), §9 (updated failure
  vocabulary), §12 (state machine).
- python/odysseus_desktop_backend/runtime_bench/isolated_server.py and
  python/tests/test_isolated_ollama_server.py — the code under
  correction.

Scope is exactly the three architectural corrections and their tests:

1. COMMAND-VERSION BINDING (§2.6, §2.7, state S12). Delete the
   pre-server port-1 version probe entirely. Run the version child
   only after endpoint ownership readiness and /api/version, with an
   ownership recheck immediately before it and OLLAMA_HOST set to the
   proven owned endpoint. The version child keeps: its own temporary
   home and empty model store, the same minimal from-empty
   environment, the exact NUL/stdout/stderr inherited-handle list, its
   own verified kill-on-close job, suspended-image verification and
   re-hash before resume, the 4 KiB combined cap, an absolute
   deadline, complete verified cleanup, and no raw output persistence.
   Parse client and command-reported server versions only through the
   closed SHA-256 version-output dialect; require both to equal the
   normalized /api/version value. Implement the §2.6 fail-closed
   table: version_output_malformed, version_endpoint_ownership_failed,
   runtime_identity_mismatch. No harness process may target port 1, a
   guessed port, or any unproven endpoint.

2. ADDRESS-AWARE TCP OWNERSHIP (§3.3). Replace the port-only
   first-PID lookup: classify every listener row by local address and
   port (exact loopback 127.0.0.1:<port>, wildcard 0.0.0.0:<port>,
   unrelated concrete-interface, irrelevant), return all relevant rows,
   and implement the closed OWNED / NOT_PRESENT / FOREIGN / AMBIGUOUS /
   UNCERTAIN result model. Multiple relevant rows fail closed as
   port_ownership_ambiguous; wildcard and loopback rows are never
   collapsed; unrelated-interface rows are never matched. Implement
   the stable foreign-owner identity: held checked
   PROCESS_QUERY_LIMITED_INFORMATION handle plus GetProcessTimes
   creation time, compared during race cleanup with PID-reuse
   detection (same PID + different creation time, or signaled held
   handle), owner_identity_unavailable / owner_identity_changed, a
   checked handle close at endpoint closure, and no persistence of
   PID, address, port, handle, or creation time. Only an unchanged
   proven foreign identity with every benchmark resource gone may
   permit the single S10 → S4 retry.

3. GENUINELY BOUNDED LOG I/O (§5, states S5/S8/S17). Replace the
   reader threads and every unbounded join with benchmark-owned
   overlapped pipe reads: single-instance uniquely named pipes
   (PIPE_ACCESS_INBOUND | FILE_FLAG_OVERLAPPED |
   FILE_FLAG_FIRST_PIPE_INSTANCE, byte mode,
   PIPE_REJECT_REMOTE_CLIENTS), inheritable child write ends only, one
   pending overlapped ReadFile per stream with a manual-reset event,
   WaitForMultipleObjects under the absolute
   readiness/attestation/version/cleanup deadlines, checked
   GetOverlappedResult, first-64-KiB + last-192-KiB / 256 KiB / 4 KiB
   caps, and the §5 cancellation sequence with checked CancelIoEx
   against the exact pending operation. Implement log_io_setup_failed,
   log_read_failed (renamed from log_reader_failed),
   io_cancellation_failed, and pending_io_cleanup_timeout. After
   cleanup-deadline exhaustion: fixed cleanup failure, no retry, never
   complete evidence, buffers stay referenced. No Python helper thread
   may remain in the I/O path and no Thread.join may remain in the
   lifecycle.

Align the lifecycle ordering and artifact validation with the §12
state machine and §9 vocabulary (runtime identity now records client,
command-reported server, and API versions). Preserve every revision-5
decision listed in the strategy: exact three-handle inheritance,
from-empty environment, temporary profile and empty store, the closed
SHA-256 dialect registry kept EMPTY, requested-versus-attested typing,
separate readiness/attestation deadlines, suspended-image
verification, bounded /api/ps requiring exactly {"models": []},
privacy-safe closed artifact, process-free dry-run default, and
synthetic-only validation.

TESTS: synthetic fixtures only (fake lifecycle API, fake TCP rows with
addresses, scripted pipe/event fixtures, fake clock). Add or update
hostile tests for at least: version output containing the
connection-warning marker; missing client version; missing server
version; client/server/API disagreement; ownership change immediately
before the version child; same-port listeners on different local
addresses; wildcard plus loopback rows for one port; multiple relevant
owners; PID reuse (same PID, different creation time); vanished
foreign owner; a pending read that never completes and ignores the
first cancellation; CancelIoEx failure; and cleanup-deadline
exhaustion returning the fixed cleanup failure without retry. Property
test: no persisted structure contains a PID, port, address, path,
handle, creation time, pipe name, or raw output.

PROHIBITED: launching the installed Ollama binary or any real server;
contacting the ordinary Ollama service; reading any real model store;
registering any dialect entry (the registry stays empty); invoking or
weakening the G-ISO-0 approval flag; downloads or new dependencies;
redesigning Phase 2, shadow stores, inference, schema v3, ABBA/BAAB
execution, planner promotion, or UI; merging, rebasing, retargeting,
or force-pushing; taking PR #38 out of draft or changing its base from
research/hardware-relative-uplift-benchmark.

DONE WHEN: the three corrections match revision 6 exactly; the
complete validation matrix passes — full Python test suite, JavaScript
suite, frontend checks, Cargo checks/tests, and `git diff --check` —
the branch is updated with ordinary pushes only, PR #38 remains a
draft targeting research/hardware-relative-uplift-benchmark, and the
result summary explicitly lists any deviation from this contract.
```
