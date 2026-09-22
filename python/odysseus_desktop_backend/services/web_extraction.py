from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from typing import Any


CHARSET_RE = re.compile(r"charset\s*=\s*['\"]?([^\s;'\"]+)", re.IGNORECASE)
BLOCKED_TAGS = {"button", "form", "iframe", "input", "noscript", "script", "style", "template"}
NON_TEXT_EVIDENCE_TAGS = {"math", "svg"}
BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "caption", "dd", "details", "div", "dl", "dt",
    "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr",
    "li", "main", "nav", "ol", "p", "pre", "section", "summary", "table", "tbody", "tfoot",
    "option", "select", "thead", "tr", "ul",
}
CELL_TAGS = {"td", "th"}


class WebExtractionError(RuntimeError):
    code = "extraction_failed"


@dataclass(frozen=True)
class ExtractedWebPage:
    title: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    visual_signals: list[str] = field(default_factory=list)
    provenance_kind: str = "exact_text"


class WebContentExtractor:
    def extract(self, body: bytes, *, content_type: str, final_url: str) -> ExtractedWebPage:
        media_type = str(content_type or "").split(";", 1)[0].strip().lower()
        if media_type == "application/pdf":
            return self._extract_pdf(body, final_url=final_url)
        if media_type == "text/plain":
            text = decode_web_text(body, content_type)
            clean = clean_extracted_text(text)
            if not clean:
                raise WebExtractionError("plain-text response was empty")
            return ExtractedWebPage(
                title=final_url,
                text=clean,
                metadata={"canonical_url": final_url},
                visual_signals=[],
                provenance_kind="exact_text",
            )
        if media_type not in {"text/html", "application/xhtml+xml"}:
            raise WebExtractionError(f"unsupported extraction content type: {media_type or 'missing'}")
        return self._extract_html(body, content_type=content_type, final_url=final_url)

    def _extract_html(self, body: bytes, *, content_type: str, final_url: str) -> ExtractedWebPage:
        try:
            from lxml import html
            from readability import Document
        except ImportError as exc:
            raise WebExtractionError(
                "HTML extraction requires readability-lxml from python/requirements.txt"
            ) from exc
        decoded = decode_web_text(body, content_type)
        try:
            original = html.fromstring(decoded, base_url=final_url)
            article = Document(decoded)
            summary = article.summary(html_partial=True)
            main = html.fromstring(summary, base_url=final_url)
        except Exception as exc:  # noqa: BLE001 - parser errors become typed extraction failures
            raise WebExtractionError("HTML content could not be parsed") from exc
        for element in main.xpath("//*"):
            if local_tag_name(element) in BLOCKED_TAGS:
                element.drop_tree()
        text = clean_extracted_text(block_aware_text_content(main))
        fallback_text = clean_extracted_text(block_aware_text_content(original))
        if len(text) < 160 and len(fallback_text) > len(text):
            text = fallback_text
        if not text:
            raise WebExtractionError("HTML extraction produced no text")
        title = clean_metadata_value(article.short_title() or first_xpath_text(original, "//title"), 300)
        canonical = first_xpath_attribute(original, "//link[contains(translate(@rel,'CANONICAL','canonical'),'canonical')]", "href")
        author = first_meta_content(original, {"author", "article:author"})
        published = first_meta_content(
            original,
            {"article:published_time", "date", "datepublished", "dc.date", "pubdate"},
        )
        language = clean_metadata_value(original.get("lang") or "", 40)
        metadata = {
            "canonical_url": canonical or final_url,
            "author": author,
            "published_at": published,
            "language": language,
        }
        return ExtractedWebPage(
            title=title or final_url,
            text=text,
            metadata={key: value for key, value in metadata.items() if value},
            visual_signals=visual_candidate_signals(original, text),
            provenance_kind="exact_text",
        )

    def _extract_pdf(self, body: bytes, *, final_url: str) -> ExtractedWebPage:
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise WebExtractionError("PDF extraction requires pypdf") from exc
        try:
            reader = PdfReader(io.BytesIO(body))
            page_text = [str(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:  # noqa: BLE001 - malformed PDFs are untrusted input
            raise WebExtractionError("PDF content could not be parsed") from exc
        text = clean_extracted_text("\n\n".join(page_text))
        if not text:
            raise WebExtractionError("PDF extraction produced no native text")
        raw_metadata = reader.metadata or {}
        title = clean_metadata_value(getattr(raw_metadata, "title", "") or final_url, 300)
        return ExtractedWebPage(
            title=title,
            text=text,
            metadata={"canonical_url": final_url, "page_count": len(page_text)},
            visual_signals=[] if len(text) >= 240 else ["scanned_or_image_document"],
            provenance_kind="native_pdf_text",
        )


def decode_web_text(body: bytes, content_type: str) -> str:
    match = CHARSET_RE.search(str(content_type or ""))
    candidates = [match.group(1)] if match else []
    candidates.extend(["utf-8", "windows-1252"])
    for charset in candidates:
        try:
            return body.decode(charset, errors="strict")
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def clean_extracted_text(text: str) -> str:
    lines: list[str] = []
    blank_pending = False
    for raw_line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = " ".join(raw_line.replace("\x00", " ").split()).strip()
        if not line:
            blank_pending = bool(lines)
            continue
        if blank_pending and lines and lines[-1] != "":
            lines.append("")
        blank_pending = False
        if not lines or line != lines[-1]:
            lines.append(line)
    return "\n".join(lines).strip()


def block_aware_text_content(root: Any) -> str:
    """Serialize parsed HTML text with separators at semantic block boundaries."""
    parts: list[str] = []

    def separator(value: str) -> None:
        if parts and parts[-1] != value:
            parts.append(value)

    def visit(element: Any) -> None:
        tag = local_tag_name(element)
        if tag in BLOCKED_TAGS or tag in NON_TEXT_EVIDENCE_TAGS:
            return
        if tag in BLOCK_TAGS:
            separator("\n")
        if element.text:
            parts.append(str(element.text))
        for child in element:
            visit(child)
            if child.tail:
                parts.append(str(child.tail))
        if tag in CELL_TAGS:
            separator("\t")
        elif tag == "br" or tag in BLOCK_TAGS:
            separator("\n")

    visit(root)
    return "".join(parts)


def local_tag_name(element: Any) -> str:
    tag = str(getattr(element, "tag", "")).lower()
    return tag.rsplit("}", 1)[-1]


def visual_candidate_signals(root: Any, extracted_text: str) -> list[str]:
    signals: list[str] = []
    text_length = len(str(extracted_text or "").strip())
    canvas_count = len(root.xpath("//canvas"))
    svg_count = len(root.xpath("//*[local-name()='svg']"))
    math_count = len(root.xpath("//*[local-name()='math']"))
    image_count = len(root.xpath("//img"))
    script_count = len(root.xpath("//script"))
    chart_markers = len(
        root.xpath(
            "//*[contains(translate(@class,'CHARTGRAPH','chartgraph'),'chart') or "
            "contains(translate(@class,'CHARTGRAPH','chartgraph'),'graph')]"
        )
    )
    if text_length == 0:
        signals.append("extraction_empty")
    elif text_length < 240:
        signals.append("very_low_text_yield")
    if script_count >= 5 and text_length < 500:
        signals.append("js_shell_suspected")
    if canvas_count:
        signals.append("canvas_present")
    if svg_count or chart_markers:
        signals.append("svg_or_chart_present")
    if math_count:
        signals.append("mathml_present")
    if image_count >= 6 and text_length < image_count * 120:
        signals.append("image_dominant")
    return signals


def first_xpath_text(root: Any, xpath: str) -> str:
    rows = root.xpath(xpath)
    if not rows:
        return ""
    row = rows[0]
    return clean_metadata_value(row.text_content() if hasattr(row, "text_content") else str(row), 300)


def first_xpath_attribute(root: Any, xpath: str, attribute: str) -> str:
    rows = root.xpath(xpath)
    if not rows:
        return ""
    return clean_metadata_value(rows[0].get(attribute) or "", 2000)


def first_meta_content(root: Any, names: set[str]) -> str:
    lowered = {name.lower() for name in names}
    for row in root.xpath("//meta[@content]"):
        key = str(row.get("name") or row.get("property") or row.get("itemprop") or "").lower()
        if key in lowered:
            return clean_metadata_value(row.get("content") or "", 500)
    return ""


def clean_metadata_value(value: Any, max_chars: int) -> str:
    return " ".join(str(value or "").replace("\x00", " ").split()).strip()[:max_chars]
