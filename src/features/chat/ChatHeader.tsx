import { Trash2 } from "lucide-react";
import type {
  AnswerStyle,
  DocumentRecord,
  ModelCapability,
  MultimodalMode,
  RagPreset,
  Session,
  Settings,
  VisionBackend
} from "../../tauri";
import { installedModelTag, isInstalledModelTag, readableModelLabel } from "../../api/models";

const ANSWER_STYLE_OPTIONS: Array<{ value: AnswerStyle; label: string }> = [
  { value: "precise", label: "Precise" },
  { value: "layman", label: "Layman" },
  { value: "detailed", label: "Detailed" },
  { value: "extract_only", label: "Extract only" },
  { value: "evidence_only", label: "Evidence only" }
];

export function ChatHeader(props: {
  answerStyle: AnswerStyle;
  busy: boolean;
  documents: DocumentRecord[];
  modelChoices: string[];
  multimodalMode: MultimodalMode;
  visionBackend: VisionBackend;
  selectedRagDocumentId: string;
  selectedSession: Session | null;
  settings: Settings;
  showVision: boolean;
  useRag: boolean;
  verifyRag: boolean;
  visionModel: string;
  visionModels: ModelCapability[];
  ragPreset: RagPreset;
  onDeleteSession: (sessionId: string) => void;
  onSetAnswerStyle: (value: AnswerStyle) => void;
  onSetRagPreset: (value: RagPreset) => void;
  onSetSelectedRagDocumentId: (value: string) => void;
  onSetSessionModel: (model: string) => void;
  onSetUseRag: (value: boolean) => void;
  onSetVerifyRag: (value: boolean) => void;
}) {
  const indexedDocuments = props.documents.filter(isRagReadyDocument);
  const activeModel = props.selectedSession?.model || String(props.settings.default_model || "llama3.2");
  const activeModelInstalled = isInstalledModelTag(activeModel, props.modelChoices);
  const activeModelValue = activeModelInstalled ? installedModelTag(activeModel, props.modelChoices) : activeModel;
  const visionLabel = props.visionModel || props.visionModels[0]?.model || "automatic";
  return (
    <header className="flex min-h-16 shrink-0 items-center justify-between gap-4 border-b border-ink/15 px-5 py-3">
      <div className="min-w-0">
        <h2 className="truncate text-lg font-semibold">{props.selectedSession?.title ?? "New chat"}</h2>
        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-ink/55">
          <span>Conversation model</span>
          <select
            className="h-8 max-w-[240px] rounded-md border border-ink/15 bg-white px-2 text-xs outline-none focus:border-tide"
            disabled={props.busy || !props.selectedSession}
            onChange={(event) => props.onSetSessionModel(event.target.value)}
            title="Use this model for future messages in this conversation"
            value={activeModelValue}
          >
            {!activeModelInstalled && activeModel.trim() && (
              <option disabled value={activeModel}>
                {readableModelLabel(activeModel, { installed: false })}
              </option>
            )}
            {props.modelChoices.map((model) => (
              <option key={model} title={model} value={model}>
                {readableModelLabel(model)}
              </option>
            ))}
          </select>
          {props.showVision && (
            <span className="rounded-md border border-ink/10 bg-white px-2 py-1">
              Vision: {visionBackendLabel(props.visionBackend)}
              {props.visionBackend === "ollama" || props.visionBackend === "automatic" ? ` - ${readableModelLabel(visionLabel)}` : ""}
            </span>
          )}
        </div>
      </div>
      <div className="flex flex-wrap items-center justify-end gap-3">
        {props.useRag && (
          <select
            className="h-10 max-w-[240px] rounded-md border border-ink/15 bg-white px-3 text-sm outline-none focus:border-tide"
            disabled={props.busy || indexedDocuments.length === 0}
            onChange={(event) => props.onSetSelectedRagDocumentId(event.target.value)}
            title="Limit retrieval to one Source"
            value={props.selectedRagDocumentId}
          >
            <option value="">All indexed Sources</option>
            {indexedDocuments.map((document) => (
              <option key={document.id} value={document.id}>
                {document.title || document.file_name}
              </option>
            ))}
          </select>
        )}
        {props.useRag && (
          <select
            className="h-10 max-w-[150px] rounded-md border border-ink/15 bg-white px-3 text-sm outline-none focus:border-tide"
            disabled={props.busy}
            onChange={(event) => props.onSetRagPreset(event.target.value as RagPreset)}
            title="RAG preset"
            value={props.ragPreset}
          >
            <option value="standard">Standard</option>
            <option value="potato">Potato Mode</option>
          </select>
        )}
        {props.useRag && (
          <select
            className="h-10 max-w-[150px] rounded-md border border-ink/15 bg-white px-3 text-sm outline-none focus:border-tide"
            disabled={props.busy || props.ragPreset === "potato"}
            onChange={(event) => props.onSetAnswerStyle(event.target.value as AnswerStyle)}
            title="Answer style"
            value={props.ragPreset === "potato" ? "evidence_only" : props.answerStyle}
          >
            {ANSWER_STYLE_OPTIONS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        )}
        <label className="flex items-center gap-2 rounded-md border border-ink/15 bg-white px-3 py-2 text-sm">
          <input
            checked={props.useRag}
            className="h-4 w-4 accent-moss"
            onChange={(event) => props.onSetUseRag(event.target.checked)}
            type="checkbox"
          />
          RAG
        </label>
        {props.useRag && (
          <label className="flex items-center gap-2 rounded-md border border-ink/15 bg-white px-3 py-2 text-sm">
            <input
              checked={props.ragPreset !== "potato" && props.verifyRag}
              className="h-4 w-4 accent-tide"
              disabled={props.ragPreset === "potato"}
              onChange={(event) => props.onSetVerifyRag(event.target.checked)}
              type="checkbox"
            />
            Verify
          </label>
        )}
        {props.selectedSession && (
          <button
            className="flex h-10 w-10 items-center justify-center rounded-md border border-clay/20 text-clay hover:bg-[#fff3ee]"
            disabled={props.busy}
            onClick={() => props.onDeleteSession(props.selectedSession!.id)}
            title="Delete session"
            type="button"
          >
            <Trash2 size={16} aria-hidden="true" />
          </button>
        )}
      </div>
    </header>
  );
}

function visionBackendLabel(backend: VisionBackend): string {
  if (backend === "florence2") return "Basic local vision - Florence 2";
  if (backend === "ollama") return "Enhanced vision - Ollama";
  if (backend === "ocr_only") return "OCR only";
  return "Automatic";
}

function isRagReadyDocument(document: DocumentRecord): boolean {
  return !document.is_deleted && document.index_status === "indexed" && !document.is_internal;
}
