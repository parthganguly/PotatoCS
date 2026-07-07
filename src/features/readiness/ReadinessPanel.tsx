import { useEffect, useRef, useState } from "react";
import { Check, Copy, RefreshCw } from "lucide-react";
import { ReadinessRow, ReadinessState } from "./readinessRows";

const STATE_DOT_CLASSES: Record<ReadinessState, string> = {
  ready: "bg-emerald-500",
  missing: "bg-amber-500",
  degraded: "bg-amber-400",
  heavy: "bg-ink/30",
  unavailable: "bg-ink/30",
  checking: "bg-sky-400",
  error: "bg-clay"
};

/**
 * Copies fixed guidance text to the clipboard. Copy-only by design: the app
 * never runs these commands and never downloads models itself.
 */
function CopyCommandButton(props: { label: string; text: string }) {
  const [copied, setCopied] = useState(false);
  const resetTimer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  async function copy() {
    try {
      await navigator.clipboard.writeText(props.text);
      setCopied(true);
      window.clearTimeout(resetTimer.current);
      resetTimer.current = window.setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard unavailable: the command text stays visible for manual copy.
    }
  }

  return (
    <div className="mt-2 flex items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded bg-ink/5 px-2 py-1 font-mono text-xs text-ink/80">
        {props.text}
      </code>
      <button
        type="button"
        onClick={copy}
        aria-label={`Copy: ${props.label}`}
        className="flex shrink-0 items-center gap-1.5 rounded border border-ink/20 px-2 py-1 text-xs font-medium text-ink/70 hover:bg-ink/5"
      >
        {copied ? <Check size={12} aria-hidden="true" /> : <Copy size={12} aria-hidden="true" />}
        {copied ? "Copied" : "Copy"}
      </button>
    </div>
  );
}

export function ReadinessPanel(props: {
  rows: ReadinessRow[];
  checking: boolean;
  onRecheck: () => void;
  onContinue: () => void;
}) {
  return (
    <div className="scrollbar-thin min-h-0 flex-1 overflow-auto px-5 py-6">
      <div className="mx-auto w-full max-w-2xl">
        <header className="mb-5">
          <h2 className="text-lg font-semibold">Is everything ready?</h2>
          <p className="mt-1 text-sm text-ink/55">
            This app runs AI on your own computer, so a couple of pieces need to be in
            place. You can keep using the app while anything here is still missing.
          </p>
        </header>

        <ul className="space-y-2">
          {props.rows.map((item) => (
            <li key={item.id} className="rounded-md border border-ink/15 bg-white p-3">
              <div className="flex items-center justify-between gap-3">
                <span className="text-sm font-medium">{item.label}</span>
                <span className="flex shrink-0 items-center gap-2 text-xs text-ink/70">
                  <span
                    aria-hidden="true"
                    className={`h-2 w-2 rounded-full ${STATE_DOT_CLASSES[item.state]}`}
                  />
                  {item.stateLabel}
                </span>
              </div>
              <p className="mt-1 text-sm text-ink/70">{item.explanation}</p>
              {item.nextStep && <p className="mt-1 text-xs text-ink/55">{item.nextStep}</p>}
              {item.guidance && (
                <div className="mt-2 rounded border border-ink/10 bg-ink/[0.03] p-2.5">
                  <ol className="list-decimal space-y-1 pl-4 text-xs text-ink/65">
                    {item.guidance.steps.map((step) => (
                      <li key={step}>{step}</li>
                    ))}
                  </ol>
                  {item.guidance.command && (
                    <CopyCommandButton
                      label={item.guidance.command.label}
                      text={item.guidance.command.text}
                    />
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>

        <footer className="mt-5 flex items-center gap-3">
          <button
            type="button"
            disabled={props.checking}
            onClick={props.onRecheck}
            className="flex items-center gap-2 rounded border border-ink/20 px-3 py-1.5 text-sm font-medium text-ink/80 hover:bg-ink/5 disabled:opacity-60"
          >
            <RefreshCw size={14} aria-hidden="true" />
            {props.checking ? "Checking..." : "Re-check"}
          </button>
          <button
            type="button"
            onClick={props.onContinue}
            className="rounded border border-ink/20 px-3 py-1.5 text-sm font-medium text-ink/80 hover:bg-ink/5"
          >
            Continue to chat
          </button>
        </footer>
      </div>
    </div>
  );
}
