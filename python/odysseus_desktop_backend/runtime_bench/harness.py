"""Benchmark run logic for Ollama and llama.cpp-server backends.

Stdlib-only HTTP against loopback endpoints. The harness controls model
residency (cold vs warm) through the runtime's own API, samples the
runtime process tree while a run is in flight, and emits schema-valid,
redaction-checked artifacts. Model outputs stay in memory: only the
quality-check verdict is persisted.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from odysseus_desktop_backend.runtime_bench.artifacts import (
    ARTIFACT_SCHEMA_VERSION,
    write_artifact,
)
from odysseus_desktop_backend.runtime_bench.sampler import ResourceSampler
from odysseus_desktop_backend.runtime_bench.shapes import BENCHMARK_SHAPES, quality_check
from odysseus_desktop_backend.services import runtime_inventory as ri

OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 300.0

ERROR_TIMEOUT = "timeout"
ERROR_CONNECTION = "connection_failure"
ERROR_HTTP = "http_error"
ERROR_MALFORMED = "malformed_response"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post_stream(url: str, payload: dict[str, Any], timeout: float):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - loopback only


def ollama_unload(model: str, *, endpoint: str = OLLAMA_ENDPOINT, timeout: float = 30.0) -> bool:
    """Ask the server to unload a model (keep_alive=0), then verify."""
    try:
        with _post_stream(f"{endpoint}/api/generate", {"model": model, "keep_alive": 0}, timeout) as resp:
            resp.read()
    except (urllib.error.URLError, OSError, TimeoutError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not ollama_resident_models(endpoint=endpoint):
            return True
        time.sleep(0.5)
    return False


def ollama_resident_models(*, endpoint: str = OLLAMA_ENDPOINT) -> list[dict[str, Any]]:
    try:
        req = urllib.request.Request(f"{endpoint}/api/ps", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, json.JSONDecodeError):
        return []
    return [item for item in data.get("models", []) if isinstance(item, dict)]


def run_ollama_shape(
    *,
    model: str,
    shape: str,
    options: dict[str, Any] | None = None,
    keep_alive: str | int | None = None,
    think: bool | None = None,
    endpoint: str = OLLAMA_ENDPOINT,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    run_index: int = 0,
    cold: bool = False,
    sample_vram: bool = True,
) -> dict[str, Any]:
    """Execute one shape once against Ollama; return a run record."""
    spec = BENCHMARK_SHAPES[shape]
    clean_options: dict[str, Any] = {"temperature": 0, "seed": 42, "num_predict": spec["num_predict"]}
    clean_options.update(options or {})
    payload: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "user", "content": spec["prompt"]}],
        "stream": True,
        "options": clean_options,
    }
    if keep_alive is not None:
        payload["keep_alive"] = keep_alive
    if think is not None:
        payload["think"] = think

    # "llama" matches ollama.exe, "ollama app.exe", AND Ollama's runner
    # subprocess llama-server.exe (which carries the model weights).
    smi = ri._nvidia_smi_path() if sample_vram else None
    recorded_options = dict(clean_options)
    if think is not None:
        recorded_options["think"] = think
    record: dict[str, Any] = {
        "run_index": run_index,
        "cold": cold,
        "options": recorded_options,
        "timings_ms": {"total": None, "load": None, "prompt_eval": None, "generation": None, "first_token": None},
        "tokens": {"prompt": 0, "generated": 0, "prompt_tps": None, "generation_tps": None},
        "memory": {},
        "quality_check": "not_applicable",
        "error_category": "",
    }

    content_parts: list[str] = []
    first_token_ms: float | None = None
    final_stats: dict[str, Any] = {}
    started = time.perf_counter()
    with ResourceSampler(exe_substring="llama", smi_path=smi) as sampler:
        try:
            with _post_stream(f"{endpoint}/api/chat", payload, timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        record["error_category"] = ERROR_MALFORMED
                        break
                    message = chunk.get("message") or {}
                    piece = message.get("content") or ""
                    if piece and first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    if piece:
                        content_parts.append(piece)
                    if chunk.get("done"):
                        final_stats = chunk
                        break
        except TimeoutError:
            record["error_category"] = ERROR_TIMEOUT
        except urllib.error.HTTPError:
            record["error_category"] = ERROR_HTTP
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            record["error_category"] = ERROR_TIMEOUT if isinstance(reason, TimeoutError) else ERROR_CONNECTION
    total_ms = (time.perf_counter() - started) * 1000

    record["memory"] = sampler.to_dict()
    record["timings_ms"]["total"] = round(total_ms, 1)
    record["timings_ms"]["first_token"] = round(first_token_ms, 1) if first_token_ms is not None else None
    if final_stats:
        record["timings_ms"]["load"] = round(int(final_stats.get("load_duration") or 0) / 1e6, 1)
        record["timings_ms"]["prompt_eval"] = round(int(final_stats.get("prompt_eval_duration") or 0) / 1e6, 1)
        record["timings_ms"]["generation"] = round(int(final_stats.get("eval_duration") or 0) / 1e6, 1)
        prompt_tokens = int(final_stats.get("prompt_eval_count") or 0)
        generated = int(final_stats.get("eval_count") or 0)
        record["tokens"]["prompt"] = prompt_tokens
        record["tokens"]["generated"] = generated
        prompt_ns = int(final_stats.get("prompt_eval_duration") or 0)
        eval_ns = int(final_stats.get("eval_duration") or 0)
        if prompt_tokens and prompt_ns:
            record["tokens"]["prompt_tps"] = round(prompt_tokens / (prompt_ns / 1e9), 2)
        if generated and eval_ns:
            record["tokens"]["generation_tps"] = round(generated / (eval_ns / 1e9), 2)
    elif not record["error_category"]:
        record["error_category"] = ERROR_MALFORMED

    if not record["error_category"]:
        record["quality_check"] = quality_check(shape, "".join(content_parts))
    residency = ollama_resident_models(endpoint=endpoint)
    for item in residency:
        if str(item.get("name") or item.get("model") or "") == model:
            size = int(item.get("size") or 0)
            size_vram = int(item.get("size_vram") or 0)
            record["residency"] = {
                "size_bytes": size,
                "size_vram_bytes": size_vram,
                "gpu_fraction": round(size_vram / size, 3) if size else None,
                "context_length": int(item.get("context_length") or 0),
            }
            break
    return record


def run_llamacpp_shape(
    *,
    endpoint: str,
    shape: str,
    n_predict: int | None = None,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    run_index: int = 0,
    cold: bool = False,
    exe_substring: str = "llama-server",
    sample_vram: bool = True,
) -> dict[str, Any]:
    """Execute one shape against a llama.cpp `llama-server` /completion API.

    Launch-time flags (threads, offload, cache type, flash attention) are
    properties of the server the caller started; they are recorded by the
    caller in the artifact's runtime block, not here.
    """
    spec = BENCHMARK_SHAPES[shape]
    payload = {
        "prompt": spec["prompt"],
        "n_predict": n_predict if n_predict is not None else spec["num_predict"],
        "temperature": 0,
        "seed": 42,
        "stream": True,
        "cache_prompt": True,
    }
    record: dict[str, Any] = {
        "run_index": run_index,
        "cold": cold,
        "options": {"n_predict": payload["n_predict"], "temperature": 0, "seed": 42},
        "timings_ms": {"total": None, "load": None, "prompt_eval": None, "generation": None, "first_token": None},
        "tokens": {"prompt": 0, "generated": 0, "prompt_tps": None, "generation_tps": None},
        "memory": {},
        "quality_check": "not_applicable",
        "error_category": "",
    }
    smi = ri._nvidia_smi_path() if sample_vram else None
    content_parts: list[str] = []
    first_token_ms: float | None = None
    final_stats: dict[str, Any] = {}
    started = time.perf_counter()
    with ResourceSampler(exe_substring=exe_substring, smi_path=smi) as sampler:
        try:
            with _post_stream(f"{endpoint}/completion", payload, timeout) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data: "):
                        continue
                    try:
                        chunk = json.loads(line[len("data: "):])
                    except json.JSONDecodeError:
                        record["error_category"] = ERROR_MALFORMED
                        break
                    piece = chunk.get("content") or ""
                    if piece and first_token_ms is None:
                        first_token_ms = (time.perf_counter() - started) * 1000
                    if piece:
                        content_parts.append(piece)
                    if chunk.get("stop"):
                        final_stats = chunk
                        break
        except TimeoutError:
            record["error_category"] = ERROR_TIMEOUT
        except urllib.error.HTTPError:
            record["error_category"] = ERROR_HTTP
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", None)
            record["error_category"] = ERROR_TIMEOUT if isinstance(reason, TimeoutError) else ERROR_CONNECTION
    total_ms = (time.perf_counter() - started) * 1000

    record["memory"] = sampler.to_dict()
    record["timings_ms"]["total"] = round(total_ms, 1)
    record["timings_ms"]["first_token"] = round(first_token_ms, 1) if first_token_ms is not None else None
    timings = final_stats.get("timings") if isinstance(final_stats.get("timings"), dict) else {}
    if timings:
        record["timings_ms"]["prompt_eval"] = round(float(timings.get("prompt_ms") or 0), 1)
        record["timings_ms"]["generation"] = round(float(timings.get("predicted_ms") or 0), 1)
        prompt_tokens = int(timings.get("prompt_n") or 0)
        generated = int(timings.get("predicted_n") or 0)
        record["tokens"]["prompt"] = prompt_tokens
        record["tokens"]["generated"] = generated
        if prompt_tokens and timings.get("prompt_ms"):
            record["tokens"]["prompt_tps"] = round(prompt_tokens / (float(timings["prompt_ms"]) / 1000), 2)
        if generated and timings.get("predicted_ms"):
            record["tokens"]["generation_tps"] = round(generated / (float(timings["predicted_ms"]) / 1000), 2)
    elif not record["error_category"]:
        record["error_category"] = ERROR_MALFORMED
    if not record["error_category"]:
        record["quality_check"] = quality_check(shape, "".join(content_parts))
    return record


def build_artifact(
    *,
    batch_id: str,
    runtime_name: str,
    runtime_version: str,
    server_env: dict[str, str] | None,
    model: dict[str, Any],
    shape: str,
    runs: list[dict[str, Any]],
    mode: str = "interactive",
    engine_kind: str = "real",
) -> dict[str, Any]:
    hardware = ri.hardware_inventory()
    hardware.pop("errors", None)
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "batch_id": batch_id,
        "captured_at": _now_iso(),
        "hardware": hardware,
        "runtime": {
            "name": runtime_name,
            "version": runtime_version,
            "server_env": dict(server_env or {}),
        },
        "model": dict(model),
        "shape": shape,
        "mode": mode,
        "engine_kind": engine_kind,
        "file_cache_state": "unknown_warmish",
        "runs": runs,
    }


def model_descriptor(tag: str) -> dict[str, Any]:
    """Look up a model's identity from the live inventory."""
    inventory = ri.model_inventory(include_details=False)
    for entry in inventory["models"]:
        if entry["tag"] == tag:
            return {
                "tag": tag,
                "format": entry["format"],
                "quantization": entry["quantization"],
                "parameter_size": entry["parameter_size"],
                "disk_bytes": entry["disk_bytes"],
            }
    return {"tag": tag, "format": "", "quantization": "", "parameter_size": "", "disk_bytes": 0}


def run_ollama_batch(
    *,
    model: str,
    shape: str,
    batch_id: str,
    artifact_dir: str,
    options: dict[str, Any] | None = None,
    keep_alive: str | int | None = None,
    think: bool | None = None,
    server_env: dict[str, str] | None = None,
    repeats: int = 3,
    include_cold: bool = True,
    timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one model/shape cell: optional cold run + N warm runs; write artifact."""
    version = ri.detect_ollama_runtime()["version"]
    runs: list[dict[str, Any]] = []
    run_index = 0
    if include_cold:
        ollama_unload(model)
        runs.append(
            run_ollama_shape(
                model=model,
                shape=shape,
                options=options,
                keep_alive=keep_alive,
                think=think,
                timeout=timeout,
                run_index=run_index,
                cold=True,
            )
        )
        run_index += 1
    for _ in range(repeats):
        runs.append(
            run_ollama_shape(
                model=model,
                shape=shape,
                options=options,
                keep_alive=keep_alive,
                think=think,
                timeout=timeout,
                run_index=run_index,
                cold=False,
            )
        )
        run_index += 1
    artifact = build_artifact(
        batch_id=batch_id,
        runtime_name="ollama",
        runtime_version=version,
        server_env=server_env,
        model=model_descriptor(model),
        shape=shape,
        runs=runs,
    )
    write_artifact(artifact, artifact_dir)
    return artifact
