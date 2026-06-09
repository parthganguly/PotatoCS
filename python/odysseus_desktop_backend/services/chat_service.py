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
    ) -> dict[str, Any]:
        content = (message or "").strip()
        if not content:
            raise ValueError("message is required")

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
                    "content": self._rag_system_prompt(rag_context, content),
                },
            )
            self._augment_latest_user_message_for_rag(ollama_messages, content)

        reply = self.models.chat(selected_model, ollama_messages)
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
        }

    def _derive_title(self, content: str) -> str:
        title = " ".join(content.split())
        if len(title) > 48:
            title = title[:45].rstrip() + "..."
        return title or "New chat"

    def _rag_system_prompt(self, rag_context: str, question: str) -> str:
        return (
            "You are answering with short retrieved evidence snippets.\n"
            f"Latest user question: {question}\n\n"
            "First answer with directly supported facts from the quoted evidence. "
            "If any quoted bullet gives an event or fact relevant to the question, "
            "use it before discussing missing details.\n\n"
            "Rules:\n"
            "- Use only the evidence snippets below for factual claims.\n"
            "- Keep different source titles, files, pages, and snippets separate. "
            "Never merge facts across unrelated documents.\n"
            "- Preserve chronology exactly. If the text says one event happened, "
            "then a story was told for 60-70 years, do not say the event lasted "
            "60-70 years.\n"
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
            "like soldier instead of repeating the subject name.\n"
            "- Do not add unstated causes, motives, emotions, biographies, or causal links.\n"
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
    ) -> None:
        for message in reversed(messages):
            if message["role"] == "user":
                message["content"] = (
                    f"{content}\n\n"
                    "Use the retrieved evidence snippets above. Start with the directly "
                    "supported facts or events in chronological order. Do not answer only "
                    "with missing background details when quoted evidence gives relevant facts."
                )
                return

    def _verify_and_correct(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        draft_answer: str,
        snippets: list[dict[str, Any]],
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
                    "Use only the retrieved evidence snippets and preserve chronology.\n\n"
                    f"Contradicted claims:\n{json.dumps(contradicted, ensure_ascii=False)}"
                ),
            },
        ]
        corrected = self.models.chat(model, correction_messages)
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
            raw = self.models.chat(
                model,
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": payload},
                ],
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
