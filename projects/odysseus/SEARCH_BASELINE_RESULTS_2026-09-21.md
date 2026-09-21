# Search baseline — 2026-09-21

**OPUS IMPLEMENTATION READY**, limited first patch: redact untrusted rejected pointer text from diagnostics. Production was not repaired. Context integrity has independently reproduced follow-up defects; fallback policy needs an explicit decision before changing its semantics. No additional reasoning-effort request or agents were needed.

## Baseline and environment

- Root: `C:\Users\Parth Ganguly\Documents\Codex\odysseus-search-v1-local-discovery`.
- Branch: `codex/search-v1-context-budget`; HEAD `9d2f21a7fe6fcae342bb1dc3ad545991816d279c`; parent `dcb6d7183a3f82a198d59717852b2dd168d88937`. Initially clean. No checkout, reset, stash, commit, push, profile reuse, or production changes.
- Python: `C:\Users\Parth Ganguly\AppData\Local\Programs\Python\Python313\python.exe`, 3.13.12; SQLite 3.50.4; pytest 9.0.3; NumPy 2.4.6; lxml 6.1.2; readability-lxml 0.8.4.1; psutil 7.2.2. SearchService and ModelService imported from this root's `python/odysseus_desktop_backend/services/`, not the main checkout.
- No applicable AGENTS.md/CLAUDE.md found in the target tree or checked ancestor directories. Ponytail applied; no Graft CLI/tool found. Ordinary source navigation used. STATUS/NEXT_TASK describe earlier main/release/hardware work, not this Search candidate; no old milestone resumed.
- Environment discovery initially queried irrelevant, absent trafilatura/BeautifulSoup metadata and an existing venv without pytest. Neither is a product failure: the default Python already has the actual HTML dependencies, and all existing Search tests passed. Nothing installed.

## Inspected execution path

`python/rpc_server.py:jobs_submit_search` → `JobService.submit_search` / worker → `DocumentJobExecutor._run_search` resolves `search_mode` (`external` factory versus `LocalDiscoveryProvider`) → `SearchService.run`.

`run` plans public query variants, retrieves local/cache evidence (local FTS + dense reciprocal-rank fusion where configured), and performs bounded acquisition through the existing provider/fetcher/store. `build_dossier` reranks/deduplicates/caps passages. `_select_evidence` resolves context, creates sentence pointers, packs a ranked window, then `_model_call` → `ModelService.chat_detailed` → loopback `/api/chat`. Flat IDs are parsed; malformed structure invokes positional fallback. `verify_evidence_span_selection` resolves only visible IDs and exact source slices. Optional bounded public repair can repeat selection. `_synthesize` and `resolve_answer_citations` produce the answer, remap citations, and may append a Sources footer. Runs/diagnostics/evidence persist separately from the assistant message and its trace; the job returns message/run IDs. Zero verified evidence raises `search_no_evidence`.

Exact matching proves textual provenance, not relevance or entailment. A completed run/`verified_exact` trace is not a semantic answer score.

Existing `EvalService` has retrieval-only, oracle-generation, and end-to-end RAG modes; its oracle mode is not Search and its normal path uses ChatService. The existing `run_rag_evals.py` now exposes an evaluation-only `--search-replay` adapter. Replay uses production `_model_call`, `_select_evidence`, verification, `_synthesize`, and citation resolution. It bypasses RPC, jobs, planning, acquisition, retrieval ranking, repair, and normal Search persistence. A frozen ranked dossier is injected; this is **not live UI/job integration proof**. Existing fixture/service tests and one RPC test cover those distinct boundaries offline.

## Tests and findings

Existing four Search suites: **164 passed**. Existing Search RPC test: **1 passed, 8 deselected**. New acceptance file: **5 passed, 7 intentionally failed**, no xfails. Privacy tests prove the private title and quote actually reach selection, and the valid/malformed cases actually synthesize private answer content; only diagnostic surfaces are asserted content-free. User-owned sources, verified quotations, and conversation history are intentionally retained.

| New failing test suffix | OBSERVED | Implication |
| --- | --- | --- |
| `small_reported_context_is_never_raised` | Reported 128 becomes 2048 | Budget may exceed reported runtime capacity. |
| `impossible_window_does_not_force_first_span` | One-token input budget still packs a span | Forced-first-span defeats the bound. |
| `impossible_complete_request_never_reaches_model` | 4,000-character question at 2048 context still calls the model | Fixed prompt overhead alone can exhaust allowance. |
| `configured_budget_agrees_with_requested_runtime_context` | Budget 8192, request options omit `num_ctx` | A configured budget does not establish requested runtime capacity. |
| `synthesis_complete_request_and_generation_fit_context` | Fully packed/visible 8-span selection feeds synthesis estimated at 2660 + 1200 generation + 256 template = 4116, above 4096 | Selection budgeting does not cover the downstream complete request. This is an estimate-based invariant failure, not measured tokenizer overflow. |
| `malformed_selection_does_not_manufacture_relevant_support` | Malformed output selects the unrelated first passage | Proposed fail-closed policy conflicts with the documented degraded fallback; see decision below. |
| `private_content_stays_out_of_durable_diagnostics[unknown_pointer]` | Synthetic private string returned as an unknown ID persists in both `search_evidence_diagnostics.selected_span_id` and `search_runs.operations_json` | Confirmed diagnostic privacy defect; first patch. |

The five passes cover valid/malformed/exception private-output handling (three cases), missing external configuration, and replay schedule/coverage/annotation separation. Existing tests already cover valid empty selection, exact retained text, unknown/non-visible rejection, late support, normal versus oversized windows, qualifiers in extraction, cancellation, private repair-query exclusion, and Source Pack matching. Tests were reused rather than duplicated.

**DECIDED:** unknown model strings must not become diagnostic identifiers. Keep fail-closed rejection and content-free reason codes. Existing `P99:S99` tests preserve syntactically valid unknown IDs; a bounded canonical ID policy can preserve that compatibility.

**DECIDED, proposed outcome policy only:** malformed output should not acquire question relevance from passage position. The new empty-selection regression deliberately differs from current behavior. Current fallback is *not silent*: metrics, trace and answer carry a degraded warning. Therefore “no fallback at all” is not presented as an already-agreed implementation invariant. Defer this policy change from the first patch.

**OBSERVED:** the estimator field is named `estimated`, but source comments/docstring claim bytes/3 never underestimates non-ASCII. A bytes-per-token heuristic cannot guarantee that for every tokenizer/input. Treat the metric as an estimate; the pilot does not validate that universal claim. Existing tiny-context clamp test protects behavior contrary to the requested capacity invariant and will need reconciliation in a later context patch.

Historical 37,458-character failure is referenced by the existing test docstring and its retired-prompt reproduction. It was not independently rerun against old production code or remeasured. The empty-profile/Source Pack historical failure was not recreated; source inspection and passing activation tests establish filesystem pack loading, not whether a particular old profile contained those files.

Missing configuration coverage here means the external provider/key or invalid mode. In local mode, absent Source Packs are not automatically a configuration error: indexed local/cache evidence can still answer. No new requirement makes an empty optional pack directory fail startup or invalidates the matcher.

## Frozen real-model pilot

Loopback endpoint `http://127.0.0.1:11434`; Ollama 0.34.2; `llama3.2:latest`, GGUF 3.2B Q4_K_M, digest `a80c4f17acd55265feec403c7aef86be0c25983ab279d83f3bcd3abbcb5b8b72`. Initially no resident models. Architecture context 131072; initial budget default 4096; subsequent `/api/ps` runtime context 4096. No proxy configured. No cloud model, web request, download, setting change, unload, or retry.

Six synthetic cases, three repetitions, counterbalanced sequential arm order, all frozen before inference. A: compact passages and software-owned source-ID mapping, no quote-transcription task. B: production span selection and, only with verified evidence, production synthesis. Both use temperature 0, thinking off, answer allowance 1200; B selection allowance 256 + JSON format. Neither sets seed or `num_ctx`, preserving production options. Per-arm existing Search deadline 90 seconds, per-call ceiling 120, batch cap 1800. Expected answers/support annotations excluded from requests. Actual boundary captures verify full equal evidence for **36/36 attempted trials**, including the failed call's submitted request; no oversized case enters this comparison.

**Completed schedule: 36/36 attempts, 35 completed arm executions, one initial A timeout; 42/54 maximum generation calls, 138.84 seconds.** The failed call is retained, with token/load metrics unavailable. B's no-evidence executions are completed replays of a normal `search_no_evidence` outcome, not supported answers.

| Measure | A compact text | B pointers |
| --- | --- | --- |
| Completed arm executions | 17/18 | 18/18 |
| JSON / schema validity | N/A (plain text) | 18/18 / 18/18 |
| Pointer membership / exact resolution | N/A | 6/18 / 6/18 |
| Relevant complete support selection | N/A | 6/18; 12 unknown IDs, no valid empty selections |
| Correct factual answers, answerable trials | 6/12 (plus 1 timeout) | 6/12 |
| Correct no-answer outcome, absent trials | 6/6 | 6/6, all through unknown-ID rejection |
| False abstentions, answerable trials | 5/12 | 6/12 |
| Clearly supported factual answer with unambiguous citation | 1/12; 5 further correct answers need footer-attribution review | 6/12 |
| Unsupported factual claims in available output | 0/17 | 0/6 nonempty answers |
| Model-emitted citation membership | 1/1 emitted IDs valid | 3/3 emitted IDs valid |
| Automatically appended Sources footers | 16/17 answers | 3/6 nonempty answers |
| Fallback / needs_more_search | 0 / 0 | 0 / 0 |
| Generation calls / responses | 18 / 17 | 24 / 24 |
| Actual prompt / output tokens, known responses only | 3516 / 410; failed-call tokens unknown | 6762 / 453 |
| Total arm latency / median completed trial | 101.10 s / 0.526 s | 37.73 s / 0.544 s |

All final rendered citation numbers belong to their software mapping. That does not establish support: A's five false abstentions have decorative footers; five correct qualifier answers also list an irrelevant distractor and are flagged for citation-attribution review. The six legitimate absent-answer abstentions are not counted as supported factual answers either. No additional judge-model call was used; rubric-guided analyst review is recorded per attempt. “Operating limit is 4 milliamperes” preserves a ceiling through “limit”; it does not assert an exact required operating current.

The rubric's human-review step has not been independently performed by a person; these are Astra's explicit semantic judgments, with the five ambiguous attribution cases left for review rather than assigned a supported-answer pass.

Case-level factual successes out of three: Vela late support A 0/B 3; Orin second passage A 0/B 0; Daro exception A 3/B 0; Pavo units A 3/B 3. Both absent cases produce three correct no-answer outcomes per arm, but B tries nonexistent pointers. This is a six-case diagnostic, not evidence that either architecture wins generally. Repetitions at temperature zero are not independent samples.

**INFERRED:** cold-load/queue effects contributed to the first timeout and B's first 25.26-second trial. **UNKNOWN:** timeout load duration and exact cause; it returned no runtime metrics. Subsequent successful calls include actual load timings. Initial/final available RAM was 2.68/2.37 GB decimal out of 16.56 GB; loaded model reports 2.91 GB total and 2.34 GB VRAM, indicating partial CPU placement. Process observations capture only names matching Ollama, not complete GPU/child-process memory. No thrashing conclusion or new memory gate.

## Reproduction and artifacts

Run from the root above with the recorded Python executable (PowerShell `python` resolves to it):

```powershell
python -m pytest -q python/tests/test_search_context_budget.py python/tests/test_search_evidence_pointers.py python/tests/test_search_v0.py python/tests/test_search_v1_local_discovery.py --tb=short
python -m pytest -q python/tests/test_v040_job_rpc.py -k search --tb=short
python -m pytest -q python/tests/test_search_baseline_acceptance.py --tb=short
python scripts/run_rag_evals.py --search-replay --output evals/results/search-baseline-2026-09-21/replay
```

The replay command was executed successfully. It refuses an existing output directory; use a new task-owned output path for a future explicitly requested comparison, never overwrite this run. Other RAG CLI options do not tune this frozen mode.

Manifest: `evals/search_replay/manifest.json`, SHA-256 `ca9d2cd9179667a9f5a7d43a6ed784ce3ec9d66dbe1df4135742c8ff9ad2cb23`.
Order SHA-256: `cde04ecb8129c93cc64eac0c45cd976f0936f38b1a5efa52df2dc0e5d0f02008`.
Adapter SHA-256 at inference: `ec64105bd2aa70b312c8c385de9db9b84c6a7ec4b7d64e9fcd470654a9241988`.

Task-owned artifacts under `evals/results/search-baseline-2026-09-21/` (Git-ignored by existing convention): `runtime.json`, `existing-tests.txt`, `acceptance-tests.txt`, `rpc-tests.txt`, `replay-console.txt`; `replay/` contains frozen manifest/order/hashes, full synthetic requests and replies in `trials.jsonl`, model/runtime/resource provenance, `analyst-grades.json`, and `audit.json`. Trial artifact SHA-256: `9f2ce1929abcc065ea1fdc7c991203ba02b95e1f8e8ecbaeeb6bd8cdaefb5fea`. Final offline audit verified hashes, order, coverage, options, and annotation exclusion; no inference rerun.

Harness corrections before final test result: strengthened synthesis test to use evidence that actually fits selection; corrected a privacy-title assertion from filename with extension to the importer’s stem title. The transient extra failures were harness errors, resolved without production changes. Live inputs/adapter/rubric were unchanged after freezing.

Not run: live UI/job Search, acquisition/SQLite.org/external discovery, old-profile reproduction, installer/release work, whole-application suite, long-term retrieval/index/resource/vision comparisons. These are outside this text-evidence baseline, not rejected product directions. No existing dogfood/profile artifacts changed.

Final Git state: same branch and HEAD; tracked modification only `scripts/run_rag_evals.py`; new `scripts/search_replay.py`, `python/tests/test_search_baseline_acceptance.py`, `evals/search_replay/manifest.json`, this report, and `projects/odysseus/SEARCH_OPUS_BRIEF_2026-09-21.md`. Production diff empty. Changes remain uncommitted and separable from the baseline. Handoff: the brief beside this report.
