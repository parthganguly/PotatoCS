export const BACKEND_DEGRADED_MESSAGE =
  "Backend unavailable. Local AI features may not work until the backend reconnects.";

export const PROFILE_IN_USE_BANNER =
  "Profile already open. PotatoCS is already running with this profile in another window. Close that window, then choose Retry backend.";
export const BACKEND_STOP_UNCONFIRMED_BANNER =
  "The previous backend could not be confirmed stopped, so a new one was not started. Close and reopen PotatoCS.";

export const BACKEND_RETRY_LABEL = "Retry backend";
export const BACKEND_RETRYING_LABEL = "Retrying...";

export type BackendDegradedEvent = {
  __odysseus_backend_degraded__: true;
  degraded: boolean;
};

export function isBackendDegradedEvent(value: unknown): value is BackendDegradedEvent {
  if (!value || typeof value !== "object") return false;
  const event = value as Partial<BackendDegradedEvent>;
  return event.__odysseus_backend_degraded__ === true && typeof event.degraded === "boolean";
}

export type BackendBannerState = {
  visible: boolean;
  message: string;
  retryLabel: string;
  retryDisabled: boolean;
};

/**
 * Derives the degraded-state banner. The message is always the fixed
 * privacy-safe copy: error details, payloads and paths from the shell
 * never reach this banner by construction.
 */
export function backendBannerState(
  degraded: boolean,
  retrying: boolean,
  reason: string | null | undefined = null
): BackendBannerState {
  // A profile owned by another window is not a crash; say what it is.
  const message =
    reason === "profile_in_use"
      ? PROFILE_IN_USE_BANNER
      : reason === "backend_stop_unconfirmed"
        ? BACKEND_STOP_UNCONFIRMED_BANNER
        : BACKEND_DEGRADED_MESSAGE;
  return {
    visible: degraded,
    message,
    retryLabel: retrying ? BACKEND_RETRYING_LABEL : BACKEND_RETRY_LABEL,
    retryDisabled: retrying
  };
}
