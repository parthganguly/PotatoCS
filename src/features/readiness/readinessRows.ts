import type { ModelCapability, OCRStatus, OllamaStatus, RAGHealth } from "../../tauri";
import { buildConversationModelOptions, isExperimentalModel } from "../../api/models";
import type { ConversationModelOption } from "../../api/models";
import {
  CHAT_MODEL_GUIDANCE,
  EMBEDDING_MODEL_GUIDANCE,
  OCR_BASIC_GUIDANCE,
  OLLAMA_INSTALL_GUIDANCE,
  OLLAMA_START_GUIDANCE,
  PRIVATE_RUNTIME_CHAT_GUIDANCE,
  PRIVATE_RUNTIME_START_GUIDANCE,
  SetupGuidance,
  VISION_OPTIONAL_GUIDANCE
} from "./setupGuidance";

/**
 * Pure mapping from the existing status calls (app_status, models.detect_ollama,
 * rag.health, ocr.status, backend_degraded event) to first-run readiness rows.
 * Every user-facing string here is fixed copy: raw error text, paths and RPC
 * payloads from the status objects never reach a row by construction, mirroring
 * the degraded-banner contract in `../shell/backendStatus.ts`.
 */

export type ReadinessState =
  | "ready"
  | "missing"
  | "degraded"
  | "heavy"
  | "unavailable"
  | "checking"
  | "error";

export type ReadinessRowId =
  | "app_shell"
  | "backend"
  | "ollama"
  | "chat_model"
  | "document_search"
  | "lexical_fallback"
  | "ocr"
  | "vision";

export type ReadinessRow = {
  id: ReadinessRowId;
  label: string;
  state: ReadinessState;
  stateLabel: string;
  explanation: string;
  nextStep: string;
  /** Fixed setup guidance for this gap; absent when nothing needs doing. */
  guidance?: SetupGuidance;
};

export type ReadinessInputs = {
  checking: boolean;
  backendDegraded: boolean;
  ollama: OllamaStatus | null;
  modelCapabilities: ModelCapability[];
  ragHealth: RAGHealth | null;
  ocrStatus: OCRStatus | null;
};

export const READINESS_STATE_LABELS: Record<ReadinessState, string> = {
  ready: "Ready",
  missing: "Not set up",
  degraded: "Working with limits",
  heavy: "Optional",
  unavailable: "Not available",
  checking: "Checking...",
  error: "Something went wrong"
};

function row(
  id: ReadinessRowId,
  label: string,
  state: ReadinessState,
  explanation: string,
  nextStep = "",
  guidance?: SetupGuidance
): ReadinessRow {
  const base: ReadinessRow = {
    id,
    label,
    state,
    stateLabel: READINESS_STATE_LABELS[state],
    explanation,
    nextStep
  };
  return guidance ? { ...base, guidance } : base;
}

/**
 * Selectable conversation models come from the same catalog logic the model
 * picker uses, so aliases and capability changes mean the same thing in both
 * places. One pass classifies the installed ones: a model counts as "usable"
 * only when its role is actually `chat` or `vision`, and `unknown` is neither
 * confirmed usable nor confirmed absent. Stale or uninstalled catalog entries
 * are ignored, and the experimental private-runtime candidate does not count
 * while its digest is not admitted.
 */
function chatModelOptionCounts(inputs: ReadinessInputs): {
  usable: ConversationModelOption[];
  unconfirmed: ConversationModelOption[];
} {
  const runtime = inputs.ollama?.private_runtime;
  const usable: ConversationModelOption[] = [];
  const unconfirmed: ConversationModelOption[] = [];
  for (const option of buildConversationModelOptions(inputs.ollama, inputs.modelCapabilities)) {
    if (!option.installed || option.stale) continue;
    if (runtime && runtime.state !== "ready" && isExperimentalModel(option.tag, runtime)) continue;
    if (option.role === "chat" || option.role === "vision") usable.push(option);
    else if (option.role === "unknown") unconfirmed.push(option);
  }
  return { usable, unconfirmed };
}

function installedModelCount(ollama: OllamaStatus, role: "chat" | "vision"): number {
  const catalog = ollama.conversation_models ?? [];
  if (catalog.length > 0) {
    return catalog.filter((model) => model.role === role && model.installed).length;
  }
  // Older status payloads without a catalog: treat any installed model as a
  // possible chat model, and claim nothing about vision.
  return role === "chat" ? (ollama.models ?? []).length : 0;
}

function ollamaRow(inputs: ReadinessInputs): ReadinessRow {
  const label = "Local AI runtime (Ollama)";
  if (inputs.checking) return row("ollama", label, "checking", "Looking for the local AI runtime.");
  const ollama = inputs.ollama;
  if (!ollama) {
    return row(
      "ollama",
      label,
      "error",
      "The runtime check did not finish.",
      "Click Re-check to try again."
    );
  }
  if (ollama.reachable) {
    return row("ollama", label, "ready", "The local AI runtime is running on this computer.");
  }
  if (ollama.private_runtime) {
    return row(
      "ollama",
      label,
      "missing",
      "The private AI runtime is not running or not reachable.",
      "Start the private runtime, then come back and click Re-check.",
      PRIVATE_RUNTIME_START_GUIDANCE
    );
  }
  if (ollama.installed) {
    return row(
      "ollama",
      label,
      "missing",
      "Ollama is installed but not running.",
      "Start Ollama, then come back and click Re-check.",
      OLLAMA_START_GUIDANCE
    );
  }
  return row(
    "ollama",
    label,
    "missing",
    "Local AI runtime not found.",
    "Install Ollama, then come back and click Re-check.",
    OLLAMA_INSTALL_GUIDANCE
  );
}

function chatModelRow(inputs: ReadinessInputs): ReadinessRow {
  const label = "Chat model";
  if (inputs.checking) return row("chat_model", label, "checking", "Looking for an installed chat model.");
  const ollama = inputs.ollama;
  if (!ollama) {
    return row("chat_model", label, "error", "The model check did not finish.", "Click Re-check to try again.");
  }
  const privateRuntime = ollama.private_runtime;
  if (!ollama.reachable) {
    if (privateRuntime) {
      return row(
        "chat_model",
        label,
        "unavailable",
        "Chat models need the private AI runtime first.",
        "Start the private runtime above, then click Re-check."
      );
    }
    return row(
      "chat_model",
      label,
      "unavailable",
      "Chat models need the local AI runtime first.",
      "Set up the runtime above, then click Re-check."
    );
  }
  // Reachable, but /api/tags failed: the inventory is unknown, not empty.
  // Never turn that into a download suggestion or an offline claim.
  if (ollama.error) {
    return row(
      "chat_model",
      label,
      "error",
      "The app could not read the installed model list.",
      "Click Re-check to try again."
    );
  }
  if (privateRuntime?.state === "unreachable") {
    return row(
      "chat_model",
      label,
      "unavailable",
      "Chat models need the private AI runtime first.",
      "Start the private runtime above, then click Re-check."
    );
  }
  const { usable, unconfirmed } = chatModelOptionCounts(inputs);
  if (usable.length > 0) {
    return row("chat_model", label, "ready", "A chat model is installed and ready to answer questions.");
  }
  // Digest-verified experimental candidate with no confirmed capabilities: not
  // counted as ready. Point at the model picker's explicit per-conversation
  // selection that already gates it.
  if (
    privateRuntime?.state === "ready" &&
    unconfirmed.length > 0 &&
    unconfirmed.every((option) => isExperimentalModel(option.tag, privateRuntime))
  ) {
    return row(
      "chat_model",
      label,
      "degraded",
      "The experimental chat model in the private runtime matches its expected version, but the app has not confirmed its abilities yet.",
      "Select it in a conversation's model picker to use it, or click Re-check to confirm again."
    );
  }
  // Installed models with unknown capabilities: neither confirmed usable nor
  // confirmed absent. Show a non-blocking re-check, never a download.
  if (unconfirmed.length > 0) {
    return row(
      "chat_model",
      label,
      "degraded",
      "Installed models were found, but the app could not confirm which one can answer chat questions yet.",
      "Click Re-check to inspect them again."
    );
  }
  if (privateRuntime) {
    if (privateRuntime.state === "digest_mismatch") {
      return row(
        "chat_model",
        label,
        "error",
        "The experimental chat model in the private runtime does not match the expected version.",
        "Reinstall the expected version in that private runtime, or add any other chat model to it, then click Re-check.",
        PRIVATE_RUNTIME_CHAT_GUIDANCE
      );
    }
    // Known compatible models are absent and unconfirmed ones are excluded:
    // the private runtime simply has no usable chat model. Ask for a generic
    // chat model there; nothing implies the experimental candidate is required.
    return row(
      "chat_model",
      label,
      "missing",
      "The private runtime has no chat model installed that can answer chat questions.",
      "Add a chat model to that private runtime using its own setup, then click Re-check.",
      PRIVATE_RUNTIME_CHAT_GUIDANCE
    );
  }
  const hasInstalledModels =
    (ollama.models?.length ?? 0) > 0 ||
    (ollama.conversation_models ?? []).some((model) => model.installed && !model.stale);
  if (hasInstalledModels) {
    return row(
      "chat_model",
      label,
      "missing",
      "Installed models cannot answer chat questions.",
      "Install a chat model from your terminal, then click Re-check.",
      CHAT_MODEL_GUIDANCE
    );
  }
  return row(
    "chat_model",
    label,
    "missing",
    "No chat model found.",
    "After installing Ollama, pull a small model from your terminal, then click Re-check.",
    CHAT_MODEL_GUIDANCE
  );
}

function documentSearchRow(inputs: ReadinessInputs): ReadinessRow {
  const label = "Document search";
  if (inputs.checking) return row("document_search", label, "checking", "Checking how your documents will be searched.");
  const health = inputs.ragHealth;
  if (!health) {
    return row("document_search", label, "error", "The search check did not finish.", "Click Re-check to try again.");
  }
  if (health.embedding?.semantic) {
    return row("document_search", label, "ready", "Smart (semantic) document search is active.");
  }
  return row(
    "document_search",
    label,
    "degraded",
    "Document search can still use basic keyword search, but smarter semantic search needs an embedding model.",
    "You can keep working now and add an embedding model later.",
    EMBEDDING_MODEL_GUIDANCE
  );
}

function lexicalFallbackRow(inputs: ReadinessInputs): ReadinessRow {
  const label = "Basic keyword search";
  if (inputs.checking) return row("lexical_fallback", label, "checking", "Checking the built-in keyword search.");
  const health = inputs.ragHealth;
  if (!health) {
    return row("lexical_fallback", label, "error", "The search check did not finish.", "Click Re-check to try again.");
  }
  if (health.embedding?.semantic) {
    return row("lexical_fallback", label, "ready", "On standby. Semantic search is handling your documents right now.");
  }
  return row(
    "lexical_fallback",
    label,
    "ready",
    "Currently searching your documents with basic keyword matching, so they stay searchable."
  );
}

function ocrRow(inputs: ReadinessInputs): ReadinessRow {
  const label = "Scanned document reading (OCR)";
  if (inputs.checking) return row("ocr", label, "checking", "Checking the tools for reading scanned documents.");
  const status = inputs.ocrStatus;
  if (!status) {
    return row("ocr", label, "error", "The OCR check did not finish.", "Click Re-check to try again.");
  }
  if (status.available) {
    return row("ocr", label, "ready", "Scanned PDFs and images can be read.");
  }
  return row(
    "ocr",
    label,
    "unavailable",
    "OCR is unavailable. Scanned PDFs may not be readable until Tesseract or a PDF renderer is installed.",
    "Regular text documents still work without OCR.",
    OCR_BASIC_GUIDANCE
  );
}

function visionRow(inputs: ReadinessInputs): ReadinessRow {
  const label = "Vision features";
  if (inputs.checking) return row("vision", label, "checking", "Checking optional vision features.");
  const ollama = inputs.ollama;
  if (ollama?.reachable && installedModelCount(ollama, "vision") > 0) {
    return row(
      "vision",
      label,
      "heavy",
      "A vision model is installed. Vision features are optional and may be slow on weak computers."
    );
  }
  return row(
    "vision",
    label,
    "heavy",
    "Vision features are optional and may be slow on weak computers. Everything else works without them.",
    "",
    VISION_OPTIONAL_GUIDANCE
  );
}

export function readinessRows(inputs: ReadinessInputs): ReadinessRow[] {
  return [
    row("app_shell", "Desktop app", "ready", "The app itself is running."),
    inputs.backendDegraded
      ? row(
          "backend",
          "Local engine",
          "error",
          "The local engine stopped responding.",
          "Use the Retry backend button in the banner above."
        )
      : row("backend", "Local engine", "ready", "The engine that stores and searches your documents is running."),
    ollamaRow(inputs),
    chatModelRow(inputs),
    documentSearchRow(inputs),
    lexicalFallbackRow(inputs),
    ocrRow(inputs),
    visionRow(inputs)
  ];
}

/**
 * Critical gaps are the ones that leave a fresh user staring at a chat that
 * cannot answer: backend down, runtime missing, or no chat model. Degraded
 * search, missing OCR and heavy vision never block the app on their own.
 */
export function hasCriticalReadinessGap(rows: ReadinessRow[]): boolean {
  return rows.some(
    (item) =>
      (item.id === "backend" || item.id === "ollama" || item.id === "chat_model") &&
      (item.state === "missing" || item.state === "error")
  );
}

export function firstRunReadinessNeeded(params: {
  sessionCount: number;
  userDocumentCount: number;
  rows: ReadinessRow[];
}): boolean {
  const freshProfile = params.sessionCount === 0 && params.userDocumentCount === 0;
  return freshProfile || hasCriticalReadinessGap(params.rows);
}
