import { invoke } from "@tauri-apps/api/core";
import { Camera, FileText, Image, Library, Paperclip, Plus, SlidersHorizontal, X } from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useState } from "react";
import { DocumentRecord, Message, ModelCapability, MultimodalMode, SourceSummary } from "../../tauri";

const MODE_OPTIONS: Array<{ value: MultimodalMode; label: string }> = [
  { value: "automatic", label: "Automatic" },
  { value: "ocr_only", label: "OCR only" },
  { value: "vision_only", label: "Vision only" },
  { value: "combined", label: "OCR + vision" }
];

export function ComposerAttachments(props: {
  attachments: SourceSummary[];
  conversationContext: SourceSummary[];
  busy: boolean;
  librarySources: SourceSummary[];
  mode: MultimodalMode;
  visionModel: string;
  visionModels: ModelCapability[];
  onAddSource: (source: SourceSummary) => void;
  onAttachFiles: () => void;
  onCaptureScreen: () => void;
  onPasteImage: () => void;
  onPromote: (source: SourceSummary, index: boolean) => void;
  onRemoveConversationSource: (source: SourceSummary) => void;
  onRemove: (source: SourceSummary) => void;
  onSetMode: (mode: MultimodalMode) => void;
  onSetVisionModel: (model: string) => void;
}) {
  const [menuOpen, setMenuOpen] = useState(false);
  const [sourcePickerOpen, setSourcePickerOpen] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const availableSources = useMemo(
    () => props.librarySources.filter((source) => (
      !props.attachments.some((item) => sameSource(item, source)) &&
      !props.conversationContext.some((item) => sameSource(item, source))
    )),
    [props.attachments, props.conversationContext, props.librarySources]
  );
  const needsVisionModel = props.mode === "vision_only" || props.mode === "combined";

  return (
    <div className="border-t border-ink/15 bg-[#eeeee6] px-5 py-3">
      <div className="mx-auto max-w-3xl">
        {props.attachments.length > 0 && (
          <div className="mb-3 flex flex-wrap gap-2">
            {props.attachments.map((source) => (
              <AttachmentChip
                key={`${source.backend_kind}-${source.id}`}
                source={source}
                onPromote={(index) => props.onPromote(source, index)}
                onRemove={() => props.onRemove(source)}
                disabled={props.busy}
              />
            ))}
          </div>
        )}

        {props.conversationContext.length > 0 && (
          <div className="mb-3 flex flex-wrap gap-2">
            {props.conversationContext.map((source) => (
              <AttachmentChip
                context
                key={`context-${source.backend_kind}-${source.id}`}
                source={source}
                onPromote={(index) => props.onPromote(source, index)}
                onRemove={() => props.onRemoveConversationSource(source)}
                disabled={props.busy}
              />
            ))}
          </div>
        )}

        <div className="relative flex items-center gap-2">
          <button
            className="flex h-9 w-9 items-center justify-center rounded-md border border-ink/15 bg-white hover:bg-[#faf9f3]"
            disabled={props.busy}
            onClick={() => setMenuOpen((open) => !open)}
            title="Add attachment"
            type="button"
          >
            <Plus size={17} aria-hidden="true" />
          </button>
          <span className="text-xs text-ink/55">
            {props.attachments.length > 0
              ? `${props.attachments.length} ready to attach`
              : props.conversationContext.length > 0
                ? `${props.conversationContext.length} in conversation`
                : "No attachments"}
          </span>

          {menuOpen && (
            <div className="absolute bottom-11 left-0 z-20 w-56 rounded-md border border-ink/15 bg-white p-1 shadow-lg">
              <MenuButton
                disabled={props.busy}
                icon={<Paperclip size={15} aria-hidden="true" />}
                label="Attach files"
                onClick={() => {
                  setMenuOpen(false);
                  props.onAttachFiles();
                }}
              />
              <MenuButton
                disabled={props.busy}
                icon={<Library size={15} aria-hidden="true" />}
                label="Add from Sources"
                onClick={() => {
                  setMenuOpen(false);
                  setSourcePickerOpen((open) => !open);
                }}
              />
              <MenuButton
                disabled={props.busy}
                icon={<Image size={15} aria-hidden="true" />}
                label="Paste image"
                onClick={() => {
                  setMenuOpen(false);
                  props.onPasteImage();
                }}
              />
              <MenuButton
                disabled={props.busy}
                icon={<Camera size={15} aria-hidden="true" />}
                label="Capture screen"
                onClick={() => {
                  setMenuOpen(false);
                  props.onCaptureScreen();
                }}
              />
              <MenuButton
                disabled={props.busy}
                icon={<SlidersHorizontal size={15} aria-hidden="true" />}
                label="Advanced image analysis"
                onClick={() => {
                  setMenuOpen(false);
                  setAdvancedOpen((open) => !open);
                }}
              />
            </div>
          )}
        </div>

        {sourcePickerOpen && (
          <div className="mt-3 max-h-44 overflow-auto rounded-md border border-ink/15 bg-white">
            {availableSources.length === 0 ? (
              <p className="px-3 py-3 text-sm text-ink/55">No saved Sources available.</p>
            ) : (
              availableSources.map((source) => (
                <button
                  className="flex w-full items-center gap-3 border-b border-ink/10 px-3 py-2 text-left text-sm last:border-b-0 hover:bg-[#faf9f3]"
                  disabled={props.busy}
                  key={`${source.backend_kind}-${source.id}`}
                  onClick={() => props.onAddSource(source)}
                  type="button"
                >
                  <SourceIcon source={source} />
                  <span className="min-w-0 flex-1 truncate">{source.display_name}</span>
                  <span className="text-xs text-ink/45">{source.source_type}</span>
                </button>
              ))
            )}
          </div>
        )}

        {advancedOpen && (
          <div className="mt-3 flex flex-wrap gap-2 rounded-md border border-ink/15 bg-white p-3">
            <select
              className="h-9 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
              disabled={props.busy}
              onChange={(event) => props.onSetMode(event.target.value as MultimodalMode)}
              value={props.mode}
            >
              {MODE_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
            {needsVisionModel && (
              <select
                className="h-9 min-w-[220px] rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
                disabled={props.busy || props.visionModels.length === 0}
                onChange={(event) => props.onSetVisionModel(event.target.value)}
                value={props.visionModel}
              >
                {props.visionModels.length === 0 ? (
                  <option value="">No confirmed vision model</option>
                ) : (
                  props.visionModels.map((model) => (
                    <option key={model.model} value={model.model}>
                      {model.model}
                    </option>
                  ))
                )}
              </select>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

export function MessageAttachments({ message }: { message: Message }) {
  const documents = message.documents ?? [];
  const artifacts = message.artifacts ?? [];
  if (documents.length === 0 && artifacts.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {documents.map((document) => (
        <div className="w-[150px] rounded-md border border-ink/15 bg-white/95 p-2 text-ink" key={document.id}>
          <div className="flex h-16 items-center justify-center rounded border border-ink/10 bg-[#faf9f3] text-tide">
            <FileText size={22} aria-hidden="true" />
          </div>
          <p className="mt-1 truncate text-xs font-medium">{document.title || document.file_name}</p>
          <p className="text-[11px] text-ink/50">{documentAttachmentStatusLabel(document)}</p>
        </div>
      ))}
      {artifacts.map((artifact) => {
        const deleted = artifact.is_deleted || artifact.status === "deleted";
        return (
          <div className="w-[150px] rounded-md border border-ink/15 bg-white/95 p-2 text-ink" key={artifact.id}>
            {deleted ? (
              <div className="flex h-16 items-center justify-center rounded border border-dashed border-ink/20 text-center text-[11px] text-ink/45">
                attachment deleted
              </div>
            ) : (
              <LocalArtifactImage className="h-16 w-full rounded border border-ink/10 object-cover" path={artifact.thumbnail_path} />
            )}
            <p className="mt-1 truncate text-xs font-medium">{artifact.name || "Image"}</p>
            <p className="text-[11px] text-ink/50">{artifact.status_label || artifact.status}</p>
          </div>
        );
      })}
    </div>
  );
}

export function AttachmentChip(props: {
  context?: boolean;
  disabled?: boolean;
  onPromote?: (index: boolean) => void;
  onRemove: () => void;
  source: SourceSummary;
}) {
  const canPromote = !props.context && props.source.scope === "session" && props.onPromote;
  const scopeLabel = props.context || props.source.conversation_status === "in_conversation"
    ? "In conversation"
    : props.source.scope === "session"
      ? "Ready to attach"
      : "Source";
  return (
    <div className="flex w-[230px] items-center gap-2 rounded-md border border-ink/15 bg-white p-2">
      {props.source.backend_kind === "artifact" ? (
        <LocalArtifactImage className="h-10 w-10 shrink-0 rounded border border-ink/10 object-cover" path={props.source.thumbnail_path} />
      ) : (
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded border border-ink/10 bg-[#faf9f3] text-tide">
          <FileText size={18} aria-hidden="true" />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <p className="truncate text-xs font-medium">{props.source.display_name}</p>
        <p className="text-[11px] text-ink/50">{scopeLabel} - {sourceAttachmentStatusLabel(props.source)}</p>
      </div>
      <button
        className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md hover:bg-[#faf9f3]"
        disabled={props.disabled}
        onClick={props.onRemove}
        title={props.context ? "Remove from conversation" : "Remove attachment"}
        type="button"
      >
        <X size={14} aria-hidden="true" />
      </button>
      {canPromote && (
        <button
          className="rounded-md border border-moss/20 bg-[#edf7ef] px-2 py-1 text-[11px] font-medium text-moss hover:bg-[#e4f1e6]"
          disabled={props.disabled}
          onClick={() => props.onPromote?.(props.source.backend_kind === "document")}
          type="button"
        >
          Add
        </button>
      )}
    </div>
  );
}

export function LocalArtifactImage({ className, path }: { className?: string; path?: string }) {
  const [url, setUrl] = useState("");
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let disposed = false;
    let objectUrl = "";
    setUrl("");
    setFailed(false);
    if (!path) {
      setFailed(true);
      return;
    }
    void invoke<number[]>("read_profile_artifact_file", { path })
      .then((bytes) => {
        if (disposed) return;
        const blob = new Blob([new Uint8Array(bytes)], { type: "image/png" });
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
      })
      .catch(() => {
        if (!disposed) setFailed(true);
      });
    return () => {
      disposed = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path]);

  if (failed || !url) {
    return (
      <div className={`flex items-center justify-center bg-[#faf9f3] text-ink/40 ${className ?? ""}`}>
        <Image size={18} aria-hidden="true" />
      </div>
    );
  }
  return <img alt="" className={className} src={url} />;
}

function MenuButton(props: {
  disabled?: boolean;
  icon: ReactNode;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      className="flex w-full items-center gap-2 rounded px-3 py-2 text-left text-sm hover:bg-[#faf9f3]"
      disabled={props.disabled}
      onClick={props.onClick}
      type="button"
    >
      {props.icon}
      <span>{props.label}</span>
    </button>
  );
}

function SourceIcon({ source }: { source: SourceSummary }) {
  if (source.backend_kind === "artifact") {
    return <LocalArtifactImage className="h-8 w-8 shrink-0 rounded border border-ink/10 object-cover" path={source.thumbnail_path} />;
  }
  return (
    <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded border border-ink/10 bg-[#faf9f3] text-tide">
      <FileText size={16} aria-hidden="true" />
    </span>
  );
}

export function sourceAttachmentStatusLabel(source: SourceSummary): string {
  if (source.backend_kind === "document" && source.source_type === "pdf") {
    return documentAttachmentStatusLabel(source.document ?? {
      id: source.id,
      title: source.display_name,
      source_path: "",
      stored_path: "",
      file_name: source.display_name,
      file_type: "pdf",
      content_hash: "",
      size_bytes: source.size_bytes,
      status: source.processing_status,
      index_status: source.indexing_status,
      is_deleted: false,
      is_low_text: source.indexing_status === "low_text",
      error: source.error ?? "",
      created_at: source.created_at,
      updated_at: source.updated_at,
      indexed_at: null,
      indexed_embedding_model: "",
      indexed_embedding_backend: "",
      ocr_status: source.diagnostics?.ocr_status ?? "",
      ocr_engine: "",
      ocr_error: source.error ?? ""
    });
  }
  if (source.backend_kind === "document") return source.source_type;
  return source.source_type;
}

export function documentAttachmentStatusLabel(document: DocumentRecord): string {
  if (document.file_type !== "pdf") return document.status_label || document.index_status || document.file_type;
  const ocrStatus = document.ocr_status || "";
  if (ocrStatus === "indexed") return "Image-based PDF · OCR indexed";
  if (ocrStatus === "running" || ocrStatus === "indexing" || ocrStatus === "needed") return "Image-based PDF · OCR processing";
  if (ocrStatus === "no_text") return "Image-based PDF · no reliable text extracted";
  if (ocrStatus === "unavailable" || document.index_status === "error" || document.status === "error") return "Image-based PDF · OCR failed";
  if (document.is_low_text || document.index_status === "low_text") return "Image-based PDF · OCR processing";
  return document.index_status || "PDF";
}

function sameSource(a: SourceSummary, b: SourceSummary): boolean {
  return a.id === b.id && a.backend_kind === b.backend_kind;
}
