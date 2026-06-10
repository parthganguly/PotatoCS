# Release Notes

## v0.1.3 - Diagnostics and Model Benchmark

v0.1.3 turns the local RAG eval harness into an in-app Diagnostics / Model
Benchmark area. This supports the broader Odysseus Desktop thesis: helping
small local models become more useful on limited hardware with retrieval,
source scoping, OCR, answer styles, verification, search, and benchmarks while
remaining private and local-first. It does not add agents, autonomous tools,
shell access, cloud sync, Chroma, Docker, or hidden HTTP.

Highlights:

- Added a Diagnostics tab with app version, profile path, backend/database/log
  paths, Ollama status, installed Ollama models, OCR dependency status, and RAG
  health.
- Added an in-app Model Benchmark runner for the existing local RAG eval
  fixtures.
- Added verifier on/off benchmark runs so local models can be compared by
  pass/fail and latency.
- Persisted compact benchmark history in profile-local SQLite.
- Added a copyable Markdown benchmark summary table.
- Reused the shared eval service from both the app and
  `scripts\run_rag_evals.py`.
- Bundled the `evals\` fixtures into the Windows installer resources.
- Clarified that benchmarks use bundled temporary eval fixtures, not the user's
  imported Documents library.
- Showed basic Ollama model stats where Ollama reports them.
- Kept evals as regression examples only; runtime behavior is not
  special-cased for any named fixture, document, or query.

Scope intentionally unchanged:

- No agents.
- No tools.
- No shell execution module.
- No email or calendar modules.
- No MCP.
- No Cookbook.
- No gallery/editor.
- No Chroma.
- No Docker.
- No hidden HTTP service.
- No model auto-download.
- No cloud-model dependency.

Validation commands for v0.1.3:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py --models llama3.2:latest
python scripts\run_rag_evals.py --models llama3.2:latest --verify
npm run tauri:build
```

## v0.1.2 - RAG Answer Quality

v0.1.2 improves general RAG answer quality for small local models without
claiming frontier-model correctness and without adding agents, tools, shell
access, cloud sync, Chroma, Docker, or hidden HTTP.

Highlights:

- Added RAG answer styles: Precise, Layman, Detailed, and Extract only.
- Added a lightweight intent/style instruction layer for RAG prompts.
- Kept Precise as the default style to preserve current behavior.
- Strengthened general evidence discipline without hard-coding specific
  documents, fixture names, or example queries.
- Preserved source-scoped retrieval for selected documents.
- Tuned verifier wording so it reads as a warning system: grounding looks okay,
  grounding needs review, unsupported claim detected, possible contradiction
  detected.
- Added a compact answer-style selector near the existing RAG and Verify
  controls.
- Added general eval cases for chronology preservation, cross-document
  contamination, procedural-document interpretation, layman explanation, and
  extract-only behavior.
- Extended tests for JSON-RPC style pass-through, style-specific prompt shaping,
  scoped retrieval, styled verifier correction, and non-RAG chat behavior.

The eval fixtures remain regression examples only. Runtime behavior is not
special-cased for any named fixture, document, or query.

Validation commands for v0.1.2:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py
python scripts\run_rag_evals.py --verify
npm run tauri:build
```

## v0.1.1 - RAG Reliability

v0.1.1 improves RAG grounding for local models while keeping the app
privacy-first and Windows-first.

Highlights:

- Added quote-first RAG answers using short evidence snippets instead of full
  noisy chunk text.
- Preserved source/page/chunk/snippet metadata for retrieved evidence.
- Added source-scoped retrieval so chat can prefer a selected indexed document.
- Added optional verifier mode to classify claims as supported, unsupported, or
  contradicted against retrieved snippets.
- Added one regeneration attempt when verifier mode detects contradicted claims.
- Added retrieved snippet and grounding status display in the chat UI.
- Added a local RAG eval harness under `evals\` and `scripts\run_rag_evals.py`.
- Added eval cases for grandfather chronology and cross-document contamination.
- Added model benchmark support for installed Ollama models with local-only
  pass/fail and latency reporting.

Scope intentionally unchanged:

- No agents.
- No tools.
- No shell execution module.
- No email or calendar modules.
- No MCP.
- No Cookbook.
- No gallery/editor.
- No Chroma.
- No Docker.
- No hidden HTTP service.
- No cloud-model dependency.

Validation commands used for v0.1.1:

```powershell
python -m pytest python\tests
npm run build
python scripts\run_rag_evals.py
python scripts\run_rag_evals.py --verify
npm run tauri:build
```
