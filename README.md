# PotatoCS / Odysseus Desktop

**Released local-first RAG desktop app for Windows — built to keep AI answers tied to inspectable evidence on ordinary hardware.**

PotatoCS is the project; **Odysseus Desktop v0.3.1** is the shipped Windows application. It combines local LLM inference with semantic retrieval, OCR, source-grounded answers, diagnostics, benchmark reports, and per-answer operation traces without a default cloud-upload path for documents or chats.

## Engineering snapshot

- **Shipped product:** Windows installer with published checksums and release notes.
- **Stack:** Tauri · Rust · Python · SQLite · Ollama · RAG · OCR.
- **AI system:** local text/embedding/vision models with source-scoped retrieval and evidence-linked answers.
- **Reliability:** explicit lexical fallback, sidecar recovery paths, operation traces, benchmark reports, and release-gate checks.
- **Validation:** Python tests, frontend build, Cargo checks, smoke testing, and installed-app smoke testing.
- **Design constraint:** useful local AI on modest consumer hardware, with privacy and hardware limits treated as product requirements.

PotatoCS explores the opposite of cloud-only AI: making models that people can actually run on the computers they already own more useful through software.

## Download v0.3.1

**PotatoCS** is the project; **Odysseus Desktop** is the shipped Windows app.

The current published release is
[Odysseus Desktop v0.3.1](https://github.com/parthganguly/odysseus-desktop/releases/tag/v0.3.1),
a **patch release** on the v0.3.0 proof/hardening line: it adds a
degraded-backend banner with a Retry action for the sidecar recovery-failure
path, plus a build-safety fix. It ships the same local-first workflow as
earlier builds — OCR, semantic retrieval (RAG), evidence-grounded answers,
and local vision — and adds no new agents, tools, research, or memory
features.

Release assets:

- Installer: `PotatoCs-Odysseus-Desktop-v0.3.1-Windows-x64-setup.exe`
- Checksum: `PotatoCs-Odysseus-Desktop-v0.3.1-SHA256SUMS.txt`

After downloading both files into the same folder, verify the installer in
PowerShell:

```powershell
$installer = ".\PotatoCs-Odysseus-Desktop-v0.3.1-Windows-x64-setup.exe"
Get-FileHash $installer -Algorithm SHA256
Get-Content ".\PotatoCs-Odysseus-Desktop-v0.3.1-SHA256SUMS.txt"
```

The `Hash` value from the first command must match the hash in the checksum
file:

```text
F130D92BB1974035EAA28089B06AC06EF65F251FA072FF9E28595180211C111D
```

Do not run the installer if the hashes do not match.

Windows may show a SmartScreen warning because this installer is not
code-signed yet. Verify the SHA-256 hash before running it.

v0.3.1 is a stabilization patch, not a claim of production readiness. See
the [v0.3.1 release notes](docs/releases/v0.3.1.md) for its full scope and
validation details. The previous release,
[v0.3.0](https://github.com/parthganguly/odysseus-desktop/releases/tag/v0.3.0),
was the proof/hardening baseline; see the
[v0.3.0 release notes](docs/releases/v0.3.0.md).

## Why PotatoCs exists

Hardware manufacturers, cloud supply chains, and the AI market increasingly
optimize around bulk demand from cloud providers, hyperscalers, frontier labs,
and other large buyers. Companies such as OpenAI, Anthropic, Alphabet, and
SpaceX are examples of the compute-intensive economy—not villains, and not
proof that consumer hardware has disappeared.

PotatoCs starts from a simpler constraint: most people have the computer they
already own. Instead of assuming cloud-scale GPUs, the project combines small
local models with deterministic software for retrieval, OCR, evidence
grounding, diagnostics, benchmarks, and transparent operation traces. It does
not claim that this makes a small model equivalent to a frontier system. The
goal is a useful, inspectable local workspace with honest limits.

## What it does

PotatoCs currently provides a Windows-first Tauri desktop shell around a Rust
supervisor, a bundled Python sidecar, a profile-local SQLite database, and an
Ollama-backed local model runtime.

- Imports PDF, text, Markdown, PNG, JPEG, and WebP Sources.
- Retrieves relevant local document chunks with semantic embeddings when an
  Ollama embedding model is installed, with an explicit lexical fallback.
- Produces evidence-grounded answers with inspectable source snippets.
- Runs local OCR through Tesseract for scanned documents and images.
- Supports local Ollama vision models such as Qwen3-VL when installed.
- Supports Florence-2 Basic local vision when its package is present.
- Runs benchmark campaigns and exports local PDF, HTML, and JSON reports.
- Stores sessions, Sources, settings, benchmark history, and operation traces
  in local SQLite profiles.

## Current release: v0.3.1

PotatoCs currently ships **Odysseus Desktop v0.3.1**, a stabilization patch
on the v0.3.0 proof/hardening release. It does not add new product features;
it makes sidecar recovery failure visible through a degraded-backend banner
with a Retry action, and fixes a build step that could delete a tracked
release checksum. See [Download v0.3.1](#download-v031) above for the
installer, checksum, and verification steps, and the
[v0.3.1 release notes](docs/releases/v0.3.1.md) for the full validation
summary. v0.3.0 remains documented as the previous proof/hardening release
in the [v0.3.0 release notes](docs/releases/v0.3.0.md).

The most recent feature release was **v0.2.1**, which added **Stats for
Nerds / Operation Trace** under assistant messages. A trace exposes
per-answer timing, model routing, pipeline state, token counts, RAG/source
identifiers, warnings, and model-trace availability without turning private
prompt or document content into diagnostic payloads. See the
[v0.2.1 release notes](docs/releases/v0.2.1.md) for that change and
validation summary.

## Hardware expectations

- The core local text and RAG workflow is designed for ordinary Windows PCs;
  a discrete GPU is helpful but not required.
- On 8 GB RAM-class machines, prefer smaller text models, OCR, short contexts,
  and conservative retrieval settings.
- Local vision models and the Florence package are substantially heavier and
  may be slow or memory-constrained on modest hardware.
- The v0.2.1 installer is large because it bundles an embedded Python runtime
  and Florence resources.
- Model speed, context capacity, and answer quality depend on the model,
  quantization, available RAM/VRAM, document size, and local runtime settings.

## Features

- Local-first Windows desktop app.
- Ollama-backed local text, embedding, and vision models.
- OCR with Tesseract and optional PDF renderers.
- Florence-2 Basic local vision support in the packaged build.
- PDF, text, Markdown, image, clipboard, and screenshot Sources.
- RAG over local documents with source-scoped retrieval.
- Evidence-grounded answers and retrieved source snippets.
- Benchmark campaigns, comparison views, and offline reports.
- Stats for Nerds / Operation Trace on individual answers.
- SQLite-backed local profiles and restart persistence.
- No cloud upload path for documents or chats by default.

## What is local

- The desktop UI, Rust supervisor, Python sidecar, and SQLite profile storage.
- Imported documents, images, chats, settings, embeddings, benchmark data,
  generated reports, and operation traces.
- Ollama inference through its loopback API, normally
  `http://127.0.0.1:11434`.
- Tesseract OCR, Florence inference, and Ollama vision inference when those
  dependencies are installed or packaged locally.
- App-to-sidecar communication over JSON-RPC on standard input/output; the app
  does not expose an internal web service.

The default profile lives under:

```text
%APPDATA%\dev.odysseus.desktop\profiles\default
```

The profile includes `app.db`, imported file copies, and local logs. Anyone
with access to that Windows account or backup can potentially access this data.

## What is not local / external dependencies

“Local-first” does not mean the installer contains every possible model or
tool.

- Ollama is installed separately and runs as a separate local service.
- Ollama text, embedding, and vision models may need to be downloaded
  separately from their model registries.
- Tesseract and a PDF renderer such as MuPDF `mutool` or Poppler `pdftoppm` may
  need to be installed separately for OCR workflows.
- Some optional model packs and build dependencies may be downloaded during
  developer build or setup steps.
- The release has no automatic cloud fallback. Configuring other tools or
  runtimes can introduce their own network and privacy behavior.

## Privacy model

- User documents, images, chats, settings, and diagnostic records are stored
  locally in the selected profile database and profile folders.
- Documents and chats are not uploaded to a PotatoCs cloud service by default;
  the current app has no such service.
- The app talks to the configured Ollama endpoint. The default is loopback, but
  users should understand the privacy implications before changing it.
- v0.2.1 operation traces deliberately exclude raw prompts, raw model
  responses, source paths, base64 images, full OCR text, full RAG context,
  retrieved chunk text, and full model thinking text.
- Local files are not encrypted by PotatoCs. Windows account security, disk
  encryption, backups, and local Ollama configuration remain part of the
  user's threat model.

## Known limitations

- The app, installer, package, and profile identifiers still use **Odysseus
  Desktop** in v0.3.1.
- The Windows installer is large.
- Ollama models are not bundled and can require substantial downloads.
- Local VLMs and Florence can be slow on consumer hardware.
- OCR quality depends on scan quality, Tesseract languages, and the available
  PDF renderer.
- Small local models can still hallucinate, miss evidence, or over-abstain.
  Citations and operation traces make behavior inspectable; they do not prove
  correctness.
- Window-specific screenshot capture is not yet supported; full-screen and
  coordinate-region capture are supported.

## Roadmap

The next planned line, v0.4, is **Potato Mode + First-Run Runtime
Simplification**: a first-run readiness screen, a one-action conservative
settings preset, guided local model setup, indexing guardrails, profile
storage visibility, and noob-safe diagnostics. See
[ROADMAP.md](projects/odysseus/ROADMAP.md) and
[V04_POTATO_MODE_SCOPE.md](projects/odysseus/V04_POTATO_MODE_SCOPE.md).
Longer-term work includes smaller packaging options, more reliable visual
routing, improved local model guidance, and clearer privacy and performance
diagnostics.

## Development setup

Prerequisites:

- Windows with WebView2
- Node.js and npm
- Rust and Cargo
- Python for tests
- Optional Ollama, Tesseract, and MuPDF/Poppler for integration tests

Install dependencies and run the main checks:

```powershell
npm install
python -m pip install -r python\requirements.txt
python -m pytest python\tests
npm run test:progress
npm run build:frontend
cargo check --manifest-path src-tauri\Cargo.toml
```

Run the desktop app in development:

```powershell
npm run tauri:dev
```

Build scripts are intentionally explicit about packaging variants:

```powershell
npm run tauri:build:core
npm run tauri:build
npm run smoke
```

`tauri:build:core` builds the smaller core package. `tauri:build` prepares the
Florence resources and builds the full NSIS installer under
`src-tauri\target\release\bundle\nsis`.

## Validation status

The v0.2.1 release candidate passed:

- 265 Python tests.
- Frontend build.
- Cargo check.
- Smoke test.
- Installed-app smoke test.

These checks establish implementation and packaging behavior for the tested
build. They are not scientific validation of model accuracy, and no benchmark
result should be generalized beyond its model, fixtures, settings, and
hardware.

## License / attribution

PotatoCs / Odysseus Desktop is distributed under the MIT License in
[LICENSE](LICENSE). It began as a focused desktop fork of the upstream
Odysseus project. Third-party software and optional dependency notices are
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md), with additional
project acknowledgments in [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
