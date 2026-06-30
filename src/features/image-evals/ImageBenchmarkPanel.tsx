import { BarChart3, Clipboard, History } from "lucide-react";
import { ImageEvalRun, ImageEvalSuite, ModelCapability, MultimodalMode } from "../../tauri";

export function ImageBenchmarkPanel(props: {
  suite: ImageEvalSuite | null;
  history: ImageEvalRun[];
  result: ImageEvalRun | null;
  mode: MultimodalMode;
  model: string;
  visionModels: ModelCapability[];
  busy: boolean;
  copyStatus: string;
  onCopySummary: () => void;
  onRun: () => void;
  onSetMode: (mode: MultimodalMode) => void;
  onSetModel: (model: string) => void;
}) {
  const needsModel = props.mode !== "ocr_only";
  const canRun = props.mode === "ocr_only" || props.model.trim().length > 0;
  const latest = props.result ?? props.history[0] ?? null;
  return (
    <div className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-3 flex items-center gap-2 text-sm font-semibold">
        <BarChart3 size={17} aria-hidden="true" />
        Image Benchmark
      </div>
      <p className="mb-3 text-xs text-ink/55">
        Separate local image suite; modes are not compared against the RAG benchmark.
      </p>
      <div className="grid gap-3 md:grid-cols-[150px_minmax(170px,1fr)_auto_auto]">
        <select
          className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
          disabled={props.busy}
          onChange={(event) => props.onSetMode(event.target.value as MultimodalMode)}
          value={props.mode}
        >
          <option value="ocr_only">OCR only</option>
          <option value="vision_only">Vision only</option>
          <option value="combined">Combined</option>
        </select>
        <select
          className="h-10 rounded-md border border-ink/20 bg-white px-3 text-sm outline-none focus:border-tide"
          disabled={props.busy || !needsModel || props.visionModels.length === 0}
          onChange={(event) => props.onSetModel(event.target.value)}
          value={props.model}
        >
          {!needsModel ? (
            <option value="">OCR only</option>
          ) : props.visionModels.length === 0 ? (
            <option value="">No confirmed vision model</option>
          ) : (
            props.visionModels.map((model) => (
              <option key={model.model} value={model.model}>
                {model.model}
              </option>
            ))
          )}
        </select>
        <button
          className="flex h-10 items-center justify-center gap-2 rounded-md bg-moss px-3 text-sm font-medium text-white hover:bg-[#35543d]"
          disabled={props.busy || !canRun}
          onClick={props.onRun}
          type="button"
        >
          <BarChart3 size={16} aria-hidden="true" />
          Run
        </button>
        <button
          className="flex h-10 items-center justify-center gap-2 rounded-md border border-ink/15 bg-white px-3 text-sm font-medium hover:bg-[#faf9f3]"
          disabled={props.busy || (!props.result && props.history.length === 0)}
          onClick={props.onCopySummary}
          type="button"
        >
          <Clipboard size={16} aria-hidden="true" />
          Copy
        </button>
      </div>
      <div className="mt-3 grid grid-cols-4 gap-2 text-xs text-ink/65">
        <Metric label="Suite" value={props.suite?.suite_name ?? "local-image-understanding"} />
        <Metric label="Version" value={props.suite?.suite_version ?? "checking"} />
        <Metric label="Cases" value={props.suite?.case_count ?? 0} />
        <Metric label="History" value={props.history.length} />
      </div>
      {props.copyStatus && <p className="mt-3 text-xs text-moss">{props.copyStatus}</p>}
      {latest && (
        <div className="mt-4 rounded-md border border-ink/10">
          <div className="flex items-center justify-between border-b border-ink/10 px-3 py-2">
            <p className="text-sm font-medium">
              {latest.mode} - {latest.model || "OCR"} - {latest.status}
            </p>
            <p className="text-xs text-ink/55">
              {latest.total_passed} passed, {latest.total_failed} failed, {latest.grader_review_count} review
            </p>
          </div>
          <div className="divide-y divide-ink/10">
            {latest.cases.slice(0, 10).map((item) => (
              <div className="px-3 py-2 text-xs" key={item.id}>
                <div className="flex items-center justify-between gap-3">
                  <span className="font-medium">{item.case_id}</span>
                  <span className={item.passed ? "text-moss" : item.grader_review_required ? "text-[#7a561d]" : "text-clay"}>
                    {item.grader_review_required ? "review" : item.passed ? "pass" : item.status}
                  </span>
                </div>
                {item.reasons.length > 0 && <p className="mt-1 text-ink/55">{item.reasons.join("; ")}</p>}
              </div>
            ))}
          </div>
        </div>
      )}
      {props.history.length > 0 && (
        <div className="mt-4">
          <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase text-ink/45">
            <History size={14} aria-hidden="true" />
            History
          </div>
          <div className="space-y-2">
            {props.history.slice(0, 4).map((run) => (
              <p className="text-xs text-ink/60" key={run.id}>
                {new Date(run.created_at).toLocaleString()} - {run.mode} - {run.total_passed}/{run.total_passed + run.total_failed} passed
              </p>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs font-medium uppercase text-ink/45">{label}</p>
      <p className="mt-0.5 text-sm">{value || "unavailable"}</p>
    </div>
  );
}
