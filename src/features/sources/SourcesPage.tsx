import { FileText, Image, Library, RefreshCw, Search, Trash2, Upload } from "lucide-react";
import { SourceBackendKind, SourceSummary } from "../../tauri";
import { LocalArtifactImage, sourceAttachmentStatusLabel } from "../attachments/ComposerAttachments";

const FILTERS = [
  { value: "all", label: "All" },
  { value: "documents", label: "Documents" },
  { value: "images", label: "Images" },
  { value: "screenshots", label: "Screenshots" },
  { value: "indexed", label: "Indexed" },
  { value: "needs_attention", label: "Needs attention" }
];

export function SourcesPage(props: {
  busy: boolean;
  error: string | null;
  notice?: string | null;
  filter: string;
  sources: SourceSummary[];
  onAddSources: () => void;
  onDelete: (source: SourceSummary) => void;
  onFilter: (filter: string) => void;
  onPromote: (source: SourceSummary, index: boolean) => void;
  onRefresh: () => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <header className="flex h-16 shrink-0 items-center justify-between border-b border-ink/15 px-5">
        <div className="min-w-0">
          <h2 className="truncate text-lg font-semibold">Sources</h2>
          <p className="truncate text-xs text-ink/55">
            {props.sources.length} saved item(s)
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            className="flex h-10 items-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
            disabled={props.busy}
            onClick={props.onRefresh}
            type="button"
          >
            <RefreshCw size={15} aria-hidden="true" />
            Refresh
          </button>
          <button
            className="flex h-10 items-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
            disabled={props.busy}
            onClick={props.onAddSources}
            type="button"
          >
            <Upload size={15} aria-hidden="true" />
            Add sources
          </button>
        </div>
      </header>

      {props.error && (
        <div className="border-b border-clay/30 bg-[#fff3ee] px-5 py-3 text-sm text-clay">
          {props.error}
        </div>
      )}

      {!props.error && props.notice && (
        <div className="border-b border-ink/15 bg-[#f4f4ec] px-5 py-3 text-sm text-ink/70">
          {props.notice}
        </div>
      )}

      <div className="border-b border-ink/15 px-5 py-3">
        <div className="flex flex-wrap gap-2">
          {FILTERS.map((filter) => (
            <button
              className={`rounded-md border px-3 py-2 text-sm font-medium ${
                props.filter === filter.value
                  ? "border-moss bg-moss text-white"
                  : "border-ink/15 bg-white hover:bg-[#faf9f3]"
              }`}
              key={filter.value}
              onClick={() => props.onFilter(filter.value)}
              type="button"
            >
              {filter.label}
            </button>
          ))}
        </div>
      </div>

      <div className="scrollbar-thin min-h-0 flex-1 overflow-auto p-5">
        {props.sources.length === 0 ? (
          <div className="flex h-full items-center justify-center text-center">
            <div>
              <div className="mx-auto mb-3 flex h-12 w-12 items-center justify-center rounded-md bg-gold/20 text-gold">
                <Library size={24} aria-hidden="true" />
              </div>
              <p className="text-base font-medium">No Sources</p>
              <p className="mt-1 text-sm text-ink/55">Add a PDF, text file, image, or screenshot.</p>
            </div>
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
            {props.sources.map((source) => (
              <SourceRow
                busy={props.busy}
                key={`${source.backend_kind}-${source.id}`}
                onDelete={() => props.onDelete(source)}
                onPromote={(index) => props.onPromote(source, index)}
                source={source}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function SourceRow(props: {
  busy: boolean;
  onDelete: () => void;
  onPromote: (index: boolean) => void;
  source: SourceSummary;
}) {
  const source = props.source;
  const isSession = source.scope === "session";
  return (
    <article className="rounded-md border border-ink/15 bg-white p-3">
      <div className="flex gap-3">
        {source.backend_kind === "artifact" ? (
          <LocalArtifactImage className="h-16 w-20 shrink-0 rounded border border-ink/10 object-cover" path={source.thumbnail_path} />
        ) : (
          <div className="flex h-16 w-20 shrink-0 items-center justify-center rounded border border-ink/10 bg-[#faf9f3] text-tide">
            <FileText size={25} aria-hidden="true" />
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-start justify-between gap-2">
            <div className="min-w-0">
              <p className="truncate text-sm font-semibold">{source.display_name}</p>
              <p className="mt-1 text-xs text-ink/55">
                {formatSourceType(source)} - {formatSourceSize(source)}
              </p>
            </div>
            <SourceKindIcon kind={source.backend_kind} />
          </div>
          <div className="mt-3 flex flex-wrap gap-2">
            <Badge label={source.conversation_status === "in_conversation" ? "In conversation" : isSession ? "Session attachment" : "Sources"} tone={isSession ? "tide" : "moss"} />
            <Badge label={sourceAttachmentStatusLabel(source)} tone={source.indexing_status === "indexed" || source.diagnostics?.ocr_status === "indexed" ? "moss" : "neutral"} />
            {source.warning && <Badge label="needs attention" tone="gold" />}
            {source.error && <Badge label="error" tone="clay" />}
          </div>
          {source.backend_kind === "document" && source.source_type === "pdf" && source.diagnostics && (
            <div className="mt-3 grid grid-cols-2 gap-2 text-[11px] text-ink/60">
              <MiniMetric label="Embedded chars" value={source.diagnostics.extracted_text_char_count ?? 0} />
              <MiniMetric label="OCR chars" value={source.diagnostics.ocr_text_char_count ?? 0} />
              <MiniMetric label="OCR quality" value={source.diagnostics.ocr_quality || "unknown"} />
              <MiniMetric label="OCR attempts" value={source.diagnostics.ocr_attempt_count ?? 0} />
              <MiniMetric label="Crop regions" value={source.diagnostics.ocr_crop_count ?? 0} />
              <MiniMetric label="VLM text" value={source.diagnostics.vlm_text_available ? "available" : "none"} />
              <MiniMetric label="OCR pages" value={source.diagnostics.ocr_pages_with_text ?? 0} />
              <MiniMetric label="Chunks" value={source.diagnostics.chunk_count ?? 0} />
            </div>
          )}
        </div>
      </div>
      <div className="mt-3 flex flex-wrap justify-end gap-2">
        {isSession && (
          <>
            <button
              className="rounded-md border border-ink/15 bg-white px-3 py-2 text-xs font-medium hover:bg-[#faf9f3]"
              disabled={props.busy}
              onClick={() => props.onPromote(false)}
              type="button"
            >
              Add to Sources
            </button>
            <button
              className="rounded-md border border-moss/25 bg-[#edf7ef] px-3 py-2 text-xs font-medium text-moss hover:bg-[#e4f1e6]"
              disabled={props.busy}
              onClick={() => props.onPromote(true)}
              type="button"
            >
              Add and index
            </button>
          </>
        )}
        <button
          className="flex h-9 w-9 items-center justify-center rounded-md border border-clay/25 bg-white text-clay hover:bg-[#fff3ee]"
          disabled={props.busy}
          onClick={props.onDelete}
          title="Delete source"
          type="button"
        >
          <Trash2 size={15} aria-hidden="true" />
        </button>
      </div>
    </article>
  );
}

function SourceKindIcon({ kind }: { kind: SourceBackendKind }) {
  if (kind === "artifact") return <Image size={16} aria-hidden="true" className="shrink-0 text-tide" />;
  return <Search size={16} aria-hidden="true" className="shrink-0 text-tide" />;
}

function Badge({ label, tone }: { label: string; tone: "moss" | "tide" | "gold" | "clay" | "neutral" }) {
  const classes = {
    moss: "border-moss/20 bg-[#edf7ef] text-moss",
    tide: "border-tide/20 bg-[#eef8f8] text-tide",
    gold: "border-gold/30 bg-[#fff8e8] text-[#7a561d]",
    clay: "border-clay/25 bg-[#fff3ee] text-clay",
    neutral: "border-ink/15 bg-[#faf9f3] text-ink/55"
  }[tone];
  return <span className={`rounded-md border px-2 py-1 text-[11px] font-medium ${classes}`}>{label}</span>;
}

function MiniMetric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-md bg-[#faf9f3] px-2 py-1">
      <p className="font-medium text-ink">{value}</p>
      <p className="text-ink/50">{label}</p>
    </div>
  );
}

function formatSourceType(source: SourceSummary): string {
  if (source.source_type === "pdf") return `${source.page_count ?? 0} page PDF`;
  if (source.source_type === "markdown") return "Markdown";
  if (source.source_type === "text") return "Text";
  if (source.source_type === "screenshot") return `${source.width ?? 0}x${source.height ?? 0} screenshot`;
  return `${source.width ?? 0}x${source.height ?? 0} image`;
}

function formatSourceSize(source: SourceSummary): string {
  const bytes = source.size_bytes;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
