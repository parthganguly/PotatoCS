import { HardDrive, RefreshCw, Trash2 } from "lucide-react";
import type { CleanupPreview, CleanupResult, StorageStatus } from "./storageModel";
import {
  cleanupPreviewCopy,
  cleanupResultCopy,
  formatBytes,
  lowDiskCopy,
  skippedCopy,
  storageCategoryRows
} from "./storageModel";

export function StoragePanel(props: {
  status: StorageStatus | null;
  preview: CleanupPreview | null;
  lastCleanup: CleanupResult | null;
  loading: boolean;
  cleaning: boolean;
  error: string | null;
  onRefresh: () => void;
  onCleanup: () => void;
}) {
  const { status, preview, lastCleanup } = props;
  const skipped = status ? skippedCopy(status.skipped_count) : null;
  return (
    <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-5 py-6">
      <div className="mx-auto w-full max-w-2xl">
        <header className="mb-5">
          <h2 className="flex items-center gap-2 text-lg font-semibold">
            <HardDrive size={18} aria-hidden="true" />
            Storage
          </h2>
          <p className="mt-1 text-sm text-ink/55">
            Where this app keeps its data on your computer, how much space it uses,
            and what you can safely clean up.
          </p>
        </header>

        {props.error && (
          <p className="mb-4 rounded-md border border-clay/30 bg-[#fff3ee] p-3 text-sm text-clay">
            {props.error}
          </p>
        )}

        {status && (
          <>
            <section className="mb-4 rounded-md border border-ink/15 bg-white p-3">
              <p className="text-sm font-medium">App data location</p>
              <code className="mt-1 block break-all rounded bg-ink/5 px-2 py-1 font-mono text-xs text-ink/80">
                {status.profile_dir}
              </code>
              <p className="mt-2 text-sm text-ink/70">
                Total app data: <strong>{formatBytes(status.total_bytes)}</strong>
              </p>
              <p className={`mt-1 text-sm ${status.low_disk ? "font-medium text-clay" : "text-ink/70"}`}>
                {lowDiskCopy(status)}
              </p>
              {skipped && <p className="mt-1 text-xs text-ink/55">{skipped}</p>}
            </section>

            <section className="mb-4">
              <h3 className="mb-2 text-sm font-semibold text-ink/80">What is using that space</h3>
              <ul className="space-y-2">
                {storageCategoryRows(status).map((row) => (
                  <li key={row.key} className="rounded-md border border-ink/15 bg-white p-3">
                    <div className="flex items-center justify-between gap-3">
                      <span className="text-sm font-medium">{row.label}</span>
                      <span className="shrink-0 text-xs text-ink/70">{formatBytes(row.bytes)}</span>
                    </div>
                    <p className="mt-1 text-xs text-ink/55">{row.explanation}</p>
                  </li>
                ))}
              </ul>
            </section>
          </>
        )}

        <section className="mb-4 rounded-md border border-ink/15 bg-white p-3">
          <p className="text-sm font-medium">Cleanup</p>
          <p className="mt-1 text-sm text-ink/70">
            {preview ? cleanupPreviewCopy(preview) : "Refresh to see what can be cleaned up."}
          </p>
          {lastCleanup && <p className="mt-2 text-sm text-ink/70">{cleanupResultCopy(lastCleanup)}</p>}
          <p className="mt-2 text-xs text-ink/55">
            Cleanup only removes leftover app data. Your imported sources, chat history,
            settings, AI model files, and files outside this app are never touched.
          </p>
        </section>

        <footer className="flex items-center gap-3">
          <button
            type="button"
            disabled={props.loading}
            onClick={props.onRefresh}
            className="flex items-center gap-2 rounded border border-ink/20 px-3 py-1.5 text-sm font-medium text-ink/80 hover:bg-ink/5 disabled:opacity-60"
          >
            <RefreshCw size={14} aria-hidden="true" />
            {props.loading ? "Checking..." : "Refresh"}
          </button>
          <button
            type="button"
            disabled={props.cleaning || props.loading || !preview}
            onClick={props.onCleanup}
            className="flex items-center gap-2 rounded border border-ink/20 px-3 py-1.5 text-sm font-medium text-ink/80 hover:bg-ink/5 disabled:opacity-60"
          >
            <Trash2 size={14} aria-hidden="true" />
            {props.cleaning ? "Cleaning..." : "Clean up"}
          </button>
        </footer>
      </div>
    </div>
  );
}
