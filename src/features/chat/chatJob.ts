import type { ChatAttempt, ChatTurn } from "../../tauri";
import type { OperationProgressEvent } from "./chatProgress";

export const CHAT_POLL_FAILURE_CODE = "chat_poll_unavailable";

// Confirmed outcomes: the question stays visible in the conversation with its own
// Retry; the composer is never refilled, so nothing invites an accidental resend.
const NOT_SAVED = "No answer was saved. The question stays in this conversation and can be retried.";
// Lost polling and unexpected failures cannot establish that no answer was saved.
const UNKNOWN = "Whether an answer was saved is unknown. Nothing was resent; use Check status before sending again.";

export const CHAT_RESULT_UNAVAILABLE_COPY =
  "The answer was generated and saved, but it could not be loaded here. It will appear in this conversation once the backend responds.";
export const CHECKING_OUTCOME_LABEL = "Checking whether the answer was saved…";
export const DEADLINE_STOPPING_LABEL = "Time limit reached — stopping…";

// Fixed copy per job message code. Raw backend errors never reach this text.
// Only the backend's confirmed failure and cancellation codes say no answer exists.
const FAILURE_COPY: Record<string, (model: string) => string> = {
  model_prepare_timeout: (model) => `${model} did not finish loading within the preparation limit. ${NOT_SAVED}`,
  model_prepare_failed: (model) => `${model} could not be prepared. Check that Ollama is running and the model is installed. ${NOT_SAVED}`,
  model_generation_timeout: (model) => `${model} loaded but did not finish the answer within the generation limit. ${NOT_SAVED}`,
  model_generation_failed: (model) => `${model} failed while generating the answer. ${NOT_SAVED}`,
  chat_turn_timeout: () => `The 5-minute limit for this question was reached before an answer was ready. ${NOT_SAVED}`,
  cancelled_by_user: () => `Cancelled. ${NOT_SAVED}`,
  [CHAT_POLL_FAILURE_CODE]: () => `Lost contact with the backend while answering. ${UNKNOWN}`,
  job_failed: () => `The answer could not be completed because of an unexpected error. ${UNKNOWN}`
};

export function chatJobFailureCopy(code: unknown, modelLabel: string): string {
  const copy = typeof code === "string" && code in FAILURE_COPY ? FAILURE_COPY[code] : FAILURE_COPY.job_failed;
  return copy(modelLabel);
}

// The backend settled these itself before reporting them; anything else needs a status check.
export function isConfirmedChatOutcome(code: unknown): boolean {
  return typeof code === "string" && code !== CHAT_POLL_FAILURE_CODE && code !== "job_failed" && code in FAILURE_COPY;
}

// States the backend progress events cannot describe; `running` defers to them.
export function chatJobStatusLabel(state: unknown, deadlineReached = false): string | null {
  if (deadlineReached && state !== "completed" && state !== "failed" && state !== "cancelled") return DEADLINE_STOPPING_LABEL;
  if (state === "queued") return "Waiting for other jobs to finish…";
  if (state === "preflighting") return "Starting…";
  if (state === "cancel_requested") return "Cancelling…";
  return null;
}

// Only this job's own progress events describe it; a stale "done" from an
// earlier import must never read as a completed answer.
export function chatJobProgressEvent(
  job: { job_id: string },
  event: OperationProgressEvent | null
): OperationProgressEvent | null {
  return event && event.operation_id === `operation-${job.job_id.toLowerCase()}` ? event : null;
}

export function canCancelChatJob(state: unknown): boolean {
  return state === "queued" || state === "preflighting" || state === "running";
}

// Image turns stay on synchronous chat.send: the job worker has no vision pipeline.
export function canSendAsChatJob(
  pendingSources: Array<{ backend_kind: string }>,
  conversationContext: Array<{ backend_kind: string }>
): boolean {
  return ![...pendingSources, ...conversationContext].some((source) => source.backend_kind === "artifact");
}

const ATTEMPT_REASON: Record<string, string> = {
  model_prepare_timeout: "the model did not finish loading within 120 s",
  model_prepare_failed: "the model could not be prepared",
  model_generation_timeout: "the answer was not finished within 120 s",
  model_generation_failed: "the model failed while answering",
  chat_turn_timeout: "the 5-minute limit was reached",
  backend_restarted: "the backend stopped before an answer was saved",
  outcome_reconciled: "stopped before an answer was saved"
};

// Fixed, inspectable copy for one attempt's outcome.
export function chatAttemptLabel(attempt: Pick<ChatAttempt, "state" | "outcome_code">): string {
  if (attempt.state === "completed") return "Answered";
  if (attempt.state === "pending") return "Queued";
  if (attempt.state === "running") return "In progress";
  if (attempt.state === "cancelled") return "Cancelled";
  const reason = ATTEMPT_REASON[attempt.outcome_code];
  if (attempt.state === "timed_out") return `Timed out — ${reason ?? "the time limit was reached"}`;
  if (attempt.state === "interrupted") return `Stopped — ${reason ?? "the backend stopped before an answer was saved"}`;
  return `Failed — ${reason ?? "an unexpected error"}`;
}

// The line under an unanswered question; null while an attempt is still active.
export function chatTurnStatusLabel(turn: ChatTurn): string | null {
  if (turn.answered || turn.latest_state === "pending" || turn.latest_state === "running") return null;
  return `Not answered. ${chatAttemptLabel({ state: turn.latest_state, outcome_code: turn.latest_outcome_code })}.`;
}
