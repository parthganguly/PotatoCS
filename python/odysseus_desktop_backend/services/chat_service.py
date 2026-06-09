from __future__ import annotations

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
        if use_rag and self.rag is not None:
            rag_context, retrieved_chunks = self.rag.build_context(
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
                    "content": (
                        "You are answering with retrieved document chunks.\n"
                        "Rules:\n"
                        "- Use only the retrieved chunks below for factual claims.\n"
                        "- Keep different source titles, files, pages, and chunks separate. "
                        "Never merge facts across unrelated documents.\n"
                        "- Preserve chronology exactly. If the text says one event happened, "
                        "then a story was told for 60-70 years, do not say the event lasted "
                        "60-70 years.\n"
                        "- Distinguish direct claims from inference. If you infer something, "
                        "say it is suggested by the text.\n"
                        "- Do not add unstated causes, motives, emotions, biographies, or "
                        "causal links.\n"
                        "- If the chunks do not answer something, say "
                        "\"the retrieved context does not say.\"\n"
                        "- Cite or name the source title/page when useful.\n\n"
                        f"Retrieved document context:\n{rag_context}"
                    ),
                },
            )

        reply = self.models.chat(selected_model, ollama_messages)
        assistant_message = self.sessions.add_message(session["id"], "assistant", reply)

        if session["title"] == "New chat":
            self.sessions.update(session["id"], {"title": self._derive_title(content)})

        return {
            "session": self.sessions.get(session["id"]),
            "user_message": user_message,
            "assistant_message": assistant_message,
            "messages": self.sessions.messages(session["id"]),
            "retrieved_chunks": retrieved_chunks,
        }

    def _derive_title(self, content: str) -> str:
        title = " ".join(content.split())
        if len(title) > 48:
            title = title[:45].rstrip() + "..."
        return title or "New chat"
