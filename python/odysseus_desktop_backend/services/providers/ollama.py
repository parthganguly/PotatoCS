from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Any

from odysseus_desktop_backend.services.providers.base import (
    ModelConnectionError,
    ModelEmptyResponseError,
    ModelInvalidModelError,
    ModelMalformedResponseError,
    ModelServiceError,
    ModelTimeoutError,
)


class OllamaProvider:
    """HTTP transport for the local Ollama server.

    Behavior (including error messages and exception categories) is moved
    verbatim from `ModelService`; the facade delegates here so its public
    results are byte-identical to the pre-seam implementation.
    """

    name = "ollama"

    def __init__(self, endpoint: str):
        self.endpoint = endpoint

    def tcp_reachable(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def get_json(self, url: str, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def post_json(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except TimeoutError as exc:
            raise ModelTimeoutError(f"timeout: Ollama request exceeded {timeout:g}s") from exc
        except socket.timeout as exc:
            raise ModelTimeoutError(f"timeout: Ollama request exceeded {timeout:g}s") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in {400, 404} and "model" in detail.lower():
                raise ModelInvalidModelError(f"invalid model: {detail or exc.reason}") from exc
            raise ModelServiceError(f"ollama http error {exc.code}: {detail or exc.reason}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ModelTimeoutError(f"timeout: Ollama request exceeded {timeout:g}s") from exc
            raise ModelConnectionError(f"connection failure: Ollama is not reachable at {self.endpoint}: {reason}") from exc
        if not raw.strip():
            raise ModelEmptyResponseError("empty response: Ollama returned an empty body")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelMalformedResponseError(f"malformed response: {exc}") from exc
