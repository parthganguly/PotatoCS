import { X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { readImportedSource } from "../../api/sources";
import type { SourceReadResult } from "../../tauri";
import { sourceReadStateCopy } from "../chat/answerEvidence";

export type SourceInspectorTarget = {
  documentId: string;
  title: string;
  // The answer's saved content hash; a different current copy is reported as changed.
  expectedContentHash?: string;
  excerpt?: {
    id: string;
    text: string;
    location: string;
    coverage: "complete_document" | "excerpt";
    truncated: boolean;
  };
};

type View = "excerpt" | "original";

// One shared, read-only inspector. All text renders as escaped, whitespace-preserving
// plain text: no HTML/Markdown rendering, no links followed, no files opened.
export function SourceInspector({ target, onClose }: { target: SourceInspectorTarget; onClose: () => void }) {
  const [view, setView] = useState<View>(target.excerpt ? "excerpt" : "original");
  const [page, setPage] = useState<SourceReadResult | null>(null);
  const [text, setText] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      previous?.focus?.();
    };
  }, []);

  async function load(offset: number) {
    setLoading(true);
    setError(null);
    try {
      const result = await readImportedSource({
        documentId: target.documentId,
        offset,
        expectedContentHash: target.expectedContentHash
      });
      setPage(result);
      setText((current) => (offset === 0 ? result.text : current + result.text));
    } catch (err) {
      setError(err instanceof Error ? err.message : "The source could not be read.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    setPage(null);
    setText("");
    setView(target.excerpt ? "excerpt" : "original");
    void load(0);
  }, [target.documentId, target.expectedContentHash]);

  const unavailable = page && page.state !== "available" ? sourceReadStateCopy(page.state) : "";
  const lineCount = text ? text.split("\n").length : 0;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4" onMouseDown={onClose}>
      <div
        aria-labelledby="source-inspector-title"
        aria-modal="true"
        className="flex max-h-[88vh] w-full max-w-3xl flex-col rounded-md border border-ink/15 bg-white shadow-xl"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="flex items-start justify-between gap-3 border-b border-ink/15 px-4 py-3">
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold" id="source-inspector-title">{target.title || "Source"}</h2>
            <p className="mt-0.5 text-xs text-ink/55">
              Read-only · document {target.documentId.slice(0, 8)}
              {page?.file_name ? ` · ${page.file_name}` : ""}
              {page?.content_hash ? ` · sha256 ${page.content_hash.slice(0, 12)}` : ""}
            </p>
          </div>
          <button
            aria-label="Close source inspector"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-ink/15 hover:bg-[#faf9f3]"
            onClick={onClose}
            ref={closeRef}
            type="button"
          >
            <X size={15} aria-hidden="true" />
          </button>
        </header>

        <div aria-label="Source views" className="flex gap-2 border-b border-ink/15 px-4 py-2" role="tablist">
          {target.excerpt && (
            <ViewTab active={view === "excerpt"} label="Supplied to the model" onClick={() => setView("excerpt")} />
          )}
          <ViewTab active={view === "original"} label="Original imported Markdown" onClick={() => setView("original")} />
        </div>

        <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-4 py-3 text-sm" role="tabpanel">
          {view === "excerpt" && target.excerpt ? (
            <>
              <p className="mb-2 text-xs text-ink/60">
                Saved evidence snapshot from this answer · {target.excerpt.id}
                {target.excerpt.location ? ` · ${target.excerpt.location}` : ""} ·{" "}
                {target.excerpt.coverage === "complete_document" ? "complete document" : "selected excerpt"}
                {target.excerpt.truncated ? " · display truncated" : ""}
              </p>
              <pre className="whitespace-pre-wrap break-words rounded-md bg-[#faf9f3] p-3 font-mono text-xs leading-5 text-ink/80">
                {target.excerpt.text}
              </pre>
            </>
          ) : loading && !text ? (
            <p aria-live="polite" className="text-xs text-ink/60" role="status">Loading the imported source…</p>
          ) : error ? (
            <div className="text-xs text-clay" role="alert">
              <p>{error}</p>
              <button className="mt-2 rounded border border-clay/30 px-2 py-1 font-medium" onClick={() => void load(0)} type="button">
                Try again
              </button>
            </div>
          ) : unavailable ? (
            <p className="text-xs text-clay" role="alert">{unavailable}</p>
          ) : (
            <>
              <p className="mb-2 text-xs text-ink/60">
                Current imported copy · lines 1–{Math.max(1, lineCount)}
                {page?.total_chars ? ` · ${text.length.toLocaleString()} of ${page.total_chars.toLocaleString()} characters` : ""}
              </p>
              <pre className="whitespace-pre-wrap break-words rounded-md bg-[#faf9f3] p-3 font-mono text-xs leading-5 text-ink/80">
                {text}
              </pre>
              {page?.next_offset != null && (
                <button
                  className="mt-2 rounded border border-ink/20 px-2 py-1 text-xs font-medium disabled:opacity-50"
                  disabled={loading}
                  onClick={() => void load(page.next_offset ?? 0)}
                  type="button"
                >
                  {loading ? "Loading…" : "Load more"}
                </button>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function ViewTab({ active, label, onClick }: { active: boolean; label: string; onClick: () => void }) {
  return (
    <button
      aria-selected={active}
      className={`rounded-md border px-3 py-1.5 text-xs font-medium ${
        active ? "border-moss bg-moss text-white" : "border-ink/15 bg-white hover:bg-[#faf9f3]"
      }`}
      onClick={onClick}
      role="tab"
      type="button"
    >
      {label}
    </button>
  );
}
