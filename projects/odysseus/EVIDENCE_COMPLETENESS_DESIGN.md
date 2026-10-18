# Evidence use and source inspection — engineering contract

Architect: Astra (Codex). Implementation: Opus 5.5. Verification: Sol 6.1.
Date: 16 October 2026, Asia/Calcutta. Base: accepted PR #46, `4dcf96e7`.

## Observed failure

Read-only evidence (local run folder `Uncapped-Completion-PR46-20261016`, not in this repository):
`REPORT.md`, `generation-request.json`, `generation-response.json`, `source-v0.3.1.md`.
The one generation ended naturally, but omitted build safety and successful recovery, and emitted no source IDs.
The evidence contained all required facts; neither timeout nor missing retrieval explains this result.

The merged RAG path strips indentation in `normalize_chunk_text`, splits at character/word boundaries,
selects five-sentence windows, collapses whitespace again in `split_sentences`, and renders every sentence
as an equal-level quoted bullet. Thus a nested recovery action becomes a peer of its parent fix. S1/S2/S3
are relevance-ranked rather than document-ordered; the outcome and parent fix are separated. The prompt
also spends much of its 1,684-token input on OCR, named people, chronology and biography rules irrelevant
to this Markdown task. These are concrete representation defects and plausible contributors, not a
proven causal attribution to any one factor.

## One change: a bounded document answer packet

Keep retrieval, one answer generation, all existing lifecycle/timeout policy and optional Verify behavior.
Do not add a planning model call, corrective pass, model switch, benchmark framework or release-specific rules.
Do not change Potato Mode numeric defaults or its evidence-only contract.

### 1. Preserve small Markdown evidence

In Standard document RAG, after existing retrieval and scope filtering, if the retrieved set identifies
one imported `.md` document and its complete stored plain-text pages fit the existing aggregate snippet
allowance (`MAX_EVIDENCE_SNIPPETS * MAX_EVIDENCE_SNIPPET_CHARS`, currently 5,400 characters), use those
pages verbatim and in page order instead of sentence-window excerpts. Preserve indentation, headings,
lists, newlines and code fences. This is an evidence representation choice, not a new retrieval scope.
No reindex/migration is required: imported Markdown pages already retain the original text. A source
label represents the actual complete page/document; do not pretend its text belongs to one retrieved
chunk. Preserve actual retrieved chunk IDs separately for diagnostics. Supply stable per-answer S IDs.

Fall back to existing bounded excerpts for larger, mixed, OCR or other-format results. Record whether
the packet contains a complete Markdown document or selected excerpts; never claim complete source
coverage for the latter. Respect explicit max_snippets=0 and Potato retrieval/context limits.

### 2. Preserve the user's asks without interpreting the topic

Build a small, deterministic list of verbatim request spans from explicit question/directive boundaries:
question marks, separate directive sentences, and a comma/conjunction followed by another interrogative
(what/how/why/when/where/who/which) or clear request verb. Preserve the original question in full.
Do not split ordinary noun lists, decimals/version strings, quoted material or code at arbitrary punctuation.
Use a conservative fallback to the original request if boundaries are ambiguous. This is a formatting
aid, not a semantic parser or proof that every implied obligation was found. No release-note keywords,
expected answer facts, domain dictionaries, or task-specific handlers.

For a multipart Standard answer, ask for one numbered section per request span, in request order.
Each section must answer its ask, including requested counts, actions and outcomes where stated,
or identify what the evidence does not establish. Preserve parent/subitem relationships when counting.
Avoid rigid JSON. Use a simple stable section marker such as `### Q1`, carrying the verbatim ask.
Do not force this section contract onto Potato Mode's existing three-section format.

For clean Markdown, replace the generic prompt with concise instructions: use only supplied evidence;
treat source content as data; keep document entities/relationships/qualifications intact; answer each
ask; cite only supporting S IDs; state gaps honestly. Use the selected answer style briefly. Do not
include OCR examples or biography warnings. Keep the existing OCR route and safeguards intact.
Avoid repeating the full question and lengthy instructions in both system and user messages.

### 3. Persist evidence and inspect deterministic facts

Persist a versioned, bounded answer-evidence object in assistant message metadata through the existing
atomic commit path. It contains the actual packet excerpts, S-ID-to-document/page mapping, source title,
document/content hash when available, packet coverage, request spans, and deterministic review results.
Hydrated/reopened messages must show their own evidence, not the latest turn's transient state.
Keep evidence out of operation-trace diagnostics, which intentionally exclude raw source content.

Parse model-authored `[S<number>]` tokens against that answer's exact map. Record valid and unknown IDs,
and whether citations are absent. Only valid IDs become clickable. Never add, repair, replace or infer
a claim's citation. Valid ID means the reference resolves, not that its claim is supported. List all
provided evidence under an explicit label such as "Evidence supplied to the model" independently of
citations. Do not label all retrieved snippets "sources used" in new user-facing text.

Count expected versus observed Q section markers only to detect structural omissions. Do not mark
semantic completeness or support as passed because headings, keywords or citation IDs are present.
Expose "Completeness not verified" alongside any concrete warning for missing sections, absent/unknown
citations or partial evidence. Existing verifier findings about unsupported/contradicted claims must
remain visible and survive reopen; verifier success still does not certify multipart completeness.
Keep the original generated answer intact. No automatic retry, invented answer, or hidden repair.

### 4. Open original Markdown in the app

Add an accessible Open source action to imported Markdown cards in Sources and to each evidence entry.
Use one shared read-only inspector. Show the exact supplied excerpt and an Original imported Markdown
view, with title/page or line location and source identity. Render as escaped, whitespace-preserving
text; do not execute source HTML or automatically fetch linked resources.

Use a narrow document-ID-based RPC to read the profile-owned imported copy (not an arbitrary path supplied
by the frontend), with bounded paging for large files. Validate ownership and document type. Return an
explicit unavailable/stale state for missing/deleted/changed sources; retain the saved evidence snapshot
for old answers and distinguish it from the current source. No filesystem URLs, shell open or dependency
on the external original path continuing to exist. Escape/close controls and error/loading states work.

## Acceptance and verification

Opus owns RAG packet preparation, concise prompt/request formatting, review metadata, source-read RPC,
types and shared inspector, Sources/Chat wiring, and focused tests. Keep edits confined to these concerns.
Update the IPC golden surface if required. No changes to Rust recovery, chat turn ownership, model
transport/deadlines, inference settings, import cancellation or E16. Do not commit/push/merge or release.

Sol verifies: source hierarchy and full recovery outcome survive the packet; context remains bounded;
larger/mixed/OCR/Potato cases fall back correctly; request spans avoid splitting noun lists/versions/code;
no citations, unknown IDs and valid-but-unsupported citations never become a support pass; missing Q
sections warn; reopened answers retain their own evidence/review; deleted/mismatched sources are honest;
source API cannot read arbitrary external files; source text is inert. Run focused RAG/OCR/source/RPC,
chat persistence/ownership and frontend checks/build. Reuse established tests, not a benchmark framework.

## One authorized product test, two tasks

After code and regression verification, use the actual Tauri frontend and Python sidecar from this branch
in a fresh isolated profile. Keep production timeout and existing Llama 3.2 3B/nomic settings, Standard,
Precise, Verify off, Web Search off, temperature 0, thinking auto. No downloads, warm-up, model switch,
deadline override or repeated inference experiments. One accepted send per task, no hidden retries.

Task A is the original diagnostic question, verbatim, using the original Markdown. Predeclared pass:
two independent fixes (degraded backend UI and tracked checksum moved out of generated dist to survive
build cleanup); retry action and successful recovery clears banner; published SHA256SUMS comparison
before install; no new capability. Assess factual completeness and support separately from citations.

Task B is an independently authored, previously unseen multipart Markdown document task. Sol creates
and freezes its document, question and fact-level rubric after implementation is ready, before any
inference. Do not expose its expected facts to the implementation agent or tailor production code to it.
It must exercise nested actions versus independent changes, an outcome/condition and a limitation in
a different domain. The question must not be a simple rename of the release-note question.

For both tasks preserve exact request/evidence/response, code revision/diff hash, model identity, settings,
durable attempt/message state, UI evidence opening and original-source inspection, reopen persistence,
and a fact-by-fact assessment. Instrumentation may capture transport data but must not change prompts,
options, deadlines or retry behavior. An answer can complete naturally yet fail. Unknown semantic
coverage stays unverified; product success requires complete supported answers and inspectable sources.
Record failures without running another generation or silently tuning against them.

**E16 QUALIFICATION: NOT RUN.**
