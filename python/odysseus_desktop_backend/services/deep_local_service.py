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


class DeepLocalService:
    """Flag-gated read-only facade over the Colibri provider.

    Serves status/plan/doctor only. Every method returns a structured dict
    (never a raw provider exception) so nontechnical users are never shown
    upstream jargon. Generation happens exclusively through the persisted
    Deep Local job system (DeepLocalJobService) — there is deliberately no
    synchronous completion surface, which would block the single-threaded
    sidecar RPC loop for the duration of a slow generation.
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

