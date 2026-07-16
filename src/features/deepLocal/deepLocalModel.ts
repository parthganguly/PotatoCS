// Deep Local (experimental) — pure view-model helpers.
//
// Deep Local is optional, text-only, slow-job inference against a Colibri
// server the user runs themselves. It is hidden unless the maintainer-level
// setting deep_local_enabled is true; a normal v0.4 user never sees it.

export type DeepLocalState =
  | "queued"
  | "checking_runtime"
  | "waiting_for_provider"
  | "running"
  | "cancel_requested"
  | "completed"
  | "failed"
  | "cancelled_before_start"
  | "interrupted";

export const DEEP_LOCAL_POLL_INTERVAL_MS = 3000;

export const DEEP_LOCAL_DISCLAIMER = [
  "Deep Local is an optional experiment. It is not Everyday Local chat and does not replace it.",
  "It is text-only and needs a Colibri server that you install and run yourself — no model is included with PotatoCS, and PotatoCS never downloads one.",
  "Answers can take minutes to hours. Jobs keep their place if you close the app; interrupted jobs are marked honestly and can be retried.",
  "Everything stays on this computer: the endpoint is restricted to 127.0.0.1 and there is no cloud fallback."
].join(" ");

const TERMINAL_STATES: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled_before_start",
  "interrupted"
]);

export type DeepLocalJobView = {
  label: string;
  explanation: string;
  terminal: boolean;
  cancellable: boolean;
  retryable: boolean;
};

export function deepLocalJobView(state: string, messageCode: string): DeepLocalJobView {
  const terminal = TERMINAL_STATES.has(state);
  switch (state) {
    case "queued":
      return view("Waiting in line", "Runs after earlier Deep Local jobs finish. Cancel is immediate while waiting.", terminal, true, false);
    case "checking_runtime":
      return view("Checking the Colibri server", "Making sure the server is reachable before the slow work starts.", terminal, true, false);
    case "waiting_for_provider":
      return view("Server is busy", "The Colibri server is working on something else. This job retries automatically for a while.", terminal, true, false);
    case "running":
      return view(
        "Working (may take minutes to hours)",
        "The request has been sent. Cancelling now only stops the wait — the server may keep computing for some time.",
        terminal,
        true,
        false
      );
    case "cancel_requested":
      return view("Stopping...", "Waiting for a safe point to stop.", terminal, false, false);
    case "completed":
      return view("Done", "The answer is stored on this computer.", terminal, false, false);
    case "failed":
      return view("Did not finish", deepLocalFailureCopy(messageCode), terminal, false, true);
    case "cancelled_before_start":
      return view("Cancelled", "Stopped before any work was sent to the server.", terminal, false, true);
    case "interrupted":
      return view(
        "Interrupted",
        messageCode === "interrupted_by_restart"
          ? "PotatoCS closed while this job was in flight. You can retry it."
          : "PotatoCS stopped waiting. The Colibri server may have kept working for a while. You can retry.",
        terminal,
        false,
        true
      );
    default:
      return view("Unknown state", "This job is in a state this version does not recognize.", true, false, false);
  }
}

export function deepLocalFailureCopy(messageCode: string): string {
  switch (messageCode) {
    case "deep_local_failed":
      return "The Colibri server could not finish this job. Check that it is running, then retry.";
    default:
      return "Something went wrong. You can retry this job.";
  }
}

export function deepLocalErrorCategoryCopy(category: string): string {
  switch (category) {
    case "disabled":
      return "Deep Local is not enabled.";
    case "connection_failure":
      return "No Colibri server was reachable at the configured address.";
    case "auth_failure":
      return "The Colibri server requires an API key (set ODYSSEUS_COLIBRI_API_KEY).";
    case "invalid_model":
      return "The server did not offer a usable model id.";
    case "queue_saturated":
    case "queue_timeout":
      return "The Colibri server stayed busy for too long.";
    case "timeout":
      return "The job ran longer than the configured time limit.";
    case "unsupported_feature":
      return "Deep Local is text-only.";
    case "interrupted":
      return "PotatoCS stopped waiting for the server.";
    case "":
      return "";
    default:
      return "The Colibri server returned something unexpected.";
  }
}

export function formatDeepLocalElapsed(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) return `${hours}h ${minutes}m`;
  if (minutes > 0) return `${minutes}m ${seconds}s`;
  return `${seconds}s`;
}

function view(
  label: string,
  explanation: string,
  terminal: boolean,
  cancellable: boolean,
  retryable: boolean
): DeepLocalJobView {
  return { label, explanation, terminal, cancellable, retryable };
}
