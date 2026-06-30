import { progressBubbleText } from "./chatProgress";
import type { OperationProgressEvent } from "./chatProgress";

export function ChatProgressBar({ event, fallbackLabel, now }: {
  event: OperationProgressEvent | null;
  fallbackLabel: string | null;
  now: number;
}) {
  const text = event ? progressBubbleText(event, now) : fallbackLabel;
  if (!text) return null;

  const complete = event?.status === "completed" || event?.stage === "done";
  return (
    <div aria-live="polite" className="flex justify-start" data-testid="chat-progress" role="status">
      <div className="flex max-w-[82%] items-center gap-2 rounded-md border border-tide/20 bg-[#eef8f8] px-3 py-2 text-sm text-tide">
        {!complete && (
          <span aria-hidden="true" className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-tide/25 border-t-tide" />
        )}
        <span>{text}</span>
      </div>
    </div>
  );
}
