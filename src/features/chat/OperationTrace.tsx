import { useEffect, useRef, useState } from "react";

import type { OperationTrace as OperationTraceData } from "../../tauri";

const MISSING_VALUE = "—";

export function OperationTrace({
  answer,
  hasEvidence = false,
  trace
}: {
  answer: string;
  hasEvidence?: boolean;
  trace: OperationTraceData;
}) {
  const [copyStatus, setCopyStatus] = useState<"idle" | "copied" | "failed">("idle");
  const copyResetTimer = useRef<number | null>(null);

  useEffect(() => () => {
    if (copyResetTimer.current !== null) window.clearTimeout(copyResetTimer.current);
  }, []);

  async function copyAnswer() {
    try {
      await navigator.clipboard.writeText(answer);
      setCopyStatus("copied");
    } catch {
      setCopyStatus("failed");
    }
    if (copyResetTimer.current !== null) window.clearTimeout(copyResetTimer.current);
    copyResetTimer.current = window.setTimeout(() => setCopyStatus("idle"), 1400);
  }

  return (
    <div className="max-w-[82%] text-xs text-ink/55">
      <div className="flex items-start gap-1.5 px-1">
        {hasEvidence && (
          <>
            <span title="Evidence is shown below this answer.">Evidence</span>
            <span aria-hidden="true">·</span>
          </>
        )}
        <details className="group min-w-0">
          <summary className="cursor-pointer list-none hover:text-ink [&::-webkit-details-marker]:hidden">
            Stats
          </summary>
          <div className="mt-2 grid w-[min(42rem,calc(100vw-4rem))] gap-3 rounded-md border border-ink/10 bg-white p-3 text-ink/75 shadow-sm sm:grid-cols-2">
            <TraceSection
              rows={[
                ["Answer latency", formatTiming(trace.timing.answer_latency_ms)],
                ["Synthesis", formatTiming(trace.timing.synthesis_elapsed_ms)],
                ["Preprocessing", formatTiming(trace.timing.preprocessing_elapsed_ms)],
                ["Vision", formatTiming(trace.timing.vision_elapsed_ms)],
                ["Verification", formatTiming(trace.timing.verifier_latency_ms)],
                ["Correction", formatTiming(trace.timing.correction_latency_ms)],
                ["Total vision analysis", formatTiming(trace.timing.total_vision_elapsed_ms)]
              ]}
              title="Timing"
            />
            <TraceSection
              rows={[
                ["Final answer", valueOrMissing(trace.models.final_answer_model)],
                ["Vision backend", valueOrMissing(trace.models.vision_backend)],
                ["Vision model", valueOrMissing(trace.models.vision_model)],
                ["OCR engine", valueOrMissing(trace.models.ocr_engine)],
                ["Embedding model", valueOrMissing(trace.models.embedding_model)],
                ["Embedding backend", valueOrMissing(trace.models.embedding_backend)]
              ]}
              title="Models"
            />
            <TraceSection
              rows={[
                ["RAG enabled", yesNo(trace.pipeline.rag_enabled)],
                ["RAG preset", valueOrMissing(trace.pipeline.rag_preset)],
                ["Verifier enabled", yesNo(trace.pipeline.verifier_enabled)],
                ["Verifier status", valueOrMissing(trace.pipeline.verifier_status)],
                ["Requested mode", valueOrMissing(trace.pipeline.requested_multimodal_mode)],
                ["Executed mode", valueOrMissing(trace.pipeline.executed_multimodal_mode)],
                ["Evidence reused", yesNo(trace.pipeline.visual_evidence_reused)],
                ["Vision rerun", yesNo(trace.pipeline.vision_rerun)],
                ["Context action", valueOrMissing(trace.pipeline.context_evidence_action)],
                ["Deterministic answer", yesNo(trace.pipeline.deterministic_visual_answer)],
                ["Answer style", valueOrMissing(trace.pipeline.answer_style)],
                ["Thinking mode", valueOrMissing(trace.pipeline.thinking_mode)],
                ["Done reason", valueOrMissing(trace.pipeline.done_reason)]
              ]}
              title="Pipeline"
            />
            <TraceSection
              rows={[
                ["Prompt tokens", numberOrMissing(trace.tokens.prompt_tokens)],
                ["Completion tokens", numberOrMissing(trace.tokens.completion_tokens)],
                ["Total duration", formatNanoseconds(trace.tokens.total_duration_ns)],
                ["Load duration", formatNanoseconds(trace.tokens.load_duration_ns)],
                ["Generation speed", formatTokenRate(trace.tokens.generation_tokens_per_second)]
              ]}
              title="Tokens"
            />
            <TraceSection
              rows={[
                ["Chunk IDs", listOrMissing(trace.sources.retrieved_chunk_ids)],
                ["Document IDs", listOrMissing(trace.sources.retrieved_document_ids)],
                ["Pages", listOrMissing(trace.sources.retrieved_page_numbers)]
              ]}
              title="Sources"
            />
            <TraceSection
              rows={[["Warnings", trace.warnings.length ? trace.warnings.join(" · ") : MISSING_VALUE]]}
              title="Warnings"
            />
            {trace.model_trace.thinking_returned && (
              <section className="sm:col-span-2">
                <h4 className="mb-1 font-semibold text-ink">Model trace</h4>
                <p className="leading-5">
                  Model trace returned by runtime, but full text was not saved.
                </p>
                <p className="mt-1 text-[11px] text-ink/50">
                  {trace.model_trace.thinking_char_count.toLocaleString()} character(s) returned
                  {trace.model_trace.thinking_truncated ? "; runtime trace was truncated" : ""}.
                </p>
              </section>
            )}
          </div>
        </details>
        <span aria-hidden="true">·</span>
        <button className="hover:text-ink" onClick={() => void copyAnswer()} type="button">
          {copyStatus === "copied" ? "Copied" : copyStatus === "failed" ? "Copy failed" : "Copy"}
        </button>
      </div>
    </div>
  );
}

function TraceSection({ rows, title }: { rows: Array<[string, string]>; title: string }) {
  return (
    <section>
      <h4 className="mb-1 font-semibold text-ink">{title}</h4>
      <dl className="space-y-1">
        {rows.map(([label, value]) => (
          <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.35fr)] gap-3" key={label}>
            <dt>{label}</dt>
            <dd className="break-words text-right text-ink">{value}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function formatTiming(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value < 0) return MISSING_VALUE;
  if (value < 100) return "<100 ms";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(1)} s`;
}

function formatNanoseconds(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value < 0) return MISSING_VALUE;
  return formatTiming(value / 1_000_000);
}

function formatTokenRate(value: number | undefined): string {
  if (value === undefined || !Number.isFinite(value) || value < 0) return MISSING_VALUE;
  return `${value.toFixed(1)} tok/s`;
}

function valueOrMissing(value: string | undefined): string {
  return value?.trim() || MISSING_VALUE;
}

function numberOrMissing(value: number | undefined): string {
  return value === undefined || !Number.isFinite(value) ? MISSING_VALUE : value.toLocaleString();
}

function yesNo(value: boolean | undefined): string {
  return value === undefined ? MISSING_VALUE : value ? "Yes" : "No";
}

function listOrMissing(values: Array<string | number>): string {
  return values.length ? values.join(", ") : MISSING_VALUE;
}
