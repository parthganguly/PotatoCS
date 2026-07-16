from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ModelServiceError(RuntimeError):
    category = "runtime_error"


class ModelTimeoutError(ModelServiceError):
    category = "timeout"


class ModelConnectionError(ModelServiceError):
    category = "connection_failure"


class ModelInvalidModelError(ModelServiceError):
    category = "invalid_model"


class ModelMalformedResponseError(ModelServiceError):
    category = "malformed_response"


class ModelEmptyResponseError(ModelServiceError):
    category = "empty_response"


class ModelAuthError(ModelServiceError):
    category = "auth_failure"


class ModelQueueSaturatedError(ModelServiceError):
    category = "queue_saturated"

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ModelQueueTimeoutError(ModelQueueSaturatedError):
    category = "queue_timeout"


class ModelUnsupportedFeatureError(ModelServiceError):
    category = "unsupported_feature"


class ModelIncompatibleServerError(ModelServiceError):
    category = "incompatible_server"


class ModelServerError(ModelServiceError):
    category = "server_error"


class ModelProviderDisabledError(ModelServiceError):
    category = "disabled"


@dataclass
class ProviderStatus:
    """Normalized reachability/readiness snapshot for a provider endpoint."""

    provider: str
    endpoint: str
    reachable: bool
    healthy: bool
    error: str = ""
    error_category: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "endpoint": self.endpoint,
            "reachable": self.reachable,
            "healthy": self.healthy,
            "error": self.error,
            "error_category": self.error_category,
            "detail": self.detail,
        }


@dataclass
class ProviderModel:
    """Normalized model listing entry."""

    provider: str
    model_id: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"provider": self.provider, "model_id": self.model_id, "detail": self.detail}


@dataclass
class ProviderChatResult:
    """Normalized text-only chat completion result.

    The Ollama path keeps its historical dict shape for compatibility with
    stored traces and tests; new providers return this type and callers
    serialize with `to_dict()`.
    """

    provider: str
    model_id: str
    content: str
    thinking: str = ""
    finish_reason: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    queue_wait_ms: int | None = None
    tokens_per_second: float | None = None
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model_id,
            "content": self.content,
            "thinking": self.thinking,
            "finish_reason": self.finish_reason,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "elapsed_ms": self.elapsed_ms,
            "queue_wait_ms": self.queue_wait_ms,
            "tokens_per_second": self.tokens_per_second,
            "warnings": list(self.warnings),
        }
