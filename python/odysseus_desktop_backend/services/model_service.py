from __future__ import annotations

import json
import shutil
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.storage import Database, utc_ms


OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
INTERACTIVE_CHAT_TIMEOUT_SECONDS = 120
BENCHMARK_ANSWER_TIMEOUT_SECONDS = 300
VERIFIER_TIMEOUT_SECONDS = 180
CORRECTION_TIMEOUT_SECONDS = 300
VERIFIER_NUM_PREDICT = 512
CORRECTION_NUM_PREDICT = 768
logger = get_logger("models")


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


class ModelService:
    def __init__(self, db: Database):
        self.db = db

    def detect_ollama(self) -> dict[str, Any]:
        installed = shutil.which("ollama") is not None
        reachable = self._tcp_reachable("127.0.0.1", 11434)
        models: list[str] = []
        model_details: list[dict[str, Any]] = []
        version = ""
        error = ""

        if reachable:
            try:
                tags = self._get_json(f"{OLLAMA_ENDPOINT}/api/tags", timeout=3)
                for item in tags.get("models", []):
                    if not isinstance(item, dict) or not item.get("name"):
                        continue
                    models.append(str(item["name"]))
                    model_details.append(self._model_info(item))
            except Exception as exc:  # noqa: BLE001 - surfaced in runtime status
                error = str(exc)
            try:
                version_data = self._get_json(f"{OLLAMA_ENDPOINT}/api/version", timeout=2)
                version = str(version_data.get("version", ""))
            except Exception:
                pass

        status = {
            "name": "ollama",
            "installed": installed,
            "reachable": reachable,
            "endpoint": OLLAMA_ENDPOINT,
            "version": version,
            "models": models,
            "model_details": model_details,
            "error": error,
            "updated_at": utc_ms(),
        }
        self._store_runtime_status(status)
        logger.info(
            "ollama detection installed=%s reachable=%s endpoint=%s models=%s error=%s",
            installed,
            reachable,
            OLLAMA_ENDPOINT,
            len(models),
            error,
        )
        return status

    def _model_info(self, item: dict[str, Any]) -> dict[str, Any]:
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        return {
            "name": str(item.get("name") or ""),
            "modified_at": str(item.get("modified_at") or ""),
            "size": int(item.get("size") or 0),
            "digest": str(item.get("digest") or ""),
            "format": str(details.get("format") or ""),
            "family": str(details.get("family") or ""),
            "parameter_size": str(details.get("parameter_size") or ""),
            "quantization_level": str(details.get("quantization_level") or ""),
        }

    def chat(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        thinking: str = "auto",
        timeout: float = INTERACTIVE_CHAT_TIMEOUT_SECONDS,
    ) -> str:
        return self.chat_detailed(
            model,
            messages,
            options=options,
            thinking=thinking,
            timeout=timeout,
        )["content"]

    def chat_detailed(
        self,
        model: str,
        messages: list[dict[str, str]],
        *,
        options: dict[str, Any] | None = None,
        thinking: str = "auto",
        timeout: float = INTERACTIVE_CHAT_TIMEOUT_SECONDS,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        if not model:
            raise ValueError("model is required")
        clean_options = dict(options or {})
        clean_options.pop("think", None)
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
        }
        normalized_thinking = normalize_thinking_mode(thinking)
        if normalized_thinking == "off":
            payload["think"] = False
        elif normalized_thinking == "on":
            payload["think"] = True
        if clean_options:
            payload["options"] = clean_options
        if response_format:
            payload["format"] = response_format

        started = time.perf_counter()
        data = self._post_json(f"{OLLAMA_ENDPOINT}/api/chat", payload, timeout=timeout)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        if not isinstance(data, dict):
            raise ModelMalformedResponseError("malformed response: Ollama response was not an object")

        message = data.get("message") if isinstance(data, dict) else None
        if isinstance(message, dict):
            content = message.get("content")
            thinking_text = message.get("thinking")
            if not isinstance(thinking_text, str):
                thinking_text = str(data.get("thinking") or "")
            if isinstance(content, str):
                if not content and not thinking_text:
                    raise ModelEmptyResponseError("empty response: Ollama returned no assistant content")
                return structured_chat_response(
                    model=model,
                    content=content,
                    thinking=thinking_text,
                    data=data,
                    elapsed_ms=elapsed_ms,
                )
        raise ModelMalformedResponseError("malformed response: Ollama returned no assistant message")

    def ps(self) -> dict[str, Any]:
        if not self._tcp_reachable("127.0.0.1", 11434):
            return {"models": [], "reachable": False, "error": "Ollama is not reachable"}
        try:
            data = self._get_json(f"{OLLAMA_ENDPOINT}/api/ps", timeout=3)
        except Exception as exc:  # noqa: BLE001 - surfaced as diagnostics
            return {"models": [], "reachable": True, "error": str(exc)}
        models = []
        for item in data.get("models", []):
            if isinstance(item, dict):
                models.append(parse_ps_model(item))
        return {"models": models, "reachable": True, "error": ""}

    def _store_runtime_status(self, status: dict[str, Any]) -> None:
        self.db.conn.execute(
            """
            INSERT INTO runtime_status(
                name, reachable, installed, endpoint, version, models_json, error, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                reachable = excluded.reachable,
                installed = excluded.installed,
                endpoint = excluded.endpoint,
                version = excluded.version,
                models_json = excluded.models_json,
                error = excluded.error,
                updated_at = excluded.updated_at
            """,
            (
                status["name"],
                1 if status["reachable"] else 0,
                1 if status["installed"] else 0,
                status["endpoint"],
                status["version"],
                json.dumps(status["models"]),
                status["error"],
                status["updated_at"],
            ),
        )
        self.db.conn.commit()

    def _tcp_reachable(self, host: str, port: int) -> bool:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            return False

    def _get_json(self, url: str, timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
        return json.loads(raw)

    def _post_json(self, url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
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
            raise ModelConnectionError(f"connection failure: Ollama is not reachable at {OLLAMA_ENDPOINT}: {reason}") from exc
        if not raw.strip():
            raise ModelEmptyResponseError("empty response: Ollama returned an empty body")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ModelMalformedResponseError(f"malformed response: {exc}") from exc


def normalize_thinking_mode(value: str | None) -> str:
    normalized = (value or "auto").strip().lower().replace("_", "-")
    if normalized in {"false", "no", "0"}:
        normalized = "off"
    if normalized in {"true", "yes", "1"}:
        normalized = "on"
    if normalized not in {"auto", "off", "on", "legacy/unrecorded"}:
        raise ValueError("thinking mode must be one of: auto, off, on")
    return normalized


def structured_chat_response(
    *,
    model: str,
    content: str,
    thinking: str,
    data: dict[str, Any],
    elapsed_ms: int,
) -> dict[str, Any]:
    prompt_eval_count = int(data.get("prompt_eval_count") or 0)
    prompt_eval_duration_ns = int(data.get("prompt_eval_duration") or 0)
    eval_count = int(data.get("eval_count") or 0)
    eval_duration_ns = int(data.get("eval_duration") or 0)
    return {
        "model": str(data.get("model") or model),
        "content": content,
        "thinking": thinking,
        "done_reason": str(data.get("done_reason") or data.get("done") or ""),
        "total_duration_ns": int(data.get("total_duration") or 0),
        "load_duration_ns": int(data.get("load_duration") or 0),
        "prompt_eval_count": prompt_eval_count,
        "prompt_eval_duration_ns": prompt_eval_duration_ns,
        "eval_count": eval_count,
        "eval_duration_ns": eval_duration_ns,
        "prompt_tokens_per_second": tokens_per_second(prompt_eval_count, prompt_eval_duration_ns),
        "generation_tokens_per_second": tokens_per_second(eval_count, eval_duration_ns),
        "elapsed_ms": elapsed_ms,
        "raw": data,
    }


def tokens_per_second(count: int, duration_ns: int) -> float | None:
    if count <= 0 or duration_ns <= 0:
        return None
    return count / (duration_ns / 1_000_000_000)


def parse_ps_model(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    size = int(item.get("size") or 0)
    size_vram = int(item.get("size_vram") or 0)
    gpu_fraction = (size_vram / size) if size > 0 else None
    cpu_fraction = (1 - gpu_fraction) if gpu_fraction is not None else None
    return {
        "name": str(item.get("name") or item.get("model") or ""),
        "model": str(item.get("model") or item.get("name") or ""),
        "digest": str(item.get("digest") or ""),
        "expires_at": str(item.get("expires_at") or ""),
        "size": size,
        "size_vram": size_vram,
        "parameter_size": str(details.get("parameter_size") or ""),
        "quantization_level": str(details.get("quantization_level") or ""),
        "context_length": int(details.get("context_length") or item.get("context_length") or 0),
        "estimated_gpu_loaded_fraction": gpu_fraction,
        "estimated_cpu_loaded_fraction": cpu_fraction,
        "partially_cpu_offloaded": bool(cpu_fraction is not None and cpu_fraction > 0.05),
        "raw": item,
    }
