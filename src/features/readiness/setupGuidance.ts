/**
 * Fixed setup guidance attached to readiness rows (Issue 5 / #15). Every
 * string here is static copy: nothing is derived from status payloads, so raw
 * errors, paths and RPC fragments can never leak into guidance by
 * construction (same contract as the row copy in `readinessRows.ts`).
 *
 * Commands are copy-only. The app never runs them, never downloads models
 * itself, and never adds network calls beyond the existing loopback Ollama
 * checks. Concrete model names here are limited to what
 * `projects/odysseus/V04_MODEL_RECOMMENDATION_DECISION.md` approves: the
 * embedding model already wired as the backend's default
 * (`nomic-embed-text`) and the chat-model default approved in that decision
 * (`llama3.2:3b`, worded "try this first", not "recommended" — no
 * potato-proof runs have validated it yet).
 */

export type SetupCommand = {
  /** Short label describing what copying this text is for. */
  label: string;
  /** Exact text placed on the clipboard. Never executed by the app. */
  text: string;
};

export type SetupGuidance = {
  /** Ordered plain-language steps the user performs outside the app. */
  steps: string[];
  /** Optional copyable text. Omitted when no approved command exists. */
  command?: SetupCommand;
};

/** Ollama is not installed at all. */
export const OLLAMA_INSTALL_GUIDANCE: SetupGuidance = {
  steps: [
    "Download Ollama from the address below and run the installer.",
    "Open the Ollama app once so it starts running in the background.",
    "Come back here and click Re-check."
  ],
  command: { label: "Download address", text: "https://ollama.com/download" }
};

/** Ollama is installed but the local server is not reachable. */
export const OLLAMA_START_GUIDANCE: SetupGuidance = {
  steps: [
    "Start the Ollama app, or run the command below in a terminal.",
    "Leave it running in the background, then click Re-check."
  ],
  command: { label: "Start Ollama from a terminal", text: "ollama serve" }
};

/**
 * Ollama runs but no chat model is installed. The command below is the
 * approved v0.4 default from `V04_MODEL_RECOMMENDATION_DECISION.md` §3: one
 * conservative command, worded "try this first" rather than "recommended"
 * since no potato-proof runs have validated it yet. The 4 GB "survival
 * machine" alternative is mentioned in words only — it does not get its own
 * copy button.
 */
export const CHAT_MODEL_GUIDANCE: SetupGuidance = {
  steps: [
    "Open a terminal and run the command below to install a small chat model. Try this first.",
    "If this computer is a very weak, 4 GB \"survival machine,\" the even smaller llama3.2:1b model may work better instead.",
    "After the download finishes, click Re-check."
  ],
  command: { label: "Install a chat model", text: "ollama pull llama3.2:3b" }
};

/** Semantic search is off because the embedding model is not installed. */
export const EMBEDDING_MODEL_GUIDANCE: SetupGuidance = {
  steps: [
    "Optional. To turn on smarter document search, run the command below in a terminal. It is a small download.",
    "Basic keyword search keeps working either way.",
    "After the download finishes, click Re-check."
  ],
  command: { label: "Install the embedding model", text: "ollama pull nomic-embed-text" }
};

/** OCR dependencies are missing. Basic pointer only — no install wizard yet. */
export const OCR_BASIC_GUIDANCE: SetupGuidance = {
  steps: [
    "Reading scanned PDFs and images needs Tesseract OCR and a PDF renderer installed on this computer.",
    "Regular text documents work without them, so this can wait.",
    "If you install them later using their own instructions, click Re-check afterwards."
  ]
};

/** No vision model installed. Clearly optional and heavy; never a command. */
export const VISION_OPTIONAL_GUIDANCE: SetupGuidance = {
  steps: [
    "Optional and heavy. Vision models are large downloads and can be very slow on weak computers.",
    "Skip this unless you specifically need the app to understand images. Everything else works without it."
  ]
};
