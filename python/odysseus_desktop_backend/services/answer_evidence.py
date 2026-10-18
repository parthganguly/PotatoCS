"""Per-answer document evidence: request spans, the persisted evidence record and
deterministic review of the generated answer.

Nothing here judges meaning. Request spans are a formatting aid cut only at explicit
question/directive boundaries; the review only checks structure (section markers),
whether model-authored [S<n>] tokens resolve to evidence this answer was given, and
which bracketed tokens look like source references but are malformed.
"""

from __future__ import annotations

import re
from typing import Any

# 2: the review records malformed source references. A saved review without
# `malformed_references` was never checked for them.
ANSWER_EVIDENCE_SCHEMA_VERSION = 2
MAX_REQUEST_SPANS = 8
MIN_SPAN_WORDS = 3
MAX_EVIDENCE_TEXT_CHARS = 6000
MAX_VERIFIER_FINDINGS = 12
MAX_MALFORMED_REFERENCES = 20
COMPLETENESS_NOT_VERIFIED = "Completeness not verified"

INTERROGATIVES = ("what", "how", "why", "when", "where", "who", "which")
# After a bare comma these usually open a relative/subordinate clause, so only a
# conjunction may introduce them as a new ask.
BARE_COMMA_INTERROGATIVES = ("what", "how", "why")
REQUEST_VERBS = (
    "explain",
    "describe",
    "list",
    "summarize",
    "summarise",
    "tell",
    "give",
    "show",
    "compare",
    "identify",
    "state",
    "outline",
    "clarify",
    "provide",
    "mention",
    "note",
)
# Verbs that also read as nouns ("state", "list", "note") never open a new clause mid-sentence.
CLAUSE_VERBS = ("explain", "describe", "summarize", "summarise", "compare", "identify", "outline", "clarify", "tell", "give")
AUXILIARY_OPENERS = ("can", "could", "would", "will", "do", "does", "did", "is", "are", "should", "please")
LEAD_INS = ("also", "then", "and", "finally", "next", "additionally", "lastly", "please")
ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "vs.", "cf.", "approx.", "no.", "fig.", "mr.", "mrs.", "ms.", "dr.")

PROTECTED_RE = re.compile(
    r"```.*?```|`[^`\n]*`|\"[^\"\n]*\"|“[^”\n]*”"
    r"|(?<!\w)'(?:[^'\n]|(?<=\w)'(?=\w))*'(?!\w)"
    r"|‘(?:[^’\n]|(?<=\w)’(?=\w))*’",
    re.S,
)
SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s)")
LIST_MARKER_RE = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s+")
WORD_RE = re.compile(r"[A-Za-z0-9']+")
_CLAUSE_WORDS = "|".join((*INTERROGATIVES, *CLAUSE_VERBS))
CLAUSE_SPLIT_RE = re.compile(
    rf"(?:,\s*(?:(?:and|or|also|then)\s+)?|\s+(?:and|or)\s+)(?=(?:{_CLAUSE_WORDS})\b)",
    re.I,
)
# Whitespace and digits are spelled out so these match the frontend's JavaScript
# regexes character for character (`\s` and `\d` differ between the two at the edges).
_WS = r"[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
_CITATION_GROUP = rf"S[0-9]+(?:{_WS}*[,;]{_WS}*S[0-9]+)*"
CITATION_ID_RE = re.compile(r"S[0-9]+")
# One left-to-right scan: a strict [S<n>] group, or any other short bracketed token.
REFERENCE_TOKEN_RE = re.compile(rf"\[({_CITATION_GROUP})\]|\[([^\[\]]{{1,40}})\]")
# Source-reference-like but not strict: an S (either case), at most one separator, a
# digit, then only IDs, digits, spaces and separators. Matches "[S.1]", "[S 1]", "[s1]",
# "[S1, S.2]" and "[S1-S3]"; not "[1]", "[S]", "[Section 2]" or "[S3 bucket]".
MALFORMED_REFERENCE_RE = re.compile(rf"{_WS}*[Ss]{_WS}*[.:#_-]?{_WS}*[0-9](?:[Ss0-9.,;:#_-]|{_WS})*")
SECTION_MARKER_RE = re.compile(r"^[ \t]*(?:#{1,6}[ \t]*)?(?:\*\*)?Q(\d+)\b", re.M)


def request_spans(question: str) -> list[str]:
    """Verbatim asks in request order, or `[question]` when boundaries are ambiguous."""
    original = (question or "").strip()
    if not original:
        return []
    protected = [match.span() for match in PROTECTED_RE.finditer(original)]
    sentences = _sentences(original, protected)
    asks: list[str] = []
    for sentence, offset in sentences:
        if _is_ask(sentence):
            asks.extend(_split_clauses(sentence, offset, protected))
        elif asks:
            # A statement after the first ask may carry an obligation we cannot place.
            return [original]
    if len(asks) < 2 or len(asks) > MAX_REQUEST_SPANS:
        return [original]
    return asks


def _inside(position: int, protected: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in protected)


def _sentences(text: str, protected: list[tuple[int, int]]) -> list[tuple[str, int]]:
    pieces: list[tuple[str, int]] = []
    line_start = 0
    for line in text.split("\n"):
        marker = LIST_MARKER_RE.match(line)
        body_start = line_start + (marker.end() if marker else 0)
        body = text[body_start : line_start + len(line)]
        start = 0
        for match in SENTENCE_END_RE.finditer(body):
            absolute = body_start + match.start()
            if _inside(absolute, protected):
                continue
            words = body[start : match.end()].split()
            if words and words[-1].lower() in ABBREVIATIONS:
                continue
            pieces.append((body[start : match.end()], body_start + start))
            start = match.end()
        pieces.append((body[start:], body_start + start))
        line_start += len(line) + 1
    stripped: list[tuple[str, int]] = []
    for piece, offset in pieces:
        lead = len(piece) - len(piece.lstrip())
        clean = piece.strip()
        if clean:
            stripped.append((clean, offset + lead))
    return stripped


def _first_words(sentence: str) -> list[str]:
    words = [word.lower() for word in WORD_RE.findall(sentence)]
    while words and words[0] in LEAD_INS:
        words = words[1:]
    return words


def _is_ask(sentence: str) -> bool:
    if sentence.endswith("?"):
        return True
    words = _first_words(sentence)
    if not words:
        return False
    return words[0] in INTERROGATIVES or words[0] in REQUEST_VERBS or (
        words[0] in AUXILIARY_OPENERS and len(words) > 1 and words[1] == "you"
    )


def _split_clauses(sentence: str, offset: int, protected: list[tuple[int, int]]) -> list[str]:
    cuts: list[tuple[int, int]] = []
    for match in CLAUSE_SPLIT_RE.finditer(sentence):
        if _inside(offset + match.start(), protected):
            continue
        separator = match.group(0)
        following = sentence[match.end() :].split(None, 1)[0].lower().strip(",.;:?!")
        bare_comma = separator.strip() == ","
        if bare_comma and following not in BARE_COMMA_INTERROGATIVES and following not in CLAUSE_VERBS:
            continue
        cuts.append(match.span())
    parts: list[str] = []
    start = 0
    for cut_start, cut_end in cuts:
        candidate = sentence[start:cut_start].strip()
        remainder = sentence[cut_end:].strip()
        # Both sides must stay a real ask; otherwise keep the clause together.
        if len(WORD_RE.findall(candidate)) < MIN_SPAN_WORDS or len(WORD_RE.findall(remainder)) < MIN_SPAN_WORDS:
            continue
        parts.append(candidate)
        start = cut_end
    parts.append(sentence[start:].strip())
    return [part for part in parts if part]


def section_instructions(spans: list[str]) -> str:
    markers = "\n".join(f"### Q{index}: {span}" for index, span in enumerate(spans, start=1))
    return (
        f"Answer in {len(spans)} numbered sections, one per ask, in this order. Begin each "
        "section with its marker line exactly as shown. In each section answer that ask, "
        "including requested counts, actions and outcomes where stated. Preserve the document's "
        "hierarchy and count the category the user asks for, including substeps when requested. "
        "If the evidence does not establish it, say so.\n"
        f"{markers}"
    )


def scan_references(answer: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Strict [S<n>] IDs in first-seen order, and each malformed reference-like token.

    A malformed token is kept whole: no valid-looking ID is salvaged from inside it.
    Offsets are code-point positions in the saved answer; nothing is rewritten.
    """
    text = answer or ""
    cited: list[str] = []
    malformed: list[dict[str, Any]] = []
    for match in REFERENCE_TOKEN_RE.finditer(text):
        if match.group(1) is not None:
            for item in CITATION_ID_RE.findall(match.group(1)):
                if item not in cited:
                    cited.append(item)
        elif MALFORMED_REFERENCE_RE.fullmatch(match.group(2)):
            malformed.append(
                {
                    "text": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                    "line": text.count("\n", 0, match.start()) + 1,
                }
            )
    return cited, malformed


def citation_ids(answer: str) -> list[str]:
    return scan_references(answer)[0]


def reference_warnings(
    cited: list[str], valid: list[str], unknown: list[str], malformed: list[dict[str, Any]]
) -> list[str]:
    """Worded exactly as the frontend's referenceWarnings(); shared fixtures pin both."""
    warnings: list[str] = []
    if not valid:
        if cited:
            warnings.append("No valid source references resolve.")
        elif malformed:
            warnings.append("No valid source references resolve: the answer's only source references are malformed.")
        else:
            warnings.append("No valid source references resolve: the answer contains no source references.")
    if unknown:
        warnings.append(f"The answer cites evidence IDs that were not supplied: {', '.join(unknown)}.")
    if malformed:
        shown = ", ".join(item["text"] for item in malformed[:MAX_MALFORMED_REFERENCES])
        extra = len(malformed) - MAX_MALFORMED_REFERENCES
        warnings.append(
            f"Malformed source reference{'s' if len(malformed) > 1 else ''}, not linked to any source: "
            f"{shown}{f' and {extra} more' if extra > 0 else ''}."
        )
    return warnings


def review_answer(
    answer: str,
    *,
    evidence_ids: list[str],
    expected_sections: int,
    coverage: str,
) -> dict[str, Any]:
    """Deterministic structural checks only; never a support or completeness pass."""
    cited, malformed = scan_references(answer)
    known = set(evidence_ids)
    valid = [item for item in cited if item in known]
    unknown = [item for item in cited if item not in known]
    observed = sorted(
        {
            int(number)
            for number in SECTION_MARKER_RE.findall(answer or "")
            if 1 <= int(number) <= expected_sections
        }
    )
    missing = [f"Q{number}" for number in range(1, expected_sections + 1) if number not in observed]
    warnings: list[str] = []
    if missing:
        warnings.append(f"Missing answer sections: {', '.join(missing)}.")
    warnings.extend(reference_warnings(cited, valid, unknown, malformed))
    if coverage != "complete_markdown":
        warnings.append("The model received selected excerpts, not the complete source.")
    return {
        "completeness": "not_verified",
        "completeness_label": COMPLETENESS_NOT_VERIFIED,
        "expected_sections": expected_sections,
        "observed_sections": [f"Q{number}" for number in observed],
        "missing_sections": missing if expected_sections else [],
        "cited_ids": cited,
        "valid_ids": valid,
        "unknown_ids": unknown,
        "citations_absent": not cited,
        "malformed_references": malformed[:MAX_MALFORMED_REFERENCES],
        "malformed_reference_count": len(malformed),
        "warnings": warnings,
    }


def packet_coverage(snippets: list[dict[str, Any]]) -> str:
    if snippets and all(snippet.get("coverage") == "complete_document" for snippet in snippets):
        return "complete_markdown"
    return "excerpts"


def evidence_entry(snippet: dict[str, Any]) -> dict[str, Any]:
    metadata = snippet.get("metadata") if isinstance(snippet.get("metadata"), dict) else {}
    text = str(snippet.get("text") or "")
    chunk_ids = [str(item) for item in snippet.get("retrieved_chunk_ids") or [] if str(item)]
    if not chunk_ids and snippet.get("chunk_id"):
        chunk_ids = [str(snippet["chunk_id"])]
    return {
        "id": str(snippet.get("snippet_id") or ""),
        "document_id": str(snippet.get("document_id") or ""),
        "source": str(snippet.get("source") or metadata.get("title") or "Document"),
        "file_type": str(metadata.get("file_type") or ""),
        "page_start": snippet.get("page_start"),
        "page_end": snippet.get("page_end"),
        "coverage": "complete_document" if snippet.get("coverage") == "complete_document" else "excerpt",
        "ocr": bool(metadata.get("ocr")),
        "text": text[:MAX_EVIDENCE_TEXT_CHARS],
        "text_truncated": len(text) > MAX_EVIDENCE_TEXT_CHARS,
        "document_content_hash": str(snippet.get("document_content_hash") or ""),
        "page_text_hash": str(snippet.get("page_text_hash") or ""),
        "retrieved_chunk_ids": chunk_ids,
    }


def build_answer_evidence(
    *,
    answer: str,
    snippets: list[dict[str, Any]],
    spans: list[str],
    sections_requested: bool,
    rag_preset: str,
    grounding: dict[str, Any] | None,
) -> dict[str, Any]:
    entries = [evidence_entry(snippet) for snippet in snippets]
    coverage = packet_coverage(snippets)
    verifier = (grounding or {}).get("verifier") if isinstance((grounding or {}).get("verifier"), dict) else {}
    return {
        "schema_version": ANSWER_EVIDENCE_SCHEMA_VERSION,
        "rag_preset": rag_preset,
        "coverage": coverage,
        "request_spans": spans,
        "sections_requested": sections_requested,
        "evidence": entries,
        "review": review_answer(
            answer,
            evidence_ids=[entry["id"] for entry in entries],
            expected_sections=len(spans) if sections_requested else 0,
            coverage=coverage,
        ),
        "verifier": {
            "enabled": bool(verifier.get("enabled")),
            "status": str(verifier.get("status") or "not_run"),
            "unsupported_claims": [
                str(item) for item in (grounding or {}).get("unsupported_claims") or []
            ][:MAX_VERIFIER_FINDINGS],
            "contradicted_claims": [
                str(item) for item in (grounding or {}).get("contradicted_claims") or []
            ][:MAX_VERIFIER_FINDINGS],
        },
    }
