import { useEffect, useRef, useState } from "react";
import { cancelJob, getJob, submitImportJobs, type JobScope } from "../../tauri";
import { ImportJobController, type JobIntent, type JobTarget, type TrackedJob } from "./jobModel";

export function useImportJobs(onIntent: (intent: JobIntent) => void | Promise<void>) {
  const intentRef = useRef(onIntent);
  intentRef.current = onIntent;
  const controllerRef = useRef<ImportJobController>();
  if (!controllerRef.current) {
    controllerRef.current = new ImportJobController(
      { submitImport: submitImportJobs, get: getJob, cancel: cancelJob },
      (intent) => intentRef.current(intent)
    );
  }
  const controller = controllerRef.current;
  const [jobs, setJobs] = useState<TrackedJob[]>([]);

  useEffect(() => controller.subscribe(setJobs), [controller]);

  return {
    jobs,
    submitJobs: (paths: string[], scope: JobScope, target: JobTarget) => controller.submit(paths, scope, target),
    cancelJob: (jobId: string) => controller.cancel(jobId),
    removeJob: (jobId: string) => controller.remove(jobId)
  };
}
