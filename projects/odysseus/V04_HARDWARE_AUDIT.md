# v0.4 Hardware / Resource Audit (Issue #20)

Status: **measurement report — P3 baseline only**. Backend-only
measurements taken 2026-07-12 on the dev machine (tier P3). Per
`POTATO_PROOF_MATRIX.md` §A/§F, P3 results cannot pass a P1/P2 budget;
every budget whose scope includes the packaged Tauri host or the UI
remains **still-proposed**. Branch
`audit/v0.4-hardware-resource-metrics`, code at `3cd01b55` (sidecar
`version=0.3.1`).

No production source was modified by this audit. No models were pulled.
No private document or profile contents were inspected. One invalid
packaged-Tauri-host attempt briefly initialized against the real
default profile path before it was terminated (§7); incidental
metadata or bookkeeping writes from that attempt cannot be excluded,
and that invalid run is excluded from all measurement evidence in this
report. Every valid backend-only measurement used the scratch
`ODYSSEUS_PROFILE_DIR` pointed at a fresh directory under a scratch
folder (`%TEMP%\odysseus-hw-audit-20260712-000122`), and each such
run's `backend.log` records that scratch `profile_dir` (spot-checked
for all 12 runs).

## Verdict

**P3 backend-only baseline established; Issue #20 not closeable on this
evidence alone.** Candidate SHA `3cd01b55b0d523269fded4ccdced71beb206784b`
on branch `audit/v0.4-hardware-resource-metrics`. Of the 11 §B budgets,
3 now have a real disposition (2 measured-fail, 1 measured-pass on
P3/P4 only); the other 8, and every P0/P1/P2 column, remain
still-proposed pending the P1/P2 runbook (§7). The one investigated
anomaly (OCR "hang", §4) is classified inconclusive and does not, on
current evidence, warrant a follow-up product-bug issue (§11). See §9
and §10 for the explicit acceptance and Potato-Mode-defaults calls.

**Constraint method availability:** not established by this audit.
Simulating P1/P2 hardware (RAM/CPU-capped VM, Windows Job Object,
Hyper-V, or similar) is required for `POTATO_PROOF_MATRIX.md` §F's
"simulated equivalent" path, but whether such a method is available on
this machine/account was never checked here — it is the first open
question for whoever runs the §7 runbook, not something this audit can
report either way.

## 1. Environment (exact machine tier)

| Item | Value |
|---|---|
| CPU | AMD Ryzen 5 4600H (6C/12T) |
| RAM | 15.4 GB usable |
| GPU | NVIDIA RTX 3050 Laptop 4 GB (+ Radeon iGPU) |
| OS | Windows 11 Home 10.0.26200 |
| Tier | **P3** ("old gaming laptop") — baseline only |
| OCR deps | tesseract + mutool detected; `OCR_PDF_RENDER_DPI = 400`, `OCR_SUBPROCESS_TIMEOUT_SECONDS = 60` |
| Ollama | `ollama serve` local; models already installed: llama3.2:1b, llama3.2:latest (=3b, the approved P1/P2 default), qwen3:8b, nomic-embed-text, qwen3-vl:2b, nemotron-nano-chat:8b. Nothing pulled during the audit. |

## 2. Method

Backend-only: the sidecar (`python/rpc_server.py`) was driven over
line-delimited JSON-RPC exactly as the Rust host drives it — no Tauri
window, no packaged host, no UI. Resource sampling: a PowerShell loop
reading the sidecar PID's working set and total CPU time every ~400 ms
(process-level; **child processes such as tesseract/mutool and the
Ollama server are NOT included in sampled RAM/CPU** — see §8).

Fixtures are fully synthetic (lorem-style filler plus one planted
codeword; no personal data):

| Fixture | Size | Notes |
|---|---|---|
| normal_30page.pdf | 24,590 B | 30 text pages, 60,573 chars; codeword on page 15 |
| scanned_2page.pdf | 206,390 B | raster-only, no text layer (Sonnet battery) |
| scanned_1page.pdf | 108,740 B | raster-only, 1 page (takeover reproduction) |

**Fixture-realism caveats (affect two budgets):**
- The 30-page PDF is unrealistically small (24.6 KB). Profile-growth
  *ratios* against it are dominated by fixed DB overhead and embedding
  vectors and overstate growth on real documents.
- The scanned fixtures were written by PIL, which encodes a 1700×2200 px
  image as a 23.6″×30.6″ page (72 dpi). At the backend's fixed 400 dpi
  render this becomes a ~115-megapixel image (PIL even emitted
  `DecompressionBombWarning`), so OCR duration and peak memory here are
  a near-worst legal case, not a typical letter-size scan (letter at
  400 dpi ≈ 15 MP, ~7.7× smaller).

### Provenance: two harnesses

The first battery (cold ×3, import ×3, ocr ×1, answer ×1) was run by a
prior Sonnet session with a temporary driver. Line-by-line review during
takeover found defects in that harness (§5); its cold/import results
were verified internally consistent and are **retained**, its OCR and
answer results were **invalid** and were re-measured with a corrected
driver (raw stdout/stderr preserved to files, responses matched by
JSON-RPC id, hard per-call timeouts, binary pipes so the stderr drain
cannot die on decode errors).

## 3. Measured results (P3, backend-only)

### 3.1 Sidecar startup (spawn → health.ping ok), n=3

| Run | Seconds |
|---|---|
| cold-test1 | 1.391 |
| cold-2 | 0.942 |
| cold-3 | 1.218 |
| **median** | **1.218** |

This is a component of "cold launch to readiness", not the full
UI-visible metric (host + WebView + readiness render unmeasured).

### 3.2 Idle sidecar (15 s idle window per run, n=3 × 36 samples)

CPU delta over the idle window: **0.000 s in all three runs (0.00 % of
one core; no busy polling — the sidecar blocks on stdin)**. Working set
flat at **47.2–47.4 MB**. Sidecar only; packaged host RAM unmeasured.

### 3.3 Import + index, 30-page text PDF (n=3, fresh profile each)

| Run | documents.import (s) | rag.search (s) | sidecar peak WS (MB) |
|---|---|---|---|
| import-1 | 7.581 | 0.156 | 54.0 |
| import-2 | 2.300 | 0.100 | 53.8 |
| import-3 | 1.820 | 0.074 | 53.7 |
| **median** | **2.300** | **0.100** | — |

import-1 includes first-load effects (embedding path warm-up); shown
individually, not smoothed away.

### 3.4 Profile size

| State | Bytes | Note |
|---|---|---|
| Fresh profile after cold run (baseline) | 316,263 | schema + logs |
| After 30-page import (each of 3 runs) | 750,853 | identical across runs |
| **Growth** | **434,590** | ≈ **17.7× the 24,590 B source** (≈ 410 KB DB growth + 24.6 KB stored copy) |
| After 1-page scanned OCR run (clean shutdown) | 1,679,268 | growth 1,363,005 ≈ 12.5× the 108,740 B source |

Against the literal §B budget (≤ 3× source size) this is a
**measured-fail**, but the ratio is an artifact of the tiny fixture: the
absolute growth (~435 KB for 30 pages / ~60 K chars, embeddings
included) would be well under 3× for any realistically sized PDF
(≥ ~220 KB). Recommendation: restate the budget with an absolute floor,
e.g. "≤ 3× source size + 1 MB".

### 3.5 OCR (scanned PDF, no text layer)

Valid run (takeover harness, 1 page, fresh profile):

| Metric | Value |
|---|---|
| documents.import total (import → OCR → embed → response) | **206.5 s** |
| breakdown | render 3.2 s → tesseract+variants ≈ 198 s → embed/index 9 chunks ≈ 5.5 s → RPC response +0.08 s |
| sidecar peak WS | **997 MB** (excludes tesseract/mutool child processes) |
| sidecar CPU total | 28.8 s |
| shutdown | graceful, rc=0, no orphans |

Partial evidence from the invalid 2-page Sonnet run: sidecar WS reached
**1,586 MB** within the first 120 s (its sampler died at its 120 s cap,
so true peak may be higher); backend completed OCR+index of both pages
in 367.8 s (backend.log 00:06:07 → 00:12:14).

No memory cap exists on the render/OCR path; the 115 MP render (§2
caveat) is legal input, so **"OCR peak memory capped" is a
measured-fail** even though typical letter-size scans would render
~7.7× smaller.

### 3.6 Sourced answer (chat.send, llama3.2:latest = approved 3b default, 30-page PDF, n=3 fresh sessions × cold+warm)

| Run | import (s) | chat cold (s) | chat warm (s) | sourced? | shutdown rc |
|---|---|---|---|---|---|
| answer-r1 | 2.15 | **16.27** (incl. Ollama model load) | 5.09 | yes | 0 |
| answer-r2 | 2.20 | 9.41 | 10.37 | yes | 0 |
| answer-r3 | 2.14 | 3.91 | 4.47 | yes | 0 |

- Every one of the 6 answers was sourced: 8 retrieved chunks, 5
  snippets, 1 document-evidence entry, grounding block present
  (backend `timings.answer_latency_ms` agreed with driver-measured
  latency within ~0.1–0.4 s across the 6 calls; largest gap 0.43 s on
  answer-r1 warm, tightest 0.10 s on answer-r2 cold — consistent with
  RPC/logging overhead outside the model call itself, re-verified
  directly against `stdout-answer-r*.raw.log` and
  `events-answer-r*.jsonl`).
- **Time to first sourced answer on a cold everything (fresh profile,
  model not resident in Ollama): 20.8 s** end-to-end, measured directly
  on answer-r1 from `spawn_start` to the sourced cold answer
  (spawn→ping 2.40 + import 2.15 + chat 16.27, per
  `events-answer-r1.jsonl`; an earlier draft quoted ~19.6 s by
  substituting the cold-run median spawn of 1.2 s for r1's own 2.4 s —
  corrected). Well under the 30 s P3/P4 budget. P1/P2 (≤ 60 s) remains
  unmeasured.
- Sidecar peak WS during chat ≈ 56.9 MB — generation RAM lives in the
  external Ollama process, which was not sampled.
- **Quality observation (not a §B budget):** none of the 6 answers
  surfaced the planted page-15 codeword; retrieval returned page-1-ish
  chunks and the model answered confidently with the wrong "codeword".
  Sourced ≠ correct. Worth a retrieval-quality look under a separate
  issue; out of scope here.

### 3.7 Shutdown / orphans (backend-only path)

10/10 completed runs (cold-test1, cold-2, cold-3, import-1, import-2,
import-3, ocr-repro-1, answer-r1, answer-r2, answer-r3) ended with
`app.shutdown` → process exit rc=0 within ~0.3 s (max observed 0.27 s,
ocr-repro-1). The 2 invalid runs (ocr-1, answer-1) were externally
killed, not gracefully shut down, and are excluded from this count —
see §4 and §5. Final sweep after all measurements: no `rpc_server.py`
python, tesseract, mutool, or pdftoppm processes remained. Crash
path and packaged-host close/crash path not measured in this v0.4
audit; historical v0.3.1 smoke is context only, not v0.4 proof.

## 4. The OCR "hang": investigation and classification

**Classification: inconclusive** (not reproducible; no confirmed
app-side RPC hang; several confirmed harness defects made the original
observation untrustworthy). Detail:

Original event (Sonnet battery, 2-page scanned fixture):
- 00:06:07 import requested → backend.log `ocr indexed ... pages=2
  chunks=15 embedded=15` at **00:12:14** (367.8 s — consistent with the
  115 MP-per-page fixture, §2).
- After 00:12:14: no further backend.log lines, no DB writes (WAL mtime
  00:12:14), and the driver — parked in a blocking `readline()` on the
  sidecar's stdout the whole time — received nothing. Had the response
  been written, that `readline()` would have returned immediately, so
  the response was genuinely never observed on stdout.
- At **00:24:41** the sidecar was killed (per the prior session's own
  account). The driver's `documents_import_scanned_end` event at that
  moment is an artifact: that driver logged `_end` *before* checking
  for EOF, so kill-EOF and real-response were indistinguishable in its
  event log. The claimed "response after 1114 s" never happened; the
  battery then auto-advanced to the answer stage.

Reproduction (one run, per plan): same code path, 1-page scanned
fixture, fresh profile, robust harness, 10-min hard timeout, concurrent
sampling, raw stdout/stderr to files. Result: **no hang** — the JSON-RPC
response (817 KB, pure ASCII, id-matched) arrived **~80 ms after `ocr
indexed`**, followed by a clean shutdown. The post-OCR code path
(ocr_service → source_service → rpc `write_json`) was also read
line-by-line: after `ocr indexed` only trivial dict assembly, one
progress event to stderr, and the stdout write remain; progress events
go to stderr (drained), responses to stdout with explicit flush. One
raw-evidence datum narrows the stall window: in the healthy repro,
`ocr indexed` is followed 40 ms later by an `ocr status` log line and
then the response; ocr-1's backend.log stops at `ocr indexed` with no
`ocr status` line, so the original stall sits inside that final window
(one stderr progress write + the stdout response write). An app-side
stall there and a defect-6 stderr-pipe stall (had the drain thread
silently died) are both consistent with the evidence and both unproven
— which is precisely why the classification is inconclusive rather
than either "confirmed app-side" or "confirmed harness bug".

What can and cannot be said:
- Confirmed: the app completed OCR+indexing correctly in the original
  run; the harness destroyed the distinguishing evidence (killed the
  sidecar without capturing state; raw stdout/stderr not persisted;
  `driver.py` was edited after the battery so the exact running version
  is unrecoverable).
- Not confirmed: any application-side RPC hang. Not confirmed: a
  specific harness bug that swallowed a delivered response (the repro's
  response bytes are ASCII, so the old harness's cp1252 text-mode read
  would also have decoded it).
- Therefore: inconclusive one-off. **No product bug issue is warranted
  on this evidence.** If it recurs under the v0.4 UI work, capture raw
  sidecar stdout before killing anything.

Related: the prior battery's answer-stage "warm chat = 407.6 s" was the
same artifact class — its events stop at `chat_send_warm_end` with no
shutdown events and no graceful-shutdown backend.log line (process
killed; `_end` = EOF). Warm chat measured properly: 4.5–10.4 s (§3.6).

## 5. Temporary-harness defects found (takeover review)

Recorded so future audits don't repeat them; the harness lives only in
scratch, not in the repo.

1. `driver.py` logged the `_end` timing event before checking whether
   `readline()` returned EOF — a killed sidecar produced a fake
   "response received" event (root of both false anomaly durations).
2. No per-stage timeout in `run_battery.ps1` — a silent stage blocked
   the battery indefinitely (the sampler was time-capped but the driver
   was not).
3. Sampler caps shorter than stages (OCR cap 120 s vs 368 s stage;
   answer cap 180 s) — peak-memory figures from those stages are lower
   bounds only.
4. Driver stdout (per-run result JSON) was printed to the console and
   never persisted — the import/answer verification payloads from the
   first battery are unrecoverable.
5. Responses were not matched by JSON-RPC id and stray stdout lines
   were not filtered (latent; no evidence it fired).
6. stderr drained via a `text=True` (locale cp1252) reader whose thread
   dies silently on any exception (latent pipe-fill deadlock risk).
7. `driver.py` was modified after the battery ran (mtime 00:33:40 vs
   battery start 00:04:59) without preserving the executed version.
8. Isolation itself was correct in every backend run: scratch
   `ODYSSEUS_PROFILE_DIR` honored, `app_data_dir=unset`, no Tauri host
   spawned, HF offline env set, proxy env stripped.

## 6. §B budget dispositions (authoritative copy)

Mirrored into `POTATO_PROOF_MATRIX.md` §B. "Component measured" notes
give the backend-only P3 evidence; they do not upgrade a still-proposed
budget. A P3 measurement can never *pass* a P0/P1/P2 budget, but a
measured-fail carries to all tiers when the failing property is
hardware-independent (bytes written by code on fixed input) or can only
be worse on weaker hardware (an uncapped allocation on less RAM) —
that a-fortiori rule is why the two fails below cover every tier.

| §B budget | P0 | P1/P2 | P3/P4 | Evidence / blocker |
|---|---|---|---|---|
| Cold launch → interactive readiness view | still-proposed | still-proposed | still-proposed | UI metric; component measured: sidecar spawn→ping median 1.22 s (§3.1). Blocker: packaged-host run needs clean second Windows user / VM (§7). |
| Idle RAM (app + sidecar, no model) | still-proposed | still-proposed | still-proposed | Host RAM unmeasured; component: sidecar idle WS ≈ 47 MB (§3.2). |
| Idle CPU (steady state) | still-proposed | still-proposed | still-proposed | Host unmeasured; component: sidecar 0.00 %, no busy polling (§3.2). |
| Profile growth after one 30-page PDF | measured-fail | measured-fail | **measured-fail** | 17.7× vs ≤ 3× budget — fixture-ratio artifact; absolute growth 435 KB (§3.4). Recommend restating budget with absolute floor. Hardware-independent (bytes on disk are determined by code + input, identical across all 3 runs; budget threshold is the same for every tier), measured on P3. |
| Import/indexing CPU (bounded, cancellable) | still-proposed | still-proposed | still-proposed | No cancel path exists yet (planned v0.4.4); UI responsiveness unmeasured. Import itself completed in 1.8–7.6 s (§3.3). |
| OCR peak memory (page-at-a-time, capped) | measured-fail | measured-fail | **measured-fail** | No cap: sidecar WS ≥ 1,586 MB on a legal 2-page input, 997 MB on 1 page (§3.5); page-at-a-time loop is honored but render size is uncapped (400 dpi fixed). Cap absence is structural — the same code renders the same uncapped image on any tier (the 115 MP render proceeded past PIL's own bomb warning) — so the "capped" budget fails tier-independently; the WS peak values themselves are P3 observations only. |
| Time to first sourced answer (small model, 30-page PDF) | still-proposed | still-proposed | **measured-pass** | 16.3 s chat / 20.8 s end-to-end cold vs ≤ 30 s (§3.6, n=3, model pre-installed). P1/P2 ≤ 60 s needs tier hardware. |
| Max document size before guardrail prompt | still-proposed | still-proposed | still-proposed | Guardrail not implemented yet (Issue 6 scope). |
| UI responsiveness during background job | still-proposed | still-proposed | still-proposed | UI unmeasured. |
| Orphan sidecars after close/crash | still-proposed | still-proposed | still-proposed | Backend-only graceful shutdown: 10/10 pass, no audit orphan processes at final sweep (§3.7). Crash path and packaged-host close/crash path not measured in this v0.4 audit. Historical v0.3.1 smoke is context only, not v0.4 proof. |
| Non-user-initiated downloads | still-proposed | still-proposed | still-proposed | No network monitor was run in this audit; harness enforced HF offline env and nothing was pulled, but that is not proof. |

## 7. Still unmeasured — UI/Tauri runbook (second Windows user or VM)

An earlier attempt (prior session) to isolate the packaged Tauri host by
redirecting `APPDATA` failed — the packaged app still resolved the real
profile location, so all packaged-host measurements were halted rather
than risk touching real data. No artifacts of that experiment survive in
the scratch directory; treat "APPDATA redirection isolates the packaged
app" as **disproven** until shown otherwise. Consequence: everything
below requires a clean second Windows user account or a VM (which also
enables the P1/P2 RAM/CPU-capped simulation §F requires):

1. Create a fresh Windows user (or VM; record CPU/RAM caps if
   simulating P1/P2 — e.g. VM with 8 GB / 4 cores).
2. Install the packaged v0.4 build; first launch with a stopwatch/
   screen recording → cold-launch-to-readiness (budget 1).
3. At idle on the readiness screen, record RAM/CPU of the host process
   tree (host + WebView + sidecar) via Task Manager or
   `Get-Process -IncludeUserName` (budgets 2, 3).
4. UI-driven import of a 30-page PDF; measure wall time and input echo
   latency during indexing (budgets 5, 9).
5. Close/crash the app during and after a job; verify zero orphan
   processes (budget 10).
6. Run the whole session under a network monitor (e.g. pktmon) to prove
   zero non-user-initiated downloads (budget 11).

## 8. Threats to validity

- **Process accounting:** sampled RAM/CPU cover the sidecar process
  only. Tesseract/mutool children (OCR) and the Ollama server (chat)
  are excluded; OCR true peak commit is higher than reported.
- **Single machine, P3 only.** Nothing here validates the potato
  thesis (P0–P2).
- **Cache effects:** runs after the first benefit from warm OS file
  caches and a warm embedding path (visible: import-1 7.6 s vs 1.8–2.3 s
  later; answer-r1 cold 16.3 s includes Ollama model load, r3 3.9 s does
  not). First-run values are reported individually, not averaged away.
- **Sampler resolution:** ~400 ms polling of working set can miss
  sub-400 ms peaks.
- **Fixture realism:** §2 caveats on both scanned-page geometry and
  30-page file size.
- **Sample sizes are small** (n=3 per repeated stage; OCR n=1 valid).

## 9. Issue #20 acceptance criteria

**Not met.** This audit is P3 backend-only. Per `POTATO_PROOF_MATRIX.md`
§A/§F, P3 results cannot pass a P1/P2 budget, and every budget whose
scope includes the packaged Tauri host or UI is still-proposed (§6, §7).
Two of eleven §B budgets have real measured dispositions
(profile-growth: measured-fail as literally worded, absolute-growth
caveat noted; OCR peak memory: measured-fail); one has a P3/P4-only
measured-pass (time to first sourced answer). The remaining eight
budgets, and both P0/P1/P2 columns for every budget, remain
still-proposed. Closing Issue #20 requires at minimum the P1/P2
reproduction runbook in §7 (clean second Windows user or VM) to be run.

## 10. Whether Potato Mode defaults may be designed

**Not yet.** Per `POTATO_PROOF_MATRIX.md` §F's own recommendation, v0.4
should not be called done — and Potato Mode default values should not
be locked — until Potato Proof passes on P1/P2-class hardware or a
clearly documented simulated equivalent. This audit did not establish
whether a constraint-method (RAM/CPU-capped VM, Windows Job Object,
Hyper-V, or similar) is available on this machine or account; that
determination was not attempted here and should be the first step of
the §7 runbook, not assumed. Until P1/P2 (real or simulated) results
exist, only the two hardware-independent findings (absolute profile
growth ~435 KB for the 30-page fixture; OCR has no memory cap) can
safely inform design discussions today.

## 11. Proposed follow-up issue

None warranted on current evidence. The OCR anomaly (§4) is classified
**inconclusive**, not a confirmed application-side bug: the corrected,
fully-instrumented reproduction (§3.5, §4) completed cleanly with no
hang, so there is nothing reproducible to file. If the original
2-page-scanned "no response for 12.4 minutes" behavior recurs under
future (e.g. v0.4 UI) testing, capture raw sidecar stdout/stderr to
files before killing anything — that is the one piece of evidence the
original battery destroyed and the one thing that would let a future
audit actually distinguish an app-side hang from a harness/environment
artifact.

## 12. Evidence

Raw events (JSONL), sampler CSVs, raw sidecar stdout/stderr, per-run
`backend.log` copies, as-found copies of the first harness, and the
takeover inventory live in the scratch logs folder
(`%TEMP%\odysseus-hw-audit-20260712-000122\logs`) — ephemeral by
nature; every number used above is transcribed in this document and
was independently re-derived from these raw files during Fable's
verification pass (re-running `analyze.py` and `parse_answers.py`
against the untouched raw logs, plus direct inspection of
`events-ocr-repro-1.jsonl`, `repro-stdout.raw.log`, and per-run
`backend.log` files).

## 13. Cleanup state

Scratch profiles and synthetic fixtures were **not** deleted after
aggregation — an earlier draft of this report incorrectly claimed they
were. As of this verification pass, `fixtures/`, `logs/`, and all
`profile/*` subdirectories (cold-test1, cold-2, cold-3, import-1..3,
ocr-1, ocr-repro-1, answer-1, answer-r1..3) still exist under
`%TEMP%\odysseus-hw-audit-20260712-000122`. This is intentional, not an
oversight: the evidence is retained so a human/Fable can independently
review the raw measurements after this session's context resets, per
the takeover protocol. No audit child process remains running
(verified: no `python.exe`/`rpc_server.py`/tesseract/mutool/pdftoppm
processes, no orphan sidecar, no packaged Tauri test app). No private
document or profile contents were inspected. Every valid measurement
run in this audit used `ODYSSEUS_PROFILE_DIR`-redirected scratch
profiles and synthetic fixtures only; the one invalid packaged-host
attempt that briefly initialized against the real default profile path
(§7) is excluded from all measurement evidence, and incidental
metadata/bookkeeping writes from it cannot be excluded. The scratch
directory should be deleted only after a human or a fresh Fable session
has reviewed it and no longer
needs it as evidence.
