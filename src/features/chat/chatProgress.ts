export type OperationProgressEvent = {
  __odysseus_progress__: true;
  operation_id: string;
  session_id: string | null;
  message_id: string | null;
  artifact_id: string | null;
  source_id: string | null;
  stage: string;
  label: string;
  status: "running" | "completed" | "error";
  started_at: number;
  elapsed_ms: number;
  progress_current: number | null;
  progress_total: number | null;
  cache_status: "reused" | "fresh" | "miss" | "not_applicable" | null;
  detail?: unknown;
};

export type ProgressSubscribe = (
  handler: (event: OperationProgressEvent) => void
) => Promise<() => void>;

export function isOperationProgressEvent(value: unknown): value is OperationProgressEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<OperationProgressEvent>;
  return event.__odysseus_progress__ === true &&
    typeof event.operation_id === "string" &&
    typeof event.stage === "string" &&
    typeof event.label === "string" &&
    typeof event.started_at === "number" &&
    typeof event.elapsed_ms === "number";
}

export function createProgressSubscription(
  subscribe: ProgressSubscribe,
  onEvent: (event: OperationProgressEvent) => void
): () => void {
  let disposed = false;
  let unlisten: (() => void) | null = null;
  void subscribe((event) => {
    if (!disposed) onEvent(event);
  }).then((nextUnlisten) => {
    if (disposed) nextUnlisten();
    else unlisten = nextUnlisten;
  }).catch(() => undefined);
  return () => {
    disposed = true;
    unlisten?.();
  };
}

export function progressElapsedMs(event: OperationProgressEvent, now: number): number {
  return Math.max(event.elapsed_ms, now - event.started_at, 0);
}

export function progressBubbleText(event: OperationProgressEvent, now: number): string {
  const seconds = Math.max(0, Math.floor(progressElapsedMs(event, now) / 1000));
  if (event.status === "completed" || event.stage === "done") {
    return `Done in ${seconds}s`;
  }
  return `${event.label} ${seconds}s`;
}

export function fallbackProgressLabel(elapsedMs: number): string | null {
  if (elapsedMs >= 120_000) {
    return "Taking longer than expected — a local model may still be loading.";
  }
  if (elapsedMs >= 3_000) {
    return "Working... this may take a while";
  }
  return null;
}
