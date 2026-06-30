# Milestone Gates

## Milestone 1: Desktop Foundation

Acceptance:

- The desktop app creates a default local profile on first launch.
- Rust starts and supervises the bundled Python sidecar.
- React talks to Rust, and Rust talks to Python using JSON-RPC over stdio.
- Settings, sessions, messages, and runtime status persist in SQLite.
- Ollama detection probes only `127.0.0.1:11434`.
- Basic chat sends one prompt to Ollama and stores the user and assistant messages.
- Closing the app sends `app.shutdown` and terminates the sidecar cleanly.
- Relaunching restores settings, sessions, and messages.

Milestone 1 must not include:

- document context
- RAG
- memory retrieval
- tools
- agents
- advanced routing
- email
- calendar
- shell tools
- Cookbook
- gallery/editor
- full MCP

## Milestone 2: Documents And RAG

Start only after Milestone 1 passes.

- Add document import, chunking, embeddings, retrieval, deletion, and reindexing.
- All vector behavior goes through a `VectorStore` abstraction.
- MVP `VectorStore` uses SQLite + NumPy.
- Embeddings are cached by chunk/content hash so unchanged chunks are not
  re-embedded.
- LanceDB or sqlite-vec must be swappable later without rewriting RAG callers.
- OCR is not implemented in Milestone 2. Low-text/scanned PDFs are marked with
  `index_status = low_text` and surfaced in the UI as Milestone 3 work.
- Chat RAG is explicit per request. Default chat remains the Milestone 1
  non-RAG path.

## Milestone 3: OCR And Migration

Start only after Milestone 2 passes.

- Detect low-text/scanned files.
- Offer optional OCR only when an engine is available.
- Store OCR text with page/source metadata and confidence where available.
- Import compatible existing Odysseus data non-destructively.
- OCR uses detected local tooling only. MVP detection looks for Tesseract plus
  a PDF renderer (`pdftoppm` or `mutool`).
- OCR output replaces the document page text and flows through the existing
  RAG path; there is no separate OCR index.
- Legacy import reads old folders only, copies compatible data into the active
  profile, and reports skipped/incompatible/failed items.

## Milestone 4: Sources, Attachments, Images, And Screenshots

Start only after the v0.1.12 RAG benchmark/report path remains stable.

- Add an artifact boundary for images instead of treating images as documents.
- Add one Sources facade over document and artifact services for PDFs, text
  files, images, and screenshots.
- Add universal chat attachments for PDFs, TXT/Markdown, images, pasted images,
  screenshots, and existing saved Sources.
- Keep direct attachments session-scoped by default and hidden from the global
  Sources library until promoted.
- Store profile-local originals, thumbnails, bounded vision derivatives,
  lossless OCR derivatives, crops, OCR text, vision observations, and combined
  evidence with additive migrations.
- Support PNG/JPEG/WebP import, clipboard image paste, and explicit
  Windows-first screenshot capture without background monitoring.
- Refactor OCR so direct images and rendered PDF pages share a timeout-bounded
  image OCR primitive.
- Use Ollama `/api/show` for model capability inspection; require confirmed
  local vision support for vision modes.
- Add optional Florence 2 Basic local vision only from a prepared local model
  pack. Normal runtime must not download model files or import the heavy
  Florence runtime at startup.
- Curate Florence and external-eyes visual evidence before weak final-model
  synthesis: deduplicate noisy entities, keep raw evidence inspectable, separate
  direct observations from supported inference, guard unsupported identity,
  location, event, brand, emotion, and causal-light claims, and treat OCR
  no-text as informational for non-text image questions.
- Default chat image routing to Automatic while keeping OCR-only, vision-only,
  and combined modes available under advanced controls.
- Link image artifacts to chat messages without storing binary payloads in
  message content.
- Link document attachments to chat messages and use scoped RAG for direct
  PDF/TXT/Markdown attachments.
- Keep session attachments available to later turns until removed from
  conversation context, while promotion to persistent Sources remains a
  separate user action.
- Keep the default model for new chats in Settings and the active conversation
  model in the chat header. Changing the active conversation model must not
  mutate the global default.
- Build the conversation model selector from installed chat-capable Ollama
  models only. Deduplicate `:latest` aliases, preserve sized tags, exclude
  embedding-only models, and mark deleted historical session models as readable
  but not selectable.
- Allow user-triggered indexing of image-derived text into existing text-only
  RAG with artifact provenance, while hiding generated internal documents from
  the normal Documents list.
- Add Source/Image/Vision diagnostics and a separate local image benchmark
  suite.

Milestone 4 must not include agents, unrestricted tools, shell execution, MCP,
cloud fallback, normal-runtime model downloads, image embeddings, generative
image editing, or continuous screenshot capture.
