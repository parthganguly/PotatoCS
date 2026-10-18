import type { AnswerEvidence, AnswerEvidenceEntry, SourceReadState } from "../../tauri";

export const EVIDENCE_SUPPLIED_LABEL = "Evidence supplied to the model";
export const COMPLETENESS_NOT_VERIFIED = "Completeness not verified";

export type CitationSegment =
  | { kind: "text"; text: string }
  | { kind: "citation"; id: string; valid: boolean }
  | { kind: "malformed"; text: string };

// Character for character the backend's regexes in answer_evidence.py (whitespace and
// digits spelled out because JS and Python disagree on \s and \d at the edges).
const WS = "[\\t\\n\\v\\f\\r \\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]";
const CITATION_GROUP = `S[0-9]+(?:${WS}*[,;]${WS}*S[0-9]+)*`;
const REFERENCE_TOKEN_RE = new RegExp(`\\[(${CITATION_GROUP})\\]|\\[([^\\[\\]]{1,40})\\]`, "g");
const MALFORMED_REFERENCE_RE = new RegExp(`^${WS}*[Ss]${WS}*[.:#_-]?${WS}*[0-9](?:[Ss0-9.,;:#_-]|${WS})*$`);
const MAX_MALFORMED_REFERENCES = 20;

// Split answer text into plain text, model-authored [S<n>] tokens and malformed
// reference-like tokens, preserving every character. Only IDs present in this answer's
// own evidence map are marked valid; a malformed token stays whole, nothing is salvaged.
export function citationSegments(text: string, validIds: readonly string[]): CitationSegment[] {
  const known = new Set(validIds);
  const segments: CitationSegment[] = [];
  const pushText = (value: string) => {
    if (!value) return;
    const last = segments[segments.length - 1];
    if (last?.kind === "text") last.text += value;
    else segments.push({ kind: "text", text: value });
  };
  let cursor = 0;
  for (const group of text.matchAll(REFERENCE_TOKEN_RE)) {
    const start = group.index ?? 0;
    pushText(text.slice(cursor, start));
    cursor = start + group[0].length;
    if (group[1] === undefined) {
      if (MALFORMED_REFERENCE_RE.test(group[2])) segments.push({ kind: "malformed", text: group[0] });
      else pushText(group[0]);
      continue;
    }
    const inner = group[1];
    pushText("[");
    let innerCursor = 0;
    for (const id of inner.matchAll(/S[0-9]+/g)) {
      const idStart = id.index ?? 0;
      pushText(inner.slice(innerCursor, idStart));
      segments.push({ kind: "citation", id: id[0], valid: known.has(id[0]) });
      innerCursor = idStart + id[0].length;
    }
    pushText(inner.slice(innerCursor) + "]");
  }
  pushText(text.slice(cursor));
  return segments;
}

// Strict IDs in first-seen order and the malformed literals, as the backend review saves them.
export function scanReferences(text: string): { cited: string[]; malformed: string[] } {
  const cited: string[] = [];
  const malformed: string[] = [];
  for (const segment of citationSegments(text, [])) {
    if (segment.kind === "citation" && !cited.includes(segment.id)) cited.push(segment.id);
    if (segment.kind === "malformed") malformed.push(segment.text);
  }
  return { cited, malformed };
}

// `malformed` null means the saved review predates the malformed-reference check.
export function referenceWarnings(
  cited: readonly string[],
  valid: readonly string[],
  unknown: readonly string[],
  malformed: readonly string[] | null,
  malformedCount = malformed?.length ?? 0
): string[] {
  const warnings: string[] = [];
  if (!valid.length) {
    if (cited.length || malformed === null) warnings.push("No valid source references resolve.");
    else if (malformedCount) warnings.push("No valid source references resolve: the answer's only source references are malformed.");
    else warnings.push("No valid source references resolve: the answer contains no source references.");
  }
  if (unknown.length) warnings.push(`The answer cites evidence IDs that were not supplied: ${unknown.join(", ")}.`);
  if (malformed === null) {
    warnings.push("Malformed source references were not checked when this answer was saved.");
  } else if (malformedCount) {
    const extra = malformedCount - MAX_MALFORMED_REFERENCES;
    warnings.push(
      `Malformed source reference${malformedCount > 1 ? "s" : ""}, not linked to any source: ` +
        `${malformed.slice(0, MAX_MALFORMED_REFERENCES).join(", ")}${extra > 0 ? ` and ${extra} more` : ""}.`
    );
  }
  return warnings;
}

export function malformedReferencesChecked(evidence: AnswerEvidence): boolean {
  return Array.isArray(evidence.review.malformed_references);
}

export function isAnswerEvidence(value: unknown): value is AnswerEvidence {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Partial<AnswerEvidence>;
  return Array.isArray(candidate.evidence) && typeof candidate.review === "object" && candidate.review !== null;
}

export function evidenceCoverageLabel(coverage: AnswerEvidence["coverage"]): string {
  return coverage === "complete_markdown"
    ? "Complete imported Markdown document"
    : "Selected excerpts — not the complete source";
}

// Concrete warnings for this answer. Completeness is never marked as passed.
export function answerEvidenceWarnings(evidence: AnswerEvidence): string[] {
  const review = evidence.review;
  const warnings: string[] = [];
  if (review.missing_sections?.length) warnings.push(`Missing answer sections: ${review.missing_sections.join(", ")}.`);
  const malformed = malformedReferencesChecked(evidence)
    ? (review.malformed_references ?? []).map((item) => item.text)
    : null;
  warnings.push(
    ...referenceWarnings(
      review.cited_ids ?? [],
      review.valid_ids ?? [],
      review.unknown_ids ?? [],
      malformed,
      review.malformed_reference_count ?? malformed?.length ?? 0
    )
  );
  if (evidence.coverage !== "complete_markdown") warnings.push("The model received selected excerpts, not the complete source.");
  for (const claim of evidence.verifier?.contradicted_claims ?? []) warnings.push(`Verifier: possible contradiction — ${claim}`);
  for (const claim of evidence.verifier?.unsupported_claims ?? []) warnings.push(`Verifier: unsupported claim — ${claim}`);
  return warnings;
}

// Answers saved before the check: a read-time scan of the saved text, labelled as such
// and never written back. Null when the saved review already holds the result.
export function readTimeMalformedReferences(evidence: AnswerEvidence, answerText: string): string[] | null {
  return malformedReferencesChecked(evidence) ? null : scanReferences(answerText).malformed;
}

export function evidenceLocation(entry: Pick<AnswerEvidenceEntry, "page_start" | "page_end">): string {
  if (!entry.page_start) return "";
  return entry.page_end && entry.page_end !== entry.page_start
    ? `pages ${entry.page_start}–${entry.page_end}`
    : `page ${entry.page_start}`;
}

export function canOpenImportedMarkdown(entry: Pick<AnswerEvidenceEntry, "file_type" | "document_id">): boolean {
  return entry.file_type === "md" && Boolean(entry.document_id);
}

export function sourceReadStateCopy(state: SourceReadState): string {
  switch (state) {
    case "available":
      return "";
    case "deleted":
      return "This source was deleted. The saved evidence snapshot from the answer is still shown.";
    case "missing":
      return "The imported copy of this source is missing from the app profile.";
    case "changed":
      return "The imported copy no longer matches the source this answer used, so it is not shown as the original.";
    case "too_large":
      return "This source is too large to open in the inspector.";
    case "unsupported":
      return "Only imported Markdown sources can be opened here.";
    default:
      return "This source is no longer available.";
  }
}
