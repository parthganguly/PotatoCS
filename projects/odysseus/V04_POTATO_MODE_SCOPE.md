# v0.4 Scope Proposal — Potato Mode / First-Run Runtime Simplification

Status: proposed scope decision for issue #3. Docs/planning only; no
implementation started. This chooses issue #3 candidate track 3 (local
model/runtime simplification) and defers the `ROADMAP.md` v0.4.0 "safe
bounded tools" line to a later version; update `ROADMAP.md` on acceptance.

## 1. Product thesis

Odysseus Desktop exists for people with ordinary computers, and v0.4 should be
judged by one blunt test: a noob on a low-end Windows machine can install the
app, see in plain language whether it is ready, run one small local model,
import one document, ask one evidence-grounded question, and recover from
failure — without ever needing to understand Ollama, embeddings, OCR
renderers, model routing, Python sidecars, RAM pressure, or diagnostics
jargon. Every v0.4 change either serves that first experience or waits.

## 2. First-20-minutes user journey

1. **Install** — download installer + checksum; honest SmartScreen copy.
2. **Launch** — app opens to a readiness view, not an empty chat.
3. **See readiness** — plain rows: app OK, local AI runtime, chat model, document search, scanned-document reading.
4. **Set up Ollama** — auto-detected; if missing, guided install steps in noob words.
5. **Choose a small model** — one recommended potato-class model with a copyable `ollama pull` command.
6. **Add one document** — browse or drag a PDF; visible progress; cancel works.
7. **Ask one question** — grounded answer in tolerable time on 8 GB-class hardware.
8. **See sources** — snippets show where the answer came from.
9. **Understand limits** — slow, heavy, or unavailable features labeled in plain words, not error codes.
10. **Recover** — backend loss shows the degraded banner; Retry restores; nothing is lost after restart.

## 3. Base function checklist

| Function | Status | Existing evidence in repo | Why it matters on potato hardware | v0.4 action |
|---|---|---|---|---|
| A1 one-click Windows install | Already | v0.3.1 NSIS installer + checksum published; `docs/releases/PotatoCs-Odysseus-Desktop-v0.3.1-SHA256SUMS.txt`, `GATE_v0.3.1.md` §5 | Noobs cannot build from source | Keep; verify unsigned-installer copy |
| A2 first-run readiness screen | Missing | No first-run/onboarding/wizard hits anywhere in `src/` | First minutes decide adoption | Build (v0.4.1) |
| A3 plain-language setup status | Partial | Boot fetches `models.detect_ollama` + `ocr.status` (`src/App.tsx:402-407`) but surfaces jargon | Noobs can't parse diagnostics | Rewrite into readiness rows |
| A4 noob-safe error copy | Partial | Fixed privacy-safe banner copy (`src/features/shell/backendStatus.ts:1-4`); model errors elsewhere are raw | Raw errors cause abandonment | Audit + rewrite user-facing errors |
| A5 current release docs accurate | Missing | README says v0.3.0 is current (`README.md:20-25,94`); `docs/releases/v0.3.1.md:43-47` says installer "Not yet built" | Noobs download the wrong build | Docs fix (see §5) |
| B1 Ollama detected | Already | `detect_ollama()` checks installed/reachable/models (`model_service.py:60-88`) | Runtime is the first dependency | Keep |
| B2 Ollama missing explained | Partial | Status returned but no guided setup in UI | First hard wall for noobs | Setup helper (v0.4.3) |
| B3 local text model detected | Already | `/api/tags` listing; model options built in `src/App.tsx:247` | Model presence gates chat | Keep |
| B4 embedding model detected | Already | `OllamaEmbeddingProvider.installed()` (`embedding_service.py:118-146`) | Gates semantic search | Surface in readiness rows |
| B5 recommended potato model preset | Missing | Only prose guidance (`README.md:117-118`); no recommendation logic found | Noobs can't pick a model | Build (v0.4.3) |
| B6 model download guidance / command copy | Missing | No `ollama pull` helper in UI | Downloads are the setup cliff | Build (v0.4.3) |
| B7 no cloud fallback by default | Already | Hardcoded loopback endpoint (`model_service.py:18`); `README.md:174` | Privacy + no surprise bills | Keep; assert in tests |
| C1 one-click Potato Mode preset | Missing | Settings are a generic KV store (`settings_service.py:8-22`), no presets | One switch beats ten knobs | Build (v0.4.2) |
| C2 small model recommendation | Missing | Same gap as B5 | Wrong model = unusable app | Fold into v0.4.2/v0.4.3 |
| C3 short context default | Unknown | Context handling in `chat_service.py` needs audit | Long contexts stall low-RAM boxes | Audit, then preset |
| C4 conservative retrieval | Partial | Retrieval default `limit: 5` (`rag_service.py:174`); not preset-driven | Retrieval breadth costs RAM/time | Wire into preset |
| C5 low embedding batch size | Missing | No batch-size control in `embedding_service.py` | Big batches spike memory | Add bounded batching |
| C6 OCR off/queued for giant docs | Missing | No page limit, queue, or throttle in `ocr_service.py` | 500-page scan can freeze a potato | Guardrail (v0.4.4) |
| C7 vision disabled / marked heavy | Partial | `ocr_only` backend exists (`src/tauri.ts:408`); heaviness only warned in `README.md:119-120` | VLMs overwhelm low-end HW | In-app "heavy" labels |
| C8 benchmark/report not default | Already | Campaigns are explicit user actions (`campaign_service.py`) | Background work steals cycles | Keep; verify no auto-runs |
| D1 import PDF/text/Markdown/images | Already | `SUPPORTED_EXTENSIONS` (`document_service.py:15`); images (`source_service.py:73-76`) | Core value path | Keep |
| D2 drag/drop or browse | Unknown | Needs UI audit (`src/features/sources/SourcesPage.tsx`) | Noob-expected interaction | Audit; add if missing |
| D3 progress visible | Already | Fixed-label progress events (`progress.py:92-101`; `lib.rs:15-16`) | Silence looks like a hang | Keep |
| D4 cancel/pause indexing | Missing | Cancel exists only for benchmark campaigns (`campaign_service.py`) | Can't escape a heavy import | Build (v0.4.4) |
| D5 lexical fallback without embeddings | Already | `_lexical_status` (`embedding_service.py:388`); lexical rerank (`rag_service.py:815-855`) | Works before embedding setup | Keep + honest copy |
| D6 source snippets shown | Already | Chat sources UI; `README.md:86,134` | Trust needs evidence | Keep |
| D7 "why no answer?" explanation | Partial | Abstain instruction in prompt (`chat_service.py:2274`); user-facing explanation unclear | Silent failure reads as broken | Add plain-words reason |
| E1 Tesseract/renderer readiness | Already | Dependency detection + plain message (`ocr_service.py:259-306`) | OCR deps confuse everyone | Surface in readiness rows |
| E2 scanned PDF detection | Already | Low-text heuristic (`document_service.py:16-17`) | Scans are common noob docs | Keep |
| E3 OCR queue/throttle | Missing | No throttling in `ocr_service.py` | OCR saturates weak CPUs | Guardrail (v0.4.4) |
| E4 image support | Already | PNG/JPEG/WebP imports (`README.md:83`; `source_service.py:73`) | Photos of docs are common | Keep |
| E5 vision models optional/heavy | Partial | Warned in README only | Prevents mystery slowness | In-app labels |
| E6 no surprise Florence requirement | Already | v0.3.1 shipped the core build without Florence (`GATE_v0.3.1.md` §5; `README.md:248-250`) | Keeps installer/RAM small | Keep core as default |
| F1 degraded backend banner | Already | `backendStatus.ts:1-37` (v0.3.1, PR #5) | Honest failure state | Keep |
| F2 Retry backend | Already | `retry_backend` (`lib.rs:865`), `user_retry` (`lib.rs:549`) | Self-service recovery | Keep |
| F3 non-idempotent RPC no replay | Already | `can_restart_and_retry` (`lib.rs:671`); test at `lib.rs:1454` | No silent duplicate writes | Keep |
| F4 ready/degraded/failed states | Partial | Degraded boolean only (`lib.rs:887`); no distinct ready/failed surface | Noobs need one clear state | Fold into readiness screen |
| F5 restart persistence | Already | Profile SQLite WAL (`storage.py:19-38`); `README.md:137` | Losing work kills trust | Keep |
| F6 clean shutdown / no orphan sidecars | Already | Bounded shutdown (`lib.rs:74,613`); zero-orphan release proof (`RELEASE_PROOF_v0.3.1.md`) | Orphans eat scarce RAM | Keep |
| G1 profile size visible | Missing | No size reporting found | Disk is scarce on potatoes | Build (v0.4.5) |
| G2 delete source + derived chunks | Partial | `delete_document` removes chunks (`rag_service.py:164-168`) but uses `mark_deleted`; file-copy cleanup unverified | Reclaiming space must work | Audit + finish (v0.4.5) |
| G3 cleanup cache/reports | Missing | No cleanup surface found | Reports/caches accumulate | Build (v0.4.5) |
| G4 low disk warning | Missing | No disk checks in backend | Full disk = corrupt profile | Add check + warning |
| G5 RAM/CPU warning / slow-machine mode | Missing | No memory/CPU sensing found | Sets honest expectations | Explore in v0.4.2 |
| G6 trace in noob language | Partial | `OperationTrace.tsx` exists but is "Stats for Nerds" | Noobs need "why slow?" not tokens | Plain-words summary (v0.4.6) |
| H1 profile location explained | Partial | Documented in `README.md:152-159` only | Users should know where data lives | Show in-app (v0.4.5) |
| H2 no cloud upload by default | Already | `README.md:180-182`; no PotatoCs cloud path exists in code | Core promise | Keep |
| H3 Ollama endpoint privacy warning | Partial | `README.md:182-183`; endpoint type exists (`src/tauri.ts:12`) with no in-app warning | Non-loopback leaks data | Warn on change |
| H4 support bundle excludes prompts/docs/paths | Missing | Trace exclusions designed (`README.md:184-186`) but no support bundle exists | Safe help-seeking | Build (v0.4.6) |
| H5 diagnostics copy button with redaction | Missing | Banner is privacy-safe by construction (`backendStatus.ts:25-29`); no copy/redact surface | Noobs paste diagnostics publicly | Build (v0.4.6) |

## 4. Already present (foundation to build on)

- Local-first Windows Tauri shell around a Rust supervisor + Python sidecar (`src-tauri/src/lib.rs`, `python/rpc_server.py`, `README.md:79-81`).
- Profile-local SQLite at `%APPDATA%\dev.odysseus.desktop\profiles\default\app.db` (`storage.py:19-38`, `README.md:152-155`).
- Ollama loopback runtime integration with detection (`model_service.py:18,60-88`).
- PDF/text/Markdown/image imports (`document_service.py:15`, `source_service.py:73-76`).
- RAG with semantic retrieval and lexical fallback (`embedding_service.py:85-146,388`, `rag_service.py:810-855`).
- Evidence-grounded answers with source snippets and abstain instruction (`chat_service.py:2274`, `README.md:86`).
- OCR via Tesseract with renderer detection and plain-language dependency messages (`ocr_service.py:259-306`).
- Optional local vision (Ollama VLMs, Florence pack) with an `ocr_only` mode (`src/tauri.ts:408`, `README.md:88-89`).
- Operation traces / diagnostics with deliberate privacy exclusions (`src/features/chat/OperationTrace.tsx`, `README.md:184-186`).
- Degraded-backend banner + Retry from v0.3.1, no-replay guarantee preserved (`backendStatus.ts`, `lib.rs:671,865`, `docs/releases/v0.3.1.md`).
- No default cloud upload path (`README.md:180-182`).

## 5. Likely missing or unclear (needs code audit)

- First-run wizard / readiness screen: no trace in `src/`.
- Potato Mode preset: settings layer has no preset concept (`settings_service.py`).
- Model recommendation logic: none found; prose-only guidance in README.
- Embedding model setup guidance: detection exists, guidance does not.
- Indexing pause/cancel/throttle: cancel exists only for benchmark campaigns.
- Giant-PDF guardrails: no OCR page limits or throttles.
- Profile storage cleanup: `mark_deleted` semantics and file-copy cleanup unverified; no size/cleanup UI.
- Noob-facing performance diagnosis: no RAM/CPU/disk sensing anywhere.
- Doc drift after v0.3.1: `README.md:20-25,94` still says v0.3.0 is the current release; `docs/releases/v0.3.1.md:43-47` still says the installer is "Not yet built" despite `GATE_v0.3.1.md` GREEN and the published checksum; `NEXT_TASK.md` still describes the v0.3.0 installer task.

## 6. Recommended v0.4 non-goals

- No agents.
- No tools (bounded or otherwise).
- No deep research.
- No long-term memory.
- No skills marketplace.
- No Cookbook.
- No blind compare.
- No new vision backend.
- No cloud sync.
- No account/login system.
- No full UI redesign unless required for first-run readiness.

## 7. v0.4 acceptance criteria

- Fresh install on a low-end Windows machine shows a readiness screen within first launch.
- User can tell whether Ollama, a text model, an embedding model, and OCR are ready — in plain language.
- Potato Mode applies conservative settings (small model, short context, conservative retrieval, low embedding batch, OCR guardrails) in one action.
- User can import one normal PDF and ask one question with visible sources.
- If embeddings are unavailable, lexical fallback still works with honest copy.
- If the backend fails, the degraded banner appears and Retry recovers.
- Heavy features (vision, Florence, benchmarks) are marked optional/heavy in-app.
- User can see profile storage size and clean it up.
- No raw prompts, documents, or paths leak in diagnostics or support bundles.
- README points to the latest release and no longer says v0.3.0 is current.

## 8. Proposed issue breakdown

- v0.4.1 — First-run readiness screen (A2, A3, B4, E1, F4).
- v0.4.2 — Potato Mode settings preset (C1-C5, G5).
- v0.4.3 — Model/Ollama setup helper (B2, B5, B6).
- v0.4.4 — Indexing throttle/pause/cancel + giant-doc guardrails (C6, D4, E3).
- v0.4.5 — Profile storage visibility and cleanup (G1-G4, H1).
- v0.4.6 — Noob diagnostics + redacted support bundle (G6, H4, H5).
- docs — Update README current-release/download sections, fix `docs/releases/v0.3.1.md` installer wording, refresh `NEXT_TASK.md`/`ROADMAP.md` (A5).

## 9. Final recommendation

Recommended v0.4 scope: Potato Mode + First-Run Runtime Simplification. Do
not start agents/tools until this baseline is excellent.
