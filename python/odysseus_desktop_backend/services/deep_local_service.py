from __future__ import annotations

import os
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services import colibri_cli
from odysseus_desktop_backend.services.providers.base import ModelServiceError
from odysseus_desktop_backend.services.providers.colibri import (
    COLIBRI_API_KEY_ENV,
    DEFAULT_COLIBRI_ENDPOINT,
    DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS,
    ColibriProvider,
)
from odysseus_desktop_backend.services.settings_service import SettingsService

logger = get_logger("deep_local")

DISABLED_MESSAGE = (
    "Deep Local (experimental) is not enabled. It is optional, text-only, and "
    "requires a Colibri server that you run yourself."
)
# Bounded spike default: complete_once is a developer proof, not a job system.
DEFAULT_ONCE_MAX_OUTPUT_TOKENS = 128


class DeepLocalService:
    """Flag-gated facade over the Colibri provider for the research spike.

    Every method returns a structured dict (never a raw provider exception)
    so nontechnical users are never shown upstream jargon. There is no UI
    for this surface; it exists to prove the vertical slice end to end.
    """

    def __init__(self, settings: SettingsService):
        self.settings = settings

    # -- configuration ---------------------------------------------------

    def _config(self) -> dict[str, Any]:
        values = self.settings.get()
        timeout = values.get("deep_local_timeout_seconds")
        try:
            timeout_seconds = float(timeout) if timeout is not None else DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS
        except (TypeError, ValueError):
            timeout_seconds = DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS
        if timeout_seconds <= 0:
            timeout_seconds = DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS
        return {
            "enabled": values.get("deep_local_enabled") is True,
            "endpoint": str(values.get("deep_local_endpoint") or DEFAULT_COLIBRI_ENDPOINT),
            "cli_path": str(values.get("deep_local_cli_path") or ""),
            "model_path": str(values.get("deep_local_model_path") or ""),
            "timeout_seconds": timeout_seconds,
        }

    def _provider_or_error(self, config: dict[str, Any]) -> tuple[ColibriProvider | None, dict[str, Any] | None]:
        if not config["enabled"]:
            return None, self._error("disabled", DISABLED_MESSAGE)
        try:
            provider = ColibriProvider(
                config["endpoint"],
                api_key=os.environ.get(COLIBRI_API_KEY_ENV) or None,
                timeout=config["timeout_seconds"],
            )
        except ValueError:
            return None, self._error(
                "disabled",
                "Deep Local endpoints must stay on this computer (127.0.0.1). "
                "Remote endpoints are not supported.",
            )
        return provider, None

    @staticmethod
    def _error(category: str, message: str, **extra: Any) -> dict[str, Any]:
        return {"ok": False, "error_category": category, "error": message, **extra}

    # -- RPC surface -----------------------------------------------------

    def status(self) -> dict[str, Any]:
        config = self._config()
        base = {
            "ok": True,
            "enabled": config["enabled"],
            "endpoint": config["endpoint"],
            "reachable": False,
            "healthy": False,
            "queue": {},
            "models": [],
            "error": "",
            "error_category": "",
        }
        provider, error = self._provider_or_error(config)
        if provider is None:
            base.update(ok=False, error=error["error"], error_category=error["error_category"])
            return base
        health = provider.health()
        base["reachable"] = health.reachable
        base["healthy"] = health.healthy
        base["queue"] = health.detail.get("queue", {})
        if not health.healthy:
            base.update(ok=False, error=health.error, error_category=health.error_category)
            return base
        try:
            base["models"] = [model.model_id for model in provider.list_models()]
        except ModelServiceError as exc:
            base.update(ok=False, error=str(exc), error_category=exc.category)
        logger.info(
            "deep_local status reachable=%s healthy=%s models=%s",
            base["reachable"],
            base["healthy"],
            len(base["models"]),
        )
        return base

    def plan(self) -> dict[str, Any]:
        """Wrap `coli plan --json`: bounded read-only readiness planning."""
        config = self._config()
        if not config["enabled"]:
            return self._error("disabled", DISABLED_MESSAGE)
        return colibri_cli.run_plan(config["cli_path"], config["model_path"])

    def doctor(self) -> dict[str, Any]:
        """Wrap `coli doctor --json`: read-only installation diagnostics."""
        config = self._config()
        if not config["enabled"]:
            return self._error("disabled", DISABLED_MESSAGE)
        return colibri_cli.run_doctor(config["cli_path"], config["model_path"])

    def complete_once(
        self,
        *,
        prompt: str,
        model: str = "",
        max_output_tokens: int = DEFAULT_ONCE_MAX_OUTPUT_TOKENS,
        temperature: float = 0.0,
        top_p: float | None = None,
        thinking: str = "off",
    ) -> dict[str, Any]:
        """Developer-only vertical proof: one bounded text-only completion.

        Synchronous: blocks the sidecar RPC loop for its full duration and
        supports no mid-flight cancellation (an abandoned call must be
        described as interrupted, never cancelled). The real product shape
        is the persisted Deep Local job system (RFC section 6).
        """
        clean_prompt = (prompt or "").strip()
        if not clean_prompt:
            raise ValueError("prompt is required")
        config = self._config()
        provider, error = self._provider_or_error(config)
        if provider is None:
            return error
        selected_model = (model or "").strip()
        if not selected_model:
            try:
                models = provider.list_models()
            except ModelServiceError as exc:
                return self._error(exc.category, str(exc))
            if len(models) != 1:
                return self._error(
                    "invalid_model",
                    "Pass a model id: the Colibri server offered "
                    f"{len(models)} models.",
                    models=[item.model_id for item in models],
                )
            selected_model = models[0].model_id
        try:
            result = provider.chat_once(
                selected_model,
                [{"role": "user", "content": clean_prompt}],
                temperature=temperature,
                top_p=top_p,
                max_output_tokens=max_output_tokens,
                thinking=thinking,
            )
        except ModelServiceError as exc:
            extra: dict[str, Any] = {}
            retry_after = getattr(exc, "retry_after_seconds", None)
            if retry_after is not None:
                extra["retry_after_seconds"] = retry_after
            return self._error(exc.category, str(exc), **extra)
        logger.info(
            "deep_local complete_once model=%s completion_tokens=%s elapsed_ms=%s queue_wait_ms=%s",
            result.model_id,
            result.completion_tokens,
            result.elapsed_ms,
            result.queue_wait_ms,
        )
        return {"ok": True, "result": result.to_dict()}
