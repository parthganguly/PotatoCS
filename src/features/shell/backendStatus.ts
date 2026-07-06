export const BACKEND_DEGRADED_MESSAGE =
  "Backend unavailable. Local AI features may not work until the backend reconnects.";

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
export function backendBannerState(degraded: boolean, retrying: boolean): BackendBannerState {
  return {
    visible: degraded,
    message: BACKEND_DEGRADED_MESSAGE,
    retryLabel: retrying ? BACKEND_RETRYING_LABEL : BACKEND_RETRY_LABEL,
    retryDisabled: retrying
  };
}
