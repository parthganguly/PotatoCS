import { X } from "lucide-react";
import type { TrackedJob } from "./jobModel";
import { canCancelJob, jobFailureCopy, jobPollUnavailableCopy, jobView } from "./jobModel";

export function JobsPanel(props: {
  jobs: TrackedJob[];
  onCancel: (jobId: string) => void;
  onRemove: (jobId: string) => void;
}) {
  if (props.jobs.length === 0) return null;
  return (
    <section aria-label="Import jobs" className="shrink-0 border-b border-ink/15 bg-[#faf9f3] px-5 py-3">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-semibold">Document imports</h3>
        <p className="text-xs text-ink/55">One at a time, in the order added</p>
      </div>
      <div className="grid gap-2 lg:grid-cols-2 xl:grid-cols-3">
        {props.jobs.map((job, index) => {
          const view = jobView(job.localState);
          const cancellable = canCancelJob(job.localState, job.cancelSubmitting);
          return (
            <article className="rounded-md border border-ink/15 bg-white p-3" key={job.job_id}>
              <div className="flex items-start justify-between gap-2">
                <div>
                  <p className="text-sm font-medium">Import job {index + 1}</p>
                  <p className="mt-1 text-xs font-semibold text-tide">{view.label}</p>
                </div>
                {view.terminal && (
                  <button aria-label="Remove import job" className="text-ink/45 hover:text-ink" onClick={() => props.onRemove(job.job_id)} type="button">
                    <X size={15} aria-hidden="true" />
                  </button>
                )}
              </div>
              <p className="mt-2 text-xs text-ink/60">
                {job.localState === "failed" ? jobFailureCopy(job.message_code) : view.explanation}
              </p>
              {jobPollUnavailableCopy(job.pollUnavailable) && (
                <p className="mt-2 text-xs text-gold">{jobPollUnavailableCopy(job.pollUnavailable)}</p>
              )}
              {cancellable && (
                <button className="mt-3 rounded-md border border-clay/25 px-3 py-1.5 text-xs font-medium text-clay hover:bg-[#fff3ee]" onClick={() => props.onCancel(job.job_id)} type="button">
                  Cancel
                </button>
              )}
              {job.cancelSubmitting && (
                <button className="mt-3 rounded-md border border-ink/15 px-3 py-1.5 text-xs font-medium text-ink/45" disabled type="button">
                  Cancelling...
                </button>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
