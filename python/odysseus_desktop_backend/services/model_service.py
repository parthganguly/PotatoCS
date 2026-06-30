from __future__ import annotations

import json
import base64
import re
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.storage import Database, utc_ms


OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
INTERACTIVE_CHAT_TIMEOUT_SECONDS = 120
VISION_CHAT_TIMEOUT_SECONDS = 240
MAX_VISION_IMAGES = 4
MAX_VISION_IMAGE_BYTES = 8 * 1024 * 1024
MAX_VISION_TOTAL_IMAGE_BYTES = 24 * 1024 * 1024
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
            "conversation_models": build_conversation_model_catalog(
                models,
                model_details=model_details,
                capabilities=list(self._cached_capabilities_by_canonical().values()),
            ),
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

    def capabilities(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        status = self.detect_ollama()
        names = [str(name) for name in status.get("models") or []]
        cached = {row["model"]: capability_row_dict(row) for row in self.db.conn.execute("SELECT * FROM model_capabilities").fetchall()}
        results: list[dict[str, Any]] = []
        for name in names:
            if refresh or name not in cached:
                try:
                    results.append(self.inspect(name))
                except Exception as exc:  # noqa: BLE001 - diagnostics should keep going
                    results.append(self._store_capability_error(name, str(exc)))
            else:
                results.append(cached[name])
        return sorted(results, key=lambda item: str(item.get("model") or ""))

    def inspect(self, model: str) -> dict[str, Any]:
        clean_model = (model or "").strip()
        if not clean_model:
            raise ValueError("model is required")
        data = self._post_json(f"{OLLAMA_ENDPOINT}/api/show", {"model": clean_model}, timeout=10)
        details = data.get("details") if isinstance(data.get("details"), dict) else {}
        model_info = data.get("model_info") if isinstance(data.get("model_info"), dict) else {}
        capabilities = [str(item).lower() for item in data.get("capabilities") or [] if isinstance(item, str)]
        capability_set = set(capabilities)
        digest = str(data.get("digest") or "")
        context_length = int(
            model_info.get("llama.context_length")
            or model_info.get("general.context_length")
            or model_info.get("context_length")
            or 0
        )
        item = {
            "model": clean_model,
            "digest": digest,
            "size": int(data.get("size") or 0),
            "family": str(details.get("family") or model_info.get("general.architecture") or ""),
            "parameter_size": str(details.get("parameter_size") or ""),
            "quantization_level": str(details.get("quantization_level") or ""),
            "context_length": context_length,
            "capabilities": capabilities,
            "text_generation": capability_state(capability_set, {"completion", "chat", "generate"}),
            "vision": capability_state(capability_set, {"vision"}),
            "embedding": capability_state(capability_set, {"embedding", "embed"}),
            "tools": capability_state(capability_set, {"tools", "tool"}),
            "thinking": "unknown",
            "raw": data,
            "inspected_at": utc_ms(),
            "error": "",
        }
        self._store_capability(item)
        return item

    def refresh_capabilities(self) -> list[dict[str, Any]]:
        return self.capabilities(refresh=True)

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

    def chat_vision_detailed(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        image_paths: list[str],
        options: dict[str, Any] | None = None,
        thinking: str = "auto",
        timeout: float = VISION_CHAT_TIMEOUT_SECONDS,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        tagged_messages = [dict(message) for message in messages]
        image_attached = False
        for message in tagged_messages:
            if str(message.get("role") or "user") == "user":
                message["_image_paths"] = list(image_paths)
                image_attached = True
                break
        if not image_attached:
            tagged_messages.append({"role": "user", "content": "", "_image_paths": list(image_paths)})
        return self.chat_vision_history_detailed(
            model,
            tagged_messages,
            options=options,
            thinking=thinking,
            timeout=timeout,
            response_format=response_format,
        )

    def chat_vision_history_detailed(
        self,
        model: str,
        messages: list[dict[str, Any]],
        *,
        options: dict[str, Any] | None = None,
        thinking: str = "auto",
        timeout: float = VISION_CHAT_TIMEOUT_SECONDS,
        response_format: str | None = None,
    ) -> dict[str, Any]:
        prepared_messages: list[dict[str, Any]] = []
        image_attached = False
        for message in messages:
            item = {
                "role": str(message.get("role") or "user"),
                "content": str(message.get("content") or ""),
            }
            raw_paths = message.get("_image_paths") or []
            image_paths = [str(path) for path in raw_paths if str(path).strip()] if isinstance(raw_paths, list) else []
            if image_paths and item["role"] == "user":
                item["images"] = encode_vision_images(image_paths)
                image_attached = True
            prepared_messages.append(item)
        if not image_attached:
            raise ValueError("at least one historical user message must include image paths")
        return self.chat_detailed(
            model,
            prepared_messages,  # type: ignore[arg-type]
            options=options,
            thinking=thinking,
            timeout=timeout,
            response_format=response_format,
        )

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

    def _cached_capabilities_by_canonical(self) -> dict[str, dict[str, Any]]:
        rows = self.db.conn.execute("SELECT * FROM model_capabilities").fetchall()
        return {canonical_model_tag(row["model"]): capability_row_dict(row) for row in rows}

    def _store_capability(self, item: dict[str, Any]) -> None:
        self.db.conn.execute(
            """
            INSERT INTO model_capabilities(
                model, digest, size, family, parameter_size, quantization_level,
                context_length, capabilities_json, text_generation, vision,
                embedding, tools, thinking, raw_json, inspected_at, error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model) DO UPDATE SET
                digest = excluded.digest,
                size = excluded.size,
                family = excluded.family,
                parameter_size = excluded.parameter_size,
                quantization_level = excluded.quantization_level,
                context_length = excluded.context_length,
                capabilities_json = excluded.capabilities_json,
                text_generation = excluded.text_generation,
                vision = excluded.vision,
                embedding = excluded.embedding,
                tools = excluded.tools,
                thinking = excluded.thinking,
                raw_json = excluded.raw_json,
                inspected_at = excluded.inspected_at,
                error = excluded.error
            """,
            (
                item["model"],
                item.get("digest", ""),
                int(item.get("size") or 0),
                item.get("family", ""),
                item.get("parameter_size", ""),
                item.get("quantization_level", ""),
                int(item.get("context_length") or 0),
                json.dumps(item.get("capabilities") or []),
                item.get("text_generation", "unknown"),
                item.get("vision", "unknown"),
                item.get("embedding", "unknown"),
                item.get("tools", "unknown"),
                item.get("thinking", "unknown"),
                json.dumps(item.get("raw") or {}),
                int(item.get("inspected_at") or utc_ms()),
                item.get("error", ""),
            ),
        )
        self.db.conn.commit()

    def _store_capability_error(self, model: str, error: str) -> dict[str, Any]:
        item = {
            "model": model,
            "digest": "",
            "size": 0,
            "family": "",
            "parameter_size": "",
            "quantization_level": "",
            "context_length": 0,
            "capabilities": [],
            "text_generation": "error",
            "vision": "error",
            "embedding": "error",
            "tools": "error",
            "thinking": "error",
            "raw": {},
            "inspected_at": utc_ms(),
            "error": error,
        }
        self._store_capability(item)
        return item

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


def capability_state(capabilities: set[str], accepted: set[str]) -> str:
    if not capabilities:
        return "unknown"
    return "yes" if capabilities.intersection(accepted) else "no"


def canonical_model_tag(model: str) -> str:
    clean = str(model or "").strip().lower()
    if not clean:
        return ""
    return clean if ":" in clean else f"{clean}:latest"


def display_model_name(model: str, *, installed: bool = True) -> str:
    clean = str(model or "").strip()
    if not clean:
        label = "No model"
    else:
        clean = re.sub(r":latest$", "", clean, flags=re.IGNORECASE)
        parts = [part for part in re.split(r"[-_:]", clean) if part]
        label = " ".join(format_model_part(part) for part in parts)
    return label if installed else f"{label} · not installed"


def format_model_part(part: str) -> str:
    if re.match(r"^\d+(?:\.\d+)?[bmk]$", part, flags=re.IGNORECASE):
        return part.upper()
    if re.match(r"^v\d+(?:\.\d+)?$", part, flags=re.IGNORECASE):
        return part.upper()
    if part.lower() == "vl":
        return "VL"
    if re.match(r"^llama\d+(?:\.\d+)?$", part, flags=re.IGNORECASE):
        return re.sub(r"^llama", "Llama ", part, flags=re.IGNORECASE)
    if part.lower() == "openhermes":
        return "OpenHermes"
    return part[:1].upper() + part[1:]


def model_role(model: str, capability: dict[str, Any] | None = None) -> str:
    capability = capability or {}
    text_generation = str(capability.get("text_generation") or "unknown")
    vision = str(capability.get("vision") or "unknown")
    embedding = str(capability.get("embedding") or "unknown")
    if embedding == "yes" and text_generation != "yes" and vision != "yes":
        return "embedding"
    if vision == "yes" and text_generation == "yes":
        return "vision"
    if text_generation == "yes":
        return "chat"
    if not capability and looks_like_embedding_model(model):
        return "embedding"
    if embedding == "yes" and text_generation != "yes":
        return "embedding"
    return "unknown"


def is_chat_selectable_model(model: str, capability: dict[str, Any] | None = None) -> bool:
    role = model_role(model, capability)
    if role == "embedding":
        return False
    capability = capability or {}
    text_generation = str(capability.get("text_generation") or "unknown")
    vision = str(capability.get("vision") or "unknown")
    embedding = str(capability.get("embedding") or "unknown")
    if text_generation == "yes":
        return True
    if text_generation == "no":
        return False
    if embedding == "yes" and vision != "yes":
        return False
    return not looks_like_embedding_model(model)


def looks_like_embedding_model(model: str) -> bool:
    clean = str(model or "").lower()
    return bool(re.search(r"\bembed(?:ding)?\b", clean) or "nomic-embed" in clean or "bge-" in clean)


def build_conversation_model_catalog(
    installed_models: list[str],
    *,
    model_details: list[dict[str, Any]] | None = None,
    capabilities: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    capabilities_by_canonical = {
        canonical_model_tag(str(item.get("model") or "")): item
        for item in capabilities or []
        if canonical_model_tag(str(item.get("model") or ""))
    }
    detail_names = {
        canonical_model_tag(str(item.get("name") or "")): str(item.get("name") or "")
        for item in model_details or []
        if canonical_model_tag(str(item.get("name") or ""))
    }
    aliases_by_canonical: dict[str, list[str]] = {}
    for model in installed_models:
        canonical = canonical_model_tag(model)
        if not canonical:
            continue
        aliases_by_canonical.setdefault(canonical, []).append(str(model).strip())

    catalog: list[dict[str, Any]] = []
    for canonical, aliases in aliases_by_canonical.items():
        tag = preferred_exact_model_tag(aliases, detail_names.get(canonical))
        capability = capabilities_by_canonical.get(canonical)
        if not is_chat_selectable_model(tag, capability):
            continue
        catalog.append(
            {
                "tag": tag,
                "canonical_tag": canonical,
                "display_name": display_model_name(tag),
                "role": model_role(tag, capability),
                "installed": True,
                "stale": False,
                "exact_tags": aliases,
                "tooltip": f"Installed tags: {', '.join(aliases)}" if len(aliases) > 1 else tag,
            }
        )
    return sorted(catalog, key=lambda item: str(item.get("display_name") or ""))


def preferred_exact_model_tag(aliases: list[str], detail_name: str | None = None) -> str:
    if detail_name and detail_name in aliases:
        return detail_name
    latest = next((tag for tag in aliases if tag.lower().endswith(":latest")), "")
    if latest:
        return latest
    return aliases[0] if aliases else ""


def historical_model_label(model: str, installed_models: list[str]) -> str:
    canonical = canonical_model_tag(model)
    installed = bool(canonical and any(canonical_model_tag(item) == canonical for item in installed_models))
    return display_model_name(model, installed=installed)


def capability_row_dict(row: Any) -> dict[str, Any]:
    return {
        "model": row["model"],
        "digest": row["digest"],
        "size": int(row["size"] or 0),
        "family": row["family"],
        "parameter_size": row["parameter_size"],
        "quantization_level": row["quantization_level"],
        "context_length": int(row["context_length"] or 0),
        "capabilities": json.loads(row["capabilities_json"] or "[]"),
        "text_generation": row["text_generation"],
        "vision": row["vision"],
        "embedding": row["embedding"],
        "tools": row["tools"],
        "thinking": row["thinking"],
        "raw": json.loads(row["raw_json"] or "{}"),
        "inspected_at": int(row["inspected_at"] or 0),
        "error": row["error"],
    }


def encode_vision_images(image_paths: list[str]) -> list[str]:
    if not image_paths:
        raise ValueError("at least one image is required")
    if len(image_paths) > MAX_VISION_IMAGES:
        raise ValueError(f"at most {MAX_VISION_IMAGES} images can be sent to a vision model")
    encoded: list[str] = []
    total_bytes = 0
    for value in image_paths:
        path = Path(value)
        size = path.stat().st_size
        if size > MAX_VISION_IMAGE_BYTES:
            raise ValueError(f"image exceeds per-image vision limit of {MAX_VISION_IMAGE_BYTES} bytes")
        total_bytes += size
        if total_bytes > MAX_VISION_TOTAL_IMAGE_BYTES:
            raise ValueError(f"vision request exceeds total image limit of {MAX_VISION_TOTAL_IMAGE_BYTES} bytes")
        encoded.append(base64.b64encode(path.read_bytes()).decode("ascii"))
    return encoded
