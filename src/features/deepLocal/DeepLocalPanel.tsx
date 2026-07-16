import { useCallback, useEffect, useRef, useState } from "react";
import {
  cancelDeepLocalJob,
  getDeepLocalJob,
  listDeepLocalJobs,
  retryDeepLocalJob,
  submitDeepLocalJob,
  type DeepLocalJobDetail,
  type DeepLocalJobSnapshot
} from "../../tauri";
import {
  DEEP_LOCAL_DISCLAIMER,
  DEEP_LOCAL_POLL_INTERVAL_MS,
  deepLocalErrorCategoryCopy,
  deepLocalJobView,
  formatDeepLocalElapsed
} from "./deepLocalModel";

// Rendered only when the maintainer-level setting deep_local_enabled is true.
// Never part of first-run; never a v0.4 default.
export function DeepLocalPanel() {
  const [jobs, setJobs] = useState<DeepLocalJobSnapshot[]>([]);
  const [question, setQuestion] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [notice, setNotice] = useState("");
  const [openResult, setOpenResult] = useState<DeepLocalJobDetail | null>(null);
  const pollFailed = useRef(false);

  const refresh = useCallback(async () => {
    try {
      setJobs(await listDeepLocalJobs());
      pollFailed.current = false;
    } catch {
      // Backend restart windows are expected; keep the last known list.
      pollFailed.current = true;
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), DEEP_LOCAL_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const submit = async () => {
    const clean = question.trim();
    if (!clean || submitting) return;
    setSubmitting(true);
    setNotice("");
    try {
      const outcome = await submitDeepLocalJob({ question: clean });
      if (outcome.ok) {
        setQuestion("");
        await refresh();
      } else {
        setNotice(deepLocalErrorCategoryCopy(outcome.error_category) || outcome.error);
      }
    } catch (error) {
      setNotice(String(error));
    } finally {
      setSubmitting(false);
    }
  };

  const cancel = async (jobId: string) => {
    try {
      await cancelDeepLocalJob(jobId);
      await refresh();
    } catch (error) {
      setNotice(String(error));
    }
  };

  const retry = async (jobId: string) => {
    setNotice("");
    try {
      const outcome = await retryDeepLocalJob(jobId);
      if (!outcome.ok) setNotice(deepLocalErrorCategoryCopy(outcome.error_category) || outcome.error);
      await refresh();
    } catch (error) {
      setNotice(String(error));
    }
  };

  const showResult = async (jobId: string) => {
    try {
      setOpenResult(await getDeepLocalJob(jobId));
    } catch (error) {
      setNotice(String(error));
    }
  };

  return (
    <section aria-label="Deep Local experimental jobs" className="shrink-0 border-b border-ink/15 bg-[#f4f2ea] px-5 py-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">
          Deep Local <span className="rounded bg-gold/20 px-1.5 py-0.5 text-[10px] font-bold uppercase tracking-wide text-ink/70">experimental</span>
        </h3>
        <p className="text-xs text-ink/55">Slow jobs on your own Colibri server</p>
      </div>
      <p className="mb-3 text-xs leading-relaxed text-ink/60">{DEEP_LOCAL_DISCLAIMER}</p>
      <div className="mb-3 flex gap-2">
        <textarea
          aria-label="Deep Local question"
          className="min-h-[38px] flex-1 resize-y rounded-md border border-ink/15 bg-white px-3 py-2 text-sm"
          placeholder="Ask a question for slow, deep synthesis (text only)"
          rows={1}
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
        />
        <button
          className="self-start rounded-md border border-tide/40 px-3 py-2 text-xs font-medium text-tide hover:bg-tide/5 disabled:opacity-50"
          disabled={!question.trim() || submitting}
          onClick={() => void submit()}
          type="button"
        >
          {submitting ? "Starting..." : "Start Deep Local job"}
        </button>
      </div>
      {notice && <p className="mb-2 text-xs text-clay">{notice}</p>}
      {jobs.length > 0 && (
        <div className="grid gap-2 lg:grid-cols-2">
          {jobs.map((job) => {
            const view = deepLocalJobView(job.state, job.message_code);
            return (
              <article className="rounded-md border border-ink/15 bg-white p-3" key={job.job_id}>
                <div className="flex items-start justify-between gap-2">
                  <p className="text-sm font-medium">{view.label}</p>
                  <p className="text-xs text-ink/45">
                    {job.started_at ? formatDeepLocalElapsed(job.elapsed_ms) : "not started"}
                    {job.queue_position != null ? ` · #${job.queue_position} in line` : ""}
                  </p>
                </div>
                <p className="mt-1 text-xs text-ink/60">{view.explanation}</p>
                {job.warnings.length > 0 && <p className="mt-1 text-xs text-gold">{job.warnings.join(" ")}</p>}
                <div className="mt-2 flex gap-2">
                  {view.cancellable && (
                    <button className="rounded-md border border-clay/25 px-3 py-1.5 text-xs font-medium text-clay hover:bg-[#fff3ee]" onClick={() => void cancel(job.job_id)} type="button">
                      Cancel
                    </button>
                  )}
                  {view.retryable && (
                    <button className="rounded-md border border-tide/30 px-3 py-1.5 text-xs font-medium text-tide hover:bg-tide/5" onClick={() => void retry(job.job_id)} type="button">
                      Retry
                    </button>
                  )}
                  {job.state === "completed" && (
                    <button className="rounded-md border border-ink/15 px-3 py-1.5 text-xs font-medium text-ink/70 hover:bg-ink/5" onClick={() => void showResult(job.job_id)} type="button">
                      Show answer
                    </button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}
      {openResult && (
        <div className="mt-3 rounded-md border border-ink/15 bg-white p-3">
          <div className="mb-1 flex items-center justify-between">
            <p className="text-xs font-semibold text-ink/70">Deep Local answer</p>
            <button aria-label="Close Deep Local answer" className="text-xs text-ink/45 hover:text-ink" onClick={() => setOpenResult(null)} type="button">
              Close
            </button>
          </div>
          <p className="mb-2 text-xs text-ink/50">Q: {openResult.question}</p>
          <p className="whitespace-pre-wrap text-sm">{openResult.result_text}</p>
        </div>
      )}
    </section>
  );
}
