# Potato Niche Essentials

Status: planning addendum to `V04_EXECUTION_PLAN.md`. Docs only. This is
the checklist of niche-but-essential low-end behaviors — the unglamorous
things that decide whether a weak machine feels safe, understandable, and
recoverable. Statuses come from the `V04_POTATO_MODE_SCOPE.md` §3 audit
plus repo reading; **Unknown means the v0.4 audit issue must resolve it**,
not that it is fine. Issue numbers refer to `V04_ISSUE_BREAKDOWN.md`.

Column key — Why: why it matters on potato hardware. Status:
Already / Partial / Missing / Unknown. Check: where to verify in repo.
Issue: suggested v0.4 issue or Future. Fable: strong-model/human review
required before merge.

## A. Startup / readiness

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| First-run readiness screen | First minutes decide adoption; noobs can't debug | Missing | `src/` (no onboarding hits) | 3 | Yes (design) |
| No empty chat as first impression | Empty chat reads as "broken" | Missing | `src/App.tsx` launch path | 3 | Yes (copy) |
| App/backend/Ollama/model/embedding/OCR status rows | One glance replaces an hour of debugging | Partial | `App.tsx:402-407`, `model_service.py:60-88`, `ocr_service.py:259-306`, `embedding_service.py:118-146` | 3 | Yes (state model) |
| Copyable exact next step per gap | Noobs cannot compose terminal commands | Missing | no `ollama pull` helper in UI | 5 | Yes (which command) |
| Offline setup explanation | Offline users must still finish setup or know why not | Missing | README assumes download works | 5 + docs issue 12 | Yes |

## B. Model fit

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| Detect installed models | Presence gates chat | Already | `model_service.py` `/api/tags`; `App.tsx:247` | keep | No |
| Recommend small/balanced/heavy | Wrong model = unusable app | Missing | no recommendation logic | 4/5 | **Human decision** |
| Warn if model likely too big for RAM | Swapping machine feels dead | Missing | no memory sensing (`G5`) | 2 audit → 4 | **Human thresholds** |
| No auto-download | Surprise multi-GB pulls kill trust and disks | Already (policy) | baton §G; no download code | keep + test | Yes (guard it) |
| Copyable `ollama pull` command | The setup cliff for noobs | Missing | no helper in UI | 5 | Yes (copy) |
| Embedding model guidance | Missing embeddings silently degrade RAG | Partial | detection exists; no guidance | 5 | Yes |
| Conservative context-length default | Long contexts stall low-RAM boxes | Unknown | `chat_service.py` context handling | 2 audit → 4 | **Human values** |

## C. Runtime resource control

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| Potato Mode preset | One switch beats ten knobs | Missing | `settings_service.py:8-22` (generic KV only) | 4 | **Human values** |
| Low retrieval count | Retrieval breadth costs RAM/time | Partial | `rag_service.py:174` (limit 5, not preset-driven) | 4 | Yes |
| Low embedding batch size | Big batches spike memory | Missing | `embedding_service.py` (no batch control) | 4 | Yes |
| OCR page cap | 500-page scan freezes a potato | Missing | `ocr_service.py` (no limits) | 6 | Yes (semantics) |
| Queue long OCR/indexing jobs | Weak CPU must stay usable | Missing | no queue/throttle | 6 | Yes (semantics) |
| Pause/cancel jobs | User must be able to escape | Missing | cancel only in `campaign_service.py` | 6 | **Yes — written spec** |
| Heavy vision off by default, labeled | VLMs overwhelm low-end HW | Partial | `src/tauri.ts:408` (`ocr_only`); README-only warnings | 4 + docs | Yes |
| "This may be slow" copy | Honest expectations beat silent crawl | Missing | no such copy in UI | 3/4 | Yes (copy) |
| No benchmark/report generation by default | Background work steals scarce cycles | Already | `campaign_service.py` (explicit user action) | keep | No |

## D. Storage

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| Profile size visible | Disk is scarce; invisibility breeds fear | Missing | no size reporting | 7 | No |
| Per-source size visible | Users must find what is eating disk | Missing | Sources UI | 7 | No |
| Derived chunk/cache/report size visible | Derived data can dwarf sources | Missing | no cleanup surface | 7 | No |
| Delete source reclaims derived data | Reclaiming space must actually work | Partial | `rag_service.py:164-168` (`mark_deleted`; file copies unverified) | 7 | **Yes — deletion review** |
| Cleanup button (caches/reports) | Easy recovery from full disk | Missing | none | 7 | Yes |
| Low disk warning | Full disk = corrupt profile | Missing | no disk checks | 7 | Yes |
| Never delete outside profile | One bad path destroys user files | Unknown | `storage.py`; issue 7 acceptance | 7 | **Yes — mandatory** |

## E. Reliability

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| Degraded backend banner | Honest failure state | Already | `backendStatus.ts:1-37` (v0.3.1) | keep | No |
| Retry backend | Self-service recovery | Already | `lib.rs:865`, `lib.rs:549` | keep | No |
| Fixed-label logs/progress | No jargon or leaked content | Already | `progress.py:92-101`, `lib.rs:15-16` | keep | No |
| No orphan sidecars | Orphans eat scarce RAM | Already | `lib.rs:74,613`; `RELEASE_PROOF_v0.3.1.md` | keep | No |
| Crash-safe job state | Kill mid-index must not corrupt | Unknown | WAL proved (`storage.py:19-38`); indexing jobs unproved | 6 | Yes |
| App close during job is safe | Potato users force-close often | Unknown | needs kill-during-job test | 6 | Yes |
| Restart persistence | Losing work kills trust | Already | `storage.py:19-38`; `README.md:137` | keep | No |

## F. Noob diagnostics

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| "Why is it slow?" panel | Slowness is the #1 potato complaint | Partial | `OperationTrace.tsx` ("Stats for Nerds" only) | 8 | Yes (copy) |
| Plain English, not trace jargon | Raw errors cause abandonment | Partial | `backendStatus.ts` good; model errors raw | 3/8 | Yes |
| Redacted support bundle | Asking for help must not leak documents | Missing | none | 8 | **Fable/human-only policy** |
| No raw prompts/docs/paths in diagnostics | Privacy is the product promise | Already (trace) | trace exclusions; extend to bundle | 8 | **Yes — may only tighten** |
| Private sentinel tests | Prove redaction, don't assert it | Partial | sentinel sweep exists for trace; extend to bundle | 8 | Yes |

## G. Installer / package

| Item | Why | Status | Check | Issue | Fable |
|---|---|---|---|---|---|
| Core vs heavy package clarity | Wrong variant wastes a potato's disk/RAM | Partial | `README.md:248-250`; `GATE_v0.3.1.md` §5 | docs issue 12 | Yes |
| No surprise Florence/heavy dep in potato path | Keeps installer and RAM small | Already | v0.3.1 core build proof | keep + gate check | No |
| Install size visible before download | Metered/small-disk users must know | Unknown | release pages | docs issue 12 | No |
| Unsigned app / SmartScreen explanation | Scary warnings lose noobs at the door | Partial | scope A1 ("verify unsigned-installer copy") | docs issue 12 | Yes (copy) |
| Hash verification docs | Trust without a signature | Already | `docs/releases/*SHA256SUMS*`; gate §5 | keep | No |
| Latest release docs stay current | Noobs download the wrong build | Partial | README drift fixed via issue 1 baton PR | 1 (verify) | No |

## Reading this list

Nothing here is exciting. All of it is the difference between "local AI
demo" and "app a person with a weak computer actually keeps." Items marked
Unknown are audit obligations, and items marked **Human decision** must
not be guessed by any model — see baton §G.
