from __future__ import annotations

import http.client
import io
import ipaddress
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.services.providers.base import (
    ModelAuthError,
    ModelConnectionError,
    ModelEmptyResponseError,
    ModelIncompatibleServerError,
    ModelInterruptedError,
    ModelInvalidModelError,
    ModelMalformedResponseError,
    ModelQueueSaturatedError,
    ModelQueueTimeoutError,
    ModelServerError,
    ModelServiceError,
    ModelTimeoutError,
    ModelUnsupportedFeatureError,
    ProviderChatResult,
    ProviderModel,
    ProviderStatus,
)

DEFAULT_COLIBRI_ENDPOINT = "http://127.0.0.1:8000"
COLIBRI_API_KEY_ENV = "ODYSSEUS_COLIBRI_API_KEY"
DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS = 3600.0
HEALTH_TIMEOUT_SECONDS = 3.0
MODELS_TIMEOUT_SECONDS = 5.0
# The server is a local privileged service, not a trusted API: bound what we
# are willing to read from it.
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
THINK_CLOSE = "</think>"

logger = get_logger("colibri")

_LOOPBACK_HOSTS = {"localhost"}


def redact_secret(text: str, secret: str | None) -> str:
    """Remove an API key from any string leaving the provider."""
    if not secret:
        return text
    return text.replace(secret, "***")


class RequestCancelHandle:
    """Lets another thread abandon one in-flight HTTP request.

    cancel() closes the underlying connection, which unblocks the thread
    waiting on the response. Closing the socket only stops *us* from
    waiting: the Colibri server notices the disconnect asynchronously (only
    when its next engine data event flows) and may keep generating for a
    long time. Callers must therefore report the outcome as "stopped
    waiting"/interrupted, never as a confirmed engine cancellation.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None
        self._cancelled = False

    def attach(self, conn: http.client.HTTPConnection) -> None:
        with self._lock:
            self._conn = conn
            if self._cancelled:
                self._close_locked()

    def detach(self) -> None:
        with self._lock:
            self._conn = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            self._close_locked()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def _close_locked(self) -> None:
        if self._conn is None:
            return
        # A blocked recv() on Windows is only released by closesocket(): both
        # shutdown() and socket.close() leave it waiting (close() cannot drop
        # the OS handle while the response reader holds a makefile() io-ref).
        # detach() + socket.close(fd) closes the handle immediately and the
        # waiting thread fails out with an OSError.
        sock = getattr(self._conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                socket.close(sock.detach())
            except OSError:
                pass
        try:
            self._conn.close()
        except OSError:
            pass


def is_loopback_endpoint(endpoint: str) -> bool:
    try:
        parts = urlsplit(endpoint)
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return False
    host = parts.hostname
    if host in _LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ColibriProvider:
    """Text-only adapter for a user-managed Colibri `coli serve` endpoint.

    Deliberately not wired into ChatService: Deep Local generation can take
    minutes to hours and must never enter the interactive chat path. See
    projects/odysseus/COLIBRI_PROVIDER_RFC.md.
    """

    name = "colibri"

    def __init__(
        self,
        endpoint: str = DEFAULT_COLIBRI_ENDPOINT,
        *,
        api_key: str | None = None,
        timeout: float = DEFAULT_DEEP_LOCAL_TIMEOUT_SECONDS,
    ):
        clean_endpoint = (endpoint or "").strip().rstrip("/")
        if not is_loopback_endpoint(clean_endpoint):
            raise ValueError(
                "Deep Local endpoints must stay on this computer (127.0.0.1); "
                f"refusing non-loopback endpoint {clean_endpoint!r}"
            )
        self.endpoint = clean_endpoint
        self._api_key = api_key or None
        self.timeout = float(timeout)

    # -- detection -------------------------------------------------------

    def health(self) -> ProviderStatus:
        """GET /health (unauthenticated upstream)."""
        try:
            data = self._request("GET", "/health", timeout=HEALTH_TIMEOUT_SECONDS, auth=False)
        except ModelServiceError as exc:
            return ProviderStatus(
                provider=self.name,
                endpoint=self.endpoint,
                reachable=not isinstance(exc, ModelConnectionError),
                healthy=False,
                error=str(exc),
                error_category=exc.category,
            )
        healthy = isinstance(data, dict) and data.get("status") == "ok"
        detail: dict[str, Any] = {}
        if isinstance(data, dict):
            scheduler = data.get("scheduler")
            if isinstance(scheduler, dict):
                detail["queue"] = {
                    "active": int(scheduler.get("active") or 0),
                    "queued": int(scheduler.get("queued") or 0),
                    "capacity": int(scheduler.get("capacity") or 0),
                    "max_queue": int(scheduler.get("max_queue") or 0),
                }
            if data.get("kv_slots") is not None:
                detail["kv_slots"] = int(data.get("kv_slots") or 0)
        return ProviderStatus(
            provider=self.name,
            endpoint=self.endpoint,
            reachable=True,
            healthy=healthy,
            error="" if healthy else "The Colibri server did not report a healthy status.",
            error_category="" if healthy else "server_error",
            detail=detail,
        )

    def list_models(self) -> list[ProviderModel]:
        data = self._request("GET", "/v1/models", timeout=MODELS_TIMEOUT_SECONDS)
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise ModelMalformedResponseError(
                "malformed response: the Colibri server returned an unexpected /v1/models shape"
            )
        models: list[ProviderModel] = []
        for item in items:
            if isinstance(item, dict) and item.get("id"):
                models.append(
                    ProviderModel(
                        provider=self.name,
                        model_id=str(item["id"]),
                        detail={"owned_by": str(item.get("owned_by") or "")},
                    )
                )
        return models

    # -- generation ------------------------------------------------------

    def chat_once(
        self,
        model_id: str,
        messages: list[dict[str, Any]],
        *,
        temperature: float | None = None,
        top_p: float | None = None,
        max_output_tokens: int | None = None,
        thinking: str = "off",
        timeout: float | None = None,
        cancel_handle: RequestCancelHandle | None = None,
    ) -> ProviderChatResult:
        """One non-streaming, text-only completion.

        With a `cancel_handle`, another thread may abandon the request
        mid-flight (raises ModelInterruptedError here). Abandoning is not a
        cancellation guarantee: callers must report the job as interrupted
        ("stopped waiting"), never as `cancelled`.
        """
        clean_model = (model_id or "").strip()
        if not clean_model:
            raise ValueError("model_id is required")
        payload_messages = self._validate_text_only(messages)
        payload: dict[str, Any] = {
            "model": clean_model,
            "messages": payload_messages,
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if top_p is not None:
            payload["top_p"] = float(top_p)
        if max_output_tokens is not None:
            if max_output_tokens < 1:
                raise ValueError("max_output_tokens must be positive")
            payload["max_tokens"] = int(max_output_tokens)
        normalized_thinking = (thinking or "off").strip().lower()
        if normalized_thinking not in {"auto", "off", "on"}:
            raise ValueError("thinking mode must be one of: auto, off, on")
        # "auto" deliberately maps to off: reasoning multiplies generation
        # time on a streamed-expert backend, so it must be an explicit choice.
        if normalized_thinking == "on":
            payload["enable_thinking"] = True

        started = time.perf_counter()
        data, headers = self._request_with_headers(
            "POST",
            "/v1/chat/completions",
            payload=payload,
            timeout=self.timeout if timeout is None else float(timeout),
            cancel_handle=cancel_handle,
        )
        elapsed_ms = int((time.perf_counter() - started) * 1000)

        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ModelMalformedResponseError(
                "malformed response: the Colibri server returned no choices"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), (str, type(None))):
            raise ModelMalformedResponseError(
                "malformed response: the Colibri server returned no assistant message"
            )
        content = message.get("content") or ""
        thinking_text = ""
        if normalized_thinking == "on" and THINK_CLOSE in content:
            thinking_text, content = content.split(THINK_CLOSE, 1)
            thinking_text = thinking_text.strip()
            content = content.strip()
        if not content and not thinking_text:
            raise ModelEmptyResponseError(
                "empty response: the Colibri server returned no assistant content"
            )
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        finish_reason = str(choices[0].get("finish_reason") or "")
        completion_tokens = int(usage.get("completion_tokens") or 0)
        queue_wait_ms: int | None = None
        raw_wait = headers.get("x-colibri-queue-wait-ms")
        if raw_wait is not None:
            try:
                queue_wait_ms = int(raw_wait)
            except ValueError:
                queue_wait_ms = None
        warnings: list[str] = []
        if finish_reason == "length":
            warnings.append(
                "The answer stopped at the output-token limit and may be incomplete."
            )
        if max_output_tokens is not None and 0 < completion_tokens < max_output_tokens and finish_reason == "length":
            warnings.append(
                "The server enforced a lower output-token limit than requested."
            )
        generation_seconds = max(elapsed_ms - (queue_wait_ms or 0), 0) / 1000
        tokens_per_second = (
            completion_tokens / generation_seconds if completion_tokens > 0 and generation_seconds > 0 else None
        )
        return ProviderChatResult(
            provider=self.name,
            model_id=str(data.get("model") or clean_model),
            content=content,
            thinking=thinking_text,
            finish_reason=finish_reason,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=completion_tokens,
            elapsed_ms=elapsed_ms,
            queue_wait_ms=queue_wait_ms,
            tokens_per_second=tokens_per_second,
            warnings=warnings,
        )

    # -- internals -------------------------------------------------------

    def _validate_text_only(self, messages: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages must be a non-empty list")
        clean: list[dict[str, str]] = []
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("each message must be an object")
            if message.get("images") or message.get("_image_paths") or message.get("attachments"):
                raise ModelUnsupportedFeatureError(
                    "Deep Local is text-only: image and attachment inputs are not supported."
                )
            if message.get("tool_calls") or message.get("role") == "tool":
                raise ModelUnsupportedFeatureError(
                    "Deep Local does not support tool calls."
                )
            content = message.get("content")
            if not isinstance(content, str):
                raise ModelUnsupportedFeatureError(
                    "Deep Local is text-only: message content must be plain text."
                )
            role = str(message.get("role") or "user")
            if role not in {"system", "user", "assistant"}:
                raise ValueError(f"unsupported message role: {role}")
            clean.append({"role": role, "content": content})
        return clean

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
        auth: bool = True,
    ) -> dict[str, Any]:
        data, _headers = self._request_with_headers(
            method, path, payload=payload, timeout=timeout, auth=auth
        )
        return data

    def _request_with_headers(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        timeout: float,
        auth: bool = True,
        cancel_handle: RequestCancelHandle | None = None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        url = f"{self.endpoint}{path}"
        headers = {"Content-Type": "application/json"}
        if auth and self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        if cancel_handle is not None:
            raw, response_headers = self._cancellable_request(
                method, url, path, body=body, headers=headers, timeout=timeout, handle=cancel_handle
            )
        else:
            raw, response_headers = self._urllib_request(
                method, url, body=body, headers=headers, timeout=timeout
            )
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ModelMalformedResponseError(
                "malformed response: the Colibri server response exceeded the size limit"
            )
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            raise ModelEmptyResponseError(
                "empty response: the Colibri server returned an empty body"
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ModelMalformedResponseError(
                f"malformed response: the Colibri server did not return JSON ({exc})"
            ) from exc
        if not isinstance(data, dict):
            raise ModelMalformedResponseError(
                "malformed response: the Colibri server response was not an object"
            )
        return data, response_headers

    def _urllib_request(
        self,
        method: str,
        url: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
    ) -> tuple[bytes, dict[str, str]]:
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                response_headers = {key.lower(): value for key, value in response.headers.items()}
        except TimeoutError as exc:
            raise ModelTimeoutError(
                f"timeout: the Colibri request exceeded {timeout:g}s"
            ) from exc
        except socket.timeout as exc:
            raise ModelTimeoutError(
                f"timeout: the Colibri request exceeded {timeout:g}s"
            ) from exc
        except urllib.error.HTTPError as exc:
            raise self._map_http_error(exc) from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, TimeoutError):
                raise ModelTimeoutError(
                    f"timeout: the Colibri request exceeded {timeout:g}s"
                ) from exc
            raise ModelConnectionError(
                "connection failure: no Colibri server is reachable at "
                f"{self.endpoint}: {redact_secret(str(reason), self._api_key)}"
            ) from exc
        return raw, response_headers

    def _cancellable_request(
        self,
        method: str,
        url: str,
        path: str,
        *,
        body: bytes | None,
        headers: dict[str, str],
        timeout: float,
        handle: RequestCancelHandle,
    ) -> tuple[bytes, dict[str, str]]:
        """http.client transport whose connection a cancel handle can close.

        Behaves like `_urllib_request` (same error mapping, via a synthesized
        HTTPError for >=400 statuses) but allows another thread to abandon
        the wait. Abandonment surfaces as ModelInterruptedError, never as a
        claim that the server stopped working.
        """
        parts = urlsplit(self.endpoint)
        if parts.scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                parts.hostname or "127.0.0.1", parts.port or 443, timeout=timeout
            )
        else:
            conn = http.client.HTTPConnection(
                parts.hostname or "127.0.0.1", parts.port or 80, timeout=timeout
            )
        # A cancelled (closed) connection must stay closed: without this,
        # http.client transparently reconnects on the next request and the
        # cancel would silently un-cancel itself.
        conn.auto_open = 0
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            raise ModelConnectionError(
                "connection failure: no Colibri server is reachable at "
                f"{self.endpoint}: {redact_secret(str(exc), self._api_key)}"
            ) from exc
        handle.attach(conn)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            status = response.status
            reason = str(response.reason or "")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            response_msg = response.msg
        except (TimeoutError, socket.timeout) as exc:
            if handle.cancelled:
                raise ModelInterruptedError(
                    "stopped waiting: this request was abandoned; the Colibri "
                    "server may keep working on it for some time"
                ) from exc
            raise ModelTimeoutError(
                f"timeout: the Colibri request exceeded {timeout:g}s"
            ) from exc
        except (OSError, http.client.HTTPException) as exc:
            if handle.cancelled:
                raise ModelInterruptedError(
                    "stopped waiting: this request was abandoned; the Colibri "
                    "server may keep working on it for some time"
                ) from exc
            raise ModelConnectionError(
                "connection failure: no Colibri server is reachable at "
                f"{self.endpoint}: {redact_secret(str(exc), self._api_key)}"
            ) from exc
        finally:
            handle.detach()
            try:
                conn.close()
            except OSError:
                pass
        if status >= 400:
            synthesized = urllib.error.HTTPError(url, status, reason, response_msg, io.BytesIO(raw))
            raise self._map_http_error(synthesized) from None
        response_headers = {key.lower(): value for key, value in response_msg.items()}
        return raw, response_headers

    def _map_http_error(self, exc: urllib.error.HTTPError) -> ModelServiceError:
        detail = ""
        error_code = ""
        error_message = ""
        try:
            raw = exc.read(MAX_RESPONSE_BYTES).decode("utf-8", errors="replace")
            parsed = json.loads(raw)
            error = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(error, dict):
                error_code = str(error.get("code") or "")
                error_message = str(error.get("message") or "")
            detail = raw
        except (OSError, ValueError):
            pass
        detail = redact_secret(detail, self._api_key)
        error_message = redact_secret(error_message, self._api_key)
        logger.warning(
            "colibri http error status=%s code=%s endpoint=%s",
            exc.code,
            error_code or "unknown",
            self.endpoint,
        )
        retry_after: float | None = None
        raw_retry = exc.headers.get("Retry-After") if exc.headers else None
        if raw_retry:
            try:
                retry_after = float(raw_retry)
            except ValueError:
                retry_after = None
        if exc.code in {401, 403}:
            return ModelAuthError(
                "authentication failure: the Colibri server requires a valid API key "
                f"(set {COLIBRI_API_KEY_ENV})"
            )
        if exc.code == 404:
            if error_code == "model_not_found":
                return ModelInvalidModelError(
                    f"invalid model: {error_message or 'the Colibri server does not offer this model id'}"
                )
            return ModelIncompatibleServerError(
                "incompatible server: the endpoint did not recognize the request path "
                "(is this really a Colibri OpenAI-compatible server?)"
            )
        if exc.code == 429:
            if error_code == "queue_timeout":
                return ModelQueueTimeoutError(
                    "queue timeout: the Colibri server was busy for too long; try again later",
                    retry_after_seconds=retry_after,
                )
            return ModelQueueSaturatedError(
                "queue saturated: the Colibri server is busy with other work; try again later",
                retry_after_seconds=retry_after,
            )
        if exc.code == 400:
            if error_code.startswith("unsupported") or error_code in {"invalid_value"}:
                return ModelUnsupportedFeatureError(
                    f"unsupported request: {error_message or 'the Colibri server rejected a request feature'}"
                )
            return ModelServiceError(
                f"colibri request error: {error_message or detail or exc.reason}"
            )
        if exc.code >= 500:
            return ModelServerError(
                "server error: the Colibri server hit an internal problem "
                f"({error_message or exc.reason})"
            )
        return ModelServiceError(
            f"colibri http error {exc.code}: {error_message or detail or exc.reason}"
        )
