import { useEffect, useRef, useState } from "react";

import { cancelJob, getJob, submitSearchJob, type JobRecord } from "../../tauri";
import { JobPollController, type JobPollApi } from "./searchModel";

export function useJobPoll<P, R extends { job: JobRecord | null }>(
  api: JobPollApi<P, R>,
  onSettled: (job: JobRecord) => void | Promise<void>,
  pollFailureCode?: string
) {
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;
  const controllerRef = useRef<JobPollController<P, R>>();
  if (!controllerRef.current) {
    controllerRef.current = new JobPollController<P, R>(
      api,
      (job) => settledRef.current(job),
      undefined,
      pollFailureCode
    );
  }
  const controller = controllerRef.current;
  const [job, setJob] = useState<JobRecord | null>(null);

  useEffect(() => controller.subscribe(setJob), [controller]);

  return {
    job,
    submit: (params: P) => controller.submit(params),
    cancel: () => controller.cancel(),
    attach: (job: JobRecord) => controller.attach(job),
    clear: () => controller.clear()
  };
}

export function useSearchJob(onSettled: (job: JobRecord) => void | Promise<void>) {
  return useJobPoll({ submit: submitSearchJob, get: getJob, cancel: cancelJob }, onSettled);
}
