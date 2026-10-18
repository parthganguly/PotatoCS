import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { build } from "esbuild";

const result = await build({
  entryPoints: ["src/features/chat/answerEvidence.ts"],
  bundle: true,
  format: "esm",
  platform: "node",
  write: false
});
const model = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString("base64")}`);

// Citation tokens: every character preserved; only IDs in this answer's map are valid.
const answer = "Fix A [S1]. Fix B [S1, S9]. See [S2][S3] and [not a cite].";
const segments = model.citationSegments(answer, ["S1", "S2"]);
assert.equal(segments.map((part) => (part.kind === "text" ? part.text : part.id)).join(""), answer);
assert.deepEqual(
  segments.filter((part) => part.kind === "citation").map((part) => [part.id, part.valid]),
  [["S1", true], ["S1", true], ["S9", false], ["S2", true], ["S3", false]]
);
assert.deepEqual(model.citationSegments("No citations here.", ["S1"]), [{ kind: "text", text: "No citations here." }]);

// Shared with the backend review (python/tests/test_answer_evidence.py): same IDs, same
// malformed literals, same warnings.
const shared = JSON.parse(readFileSync("python/tests/fixtures/source_reference_cases.json", "utf8"));
for (const item of shared.cases) {
  const segments = model.citationSegments(item.text, shared.evidence_ids);
  const rendered = segments.map((part) => (part.kind === "citation" ? part.id : part.text)).join("");
  assert.equal(rendered, item.text, item.name);
  const scanned = model.scanReferences(item.text);
  assert.deepEqual(scanned.cited, item.cited, item.name);
  assert.deepEqual(scanned.malformed, item.malformed, item.name);
  const citations = segments.filter((part) => part.kind === "citation");
  const valid = [...new Set(citations.filter((part) => part.valid).map((part) => part.id))];
  const unknown = [...new Set(citations.filter((part) => !part.valid).map((part) => part.id))];
  assert.deepEqual(valid, item.valid, item.name);
  assert.deepEqual(unknown, item.unknown, item.name);
  assert.deepEqual(model.referenceWarnings(scanned.cited, valid, unknown, scanned.malformed), item.warnings, item.name);
}

function evidence(overrides = {}, review = {}, verifier = {}) {
  return {
    schema_version: 2,
    rag_preset: "standard",
    coverage: "complete_markdown",
    request_spans: [],
    sections_requested: false,
    evidence: [],
    review: {
      completeness: "not_verified",
      completeness_label: "Completeness not verified",
      expected_sections: 0,
      observed_sections: [],
      missing_sections: [],
      cited_ids: ["S1"],
      valid_ids: ["S1"],
      unknown_ids: [],
      citations_absent: false,
      malformed_references: [],
      malformed_reference_count: 0,
      warnings: [],
      ...review
    },
    verifier: { enabled: false, status: "not_run", unsupported_claims: [], contradicted_claims: [], ...verifier },
    ...overrides
  };
}

assert.equal(model.isAnswerEvidence(evidence()), true);
assert.equal(model.isAnswerEvidence({ operation_trace: {} }), false);
assert.equal(model.isAnswerEvidence(null), false);

// A clean structural review yields no warning but completeness is still never "passed".
assert.deepEqual(model.answerEvidenceWarnings(evidence()), []);
assert.equal(model.COMPLETENESS_NOT_VERIFIED, "Completeness not verified");
assert.equal(model.EVIDENCE_SUPPLIED_LABEL, "Evidence supplied to the model");

const warned = model.answerEvidenceWarnings(
  evidence(
    { coverage: "excerpts" },
    { missing_sections: ["Q2", "Q4"], cited_ids: ["S7"], valid_ids: [], unknown_ids: ["S7"] },
    { status: "failed", unsupported_claims: ["claim x"], contradicted_claims: ["claim y"] }
  )
);
assert.deepEqual(warned, [
  "Missing answer sections: Q2, Q4.",
  "No valid source references resolve.",
  "The answer cites evidence IDs that were not supplied: S7.",
  "The model received selected excerpts, not the complete source.",
  "Verifier: possible contradiction — claim y",
  "Verifier: unsupported claim — claim x"
]);

// The card reads the saved review, not a fresh scan: saved malformed literals are shown
// as saved, and a capped list still reports the full count.
const savedMalformed = Array.from({ length: 20 }, (_, n) => ({ text: `[S.${n}]`, start: 0, end: 0, line: 1 }));
assert.equal(
  model.answerEvidenceWarnings(evidence({}, { cited_ids: [], valid_ids: [], citations_absent: true, malformed_references: savedMalformed, malformed_reference_count: 23 }))[1],
  `Malformed source references, not linked to any source: ${savedMalformed.map((item) => item.text).join(", ")} and 3 more.`
);
assert.equal(model.readTimeMalformedReferences(evidence(), "Saved [S.1]."), null);

// A review saved before the check (schema 1) means "not checked", never "none found".
const legacy = evidence({ schema_version: 1 }, { cited_ids: [], valid_ids: [], citations_absent: true });
delete legacy.review.malformed_references;
delete legacy.review.malformed_reference_count;
assert.equal(model.malformedReferencesChecked(legacy), false);
assert.deepEqual(model.answerEvidenceWarnings(legacy), [
  "No valid source references resolve.",
  "Malformed source references were not checked when this answer was saved."
]);
// Its read-time diagnostic is separate and labelled by the card; the saved review is untouched.
const legacySnapshot = JSON.stringify(legacy);
assert.deepEqual(model.readTimeMalformedReferences(legacy, "No end date [S.1]."), ["[S.1]"]);
assert.deepEqual(model.readTimeMalformedReferences(legacy, "No references."), []);
assert.equal(JSON.stringify(legacy), legacySnapshot);
assert.equal(model.evidenceCoverageLabel("excerpts").includes("not the complete source"), true);

assert.equal(model.evidenceLocation({ page_start: 1, page_end: 1 }), "page 1");
assert.equal(model.evidenceLocation({ page_start: 2, page_end: 3 }), "pages 2–3");
assert.equal(model.evidenceLocation({ page_start: null, page_end: null }), "");
assert.equal(model.canOpenImportedMarkdown({ file_type: "md", document_id: "doc" }), true);
assert.equal(model.canOpenImportedMarkdown({ file_type: "pdf", document_id: "doc" }), false);

for (const state of ["deleted", "missing", "changed", "too_large", "unsupported", "not_found"]) {
  assert.notEqual(model.sourceReadStateCopy(state), "", state);
}
assert.equal(model.sourceReadStateCopy("available"), "");
assert.match(model.sourceReadStateCopy("changed"), /no longer matches/);

// Source text is inert: rendered as escaped plain text, never HTML, never a path.
for (const file of [
  "src/features/sources/SourceInspector.tsx",
  "src/features/chat/AnswerEvidenceCard.tsx",
  "src/features/chat/answerEvidence.ts"
]) {
  const text = readFileSync(file, "utf8");
  assert.equal(text.includes("dangerouslySetInnerHTML"), false, file);
  assert.equal(/\bhref=/.test(text), false, file);
}
const inspector = readFileSync("src/features/sources/SourceInspector.tsx", "utf8");
assert.match(inspector, /role="dialog"/);
assert.match(inspector, /aria-modal="true"/);
assert.match(inspector, /event\.key === "Escape"/);
assert.match(inspector, /whitespace-pre-wrap/);
assert.match(inspector, /readImportedSource\(\{/);
// Windows checkouts use CRLF; the multi-line match below is written with LF.
const sourcesApi = readFileSync("src/api/sources.ts", "utf8").replace(/\r\n/g, "\n");
assert.match(sourcesApi, /rpc<SourceReadResult>\("documents\.read_source", \{\n    document_id: params\.documentId,/);

// Each answer renders its own saved evidence; the transient latest-turn panel steps aside.
const app = readFileSync("src/App.tsx", "utf8");
assert.match(app, /isAnswerEvidence\(message\.metadata\?\.answer_evidence\)/);
assert.match(app, /latestAssistantMessageId && !answerEvidence && props\.retrievedChunks\.length > 0/);
assert.equal((app.match(/<SourceInspector /g) ?? []).length, 1);
// Malformed and unknown tokens keep their literal text, are visibly marked and never open a source.
const cited = app.slice(app.indexOf("function CitedAnswerText("), app.indexOf("function VerificationNotice("));
assert.match(cited, /"Malformed source reference"/);
assert.match(cited, /"Malformed source reference \(read-time check\)"/);
assert.match(cited, /marker\("Unknown source ID"\)/);
assert.match(cited, /support is not verified/);
assert.equal((cited.match(/onOpenSource\(/g) ?? []).length, 1);
assert.doesNotMatch(cited, /verified source|supported by/i);
const card = readFileSync("src/features/chat/AnswerEvidenceCard.tsx", "utf8");
assert.match(card, /Read-time check of the saved text, not part of the saved review/);
assert.match(app, /<AnswerEvidenceCard answerText=\{message\.content\}/);

console.log("answer evidence UI checks passed");
