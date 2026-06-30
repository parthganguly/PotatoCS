import { convertFileSrc } from "@tauri-apps/api/core";
import { ArtifactAnalysisRun } from "../../tauri";

export function imageAssetSrc(path?: string): string {
  if (!path) return "";
  try {
    return convertFileSrc(path);
  } catch {
    return "";
  }
}

export function ArtifactAnalysisCard({ analysis }: { analysis: ArtifactAnalysisRun }) {
  const output = analysis.output ?? {};
  const answer = String(output.answer ?? "");
  const ocrText = String(output.ocr_text ?? "");
  const vision = asRecord(output.vision_observations);
  const visualEvidence = asRecord(output.visual_evidence);
  const curatedVisualEvidence = asRecord(output.curated_visual_evidence ?? analysis.evidence?.curated_visual_evidence);
  const retrievalMetadata = asRecord(curatedVisualEvidence.retrieval_metadata);
  const provenance = asRecord(output.provenance);
  const preprocessing = asRecord(analysis.evidence?.preprocessing);
  const original = asRecord(preprocessing.original);
  const visionInput = asRecord(preprocessing.vision_input);
  const ocrInput = asRecord(preprocessing.ocr_input);
  const finalModel = String(provenance.final_answer_model || provenance.requested_final_model || "");
  const requestedBackend = String(provenance.requested_backend || provenance.vision_backend_requested || analysis.requested_vision_backend || "");
  const visionBackend = String(provenance.vision_backend || analysis.actual_vision_backend || visualEvidence.backend || "");
  const visionModel = String(provenance.vision_inspection_model || provenance.vision_model || analysis.actual_vision_model || "");
  const failedStage = String(provenance.failed_stage || analysis.evidence?.failed_stage || "");
  const perceptionCompleted = Boolean(provenance.perception_completed || analysis.evidence?.perception_completed);
  const synthesisStarted = Boolean(provenance.synthesis_started || asRecord(analysis.evidence?.synthesis).synthesis_started);
  const ocrEngine = String(provenance.ocr_engine || analysis.ocr_engine || "");
  const modeRequested = String(provenance.mode_requested || analysis.mode || "");
  const modeExecuted = String(provenance.mode_executed || analysis.evidence?.mode_executed || analysis.mode || "");
  const questionType = String(curatedVisualEvidence.question_type || retrievalMetadata.question_type || "");
  const retrievedCount = Number(retrievalMetadata.retrieved_count ?? provenance.visual_retrieved_snippet_count ?? 0);
  const snippetCount = Number(retrievalMetadata.snippet_count ?? 0);
  const rawEvidenceReused = Boolean(provenance.raw_evidence_reused || asRecord(analysis.evidence?.conversation_context).raw_evidence_reused);
  const curatedRecomputed = Boolean(provenance.curated_evidence_recomputed || asRecord(analysis.evidence?.conversation_context).curated_evidence_recomputed);
  const visualRetrievalRecomputed = Boolean(
    retrievalMetadata.retrieval_recomputed || provenance.visual_snippets_retrieved || asRecord(analysis.evidence?.conversation_context).visual_snippets_retrieved
  );
  const visionRerun = Boolean(provenance.vision_rerun || asRecord(analysis.evidence?.conversation_context).vision_rerun);
  return (
    <div className="rounded-md border border-ink/15 bg-white p-4">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="text-sm font-semibold">Image Evidence</p>
          <p className="text-xs text-ink/55">
            {analysis.status} - {analysis.stage}
          </p>
        </div>
        <span className="rounded-md border border-tide/20 bg-[#eef8f8] px-2 py-1 text-xs text-tide">
          {finalModel || visionModel || ocrEngine || "local"}
        </span>
      </div>
      <div className="mb-3 grid gap-2 text-xs text-ink/65 md:grid-cols-2">
        <EvidenceMetric label="Final answer model" value={finalModel || "unavailable"} />
        <EvidenceMetric label="Requested backend" value={visionBackendLabel(requestedBackend)} />
        <EvidenceMetric label="Actual backend" value={visionBackendLabel(visionBackend)} />
        <EvidenceMetric label="Vision inspection model" value={visionModel || "unavailable"} />
        <EvidenceMetric label="Failed stage" value={failedStage || "none"} />
        <EvidenceMetric label="Perception completed" value={perceptionCompleted ? "yes" : "no"} />
        <EvidenceMetric label="Synthesis started" value={synthesisStarted ? "yes" : "no"} />
        <EvidenceMetric label="OCR engine" value={ocrEngine || "unavailable"} />
        <EvidenceMetric label="Mode" value={`${modeRequested || "unknown"} -> ${modeExecuted || "unknown"}`} />
        <EvidenceMetric label="Question type" value={questionType || "unknown"} />
        <EvidenceMetric label="Retrieved snippets" value={snippetCount ? `${retrievedCount} of ${snippetCount}` : String(retrievedCount || 0)} />
        <EvidenceMetric label="Raw evidence reused" value={rawEvidenceReused ? "yes" : "no"} />
        <EvidenceMetric label="Evidence recomputed" value={curatedRecomputed || visualRetrievalRecomputed ? "yes" : "no"} />
        <EvidenceMetric label="Vision rerun" value={visionRerun ? "yes" : "no"} />
      </div>
      {analysis.error && <p className="mb-3 rounded-md border border-clay/25 bg-[#fff3ee] px-3 py-2 text-xs text-clay">{analysis.error}</p>}
      {analysis.warnings.length > 0 && (
        <p className="mb-3 rounded-md border border-gold/30 bg-[#fff8e8] px-3 py-2 text-xs text-[#7a561d]">
          {analysis.warnings.join("; ")}
        </p>
      )}
      {answer && (
        <section className="mb-3">
          <p className="text-xs font-semibold uppercase text-ink/45">Answer</p>
          <p className="mt-1 whitespace-pre-wrap text-sm leading-6">{answer}</p>
        </section>
      )}
      {ocrText && (
        <details className="mb-3 rounded-md border border-ink/10 bg-[#faf9f3] p-3">
          <summary className="cursor-pointer text-xs font-semibold text-ink/65">Exact OCR text</summary>
          <p className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap text-xs leading-5 text-ink/75">{ocrText}</p>
        </details>
      )}
      {hasCuratedVisualEvidence(curatedVisualEvidence) && (
        <details className="rounded-md border border-ink/10 bg-[#faf9f3] p-3" open>
          <summary className="cursor-pointer text-xs font-semibold text-ink/65">Retrieved visual evidence</summary>
          <div className="mt-2 space-y-2 text-xs leading-5 text-ink/75">
            <ObservationList label="Retrieved visual evidence" value={textFromSnippets(curatedVisualEvidence.retrieved_visual_snippets)} />
            <ObservationList label="Direct observations" value={textFromRecords(curatedVisualEvidence.direct_observations)} />
            <ObservationList label="Supported inference" value={textFromRecords(curatedVisualEvidence.allowed_inferences)} />
            <ObservationList label="Limitations" value={curatedLimitations(curatedVisualEvidence)} />
          </div>
        </details>
      )}
      {(hasVisionObservations(vision) || hasStructuredVisualEvidence(visualEvidence)) && (
        <details className="mt-3 rounded-md border border-ink/10 bg-[#faf9f3] p-3">
          <summary className="cursor-pointer text-xs font-semibold text-ink/65">Raw perception output</summary>
          <div className="mt-2 space-y-2 text-xs leading-5 text-ink/75">
            {hasVisionObservations(vision) && (
              <div className="space-y-2">
                <p className="font-medium">Model visual observations</p>
                {String(vision.summary ?? "").trim() && <p>{String(vision.summary)}</p>}
                {["visible_objects", "spatial_relations", "interface_elements", "uncertain_observations", "not_visible_or_not_determinable", "model_visible_text"].map((key) => (
                  <ObservationList key={key} label={key.replace(/_/g, " ")} value={vision[key]} />
                ))}
              </div>
            )}
            {String(visualEvidence.summary ?? "").trim() && <p>{String(visualEvidence.summary)}</p>}
            <ObservationList label="tasks" value={visualEvidence.tasks} />
            <ObservationList label="objects" value={labelsFromRecords(visualEvidence.objects, "label")} />
            <ObservationList label="regions" value={labelsFromRecords(visualEvidence.regions, "caption")} />
            <ObservationList label="visible text" value={labelsFromRecords(visualEvidence.text, "text")} />
            <ObservationList label="not determinable" value={visualEvidence.not_determinable} />
          </div>
        </details>
      )}
      {(Object.keys(original).length > 0 || Object.keys(visionInput).length > 0 || Object.keys(ocrInput).length > 0) && (
        <details className="mt-3 rounded-md border border-ink/10 bg-[#faf9f3] p-3">
          <summary className="cursor-pointer text-xs font-semibold text-ink/65">Analysis details</summary>
          <div className="mt-2 grid gap-2 text-xs text-ink/70 md:grid-cols-3">
            <DerivativeDetail title="Original" value={original} />
            <DerivativeDetail title="Vision input" value={visionInput} />
            <DerivativeDetail title="OCR input" value={ocrInput} />
          </div>
        </details>
      )}
    </div>
  );
}

function EvidenceMetric({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-ink/10 bg-[#faf9f3] px-3 py-2">
      <p className="text-[11px] font-semibold uppercase text-ink/45">{label}</p>
      <p className="mt-1 truncate text-xs">{value}</p>
    </div>
  );
}

function ObservationList({ label, value }: { label: string; value: unknown }) {
  const items = Array.isArray(value) ? value.map(String).filter(Boolean) : [];
  if (items.length === 0) return null;
  return (
    <div>
      <p className="font-medium capitalize">{label}</p>
      <ul className="mt-1 list-disc space-y-1 pl-4">
        {items.map((item, index) => (
          <li key={`${item}-${index}`}>{item}</li>
        ))}
      </ul>
    </div>
  );
}

function DerivativeDetail({ title, value }: { title: string; value: Record<string, unknown> }) {
  if (Object.keys(value).length === 0) return null;
  const width = Number(value.width ?? 0);
  const height = Number(value.height ?? 0);
  const bytes = Number(value.byte_size ?? value.size_bytes ?? 0);
  const format = String(value.format ?? value.mime_type ?? "");
  return (
    <div className="rounded-md border border-ink/10 bg-white px-3 py-2">
      <p className="font-semibold">{title}</p>
      <p className="mt-1">{format || "unknown"}</p>
      <p>{width && height ? `${width} x ${height}` : "dimensions unavailable"}</p>
      <p>{bytes ? formatBytes(bytes) : "size unavailable"}</p>
    </div>
  );
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function hasVisionObservations(value: Record<string, unknown>): boolean {
  return Object.values(value).some((entry) => {
    if (Array.isArray(entry)) return entry.length > 0;
    return Boolean(String(entry ?? "").trim());
  });
}

function hasStructuredVisualEvidence(value: Record<string, unknown>): boolean {
  if (String(value.summary ?? "").trim()) return true;
  return ["objects", "regions", "text", "grounded_phrases"].some((key) => Array.isArray(value[key]) && (value[key] as unknown[]).length > 0);
}

function hasCuratedVisualEvidence(value: Record<string, unknown>): boolean {
  return ["retrieved_visual_snippets", "direct_observations", "allowed_inferences", "unsupported_requests", "warnings"].some(
    (key) => Array.isArray(value[key]) && (value[key] as unknown[]).length > 0
  );
}

function textFromRecords(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(asRecord(item).text ?? "")).filter(Boolean);
}

function textFromSnippets(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const record = asRecord(item);
    const id = String(record.id ?? "").trim();
    const text = String(record.text ?? "").trim();
    if (!text) return "";
    return id ? `[${id}] ${text}` : text;
  }).filter(Boolean);
}

function curatedLimitations(value: Record<string, unknown>): string[] {
  const unsupported = Array.isArray(value.unsupported_requests) ? value.unsupported_requests.map(String).filter(Boolean) : [];
  const warnings = Array.isArray(value.warnings) ? value.warnings.map(String).filter(Boolean) : [];
  const excluded = Array.isArray(value.excluded_irrelevant_entities) ? value.excluded_irrelevant_entities.map(String).filter(Boolean) : [];
  const result = [...unsupported, ...warnings];
  if (excluded.length > 0) {
    result.push(`Excluded from synthesis: ${excluded.slice(0, 12).join(", ")}`);
  }
  return result;
}

function labelsFromRecords(value: unknown, key: string): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => {
    const record = asRecord(item);
    return String(record[key] ?? "");
  }).filter(Boolean);
}

function visionBackendLabel(value: string): string {
  if (value === "florence2") return "Basic local vision - Florence 2";
  if (value === "ollama") return "Enhanced vision - Ollama";
  if (value === "ocr_only") return "OCR only";
  return value || "unavailable";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
