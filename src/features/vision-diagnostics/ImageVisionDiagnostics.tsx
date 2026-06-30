import { Cpu, Image, ScreenShare } from "lucide-react";
import { ImageVisionDiagnostics as ImageVisionDiagnosticsData, ModelCapability } from "../../tauri";

export function ImageVisionDiagnostics(props: { data?: ImageVisionDiagnosticsData; selectedVisionModel: string; onRefreshModels: () => void; busy: boolean }) {
  const data = props.data;
  const capabilities = data?.model_capabilities ?? [];
  return (
    <div className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-3 flex items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm font-semibold">
          <Image size={17} aria-hidden="true" />
          Image & Vision
        </div>
        <button
          className="rounded-md border border-ink/15 px-3 py-1.5 text-xs font-medium hover:bg-[#faf9f3]"
          disabled={props.busy}
          onClick={props.onRefreshModels}
          type="button"
        >
          Refresh models
        </button>
      </div>
      {!data ? (
        <p className="text-sm text-ink/55">Image diagnostics unavailable.</p>
      ) : (
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 text-sm lg:grid-cols-4">
            <Metric label="Images" value={data.artifacts.artifact_count} />
            <Metric label="Sources" value={data.sources ? `${data.sources.library_count} library / ${data.sources.session_count} session` : "unknown"} />
            <Metric label="Derivations" value={data.artifacts.derivation_count} />
            <Metric label="Vision derivatives" value={data.artifacts.vision_derivative_count ?? 0} />
            <Metric label="OCR derivatives" value={data.artifacts.ocr_derivative_count ?? 0} />
            <Metric label="RAG sources" value={data.artifacts.rag_source_count} />
            <Metric label="Errors/interrupted" value={data.artifacts.interrupted_or_error_analysis_count} />
            <Metric label="Formats" value={data.artifacts.supported_formats.join(", ")} />
            <Metric label="Max file" value={formatBytes(data.artifacts.max_original_bytes)} />
            <Metric label="Max pixels" value={data.artifacts.max_decoded_pixels.toLocaleString()} />
            <Metric label="Images/turn" value={data.artifacts.max_images_per_turn} />
            <Metric label="Preprocess" value={data.artifacts.preprocessing_version ?? "unknown"} />
            <Metric label="Vision input" value={`${data.artifacts.vision_max_edge ?? "?"} px / ${data.artifacts.vision_jpeg_quality ?? "?"} q`} />
            <Metric label="OCR input" value={`${data.artifacts.ocr_max_edge ?? "?"} px PNG`} />
          </div>

          <div className="rounded-md border border-ink/10 bg-[#faf9f3] p-3 text-xs text-ink/70">
            <div className="mb-2 flex items-center gap-2 font-semibold text-ink">
              <ScreenShare size={15} aria-hidden="true" />
              Capture support
            </div>
            <p>
              Full screen: {yesNo(data.capture.full_screen)}; region: {yesNo(data.capture.region)}; window: {yesNo(data.capture.window)}; clipboard: {yesNo(Boolean(data.capture.clipboard_image))}
            </p>
            <p className="mt-1">{data.capture.message}</p>
          </div>

          {data.florence && (
            <div className="rounded-md border border-ink/10 bg-[#faf9f3] p-3 text-xs text-ink/70">
              <div className="mb-2 flex items-center gap-2 font-semibold text-ink">
                <Cpu size={15} aria-hidden="true" />
                Florence 2 Basic
              </div>
              <div className="grid gap-2 md:grid-cols-3">
                <Metric label="State" value={data.florence.state} />
                <Metric label="Failed stage" value={data.florence.failed_stage || "none"} />
                <Metric label="Ready" value={yesNo(data.florence.ready)} />
                <Metric label="License" value={data.florence.license} />
                <Metric label="Revision" value={shortHash(data.florence.revision)} />
                <Metric label="Trust remote code" value={yesNo(data.florence.trust_remote_code)} />
                <Metric label="Runtime downloads" value={yesNo(data.florence.normal_runtime_downloads)} />
                <Metric label="Selected source" value={data.florence.selected_pack_source || "none"} />
              </div>
              <p className="mt-2">{data.florence.message}</p>
              {data.florence.pack_dir && <p className="mt-1 truncate" title={data.florence.pack_dir}>{data.florence.pack_dir}</p>}
              {data.florence.python_executable && <p className="mt-1 truncate" title={data.florence.python_executable}>Python: {data.florence.python_executable}</p>}
              {data.florence.path_context?.resource_dir && <p className="mt-1 truncate" title={data.florence.path_context.resource_dir}>Resource dir: {data.florence.path_context.resource_dir}</p>}
              {data.florence.path_context?.dev_repo_root && <p className="mt-1 truncate" title={data.florence.path_context.dev_repo_root}>Dev repo root: {data.florence.path_context.dev_repo_root}</p>}
              <p className="mt-1">Native classes: {data.florence.native_class_status}</p>
              {data.florence.searched_candidates && data.florence.searched_candidates.length > 0 && (
                <div className="mt-3 overflow-hidden rounded-md border border-ink/10 bg-white">
                  <table className="w-full border-collapse text-left">
                    <thead className="bg-[#f4f3ec] text-ink/60">
                      <tr>
                        <th className="px-2 py-1.5">Source</th>
                        <th className="px-2 py-1.5">Exists</th>
                        <th className="px-2 py-1.5">Manifest</th>
                        <th className="px-2 py-1.5">Path</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.florence.searched_candidates.map((candidate) => (
                        <tr className="border-t border-ink/10" key={`${candidate.source}:${candidate.path}`}>
                          <td className="px-2 py-1.5 font-medium">{candidate.source}</td>
                          <td className="px-2 py-1.5">{yesNo(candidate.exists)}</td>
                          <td className="px-2 py-1.5">{candidate.manifest_parsed ? "parsed" : candidate.manifest_present ? "failed" : "missing"}</td>
                          <td className="max-w-[420px] truncate px-2 py-1.5" title={candidate.rejection_reason || candidate.path}>{candidate.path}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          <div>
            <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase text-ink/45">
              <Cpu size={14} aria-hidden="true" />
              Model capabilities
            </div>
            <div className="overflow-hidden rounded-md border border-ink/10">
              <table className="w-full border-collapse text-left text-xs">
                <thead className="bg-[#f4f3ec] text-ink/60">
                  <tr>
                    <th className="px-3 py-2">Model</th>
                    <th className="px-3 py-2">Vision</th>
                    <th className="px-3 py-2">Text</th>
                    <th className="px-3 py-2">Family</th>
                    <th className="px-3 py-2">Error</th>
                  </tr>
                </thead>
                <tbody>
                  {capabilities.length === 0 ? (
                    <tr>
                      <td className="px-3 py-3 text-ink/55" colSpan={5}>
                        No capability rows yet.
                      </td>
                    </tr>
                  ) : (
                    capabilities.map((capability) => (
                      <CapabilityRow capability={capability} selected={capability.model === props.selectedVisionModel} key={capability.model} />
                    ))
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function CapabilityRow({ capability, selected }: { capability: ModelCapability; selected: boolean }) {
  return (
    <tr className={selected ? "bg-[#edf7ef]" : "border-t border-ink/10"}>
      <td className="max-w-[260px] truncate px-3 py-2 font-medium" title={capability.model}>
        {capability.model}
      </td>
      <td className="px-3 py-2">{capability.vision}</td>
      <td className="px-3 py-2">{capability.text_generation}</td>
      <td className="px-3 py-2">{capability.family || capability.parameter_size || "unknown"}</td>
      <td className="max-w-[260px] truncate px-3 py-2 text-clay" title={capability.error}>
        {capability.error}
      </td>
    </tr>
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

function yesNo(value: boolean): string {
  return value ? "yes" : "no";
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function shortHash(value: string): string {
  return value ? value.slice(0, 12) : "unknown";
}
