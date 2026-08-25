import type { JobRecord, SubmitSearchJobResult } from "../../tauri";

const ACTIVE_STATES = new Set(["queued", "preflighting", "running", "cancel_requested"]);
const TERMINAL_STATES = new Set(["completed", "cancelled", "failed"]);
export const SEARCH_POLL_INTERVAL_MS = 1000;
export const SEARCH_POLL_MAX_FAILURES = 3;
export const SEARCH_PRIVACY_DISCLOSURE =
  "Search sends your question and public search-query reformulations to Brave Search. Content from local Sources is never included.";

const FAILURE_COPY: Record<string, string> = {
  search_provider_unconfigured: "Web Search needs configuration. Set ODYSSEUS_BRAVE_SEARCH_API_KEY and restart the app.",
  search_provider_failed: "The web Search provider could not be reached. Check the connection and try again.",
  search_no_evidence: "Search finished without enough exact, verifiable evidence to answer safely.",
  search_budget_exhausted: "Search reached its bounded time or acquisition budget before it could finish.",
  model_unavailable: "The selected local model was unavailable during Search.",
  search_failed: "Web Search could not complete. No unsupported answer was produced.",
  search_poll_unavailable: "Search status could not be recovered after repeated backend polling failures. The UI has been released; check backend status before retrying."
};

export function isActiveSearchState(state: unknown): boolean {
  return typeof state === "string" && ACTIVE_STATES.has(state);
}

export function canCancelSearch(state: unknown): boolean {
  return state === "queued" || state === "preflighting" || state === "running";
}

export function shouldShowChatProgress(busy: boolean, searchState: unknown): boolean {
  return busy && !isActiveSearchState(searchState);
}

export function searchJobLabel(state: unknown): string {
  if (state === "queued") return "Search queued";
  if (state === "preflighting") return "Preparing Search";
  if (state === "cancel_requested") return "Cancelling Search…";
  return "Researching the web…";
}

export function searchFailureCopy(code: unknown): string {
  return typeof code === "string" && code in FAILURE_COPY ? FAILURE_COPY[code] : FAILURE_COPY.search_failed;
}

export function searchPollDelay(failureCount: number): number {
  return SEARCH_POLL_INTERVAL_MS * Math.min(4, 2 ** Math.max(0, failureCount));
}

type SearchSubmitParams = {
  query: string;
  sessionId?: string;
  model: string;
  secondRoundEnabled?: boolean;
};

type SearchJobApi = {
  submit: (params: SearchSubmitParams) => Promise<SubmitSearchJobResult>;
  get: (jobId: string) => Promise<JobRecord>;
  cancel: (jobId: string) => Promise<JobRecord>;
};

type Scheduler = {
  set: (callback: () => void, delayMs: number) => number;
  clear: (timerId: number) => void;
};

const browserScheduler: Scheduler = {
  set: (callback, delayMs) => globalThis.setTimeout(callback, delayMs) as unknown as number,
  clear: (timerId) => globalThis.clearTimeout(timerId)
};

export class SearchJobController {
  private job: JobRecord | null = null;
  private listener: (job: JobRecord | null) => void = () => undefined;
  private timerId: number | null = null;
  private active = false;
  private pollFailures = 0;
  private handledTerminalId = "";

  constructor(
    private readonly api: SearchJobApi,
    private readonly onSettled: (job: JobRecord) => void | Promise<void>,
    private readonly scheduler: Scheduler = browserScheduler
  ) {}

  subscribe(listener: (job: JobRecord | null) => void): () => void {
    this.listener = listener;
    this.active = true;
    this.emit();
    this.schedule();
    return () => {
      this.active = false;
      this.clearTimer();
      this.listener = () => undefined;
    };
  }

  async submit(params: SearchSubmitParams): Promise<SubmitSearchJobResult> {
    this.clearTimer();
    const result = await this.api.submit(params);
    this.job = result.job;
    this.pollFailures = 0;
    this.handledTerminalId = "";
    this.emit();
    this.schedule();
    return result;
  }

  async cancel(): Promise<void> {
    if (!this.job || TERMINAL_STATES.has(this.job.state) || this.job.state === "cancel_requested") return;
    try {
      this.job = await this.api.cancel(this.job.job_id);
      this.emit();
      this.schedule();
    } catch {
      // Polling remains authoritative when cancellation races a transition.
    }
  }

  clear(): void {
    this.clearTimer();
    this.job = null;
    this.pollFailures = 0;
    this.emit();
  }

  private schedule(delayMs = SEARCH_POLL_INTERVAL_MS): void {
    this.clearTimer();
    if (!this.active || !this.job || TERMINAL_STATES.has(this.job.state)) return;
    this.timerId = this.scheduler.set(() => {
      this.timerId = null;
      void this.poll();
    }, delayMs);
  }

  private async poll(): Promise<void> {
    const current = this.job;
    if (!this.active || !current || TERMINAL_STATES.has(current.state)) return;
    try {
      const next = await this.api.get(current.job_id);
      if (!this.active || this.job?.job_id !== current.job_id) return;
      this.pollFailures = 0;
      this.job = next;
      this.emit();
      if (TERMINAL_STATES.has(next.state)) {
        await this.settle(next);
      } else {
        this.schedule();
      }
    } catch {
      if (!this.active || this.job?.job_id !== current.job_id) return;
      this.pollFailures += 1;
      if (this.pollFailures >= SEARCH_POLL_MAX_FAILURES) {
        this.job = {
          ...current,
          state: "failed",
          message_code: "search_poll_unavailable",
          finished_at: Date.now(),
          queue_position: null
        };
        this.emit();
        await this.settle(this.job);
        return;
      }
      this.schedule(searchPollDelay(this.pollFailures));
    }
  }

  private async settle(job: JobRecord): Promise<void> {
    this.clearTimer();
    if (this.handledTerminalId === job.job_id) return;
    this.handledTerminalId = job.job_id;
    await this.onSettled(job);
  }

  private emit(): void {
    this.listener(this.job);
  }

  private clearTimer(): void {
    if (this.timerId === null) return;
    this.scheduler.clear(this.timerId);
    this.timerId = null;
  }
}
