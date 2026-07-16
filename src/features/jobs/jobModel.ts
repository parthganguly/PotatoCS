import type { JobRecord, JobScope } from "../../tauri";

export const JOB_POLL_INTERVAL_MS = 2000;

export type KnownJobState =
  | "queued"
  | "preflighting"
  | "running"
  | "cancel_requested"
  | "cancelled"
  | "failed"
  | "completed";

export type LocalJobState = KnownJobState | "interrupted" | "unknown";
export type JobTarget = "library" | "chat";

export type TrackedJob = JobRecord & {
  localState: LocalJobState;
  target: JobTarget;
  cancelSubmitting: boolean;
  pollUnavailable: boolean;
};

export type JobView = {
  state: LocalJobState;
  label: string;
  explanation: string;
  terminal: boolean;
};

export type JobIntent = {
  job: TrackedJob;
  refreshSources: boolean;
  resubmit: false;
};

export type JobApi = {
  submitImport(paths: string[], scope: JobScope): Promise<JobRecord[]>;
  get(jobId: string): Promise<JobRecord>;
  cancel(jobId: string): Promise<JobRecord>;
};

export type JobScheduler = {
  set(callback: () => void, delayMs: number): unknown;
  clear(handle: unknown): void;
};

const STATE_VIEWS: Record<KnownJobState, JobView> = {
  queued: {
    state: "queued",
    label: "Queued",
    explanation: "Waiting for earlier imports to finish.",
    terminal: false
  },
  preflighting: {
    state: "preflighting",
    label: "Preflighting",
    explanation: "Checking this document before import.",
    terminal: false
  },
  running: {
    state: "running",
    label: "Running",
    explanation: "Importing and indexing this document.",
    terminal: false
  },
  cancel_requested: {
    state: "cancel_requested",
    label: "Cancelling...",
    explanation: "Waiting for the job to reach a safe stopping point.",
    terminal: false
  },
  cancelled: {
    state: "cancelled",
    label: "Cancelled",
    explanation: "The import was cancelled and no partial source is visible.",
    terminal: true
  },
  failed: {
    state: "failed",
    label: "Failed",
    explanation: "The document could not be imported.",
    terminal: true
  },
  completed: {
    state: "completed",
    label: "Completed",
    explanation: "The document was imported successfully.",
    terminal: true
  }
};

const UNKNOWN_VIEW: JobView = {
  state: "unknown",
  label: "Status unavailable",
  explanation: "The current job status is unavailable. Check Sources before trying again.",
  terminal: true
};

const INTERRUPTED_VIEW: JobView = {
  state: "interrupted",
  label: "Interrupted",
  explanation: "This background job was interrupted. Check Sources and try again if the document is not present.",
  terminal: true
};

const FAILURE_COPY: Record<string, string> = {
  cancelled_by_user: "The import was cancelled.",
  job_failed: "The document could not be imported. Try again.",
  file_not_found: "The selected file could not be found. Choose it again and retry.",
  unsupported_file_type: "This file type is not supported. Choose a PDF, text, or Markdown document.",
  document_not_found: "The document could not be found. Refresh Sources and try again.",
  document_busy: "This document is busy with another operation. Wait for it to finish and try again.",
  ocr_unavailable: "Text recognition is unavailable. Install the required OCR tools and try again.",
  ocr_no_text: "No readable text was found in this document.",
  ocr_page_too_large: "One of this document's pages is too large to read safely on this computer. Try a version of the document with smaller pages.",
  ocr_too_many_pages: "This document has more pages than this app will read in one go (limit: 400). Split the document and import the parts you need."
};

const GENERIC_FAILURE_COPY = "The document could not be imported. Try again.";
const POLL_UNAVAILABLE_COPY = "Job updates are temporarily unavailable. The app will keep checking.";
const SUBMISSION_FAILURE_COPY = "The document import could not be started. Check the backend and try again.";

/**
 * Fixed-copy job view mapping. Raw states, errors, paths, payloads, and
 * backend messages never reach the UI by construction.
 */
export function jobView(state: unknown): JobView {
  if (typeof state === "string" && state in STATE_VIEWS) {
    return STATE_VIEWS[state as KnownJobState];
  }
  if (state === "interrupted") return INTERRUPTED_VIEW;
  return UNKNOWN_VIEW;
}

/** Fixed guardrail mapping; arbitrary backend message text is never accepted. */
export function jobFailureCopy(code: unknown): string {
  return typeof code === "string" && code in FAILURE_COPY ? FAILURE_COPY[code] : GENERIC_FAILURE_COPY;
}

export function jobPollUnavailableCopy(unavailable: boolean): string | null {
  return unavailable ? POLL_UNAVAILABLE_COPY : null;
}

export function jobSubmissionFailureCopy(): string {
  return SUBMISSION_FAILURE_COPY;
}

export function isTerminalJobState(state: unknown): boolean {
  return state === "cancelled" || state === "completed" || state === "failed" || state === "interrupted" || state === "unknown";
}

export function canCancelJob(state: unknown, cancelSubmitting = false): boolean {
  return !cancelSubmitting && (state === "queued" || state === "preflighting" || state === "running");
}

export function isMissingJobError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : typeof error === "string" ? error : "";
  return /job not found:/i.test(text);
}

const defaultScheduler: JobScheduler = {
  set: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clear: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>)
};

export class ImportJobController {
  private jobs: TrackedJob[] = [];
  private listeners = new Set<(jobs: TrackedJob[]) => void>();
  private timers = new Map<string, unknown>();
  private polling = new Set<string>();
  private handled = new Set<string>();
  private cancelSubmitting = new Set<string>();

  constructor(
    private readonly api: JobApi,
    private readonly onIntent: (intent: JobIntent) => void | Promise<void>,
    private readonly scheduler: JobScheduler = defaultScheduler,
    private readonly intervalMs = JOB_POLL_INTERVAL_MS
  ) {}

  subscribe(listener: (jobs: TrackedJob[]) => void): () => void {
    this.listeners.add(listener);
    listener(this.snapshot());
    this.resumePolling();
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stopAllPolling();
    };
  }

  async submit(paths: string[], scope: JobScope, target: JobTarget): Promise<void> {
    if (paths.length === 0) return;
    const submitted = await this.api.submitImport(paths, scope);
    for (const record of submitted) {
      const state = jobView(record.state).state;
      const job: TrackedJob = {
        ...record,
        localState: state,
        target,
        cancelSubmitting: false,
        pollUnavailable: false
      };
      this.jobs = [...this.jobs.filter((item) => item.job_id !== job.job_id), job];
    }
    this.emit();
    for (const record of submitted) this.observe(record.job_id);
  }

  cancel(jobId: string): Promise<void> {
    const job = this.find(jobId);
    if (!job || !canCancelJob(job.localState, job.cancelSubmitting) || this.cancelSubmitting.has(jobId)) {
      return Promise.resolve();
    }

    // The guard and rendered disabled state are set before the first await.
    this.cancelSubmitting.add(jobId);
    this.patch(jobId, { cancelSubmitting: true });
    return this.api.cancel(jobId).then(
      (record) => {
        this.applyRecord(jobId, record);
      },
      () => {
        // Cancel is never retried. Polling continues to discover the honest outcome.
      }
    ).finally(() => {
      this.cancelSubmitting.delete(jobId);
      const current = this.find(jobId);
      if (current) this.patch(jobId, { cancelSubmitting: false });
    });
  }

  remove(jobId: string): void {
    this.stopPolling(jobId);
    this.jobs = this.jobs.filter((job) => job.job_id !== jobId);
    this.emit();
  }

  unmount(): void {
    this.listeners.clear();
    this.stopAllPolling();
  }

  private observe(jobId: string): void {
    const job = this.find(jobId);
    if (this.listeners.size === 0 || !job || isTerminalJobState(job.localState) || this.timers.has(jobId) || this.polling.has(jobId)) return;
    const handle = this.scheduler.set(() => {
      this.timers.delete(jobId);
      void this.poll(jobId);
    }, this.intervalMs);
    this.timers.set(jobId, handle);
  }

  private async poll(jobId: string): Promise<void> {
    const current = this.find(jobId);
    if (!current || isTerminalJobState(current.localState) || this.polling.has(jobId)) return;
    this.polling.add(jobId);
    try {
      const record = await this.api.get(jobId);
      this.applyRecord(jobId, record);
    } catch (error) {
      if (isMissingJobError(error)) {
        this.patch(jobId, { localState: "interrupted", pollUnavailable: false });
        this.finish(jobId);
      } else if (this.find(jobId)) {
        this.patch(jobId, { pollUnavailable: true });
      }
    } finally {
      this.polling.delete(jobId);
      this.observe(jobId);
    }
  }

  private applyRecord(jobId: string, record: JobRecord): void {
    if (!this.find(jobId)) return;
    const localState = jobView(record.state).state;
    this.patch(jobId, { ...record, localState, pollUnavailable: false });
    if (isTerminalJobState(localState) || localState === "unknown") this.finish(jobId);
  }

  private finish(jobId: string): void {
    this.stopPolling(jobId);
    const job = this.find(jobId);
    if (!job || this.handled.has(jobId)) return;
    this.handled.add(jobId);
    if (job.localState === "completed" || job.localState === "cancelled" || job.localState === "interrupted") {
      void Promise.resolve(this.onIntent({ job, refreshSources: true, resubmit: false })).catch(() => undefined);
    }
  }

  private resumePolling(): void {
    for (const job of this.jobs) this.observe(job.job_id);
  }

  private stopPolling(jobId: string): void {
    const timer = this.timers.get(jobId);
    if (timer !== undefined) this.scheduler.clear(timer);
    this.timers.delete(jobId);
  }

  private stopAllPolling(): void {
    for (const jobId of [...this.timers.keys()]) this.stopPolling(jobId);
  }

  private find(jobId: string): TrackedJob | undefined {
    return this.jobs.find((job) => job.job_id === jobId);
  }

  private patch(jobId: string, patch: Partial<TrackedJob>): void {
    this.jobs = this.jobs.map((job) => job.job_id === jobId ? { ...job, ...patch } : job);
    this.emit();
  }

  private snapshot(): TrackedJob[] {
    return this.jobs.map((job) => ({ ...job }));
  }

  private emit(): void {
    const snapshot = this.snapshot();
    for (const listener of this.listeners) listener(snapshot);
  }
}
