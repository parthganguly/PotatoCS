import { listen } from "@tauri-apps/api/event";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import {
  carriesOverToNewOperation,
  createProgressSubscription,
  fallbackProgressLabel,
  isOperationProgressEvent
} from "./chatProgress";
import type { OperationProgressEvent } from "./chatProgress";

export function useChatProgress(active: boolean) {
  const activeRef = useRef(active);
  const pendingEventRef = useRef<{ event: OperationProgressEvent; receivedAt: number } | null>(null);
  activeRef.current = active;
  const [current, setCurrent] = useState<OperationProgressEvent | null>(null);
  const [history, setHistory] = useState<OperationProgressEvent[]>([]);
  const [operationStartedAt, setOperationStartedAt] = useState(0);
  const [now, setNow] = useState(() => Date.now());

  useLayoutEffect(() => {
    if (!active) {
      setCurrent(null);
      setOperationStartedAt(0);
      return;
    }
    const pending = pendingEventRef.current;
    setCurrent(pending && carriesOverToNewOperation(pending.event, Date.now() - pending.receivedAt) ? pending.event : null);
    pendingEventRef.current = null;
    setOperationStartedAt(Date.now());
    setNow(Date.now());
  }, [active]);

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [active]);

  useEffect(() => createProgressSubscription(
    async (handler) => listen<unknown>("operation_progress", (event) => {
      if (isOperationProgressEvent(event.payload)) handler(event.payload);
    }),
    (event) => {
      setHistory((items) => [...items, event].slice(-10));
      pendingEventRef.current = { event, receivedAt: Date.now() };
      if (activeRef.current) setCurrent(event);
    }
  ), []);

  const fallbackLabel = active && !current && operationStartedAt
    ? fallbackProgressLabel(now - operationStartedAt)
    : null;

  return { current, fallbackLabel, history, now };
}
