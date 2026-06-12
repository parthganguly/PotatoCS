from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from typing import Any

from odysseus_desktop_backend.services.model_service import ModelService
from odysseus_desktop_backend.services.session_service import SessionService
from odysseus_desktop_backend.services.settings_service import SettingsService

if TYPE_CHECKING:
    from odysseus_desktop_backend.services.rag_service import RAGService


ANSWER_STYLES = {
    "precise": "Precise",
    "layman": "Layman",
    "detailed": "Detailed",
    "extract_only": "Extract only",
    "evidence_only": "Evidence only",
}
DEFAULT_ANSWER_STYLE = "precise"
DEFAULT_TEMPERATURE = 0.0
RAG_PRESETS = {"standard", "potato"}
DEFAULT_RAG_PRESET = "standard"
POTATO_RETRIEVAL_LIMIT = 2


class ChatService:
    """Chat service.

    Default chat remains the simple Milestone 1 path. Milestone 2 RAG is opt-in
    per request and only injects retrieved document chunks.
    """

    def __init__(
        self,
        sessions: SessionService,
        settings: SettingsService,
        models: ModelService,
        rag: RAGService | None = None,
    ):
        self.sessions = sessions
        self.settings = settings
        self.models = models
        self.rag = rag

    def send(
        self,
        message: str,
        session_id: str | None = None,
        model: str | None = None,
        use_rag: bool = False,
        document_ids: list[str] | None = None,
        verify_rag: bool = False,
        answer_style: str | None = None,
        temperature: float = DEFAULT_TEMPERATURE,
        rag_preset: str | None = None,
    ) -> dict[str, Any]:
        content = (message or "").strip()
        if not content:
            raise ValueError("message is required")
        selected_rag_preset = normalize_rag_preset(rag_preset) if use_rag else DEFAULT_RAG_PRESET
        potato_mode = use_rag and selected_rag_preset == "potato"
        selected_answer_style = (
            "evidence_only" if potato_mode else normalize_answer_style(answer_style)
            if use_rag
            else DEFAULT_ANSWER_STYLE
        )
        if potato_mode:
            verify_rag = False
            temperature = DEFAULT_TEMPERATURE

        current_settings = self.settings.get()
        selected_model = (model or current_settings.get("default_model") or "llama3.2").strip()
        session = self.sessions.ensure(session_id, model=selected_model)

        if not session.get("model"):
            session = self.sessions.update(session["id"], {"model": selected_model})

        user_message = self.sessions.add_message(session["id"], "user", content)
        history = self.sessions.messages(session["id"])
        rag_context = ""
        retrieved_chunks: list[dict[str, Any]] = []
        retrieved_snippets: list[dict[str, Any]] = []
        if use_rag and self.rag is not None:
            rag_context, retrieved_chunks, retrieved_snippets = self.rag.build_quote_context(
                content,
                document_ids=document_ids,
                limit=POTATO_RETRIEVAL_LIMIT if potato_mode else 4,
            )

        ollama_messages = [
            {"role": item["role"], "content": item["content"]}
            for item in history
            if item["role"] in {"system", "user", "assistant"}
        ]
        if rag_context:
            ollama_messages.insert(
                0,
                {
                    "role": "system",
                    "content": self._rag_system_prompt(
                        rag_context,
                        content,
                        selected_answer_style,
                        selected_rag_preset,
                    ),
                },
            )
            self._augment_latest_user_message_for_rag(
                ollama_messages,
                content,
                selected_answer_style,
                selected_rag_preset,
            )

        generation_options = {"temperature": float(temperature)}
        reply = self._chat_model(selected_model, ollama_messages, generation_options)
        grounding = self._grounding_report(
            retrieved_snippets,
            enabled=verify_rag,
            status="not_run" if rag_context else "no_evidence",
        )
        if rag_context and verify_rag and retrieved_snippets:
            reply, grounding = self._verify_and_correct(
                model=selected_model,
                messages=ollama_messages,
                draft_answer=reply,
                snippets=retrieved_snippets,
                answer_style=selected_answer_style,
                options=generation_options,
            )
        assistant_message = self.sessions.add_message(session["id"], "assistant", reply)

        if session["title"] == "New chat":
            self.sessions.update(session["id"], {"title": self._derive_title(content)})

        return {
            "session": self.sessions.get(session["id"]),
            "user_message": user_message,
            "assistant_message": assistant_message,
            "messages": self.sessions.messages(session["id"]),
            "retrieved_chunks": retrieved_chunks,
            "retrieved_snippets": retrieved_snippets,
            "grounding": grounding,
            "answer_style": selected_answer_style,
            "temperature": float(temperature),
            "rag_preset": selected_rag_preset,
        }

    def _chat_model(
        self,
        model: str,
        messages: list[dict[str, str]],
        options: dict[str, Any],
    ) -> str:
        try:
            return self.models.chat(model, messages, options=options)
        except TypeError:
            return self.models.chat(model, messages)

    def _derive_title(self, content: str) -> str:
        title = " ".join(content.split())
        if len(title) > 48:
            title = title[:45].rstrip() + "..."
        return title or "New chat"

    def _rag_system_prompt(
        self,
        rag_context: str,
        question: str,
        answer_style: str,
        rag_preset: str,
    ) -> str:
        return (
            "You are answering with short retrieved evidence snippets.\n"
            f"Latest user question: {question}\n\n"
            f"Answer style: {ANSWER_STYLES[answer_style]}.\n"
            f"{self._answer_style_instructions(answer_style)}\n\n"
            f"{self._rag_preset_instructions(rag_preset)}\n\n"
            "First answer with directly supported facts from the quoted evidence. "
            "If any quoted bullet gives an event or fact relevant to the question, "
            "use it before discussing missing details.\n\n"
            "Rules:\n"
            "- Use only the evidence snippets below for factual claims.\n"
            "- Keep different source titles, files, pages, and snippets separate. "
            "Never merge facts across unrelated documents.\n"
            "- Preserve chronology exactly. Do not move a duration, cause, action, "
            "or outcome from one event or person to another.\n"
            "- Avoid changing who did what, when, where, or for how long.\n"
            "- For broad questions like \"tell me about\", first summarize the "
            "directly supported events or facts in chronological order. Mention "
            "missing identity/background details only after those facts.\n"
            "- Do not lead with \"the retrieved context does not say\" when the "
            "snippets contain direct events or facts relevant to the requested "
            "source, story, or subject.\n"
            "- Distinguish direct claims from inference. If you infer something, "
            "say it is suggested by the text.\n"
            "- If a source heading or title names the user's subject, use the "
            "following same-snippet story as relevant to that subject. Do not say "
            "there is no information merely because later sentences use a role "
            "or description instead of repeating the subject name.\n"
            "- Do not add unstated causes, motives, emotions, biographies, or causal links.\n"
            "- Never treat a document's existence as proof of a real-world crisis, "
            "diagnosis, cause, motive, event, or conclusion unless the snippets "
            "directly state it.\n"
            "- Answer with this shape when there is relevant evidence: "
            "\"The retrieved context says ...\" followed by supported facts. "
            "Then, only if useful, add \"The retrieved context does not say ...\" "
            "for missing details.\n"
            "- Use only \"the retrieved context does not say\" as the whole answer "
            "when no retrieved snippet contains relevant facts.\n"
            "- Cite or name the source title/page when useful.\n\n"
            f"Retrieved evidence snippets:\n{rag_context}"
        )

    def _augment_latest_user_message_for_rag(
        self,
        messages: list[dict[str, str]],
        content: str,
        answer_style: str,
        rag_preset: str,
    ) -> None:
        for message in reversed(messages):
            if message["role"] == "user":
                preset_note = (
                    "Potato Mode is active: keep the answer short, quote-first, "
                    "and say what cannot be confirmed."
                    if rag_preset == "potato"
                    else "Use the retrieved evidence snippets above"
                )
                message["content"] = (
                    f"{content}\n\n"
                    f"{preset_note} in {ANSWER_STYLES[answer_style]} style. "
                    "Start with the directly supported facts or events in chronological order. "
                    "Do not answer only with missing background details when quoted evidence "
                    "gives relevant facts."
                )
                return

    def _answer_style_instructions(self, answer_style: str) -> str:
        if answer_style == "layman":
            return (
                "Layman style:\n"
                "- Explain what the retrieved document or context is about in plain English.\n"
                "- Translate procedural, bureaucratic, legal, technical, medical, or "
                "institutional language into practical meaning.\n"
                "- Clearly separate what the snippets directly state, what that practically "
                "means, and what the snippets do not prove.\n"
                "- Do not invent a concrete crisis, diagnosis, cause, motive, event, or "
                "conclusion when the snippets only contain forms, procedures, notices, or "
                "instructions.\n"
                "- Do not put internal chunk IDs in the final answer prose."
            )
        if answer_style == "detailed":
            return (
                "Detailed style:\n"
                "- Give a fuller answer while staying grounded in retrieved evidence.\n"
                "- Organize the answer clearly.\n"
                "- Include caveats and what is not established.\n"
                "- Do not merge unrelated documents or sources."
            )
        if answer_style == "extract_only":
            return (
                "Extract only style:\n"
                "- Return only facts directly stated in retrieved snippets.\n"
                "- Do not interpret, speculate, infer practical meaning, or explain what "
                "something probably means.\n"
                "- If the requested answer is not directly present, say that the retrieved "
                "context does not say."
            )
        if answer_style == "evidence_only":
            return (
                "Evidence only style:\n"
                "- Use exactly these section headings in this order:\n"
                "  Answer:\n"
                "  Evidence:\n"
                "  Not found / cannot confirm:\n"
                "- In Answer, give only one or two short directly supported claims.\n"
                "- In Evidence, list the short quoted evidence snippets or source/page labels "
                "that support the answer.\n"
                "- In Not found / cannot confirm, list any requested details that are absent. "
                "If nothing is missing, write \"None\".\n"
                "- Do not synthesize, speculate, infer motives, or fill gaps."
            )
        return (
            "Precise style:\n"
            "- Stay close to the retrieved evidence.\n"
            "- Preserve chronology.\n"
            "- Do not add broad interpretation unless the user asks.\n"
            "- Separate directly stated facts from uncertainty.\n"
            "- Say the retrieved context does not say when evidence is missing."
        )

    def _rag_preset_instructions(self, rag_preset: str) -> str:
        if rag_preset == "potato":
            return (
                "Potato Mode preset:\n"
                "- This mode is for weak local models and limited hardware.\n"
                "- Use quote-first evidence only.\n"
                "- Keep the answer short.\n"
                "- Be strict: if the snippets do not directly answer a detail, put it under "
                "\"Not found / cannot confirm\".\n"
                "- Do not speculate or produce broad synthesis.\n"
                "- Prefer saying what is missing over guessing."
            )
        return "Standard RAG preset."

    def _verify_and_correct(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        draft_answer: str,
        snippets: list[dict[str, Any]],
        answer_style: str,
        options: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        first_report = self._verify_answer(model, draft_answer, snippets)
        contradicted = first_report.get("contradicted_claims") or []
        if not contradicted:
            return draft_answer, first_report

        correction_messages = messages + [
            {"role": "assistant", "content": draft_answer},
            {
                "role": "user",
                "content": (
                    "Revise the answer once. Remove or correct every contradicted claim below. "
                    f"Use only the retrieved evidence snippets, preserve chronology, and keep "
                    f"{ANSWER_STYLES[answer_style]} style.\n\n"
                    f"Contradicted claims:\n{json.dumps(contradicted, ensure_ascii=False)}"
                ),
            },
        ]
        corrected = self._chat_model(model, correction_messages, options)
        final_report = self._verify_answer(model, corrected, snippets)
        final_report["regenerated"] = True
        final_report["draft_verifier"] = first_report
        return corrected, final_report

    def _verify_answer(
        self,
        model: str,
        answer: str,
        snippets: list[dict[str, Any]],
    ) -> dict[str, Any]:
        prompt = (
            "Use this as a lightweight warning pass, not as a perfect judge. "
            "Check the answer only against the retrieved evidence snippets. "
            "Extract factual claims from the answer. Mark each claim as supported, "
            "unsupported, or contradicted. A claim is supported only if the snippets say it. "
            "Return strict JSON only with this shape: "
            "{\"claims\":[{\"text\":\"...\",\"status\":\"supported|unsupported|contradicted\","
            "\"reason\":\"...\",\"source_ids\":[\"S1\"]}],"
            "\"unsupported_claims\":[\"...\"],\"contradicted_claims\":[\"...\"]}."
        )
        payload = (
            f"Retrieved evidence snippets:\n{format_snippets_for_verifier(snippets)}\n\n"
            f"Answer:\n{answer}"
        )
        try:
            raw = self._chat_model(
                model,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": payload},
                ],
                {"temperature": DEFAULT_TEMPERATURE},
            )
            data = parse_json_object(raw)
            return self._normalize_verification_report(snippets, data)
        except Exception as exc:  # noqa: BLE001 - verifier is optional and must not break chat
            return self._grounding_report(
                snippets,
                enabled=True,
                status="error",
                error=str(exc),
            )

    def _normalize_verification_report(
        self,
        snippets: list[dict[str, Any]],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        claims = data.get("claims") if isinstance(data.get("claims"), list) else []
        normalized_claims: list[dict[str, Any]] = []
        unsupported_claims: list[str] = []
        contradicted_claims: list[str] = []

        for claim in claims:
            if not isinstance(claim, dict):
                continue
            text = str(claim.get("text") or "").strip()
            status = str(claim.get("status") or "unsupported").strip().lower()
            if status not in {"supported", "unsupported", "contradicted"}:
                status = "unsupported"
            if not text:
                continue
            normalized = {
                "text": text,
                "status": status,
                "reason": str(claim.get("reason") or "").strip(),
                "source_ids": [
                    str(item)
                    for item in claim.get("source_ids", [])
                    if isinstance(item, str)
                ],
            }
            normalized_claims.append(normalized)
            if status == "unsupported":
                unsupported_claims.append(text)
            elif status == "contradicted":
                contradicted_claims.append(text)

        explicit_unsupported = data.get("unsupported_claims")
        if isinstance(explicit_unsupported, list):
            unsupported_claims.extend(str(item) for item in explicit_unsupported if item)
        explicit_contradicted = data.get("contradicted_claims")
        if isinstance(explicit_contradicted, list):
            contradicted_claims.extend(str(item) for item in explicit_contradicted if item)

        unsupported_claims = dedupe_strings(unsupported_claims)
        contradicted_claims = dedupe_strings(contradicted_claims)
        status = "passed" if not unsupported_claims and not contradicted_claims else "failed"
        return self._grounding_report(
            snippets,
            enabled=True,
            status=status,
            claims=normalized_claims,
            unsupported_claims=unsupported_claims,
            contradicted_claims=contradicted_claims,
        )

    def _grounding_report(
        self,
        snippets: list[dict[str, Any]],
        *,
        enabled: bool,
        status: str,
        claims: list[dict[str, Any]] | None = None,
        unsupported_claims: list[str] | None = None,
        contradicted_claims: list[str] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        return {
            "mode": "quote_first" if snippets else "none",
            "sources_used": [
                {
                    "snippet_id": snippet.get("snippet_id"),
                    "document_id": snippet.get("document_id"),
                    "source": snippet.get("source"),
                    "page_start": snippet.get("page_start"),
                    "page_end": snippet.get("page_end"),
                    "chunk_id": snippet.get("chunk_id"),
                }
                for snippet in snippets
            ],
            "verifier": {
                "enabled": enabled,
                "status": status,
                "passed": status == "passed" if enabled else None,
                "error": error,
            },
            "claims": claims or [],
            "unsupported_claims": unsupported_claims or [],
            "contradicted_claims": contradicted_claims or [],
            "regenerated": False,
        }


def format_snippets_for_verifier(snippets: list[dict[str, Any]]) -> str:
    lines = []
    for snippet in snippets:
        page = snippet.get("page_start")
        location = f", page {page}" if page else ""
        lines.append(
            f"[{snippet.get('snippet_id')}] {snippet.get('source')}{location}: "
            f"\"{snippet.get('text')}\""
        )
    return "\n".join(lines)


def parse_json_object(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end <= start:
            raise
        data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("verifier response must be a JSON object")
    return data


def dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        key = " ".join(str(value).split()).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(str(value))
    return deduped


def normalize_answer_style(value: str | None) -> str:
    if value is None:
        return DEFAULT_ANSWER_STYLE
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "extract":
        normalized = "extract_only"
    if normalized == "evidence":
        normalized = "evidence_only"
    if normalized not in ANSWER_STYLES:
        allowed = ", ".join(sorted(ANSWER_STYLES))
        raise ValueError(f"answer_style must be one of: {allowed}")
    return normalized


def normalize_rag_preset(value: str | None) -> str:
    if value is None:
        return DEFAULT_RAG_PRESET
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in {"potato_mode", "potato"}:
        return "potato"
    if normalized not in RAG_PRESETS:
        allowed = ", ".join(sorted(RAG_PRESETS))
        raise ValueError(f"rag_preset must be one of: {allowed}")
    return normalized
