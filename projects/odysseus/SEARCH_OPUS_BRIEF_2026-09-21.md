# Opus High — bounded Search diagnostic privacy patch

**OPUS IMPLEMENTATION READY.** Use Ponytail and Graft if already installed. Do not install them. No architecture replacement is requested. Astra has finished baseline inspection, tests, and the live pilot; no production repair was made.

Worktree: `C:\Users\Parth Ganguly\Documents\Codex\odysseus-search-v1-local-discovery`.
Baseline: `9d2f21a7fe6fcae342bb1dc3ad545991816d279c`, branch `codex/search-v1-context-budget`, parent `dcb6d7183a3f82a198d59717852b2dd168d88937`. Preserve the uncommitted test/evaluation/report deliverables. Inspect current status before starting; do not reset or overwrite work. Python: `C:\Users\Parth Ganguly\AppData\Local\Programs\Python\Python313\python.exe`.

## User-visible outcome and smallest first patch

When a model echoes private source content in a supposed span identifier, Search must reject it without copying that content into durable diagnostics. Sources, quotations, and conversation remain available to their owner. Unknown IDs must never acquire evidence or citations.

Confirmed reproduction: `python/tests/test_search_baseline_acceptance.py::test_private_content_stays_out_of_durable_diagnostics[unknown_pointer]`. This calls actual SearchService retrieval/selection/verification/persistence on a temporary synthetic private document. A model reply containing `{"span_ids":["SYNTHETIC_PRIVATE_SENTINEL_9381"],"needs_more_search":false}` is schema-valid today. Verification rejects it, but copies the raw string to `EvidenceDiagnostic` and `TraceOperation`; both persist.

Trace every caller of `verify_evidence_span_selection` and its diagnostic serialization. Make the smallest change at the shared rejected-pointer diagnostic boundary, so round one, repair, persistence and Operation Trace are covered together. Retain content-free `unknown_span`, counts, and `pointer_resolved=false`. Only bounded canonical pointer identifiers may be recorded; arbitrary model strings must be omitted/redacted. Keep exact membership verification unchanged. No new framework, dependency, schema, prompt, model-default, or broad logging rewrite is needed.

Compatibility: existing tests expect canonical unknown `P99:S99` to remain identifiable; preserve that through a bounded grammar check if using this policy. A stricter “known IDs only” policy would require an explicit documented test-contract change, not silent test deletion. Do not store truncated private text, its hash, or a second raw-output field as a workaround.

## Acceptance

```powershell
python -m pytest -q python/tests/test_search_baseline_acceptance.py -k private_content_stays_out --tb=short
python -m pytest -q python/tests/test_search_context_budget.py python/tests/test_search_evidence_pointers.py python/tests/test_search_v0.py python/tests/test_search_v1_local_discovery.py --tb=short
python -m pytest -q python/tests/test_v040_job_rpc.py -k search --tb=short
python -m pytest -q python/tests/test_search_baseline_acceptance.py --tb=short
```

First command must become **4 passed** (currently 3 pass, 1 fail). Existing Search suites must retain **164 passes** and RPC **1 pass**. Full new suite should become **6 passed, 6 known failures** after this privacy-only patch. Enumerate those remaining failures; do not mark the entire baseline green, weaken tests, or add xfails. Add a focused bounded-ID edge check only if the chosen validation introduces uncovered logic. No live inference is needed to prove this first patch.

## Demonstrated follow-ups, not part of the first patch

| Defect / observation | Reproduction in new acceptance file | Follow-up scope |
| --- | --- | --- |
| Small reported capacity raised to 2048 | `test_small_reported_context_is_never_raised` | Preserve lower runtime limits or refuse safely; reconcile old clamp expectation. |
| Forced span and fixed overhead exceed selection allowance | `test_impossible_window_does_not_force_first_span`, `test_impossible_complete_request_never_reaches_model` | Budget the complete request and refuse an impossible call. |
| Configured budget is not sent as requested context | `test_configured_budget_agrees_with_requested_runtime_context` | Make request capacity and accounting agree without shared Ollama changes. |
| Synthesis exceeds the estimator-based request bound after valid selection | `test_synthesis_complete_request_and_generation_fit_context` | Cover downstream request plus its 1200-token generation allowance. |
| Positional fallback chooses an unrelated passage | `test_malformed_selection_does_not_manufacture_relevant_support` | Outcome-policy decision required; current fallback is explicitly degraded, not silent. |

The four context rows are candidates for a subsequent coherent request-integrity patch, not a mandate to fix everything now. Token estimates remain estimates, not universal tokenizer guarantees. The fallback regression deliberately expresses a proposed stronger policy; do not change production semantics merely to make it green without resolving that contract.

## Invariants and limits

Preserve exact retained-source resolution, rejection of unknown/non-visible pointers, valid-empty versus malformed distinctions, local/external configuration routing, private repair-query isolation, bounded retrieval/acquisition, cancellation, staging/rollback, and existing Source Packs. Do not add live web searches, model downloads, cloud inference, raw private diagnostics, installer/release work, or cleanup refactors. Never modify personal or past dogfood profiles.

Pilot evidence is mixed: 36 attempts, 35 completed, one initial timeout, 42 generation calls; A and B each answer 6/12 answerable cases correctly. B is valid JSON 18/18 but selects unknown IDs 12/18, which fail closed. A often abstains with a decorative Sources footer. Six no-answer outcomes per arm are appropriate for absent evidence; B reaches them by rejecting bad IDs rather than selecting an empty list. This supports better boundary/outcome handling, not removing the pointer architecture or requiring it to win.

Unresolved, separate from this patch: exact load-timeout cause; tokenizer calibration across inputs; whether/how to retain a relevance-aware degraded fallback; citation-footer attribution; wider model/corpus performance; UI/job live-model behavior. Vision, index accumulation, broader discovery, and resource-adaptive execution remain later product questions. None blocks this privacy fix or currently requires an architecture decision at higher effort.

Evidence, commands, all failures, hashes, and artifact locations: `projects/odysseus/SEARCH_BASELINE_RESULTS_2026-09-21.md`. Frozen cases: `evals/search_replay/manifest.json`. Raw synthetic evidence and per-attempt analyst grades: `evals/results/search-baseline-2026-09-21/replay/`. Stop after the bounded implementation and its verification; do not bundle the follow-ups.
