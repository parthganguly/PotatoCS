import { useEffect, useRef, useState } from "react";

import { cancelJob, getJob, submitSearchJob, type JobRecord } from "../../tauri";
import { SearchJobController } from "./searchModel";

export function useSearchJob(onSettled: (job: JobRecord) => void | Promise<void>) {
  const settledRef = useRef(onSettled);
  settledRef.current = onSettled;
  const controllerRef = useRef<SearchJobController>();
  if (!controllerRef.current) {
    controllerRef.current = new SearchJobController(
      { submit: submitSearchJob, get: getJob, cancel: cancelJob },
      (job) => settledRef.current(job)
    );
  }
  const controller = controllerRef.current;
  const [job, setJob] = useState<JobRecord | null>(null);

  useEffect(() => controller.subscribe(setJob), [controller]);

  async function submit(params: {
    query: string;
    sessionId?: string;
    model: string;
    secondRoundEnabled?: boolean;
  }) {
    return controller.submit(params);
  }

  async function cancel(): Promise<void> {
    await controller.cancel();
  }

  return { job, submit, cancel, clear: () => controller.clear() };
}
