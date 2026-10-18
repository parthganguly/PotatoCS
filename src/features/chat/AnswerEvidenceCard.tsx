import { FileText } from "lucide-react";
import type { AnswerEvidence, AnswerEvidenceEntry } from "../../tauri";
import type { SourceInspectorTarget } from "../sources/SourceInspector";
import {
  COMPLETENESS_NOT_VERIFIED,
  EVIDENCE_SUPPLIED_LABEL,
  answerEvidenceWarnings,
  canOpenImportedMarkdown,
  evidenceCoverageLabel,
  evidenceLocation,
  readTimeMalformedReferences
} from "./answerEvidence";

export function inspectorTargetForEntry(entry: AnswerEvidenceEntry): SourceInspectorTarget {
  return {
    documentId: entry.document_id,
    title: entry.source,
    expectedContentHash: entry.document_content_hash || undefined,
    excerpt: {
      id: entry.id,
      text: entry.text,
      location: evidenceLocation(entry),
      coverage: entry.coverage,
      truncated: entry.text_truncated
    }
  };
}

// The evidence this one answer was given, with its own saved review. Listing an entry
// does not mean the answer used or is supported by it.
export function AnswerEvidenceCard({
  evidence,
  answerText,
  onOpenSource
}: {
  evidence: AnswerEvidence;
  answerText: string;
  onOpenSource: (target: SourceInspectorTarget) => void;
}) {
  const warnings = answerEvidenceWarnings(evidence);
  const readTime = readTimeMalformedReferences(evidence, answerText);
  return (
    <div className="max-w-[82%] rounded-md border border-tide/25 bg-[#eef8f8] px-3 py-2 text-xs text-tide" data-testid="answer-evidence">
      <div className="flex flex-wrap items-center gap-2 font-medium">
        <FileText size={14} aria-hidden="true" />
        <span>{EVIDENCE_SUPPLIED_LABEL}</span>
        <span className="rounded-md border border-tide/20 bg-white px-2 py-0.5 text-[11px]">
          {evidenceCoverageLabel(evidence.coverage)}
        </span>
        <span className="rounded-md border border-gold/40 bg-gold/10 px-2 py-0.5 text-[11px] text-ink/75">
          {COMPLETENESS_NOT_VERIFIED}
        </span>
      </div>
      {warnings.length > 0 && (
        <ul className="mt-2 space-y-0.5 rounded-md border border-clay/20 bg-[#fff7f2] px-2 py-1.5 text-clay">
          {warnings.map((warning) => <li key={warning}>{warning}</li>)}
        </ul>
      )}
      {readTime && (
        <p className="mt-2 rounded-md border border-ink/15 bg-white px-2 py-1.5 text-ink/70" data-testid="read-time-reference-check">
          Read-time check of the saved text, not part of the saved review:{" "}
          {readTime.length
            ? `Malformed source reference${readTime.length > 1 ? "s" : ""}: ${readTime.join(", ")}.`
            : "no malformed source references found."}
        </p>
      )}
      <div className="mt-2 space-y-1.5">
        {evidence.evidence.map((entry) => {
          const location = evidenceLocation(entry);
          return (
            <details className="rounded-md border border-tide/15 bg-white px-2 py-1.5 text-ink/75" key={entry.id}>
              <summary className="cursor-pointer text-xs font-medium text-tide">
                {entry.id}: {entry.source}
                {location ? `, ${location}` : ""} · {entry.coverage === "complete_document" ? "complete document" : "excerpt"}
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words font-mono text-xs leading-5 text-ink/70">
                {entry.text}
              </pre>
              {canOpenImportedMarkdown(entry) && (
                <button
                  className="mt-2 rounded border border-tide/30 px-2 py-0.5 font-medium text-tide hover:bg-tide/10"
                  onClick={() => onOpenSource(inspectorTargetForEntry(entry))}
                  type="button"
                >
                  Open source
                </button>
              )}
            </details>
          );
        })}
      </div>
    </div>
  );
}
