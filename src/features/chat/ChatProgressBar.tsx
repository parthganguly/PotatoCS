import type { JobRecord } from "../../tauri";
import { canCancelChatJob, chatJobProgressEvent, chatJobStatusLabel } from "./chatJob";
import { progressBubbleText } from "./chatProgress";
import type { OperationProgressEvent } from "./chatProgress";

export function ChatProgressBar({ event, fallbackLabel, now, job, awaitingJob, onCancel }: {
  event: OperationProgressEvent | null;
  fallbackLabel: string | null;
  now: number;
  job?: JobRecord | null;
  // A job is being submitted: its id, and so its own events, are not known yet.
  awaitingJob?: boolean;
  onCancel?: () => void;
}) {
  const shown = job ? chatJobProgressEvent(job, event) : awaitingJob ? null : event;
  // The backend's own deadline passed and work is being stopped; Cancel adds nothing.
  const stopping = Boolean(job?.deadline_reached);
  const cancellable = Boolean(job && onCancel) && canCancelChatJob(job?.state) && !stopping;
  const text = chatJobStatusLabel(job?.state, stopping) ??
    (shown ? progressBubbleText(shown, now) : awaitingJob && !job ? "Starting…" : fallbackLabel) ??
    (cancellable ? "Working…" : null);
  if (!text) return null;
  // Elapsed since the backend accepted Send; an elapsed time, never an estimate.
  const total = job ? ` · ${Math.max(0, Math.floor((now - job.created_at) / 1000))}s total` : "";

  const complete = shown?.status === "completed" || shown?.stage === "done";
  return (
    <div aria-live="polite" className="flex justify-start" data-testid="chat-progress" role="status">
      <div className="flex max-w-[82%] items-center gap-2 rounded-md border border-tide/20 bg-[#eef8f8] px-3 py-2 text-sm text-tide">
        {!complete && (
          <span aria-hidden="true" className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-tide/25 border-t-tide" />
        )}
        <span>{text}{total}</span>
        {cancellable && (
          <button
            className="ml-2 rounded border border-clay/25 px-2.5 py-1 text-xs font-medium text-clay hover:bg-[#fff3ee]"
            onClick={onCancel}
            type="button"
          >
            Cancel
          </button>
        )}
      </div>
    </div>
  );
}
