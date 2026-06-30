import type { OperationProgressEvent } from "./chatProgress";

export function OperationProgressDiagnostics({ events }: { events: OperationProgressEvent[] }) {
  return (
    <div className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-3 text-sm font-semibold">Latest operation progress</div>
      {events.length === 0 ? (
        <p className="text-xs text-ink/55">No progress events observed in this app session.</p>
      ) : (
        <div className="space-y-2">
          {[...events].reverse().map((event, index) => (
            <div className="rounded border border-ink/10 px-2.5 py-2 text-xs" key={`${event.operation_id}-${event.stage}-${event.elapsed_ms}-${index}`}>
              <div className="flex items-center justify-between gap-3">
                <span className="font-medium">{event.stage}</span>
                <span className="text-ink/45">{event.status}</span>
              </div>
              <div className="mt-1 flex flex-wrap gap-x-3 text-ink/55">
                <span>{Math.floor(event.elapsed_ms / 1000)}s</span>
                {event.cache_status && <span>cache: {event.cache_status}</span>}
                {event.progress_current !== null && (
                  <span>
                    {event.progress_current}
                    {event.progress_total !== null ? `/${event.progress_total}` : ""}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
