# Isolated Ollama Benchmark Server Strategy

Status: PROPOSED, revision 4 — dev-only measurement-infrastructure
design (Fable, 2026-07-19; revised same day after maintainer review and
Phase 1 contract correction). No real servers or models were run and
nothing was downloaded or installed. PRs #33 / #35 / #36 / #37 are
inputs and are not modified by this document.

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

Revision 4 Phase 1 review corrections: the command-version probe is a
separate suspended, minimal-environment, kill-on-close-job-owned process
with a 4 KiB combined-output cap and a true deadline; every child uses
`STARTUPINFOEX` with `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` so only stdout
and stderr pipe writers are inherited; startup dialects live in a closed
committed SHA-256 registry; readiness and attestation use separate clocks;
the real candidate-port race retries only after complete verified cleanup;
and the suspended process image plus executable hash are revalidated before
assignment/resume. The registry intentionally contains no entry for the
installed binary in this correction cycle. Registering that binary's
reviewed dialect is a separate prerequisite before human G-ISO-0 execution.

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
`llm/llama_server.go`, repo file listing).

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
   launch);
3. the strictly normalized version parsed from `ollama --version` for
   that same executable;
4. the same normalized representation parsed from `/api/version` for
   the owned child endpoint.

The command and API versions are required and must match. The child's
startup-log version is optional because log shape is not a stable API.
If the fixture-reviewed dialect for the attested executable hash yields a
parseable startup version, it is normalized and must match. If it does
not, the artifact records the typed state `unattested`; absence alone
does not fail an otherwise valid identity bind. Raw command output and
raw startup logs remain memory-only. A required disagreement, a parseable
startup-version disagreement, or a hash/basename change is
**`runtime_identity_mismatch`** and invalidates the session, fail closed.
Log-shape and flag-spelling expectations are fixture-driven per attested
identity, never assumed from upstream `main` (consistent with this
branch's fail-closed metadata rules, commits `0ccad259`…`b5308a4a`).

The command-version evidence is itself isolated: `--version` is created
suspended with the same deny-by-default environment, temporary profile,
and empty model store as the server; assigned to its own verified
kill-on-close Job Object; then resumed under a true wall-clock deadline.
Combined stdout/stderr retention is capped at 4 KiB, overflow or timeout
kills the whole probe job, and descendant/handle/temp cleanup is verified.
Plain `subprocess.run` is forbidden. After each suspended process creation
(probe and server), the checked process image is compared to the resolved
executable and that file is re-hashed before assignment/resume; disagreement
is `runtime_identity_mismatch`. Image paths and raw probe output remain
memory-only.

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

### 3.3 Port ownership — verified before first contact

- Candidate port chosen by probe-binding a fresh randomized dynamic-range
  loopback port, then closing the probe socket; the child is launched with
  that port. A retry requires proof of a genuine race: a foreign listener
  owns the previously free candidate and our child exits before contact.
  The whole failed attempt is terminated and must prove no descendants,
  stopped readers, closed handles, released port, and removed temporary
  space before a fresh candidate is tried. Job, identity, log, attestation,
  and arbitrary process failures are never retried. Exhaustion reports
  `port_bind_failed` with the bounded outer attempt count.
- **Ownership is verified before the first HTTP request**: once the
  child is running, the harness reads the Windows TCP table
  (`GetExtendedTcpTable`, ctypes, read-only) and confirms the PID
  listening on the benchmark port is the child (or a member of its
  job object). Only then is `/api/version` called. A mismatch is
  `port_hijacked` and invalidates the session — no request is ever
  sent to a socket the harness has not proven it owns.
- After teardown, the harness re-reads the TCP table and confirms the
  benchmark port has **no listener**; a survivor is
  `port_not_closed` (§4).
- The ordinary service's endpoint — whatever it is (§3.5) — is
  excluded from candidate selection. No fixed port number, including
  11434, is assumed or hardcoded as *the* ordinary port; discovery
  decides.

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

1. **Job object ownership.** Both the version probe and server child are
   created suspended, assigned
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
   exactly the stdout and stderr pipe write handles. No stdin or unrelated
   inheritable parent handle reaches either child. Every attribute-list API
   result is checked and the list is deleted during creation cleanup.
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
4. **Port closure.** The TCP table is re-checked (§3.3); a listener
   remaining on the benchmark port is `port_not_closed` and aborts the
   batch.
5. **Temporary-space teardown.** The session's temporary profile,
   temp dirs, and shadow store are removed; failure to remove is
   recorded (`teardown_incomplete`) but does not retroactively
   invalidate completed measurements — it blocks the *next* session
   until resolved.

---

## 5. Bounded in-memory logs and privacy-safe artifacts

The child's stdout/stderr are drained continuously by reader threads
(an undrained pipe on Windows blocks the writer — a stalled server
that "never becomes ready" would otherwise be self-inflicted) into a
**memory-only** bounded capture: first 64 KiB + last 192 KiB, 256 KiB
cap, truncation flag. INFO level only (`OLLAMA_DEBUG=0`,
`OLLAMA_DEBUG_LOG_REQUESTS=0`). The readiness clock starts immediately
before `ResumeThread`, excluding hashing, the owned version probe,
temporary-space creation, and suspended process/job setup. If the owned
endpoint does not answer within the readiness deadline (default 30 s), the
tree is killed and the session is `startup_timeout`; capture overflow is
`startup_log_overflow`.

API readiness is not attestation readiness. After ownership and
`/api/version` succeed, a separate bounded attestation clock (default 10 s)
continues draining logs until every mandatory pattern in the reviewed
dialect is observed. Process exit, overflow, and reader failure remain
fatal during this interval. Deadline expiry yields `attestation_missing`
with bounded timeout metadata; there is no scheduling-only `sleep(0)` path.

**Raw logs are never persisted.** They are parsed in memory into typed
attestation records (§6) and then discarded. Durable artifacts contain
**no** PID, no port number, no executable path, no model-store or
shadow-store or temporary-profile path, no raw log excerpts, and no
command lines.

Phase 1 has its own standalone closed artifact; it does not implement or
reuse performance schema v3:

- `schema_version: 1`;
- `artifact_kind: isolated_ollama_server_attestation`;
- captured UTC timestamp;
- runtime identity: executable basename, executable SHA-256, normalized
  binary-command version, normalized API version, and the optional typed
  startup-version state/value/source;
- requested typed settings, using only `loopback: true` and
  `store_kind: empty_temp` markers for the generated endpoint/store;
- attested typed settings and their closed sources;
- endpoint-owner and job-assignment booleans;
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

Startup parsing is selected only through a closed, committed registry
keyed by executable SHA-256. Registry construction validates that every
entry's key and dialect identity match; patterns are compiled, bounded in
length, contain exactly one capture group, use only allowlisted setting
names and sources, and include mandatory Phase 1 `noprune` and `no_cloud`
markers. Caller-supplied regexes or fixture paths are forbidden. An unknown
hash is rejected before either `--version` or `serve` starts as
`attestation_dialect_unavailable`. The empty registry in revision 4 is
intentional: it prevents the CLI from implying that G-ISO-0 can complete
before the installed binary's dialect has been independently reviewed and
committed.

| Typed field | Source | Attests |
| --- | --- | --- |
| `runtime_identity` (§2.6 normalized bind) | file hash, `--version`, `/api/version`, optional startup log | one specific binary served this session |
| `endpoint_owner_verified` | TCP table, pre-first-request (§3.3) | it is *our* server |
| `effective_env_report` (typed subset) | startup config report (envconfig `Values()`) | env the server parsed — incl. noprune and no-cloud |
| `flash_attention_applied` (`on`/`off`/`auto`/`unattested`) | runner launch line | flag handed to the runner |
| `kv_cache_type_applied` (type or `unattested`) | runner launch line | flag handed to the runner |
| `model_identity` / `size_vram` / `context_length` / `expires_at` | `/api/ps` after load (inference phases only) | identity, placement, effective context, residency policy |

Binding rules (all fail-closed, consistent with this branch's
metadata rules):

1. Every artifact records requested and attested side by side; each
   attested field carries `source` ∈ {`startup_log`, `runner_log`,
   `api_ps`, `api_version`, `binary_version`, `file_hash`,
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
5. Any required normalized command/API disagreement, or a disagreement
   with a parseable startup-log version, → `runtime_identity_mismatch`
   (§2.6), session invalid. An unparseable or absent startup-log version
   remains typed `unattested` and does not by itself invalidate identity.

---

## 7. First launch: empty-store attestation (gated, no inference)

The first real launch of an isolated server on the dev machine is a
dedicated **attestation launch**, separately human-approved (gate
G-ISO-0, §11), and deliberately inert:

**Separate prerequisite before approval/execution:** resolve and hash the
installed binary without launching it, independently review synthetic/raw
log samples outside this implementation run, and commit a validated dialect
entry for that exact SHA-256. Revision 4 deliberately does not invent or
register the installed binary's dialect. Until that prerequisite lands,
the real CLI fails `attestation_dialect_unavailable` before process creation;
the approval flag alone is insufficient.

- **temporary empty model store** (a fresh empty directory as
  `OLLAMA_MODELS`) — the user's model files are not exposed in any
  form, not even via shadow links;
- **temporary home/profile** (§3.2);
- **no model load, no inference** — the only requests are
  `/api/version` and `/api/ps` (expected empty).

Its sole purpose is to prove the mechanism itself, on the installed
binary, before any model is ever involved:

1. required normalized runtime identity binds with no mismatch (§2.6),
   with startup-log version either matching or typed `unattested`;
2. the server binds loopback-only on the assigned port;
3. TCP-table ownership verification works pre-first-request;
4. the startup config report shows `OLLAMA_NOPRUNE=1` and
   `OLLAMA_NO_CLOUD=1` parsed as requested (this attestation is the
   precondition for ever considering the shared-store fallback,
   §3.4);
5. log capture stays within bounds and parses into typed records;
6. Job Object teardown leaves zero survivors (orphan scan clean);
7. the benchmark port has no listener after teardown.

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
string.

| Category | Raised when | Invalidates |
| --- | --- | --- |
| `platform_unsupported` | required Windows lifecycle APIs are unavailable | launch |
| `executable_not_found` | configured executable cannot be resolved | launch |
| `executable_identity_unavailable` | executable hash or normalized command version cannot be established | launch |
| `attestation_dialect_unavailable` | executable hash has no closed committed reviewed dialect | launch before process creation |
| `temp_space_failed` | isolated profile, scratch, or empty store cannot be created | launch |
| `port_bind_failed` | no candidate port bound after retries | session |
| `process_create_failed` | checked suspended process creation fails | session |
| `process_attribute_list_failed` | STARTUPINFOEX handle-list sizing/initialization/update fails | launch |
| `process_attribute_list_cleanup_failed` | attribute-list or post-create handle cleanup cannot be completed | launch |
| `job_create_failed` | checked Job Object creation fails | session |
| `job_limit_configuration_failed` | kill-on-close limit configuration fails | session |
| `job_assignment_failed` | assignment or assignment verification fails | session |
| `process_resume_failed` | checked primary-thread resume fails | session |
| `ownership_probe_unavailable` | TCP/process ownership cannot be established using checked read-only APIs | session |
| `port_hijacked` | TCP-table owner of benchmark port ∉ job, pre-request (§3.3) | session |
| `port_not_closed` | listener remains on benchmark port after teardown (§4.4) | batch (aborts) |
| `startup_timeout` | endpoint not answering by deadline | session |
| `startup_process_exit` | child exits before readiness | session |
| `startup_log_overflow` | capture cap hit before readiness | session |
| `log_reader_failed` | a stdout/stderr drain fails | session |
| `version_probe_timeout` | owned command-version probe exceeds its wall deadline | launch |
| `version_probe_output_overflow` | combined version output exceeds 4 KiB | launch |
| `version_probe_failed` | owned version probe exits nonzero, emits no usable output, or its reader fails | launch |
| `version_probe_cleanup_failed` | probe tree/reader/handle/temporary cleanup cannot be proven | launch |
| `runtime_identity_mismatch` | required normalized versions disagree, or a parseable startup version disagrees (§2.6) | session; batch if the binary changed mid-batch |
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
| `teardown_incomplete` | temp profile/shadow store not fully removed (§4.5) | blocks next session |
| `session_incomplete` | fewer than the declared runs completed | session |
| `block_incomplete` | any of a block's four sessions invalid | the block |
| `inconsistent_across_blocks` | block difference reports disagree in direction (§8) | pooled verdict (reported, not averaged) |

Every Windows API return value is checked. Durable diagnostics expose
only this fixed vocabulary and closed bounded numeric metadata; raw Win32
messages, paths, handles, process identifiers, and port numbers are never
persisted.

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
   reviewed dialect entry committed for the resolved executable SHA-256 is
   a prerequisite to seeking or exercising this approval; unknown hashes
   fail before process creation.
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

## 12. Implementation ladder

The work is deliberately split so that no single agent task ever holds
both "can launch servers" and "touches model files":

- **Phase 1 (the only task prompted below)** — isolated child-server
  *lifecycle and attestation* against synthetic fixtures: temp
  home/store construction, minimal env, job objects, bounded
  in-memory logs, typed attestation, identity binding, TCP ownership,
  readiness, shutdown, orphan and port-closure verification. No model
  is ever loaded; no real server is launched by code or tests.
- **Gate G-ISO-0**, then the human-executed empty-store attestation
  launch (§7) using Phase 1's CLI.
- **Phase 2** — shadow-store construction (§3.4) + the
  ordinary-service discovery preflight (§3.5), still no inference.
- **Phase 3** — server sessions, priming, ABBA/BAAB blocks, schema v3
  artifacts + batch manifest (§8), behind G-ISO-1.
- **Phase 4** — block-level comparison and `inconsistent_across_blocks`
  reporting, feeding the existing compare pipeline.

Each later phase gets its own narrow prompt only after the previous
phase's review. Phase 1 is based on
`research/hardware-relative-uplift-benchmark` at `b5308a4a` and is
published from a separate implementation branch.

### Phase 1 implementation prompt for GPT-5.6 Sol/Codex

```
TASK: Phase 1 only — isolated Ollama child-server lifecycle and typed
attestation for the runtime_bench harness. Lifecycle and attestation
infrastructure with synthetic tests; no model work of any kind.

Base: research/hardware-relative-uplift-benchmark (b5308a4a). Do not
modify main, feat/deep-local-fable, or any planner/RPC/UI/provider code.

CONTEXT (read first):
- projects/odysseus/ISOLATED_OLLAMA_BENCHMARK_SERVER_STRATEGY.md —
  the contract. Phase 1 implements sections 2.6, 3.1-3.3, 4, 5, 6
  (identity + startup attestation only), and 7's mechanics. Follow
  every fail-closed rule exactly.
- python/odysseus_desktop_backend/runtime_bench/paired.py and
  paired_artifacts.py — reuse the loopback guard, redaction
  discipline, and closed-schema style. Its schema-v2 SERVER_ENV_KEYS
  is not the child-process allowlist.
- python/odysseus_desktop_backend/runtime_bench/__main__.py — CLI
  conventions (subcommand style, JSON summaries, nonzero on failure).

BUILD exactly this, dev-only, stdlib-only, Windows-first,
loopback-only:

1. isolated_server.py — an IsolatedOllamaServer lifecycle owner:
   a) Temporary session space: create per-session temp USERPROFILE
      (with LOCALAPPDATA subdir), TEMP/TMP dirs, and an EMPTY model
      store dir; delete all of them at teardown; report
      teardown_incomplete on failure. Never resolve, read, or link
      the user's real model store in Phase 1 — there is no code path
      to it.
   b) Minimal explicit child environment per strategy section 3.2:
      constructed from empty; only SystemRoot, SystemDrive, minimal
      explicit PATH (install dir + lib subdir + System32), temp
      USERPROFILE/HOMEDRIVE/HOMEPATH/TEMP/TMP/LOCALAPPDATA, plus
      NO_PROXY=127.0.0.1,localhost, OLLAMA_DEBUG_LOG_REQUESTS=0,
      OLLAMA_NO_CLOUD=1, OLLAMA_NOPRUNE=1, OLLAMA_HOST, OLLAMA_MODELS
      (the empty temp store), OLLAMA_NUM_PARALLEL=1,
      OLLAMA_MAX_LOADED_MODELS=1, OLLAMA_MAX_QUEUE=1, OLLAMA_DEBUG=0,
      These form the closed FIXED_INTERNAL_ENV_KEYS and cannot be
      supplied or overridden by callers. Define the separate closed
      USER_OVERRIDE_ENV_KEYS containing only OLLAMA_FLASH_ATTENTION,
      OLLAMA_KV_CACHE_TYPE, OLLAMA_KEEP_ALIVE and
      OLLAMA_CONTEXT_LENGTH. Reject fixed-key attempts and all other
      keys before any allocation or probe. Unit-test that
      inherited OLLAMA_*/proxy/secret variables can never appear.
   c) Runtime identity binding per section 2.6: resolve the
      executable, record basename, compute SHA-256, capture
      `ollama --version` output through its own suspended,
      minimal-environment, job-owned, 4-KiB-capped, true-deadline
      lifecycle (never subprocess.run); strictly normalize it and the
      /api/version response into one representation and require them
      to match. A parseable startup-log version must normalize to the
      same value; an absent or unparseable startup-log version is the
      typed state unattested and does not fail identity. Raw version
      output and the resolved path are memory-only. Query the suspended
      process image and re-hash before job assignment/resume.
   d) Port policy per section 3.3: probe-bind a fresh dynamic candidate,
      bounded whole-launch retries, exclusion list for endpoints identified as
      non-owned (Phase 1 takes an explicit exclusion list argument;
      discovery itself is Phase 2). TCP-table ownership check
      (GetExtendedTcpTable via ctypes, read-only) BEFORE the first
      HTTP request; port_hijacked on mismatch. Post-teardown
      port-closure check; port_not_closed on survivor. Retry only a
      proven foreign-owner + child-exit bind race, and only after full
      tree/reader/handle/port/temp cleanup.
   e) Windows Job Object ownership per section 4: CREATE_SUSPENDED ->
      STARTUPINFOEX with an exact stdout/stderr HANDLE_LIST ->
      CreateJobObjectW -> SetInformationJobObject with
      JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE -> AssignProcessToJobObject
      -> verify (job_assignment_verified) -> ResumeThread. Bounded
      graceful-first shutdown (Phase 1 has no model to unload: ready
      -> terminate job), orphan verification by parentage/job
      membership only (never image name), via read-only
      CreateToolhelp32Snapshot.
   f) Bounded in-memory log capture per section 5: reader threads,
      first 64 KiB + last 192 KiB, 256 KiB cap, truncation flag,
      readiness deadline starting immediately before ResumeThread
      (default 30 s), then a separate bounded mandatory-marker
      attestation deadline (default 10 s) -> startup_timeout /
      startup_log_overflow. Raw capture is never written to disk and
      is discarded after parsing.
   g) Typed startup attestation per section 6: parse (fixture-driven
      through a closed committed SHA-256 registry, never caller regex) the
      startup config report and, when present, the runner launch
      line, into typed fields each carrying a source or "unattested".
      Unknown hashes fail attestation_dialect_unavailable before launch;
      do not invent an installed-binary dialect in Phase 1 corrections.
      Persistable output uses the standalone schema_version 1 /
      artifact_kind isolated_ollama_server_attestation contract in
      section 5. It contains no pid, port, path, raw excerpt, command
      line, prompt, generated output, or schema-v3 performance field.

2. Failure categories: implement exactly the section 9 categories
   reachable in Phase 1, including platform/executable/temp failures,
   each process/job stage, ownership probe availability, early process
   exit, and log-reader failure. Check every Windows API result. Expose
   only the fixed category and closed bounded numeric metadata; never
   raw Win32 messages or identity-bearing values. Unknown categories
   are validation errors.

3. CLI: python -m odysseus_desktop_backend.runtime_bench attest
   --dry-run (default): validate configuration, print the typed
   launch plan as JSON, spawn nothing. Real execution requires an
   explicit --approved-g-iso-0 flag AND is refused when stdin is not
   a terminal; the default invocation can never launch a process.
   JSON summary out; nonzero exit on any failure category.

4. Tests, same discipline as the existing harness/paired tests, all
   against synthetic fixtures (fake process objects, fake pipes with
   scripted log bytes, fake TCP tables, fake clock; loopback socket
   fixtures only for the port-probe logic): env construction
   (deny-by-default, secret/proxy exclusion, allowlist rejection),
   temp-space lifecycle incl. teardown_incomplete, identity binding
   agree/disagree paths, port policy incl. exclusion list and
   pre-request ownership and post-teardown closure, job-object call
   sequence and orphan verdicts (fake API layer), log bounds and
   truncation, attestation parsing for at least two fixture log
   dialects plus an unparseable dialect yielding "unattested",
   privacy of persisted output (property test: no pid/port/path
   strings in any persisted structure), every Phase 1 category
   reachable. Tests never spawn a real ollama, never touch the real
   model store, never open non-loopback sockets.

DO NOT: implement model loading, inference, shadow-store
construction, ordinary-service discovery, schema-v3 performance
artifacts, sessions/blocks/priming, comparison logic, or any
auto-launch of a real server; do not modify paired.py public
behavior, shapes, prompts, planner, services/, providers, UI, or
settings; do not add dependencies; do not download anything; do not
signal any process the harness did not create; do not write raw logs
to disk. During implementation, do not invoke the real-launch approval
flag, launch the installed Ollama binary, probe the ordinary Ollama
service, or read the real model store. Execute only the dry-run CLI and
synthetic fixture lifecycle.

DONE WHEN: full existing test suite green plus the new tests; a
synthetic end-to-end lifecycle (fake process fixture) produces a
typed standalone attestation dict that validates and contains no
forbidden fields; the dry-run CLI emits the plan without spawning;
result summary explicitly lists any deviation from this contract.
```

The real empty-store attestation launch (§7) is **not** part of
Phase 1: it happens only after Phase 1 review and an explicit G-ISO-0
approval, executed by a human using the reviewed CLI.

---

## 13. Non-goals

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
model store, which binds the identity of the binary it ran, attests
what that binary actually applied, persists only typed privacy-safe
evidence, and dies provably clean. The ladder to get there starts
deliberately small: Phase 1 builds and tests the lifecycle against
fixtures alone, a human then launches one empty, model-less server to
prove the mechanism on the real binary, and only after that does any
model file — via shadow links, never the original store — enter the
picture.
