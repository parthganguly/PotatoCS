"""Fake-server contract tests for the Colibri Deep Local adapter (Phase 2).

The fake server speaks the OpenAI-compatible subset that `coli serve`
implements at upstream commit 550ddcba, including its error object shape,
queue-saturation semantics, and the `x-colibri-queue-wait-ms` header.
No real model or Colibri installation is required.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from odysseus_desktop_backend.services.deep_local_service import DeepLocalService
from odysseus_desktop_backend.services.providers.base import (
    ModelAuthError,
    ModelInterruptedError,
    ModelConnectionError,
    ModelEmptyResponseError,
    ModelIncompatibleServerError,
    ModelInvalidModelError,
    ModelMalformedResponseError,
    ModelQueueSaturatedError,
    ModelQueueTimeoutError,
    ModelServerError,
    ModelTimeoutError,
    ModelUnsupportedFeatureError,
)
from odysseus_desktop_backend.services.providers.colibri import (
    COLIBRI_API_KEY_ENV,
    ColibriProvider,
    RequestCancelHandle,
    is_loopback_endpoint,
    redact_secret,
)
from odysseus_desktop_backend.services.settings_service import SettingsService
from odysseus_desktop_backend.storage import Database

SENTINEL_KEY = "SENTINEL-COLIBRI-KEY-9f31"
MODEL_ID = "glm-5.2-colibri"


def _openai_error(message: str, code: str, error_type: str = "invalid_request_error") -> bytes:
    return json.dumps({"error": {"message": message, "type": error_type, "param": None, "code": code}}).encode()


class _FakeColibriHandler(BaseHTTPRequestHandler):
    behavior: dict[str, Any] = {}
    seen: list[dict[str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _send(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = self.behavior.get("api_key")
        if not expected:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {expected}"

    def do_GET(self) -> None:
        if self.path == "/health":
            mode = self.behavior.get("health_mode", "ok")
            if mode == "ok":
                self._send(
                    200,
                    json.dumps(
                        {
                            "status": "ok",
                            "scheduler": {
                                "active": 0,
                                "queued": 0,
                                "capacity": 1,
                                "max_queue": 8,
                                "queue_timeout_seconds": 300,
                                "admitted": 3,
                                "completed": 3,
                                "rejected": 0,
                                "timed_out": 0,
                                "cancelled": 0,
                            },
                            "kv_slots": 1,
                        }
                    ).encode(),
                )
            elif mode == "unhealthy":
                self._send(200, json.dumps({"status": "starting"}).encode())
            else:
                self._send(500, _openai_error("engine failed", "engine_error", "server_error"))
            return
        if not self._authorized():
            self._send(401, _openai_error("Invalid or missing API key.", "invalid_api_key", "authentication_error"))
            return
        if self.path == "/v1/models":
            self._send(
                200,
                json.dumps(
                    {
                        "object": "list",
                        "data": [{"id": MODEL_ID, "object": "model", "created": 1, "owned_by": "colibri"}],
                    }
                ).encode(),
            )
            return
        self._send(404, _openai_error("Not found.", "not_found"))

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append({"path": self.path, "body": body, "authorization": self.headers.get("Authorization", "")})
        if not self._authorized():
            self._send(401, _openai_error("Invalid or missing API key.", "invalid_api_key", "authentication_error"))
            return
        if self.path != "/v1/chat/completions":
            self._send(404, _openai_error("Not found.", "not_found"))
            return
        if body.get("model") != MODEL_ID:
            self._send(404, _openai_error(f"The model `{body.get('model')}` does not exist.", "model_not_found"))
            return
        mode = self.behavior.get("chat_mode", "ok")
        if mode == "queue_full":
            self._send(
                429,
                _openai_error("The inference queue is full.", "queue_full", "rate_limit_error"),
                {"Retry-After": "1"},
            )
            return
        if mode == "queue_timeout":
            self._send(
                429,
                _openai_error("Timed out waiting for the inference engine.", "queue_timeout", "rate_limit_error"),
                {"Retry-After": "1"},
            )
            return
        if mode == "server_error":
            self._send(500, _openai_error("The colibri engine failed to process the request.", "engine_error", "server_error"))
            return
        if mode == "unsupported":
            self._send(400, _openai_error("Colibri currently supports text message content only.", "unsupported_content_type"))
            return
        if mode == "malformed":
            self._send(200, b"<html>surprise</html>")
            return
        if mode == "empty_body":
            self._send(200, b"")
            return
        if mode == "empty_content":
            payload = self._completion("")
            self._send(200, json.dumps(payload).encode())
            return
        if mode == "slow":
            time.sleep(1.5)
            self._send(200, json.dumps(self._completion("late answer")).encode(), {"x-colibri-queue-wait-ms": "0"})
            return
        content = self.behavior.get("content", "Deep answer.")
        if body.get("enable_thinking"):
            content = "chain of reasoning here</think>" + content
        finish = self.behavior.get("finish_reason", "stop")
        self._send(
            200,
            json.dumps(self._completion(content, finish)).encode(),
            {"x-colibri-queue-wait-ms": self.behavior.get("queue_wait_ms", "42")},
        )

    @staticmethod
    def _completion(content: str, finish_reason: str = "stop") -> dict[str, Any]:
        return {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 1,
            "model": MODEL_ID,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content, "refusal": None},
                    "logprobs": None,
                    "finish_reason": finish_reason,
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27},
        }


@pytest.fixture()
def fake_colibri():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeColibriHandler)
    _FakeColibriHandler.behavior = {}
    _FakeColibriHandler.seen = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield endpoint
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def make_provider(endpoint: str, **kwargs: Any) -> ColibriProvider:
    return ColibriProvider(endpoint, **kwargs)


# -- endpoint safety ------------------------------------------------------


@pytest.mark.parametrize(
    "endpoint",
    ["http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"],
)
def test_loopback_endpoints_accepted(endpoint: str) -> None:
    assert is_loopback_endpoint(endpoint) is True


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:8000",
        "http://192.168.1.10:8000",
        "https://10.0.0.5",
        "ftp://127.0.0.1",
        "not a url",
        "",
    ],
)
def test_non_loopback_endpoints_rejected(endpoint: str) -> None:
    assert is_loopback_endpoint(endpoint) is False
    with pytest.raises(ValueError):
        ColibriProvider(endpoint)


# -- detection ------------------------------------------------------------


def test_health_success(fake_colibri: str) -> None:
    status = make_provider(fake_colibri).health()
    assert status.reachable is True
    assert status.healthy is True
    assert status.detail["queue"] == {"active": 0, "queued": 0, "capacity": 1, "max_queue": 8}
    assert status.detail["kv_slots"] == 1
    assert status.error == ""


def test_health_unhealthy_status(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["health_mode"] = "unhealthy"
    status = make_provider(fake_colibri).health()
    assert status.reachable is True
    assert status.healthy is False
    assert status.error_category == "server_error"


def test_health_connection_refused() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    status = make_provider(f"http://127.0.0.1:{closed_port}").health()
    assert status.reachable is False
    assert status.healthy is False
    assert status.error_category == "connection_failure"


def test_models_listing(fake_colibri: str) -> None:
    models = make_provider(fake_colibri).list_models()
    assert [item.model_id for item in models] == [MODEL_ID]
    assert models[0].provider == "colibri"


# -- completion normalization ---------------------------------------------


def test_normal_completion_normalized(fake_colibri: str) -> None:
    result = make_provider(fake_colibri).chat_once(
        MODEL_ID,
        [{"role": "user", "content": "hello"}],
        temperature=0.0,
        max_output_tokens=64,
    )
    assert result.provider == "colibri"
    assert result.model_id == MODEL_ID
    assert result.content == "Deep answer."
    assert result.thinking == ""
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 7
    assert result.queue_wait_ms == 42
    assert result.warnings == []
    sent = _FakeColibriHandler.seen[-1]["body"]
    assert sent["stream"] is False
    assert sent["temperature"] == 0.0
    assert sent["max_tokens"] == 64
    assert "enable_thinking" not in sent


def test_thinking_content_split(fake_colibri: str) -> None:
    result = make_provider(fake_colibri).chat_once(
        MODEL_ID,
        [{"role": "user", "content": "hello"}],
        thinking="on",
    )
    assert _FakeColibriHandler.seen[-1]["body"]["enable_thinking"] is True
    assert result.thinking == "chain of reasoning here"
    assert result.content == "Deep answer."


def test_auto_thinking_maps_to_off(fake_colibri: str) -> None:
    make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}], thinking="auto")
    assert "enable_thinking" not in _FakeColibriHandler.seen[-1]["body"]


def test_length_limited_completion_warns(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["finish_reason"] = "length"
    result = make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])
    assert result.finish_reason == "length"
    assert any("output-token limit" in warning for warning in result.warnings)


# -- error taxonomy --------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("malformed", ModelMalformedResponseError),
        ("empty_body", ModelEmptyResponseError),
        ("empty_content", ModelEmptyResponseError),
        ("server_error", ModelServerError),
        ("unsupported", ModelUnsupportedFeatureError),
    ],
)
def test_response_error_categories(fake_colibri: str, mode: str, expected: type[Exception]) -> None:
    _FakeColibriHandler.behavior["chat_mode"] = mode
    with pytest.raises(expected):
        make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])


def test_queue_full_maps_to_queue_saturated_with_retry_hint(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["chat_mode"] = "queue_full"
    with pytest.raises(ModelQueueSaturatedError) as excinfo:
        make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])
    assert excinfo.value.category == "queue_saturated"
    assert excinfo.value.retry_after_seconds == 1.0


def test_queue_timeout_maps_to_queue_timeout(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["chat_mode"] = "queue_timeout"
    with pytest.raises(ModelQueueTimeoutError) as excinfo:
        make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])
    assert excinfo.value.category == "queue_timeout"


def test_wrong_model_id_maps_to_invalid_model(fake_colibri: str) -> None:
    with pytest.raises(ModelInvalidModelError):
        make_provider(fake_colibri).chat_once("wrong-model", [{"role": "user", "content": "hi"}])


def test_wrong_endpoint_path_maps_to_incompatible_server(fake_colibri: str) -> None:
    provider = make_provider(fake_colibri)
    with pytest.raises(ModelIncompatibleServerError):
        provider._request("GET", "/v1/not-a-colibri-path", timeout=5.0)


def test_auth_failure_maps_to_auth_error(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["api_key"] = SENTINEL_KEY
    with pytest.raises(ModelAuthError) as excinfo:
        make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])
    assert COLIBRI_API_KEY_ENV in str(excinfo.value)


def test_api_key_authorizes_and_never_leaks(fake_colibri: str, caplog: pytest.LogCaptureFixture) -> None:
    _FakeColibriHandler.behavior["api_key"] = SENTINEL_KEY
    provider = make_provider(fake_colibri, api_key=SENTINEL_KEY)
    with caplog.at_level(logging.DEBUG):
        result = provider.chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])
        _FakeColibriHandler.behavior["chat_mode"] = "server_error"
        with pytest.raises(ModelServerError) as excinfo:
            provider.chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])
    assert result.content == "Deep answer."
    assert SENTINEL_KEY not in str(excinfo.value)
    assert SENTINEL_KEY not in caplog.text
    assert SENTINEL_KEY not in json.dumps(result.to_dict())


def test_connection_refused_maps_to_connection_failure() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    with pytest.raises(ModelConnectionError):
        make_provider(f"http://127.0.0.1:{closed_port}").chat_once(MODEL_ID, [{"role": "user", "content": "hi"}])


def test_request_timeout_maps_to_timeout(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["chat_mode"] = "slow"
    with pytest.raises(ModelTimeoutError):
        make_provider(fake_colibri).chat_once(MODEL_ID, [{"role": "user", "content": "hi"}], timeout=0.2)


@pytest.mark.parametrize(
    "message",
    [
        {"role": "user", "content": "look", "images": ["abc"]},
        {"role": "user", "content": "look", "_image_paths": ["C:/tmp/x.png"]},
        {"role": "user", "content": [{"type": "image_url", "image_url": "x"}]},
        {"role": "tool", "content": "result"},
    ],
)
def test_multimodal_and_tool_inputs_rejected_client_side(fake_colibri: str, message: dict[str, Any]) -> None:
    with pytest.raises(ModelUnsupportedFeatureError):
        make_provider(fake_colibri).chat_once(MODEL_ID, [message])
    assert _FakeColibriHandler.seen == []  # never reached the server


def test_redact_secret_helper() -> None:
    assert redact_secret("Bearer abc123 failed", "abc123") == "Bearer *** failed"
    assert redact_secret("no secret here", None) == "no secret here"


# -- cancellable transport (RequestCancelHandle) ----------------------------


def test_cancel_handle_interrupts_in_flight_request(fake_colibri: str) -> None:
    """cancel() while blocked on the response raises ModelInterruptedError
    promptly — the caller stops waiting; no claim the engine stopped."""
    _FakeColibriHandler.behavior["chat_mode"] = "slow"
    handle = RequestCancelHandle()
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            make_provider(fake_colibri).chat_once(
                MODEL_ID, [{"role": "user", "content": "hi"}], cancel_handle=handle
            )
            outcome["result"] = "completed"
        except ModelInterruptedError:
            outcome["result"] = "interrupted"
        except Exception as exc:  # noqa: BLE001 - test records the surprise
            outcome["result"] = f"unexpected: {type(exc).__name__}"

    worker = threading.Thread(target=run)
    worker.start()
    time.sleep(0.3)  # request is in flight (fake handler sleeps 1.5s)
    started = time.monotonic()
    handle.cancel()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert time.monotonic() - started < 2.0
    assert outcome["result"] == "interrupted"


def test_cancel_handle_before_request_fails_immediately(fake_colibri: str) -> None:
    handle = RequestCancelHandle()
    handle.cancel()
    with pytest.raises((ModelInterruptedError, ModelConnectionError)):
        make_provider(fake_colibri).chat_once(
            MODEL_ID, [{"role": "user", "content": "hi"}], cancel_handle=handle
        )


def test_uncancelled_handle_does_not_change_result(fake_colibri: str) -> None:
    handle = RequestCancelHandle()
    result = make_provider(fake_colibri).chat_once(
        MODEL_ID, [{"role": "user", "content": "hi"}], cancel_handle=handle
    )
    assert result.content == "Deep answer."
    assert result.queue_wait_ms == 42


def test_cancellable_transport_maps_http_errors_identically(fake_colibri: str) -> None:
    _FakeColibriHandler.behavior["chat_mode"] = "queue_full"
    with pytest.raises(ModelQueueSaturatedError) as excinfo:
        make_provider(fake_colibri).chat_once(
            MODEL_ID, [{"role": "user", "content": "hi"}], cancel_handle=RequestCancelHandle()
        )
    assert excinfo.value.retry_after_seconds == 1.0


# -- DeepLocalService (flag-gated RPC facade) ------------------------------


@pytest.fixture()
def settings(tmp_path):
    db = Database(tmp_path)
    try:
        yield SettingsService(db)
    finally:
        db.close()


def test_deep_local_disabled_by_default(settings: SettingsService) -> None:
    service = DeepLocalService(settings)
    status = service.status()
    assert status["ok"] is False
    assert status["enabled"] is False
    assert status["error_category"] == "disabled"
    once = service.complete_once(prompt="hello")
    assert once["ok"] is False
    assert once["error_category"] == "disabled"


def test_deep_local_rejects_non_loopback_endpoint(settings: SettingsService) -> None:
    settings.set({"deep_local_enabled": True, "deep_local_endpoint": "http://192.168.0.5:8000"})
    status = DeepLocalService(settings).status()
    assert status["ok"] is False
    assert status["error_category"] == "disabled"
    assert "127.0.0.1" in status["error"]


def test_deep_local_status_happy_path(settings: SettingsService, fake_colibri: str) -> None:
    settings.set({"deep_local_enabled": True, "deep_local_endpoint": fake_colibri})
    status = DeepLocalService(settings).status()
    assert status["ok"] is True
    assert status["reachable"] is True
    assert status["healthy"] is True
    assert status["models"] == [MODEL_ID]
    assert status["queue"]["max_queue"] == 8


def test_deep_local_complete_once_auto_selects_sole_model(settings: SettingsService, fake_colibri: str) -> None:
    settings.set({"deep_local_enabled": True, "deep_local_endpoint": fake_colibri})
    outcome = DeepLocalService(settings).complete_once(prompt="What do the sources say?")
    assert outcome["ok"] is True
    result = outcome["result"]
    assert result["provider"] == "colibri"
    assert result["model"] == MODEL_ID
    assert result["content"] == "Deep answer."
    assert result["completion_tokens"] == 7
    assert result["queue_wait_ms"] == 42


def test_deep_local_complete_once_maps_provider_errors(settings: SettingsService, fake_colibri: str) -> None:
    settings.set({"deep_local_enabled": True, "deep_local_endpoint": fake_colibri})
    _FakeColibriHandler.behavior["chat_mode"] = "queue_full"
    outcome = DeepLocalService(settings).complete_once(prompt="hello", model=MODEL_ID)
    assert outcome["ok"] is False
    assert outcome["error_category"] == "queue_saturated"
    assert outcome["retry_after_seconds"] == 1.0
    assert "busy" in outcome["error"]
