"""Read-only hardware / runtime / installed-model inventory.

Local-only, shell-free where practical (ctypes for CPU/RAM/ISA probes),
bounded subprocesses where unavoidable (nvidia-smi, llama-server
--version). Never downloads anything, never installs anything, never
performs remote discovery, never writes to the database, and never lets
raw filesystem paths or usernames escape into logs or returned error
strings.

See projects/odysseus/LOCAL_RUNTIME_ACCELERATION_RFC.md sections 3-4 for
the schemas this module implements.
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import socket
import subprocess
import urllib.request
from typing import Any, Callable

from odysseus_desktop_backend.logging_config import get_logger
from odysseus_desktop_backend.storage import utc_ms

logger = get_logger("runtime_inventory")

HARDWARE_SCHEMA_VERSION = 1
MODEL_SCHEMA_VERSION = 1
RUNTIME_SCHEMA_VERSION = 1

OLLAMA_ENDPOINT = "http://127.0.0.1:11434"
OLLAMA_HOST = "127.0.0.1"
OLLAMA_PORT = 11434

PROBE_TIMEOUT_SECONDS = 5.0
HTTP_TIMEOUT_SECONDS = 5.0

# Fixed error categories (RFC section 3): nothing else may appear in the
# `error_category` fields this module emits.
ERROR_PROBE_TIMEOUT = "probe_timeout"
ERROR_PROBE_UNAVAILABLE = "probe_unavailable"
ERROR_PROBE_FAILED = "probe_failed"

# Documented IsProcessorFeaturePresent constants, verified live on the
# dev machine (AVX2 true / AVX512F false on Zen 2, as expected).
_PF_FEATURES = {
    "ssse3": 36,
    "sse4_1": 37,
    "sse4_2": 38,
    "avx": 39,
    "avx2": 40,
    "avx512f": 41,
}


def redact_local_identifiers(text: str) -> str:
    """Strip the local username and home directory from a string.

    Belt-and-braces: inventory code should never put paths into messages
    in the first place; this guards library exception text.
    """
    clean = str(text or "")
    home = os.path.expanduser("~")
    if home and home not in ("~", "/"):
        clean = clean.replace(home, "<home>")
        clean = clean.replace(home.replace("\\", "\\\\"), "<home>")
        clean = clean.replace(home.replace("\\", "/"), "<home>")
    for env_key in ("USERNAME", "USER"):
        username = os.environ.get(env_key) or ""
        if len(username) >= 2:
            clean = clean.replace(username, "<user>")
    return clean


# -- hardware ------------------------------------------------------------


def _windows_memory_status() -> tuple[int, int]:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_uint32),
            ("dwMemoryLoad", ctypes.c_uint32),
            ("ullTotalPhys", ctypes.c_uint64),
            ("ullAvailPhys", ctypes.c_uint64),
            ("ullTotalPageFile", ctypes.c_uint64),
            ("ullAvailPageFile", ctypes.c_uint64),
            ("ullTotalVirtual", ctypes.c_uint64),
            ("ullAvailVirtual", ctypes.c_uint64),
            ("ullAvailExtendedVirtual", ctypes.c_uint64),
        ]

    status = MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("GlobalMemoryStatusEx failed")
    return int(status.ullTotalPhys), int(status.ullAvailPhys)


def _windows_physical_cores() -> int:
    class SLPI(ctypes.Structure):
        class _U(ctypes.Union):
            _fields_ = [("Flags", ctypes.c_byte), ("Reserved", ctypes.c_uint64 * 2)]

        _fields_ = [
            ("ProcessorMask", ctypes.c_size_t),
            ("Relationship", ctypes.c_int),
            ("u", _U),
        ]

    kernel32 = ctypes.windll.kernel32
    needed = ctypes.c_uint32(0)
    kernel32.GetLogicalProcessorInformation(None, ctypes.byref(needed))
    count = needed.value // ctypes.sizeof(SLPI)
    if count <= 0:
        raise OSError("GetLogicalProcessorInformation sizing failed")
    buffer = (SLPI * count)()
    if not kernel32.GetLogicalProcessorInformation(buffer, ctypes.byref(needed)):
        raise OSError("GetLogicalProcessorInformation failed")
    relation_processor_core = 0
    cores = sum(1 for item in buffer if item.Relationship == relation_processor_core)
    if cores <= 0:
        raise OSError("no processor cores reported")
    return cores


def _windows_isa_features() -> dict[str, bool]:
    kernel32 = ctypes.windll.kernel32
    return {name: bool(kernel32.IsProcessorFeaturePresent(const)) for name, const in _PF_FEATURES.items()}


def _run_bounded(argv: list[str], timeout: float = PROBE_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    """Run a probe subprocess: argv only, no shell, hard timeout."""
    return subprocess.run(  # noqa: S603 - argv array, shell=False, bounded
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )


def _nvidia_smi_path(which: Callable[[str], str | None] = shutil.which) -> str | None:
    found = which("nvidia-smi")
    if found:
        return found
    default = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe")
    return default if os.path.isfile(default) else None


def _probe_nvidia_gpus(
    run: Callable[..., subprocess.CompletedProcess[str]] = _run_bounded,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[list[dict[str, Any]], str]:
    """Return (gpus, error_category). Absent tooling is not an error."""
    smi = _nvidia_smi_path(which)
    if smi is None:
        return [], ""
    try:
        proc = run(
            [
                smi,
                "--query-gpu=name,memory.total,memory.free,driver_version",
                "--format=csv,noheader,nounits",
            ]
        )
    except subprocess.TimeoutExpired:
        return [], ERROR_PROBE_TIMEOUT
    except OSError:
        return [], ERROR_PROBE_UNAVAILABLE
    if proc.returncode != 0:
        return [], ERROR_PROBE_FAILED
    gpus: list[dict[str, Any]] = []
    for line in (proc.stdout or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 4 or not parts[0]:
            continue
        try:
            total_mib = float(parts[1])
            free_mib = float(parts[2])
        except ValueError:
            continue
        gpus.append(
            {
                "vendor": "nvidia",
                "name": parts[0],
                "vram_total_bytes": int(total_mib * 1024 * 1024),
                "vram_free_bytes": int(free_mib * 1024 * 1024),
                "driver_version": parts[3],
                "source": "nvidia-smi",
            }
        )
    return gpus, ""


def hardware_inventory(
    *,
    profile_dir: str | None = None,
    model_store_dir: str | None = None,
    gpu_prober: Callable[[], tuple[list[dict[str, Any]], str]] | None = None,
) -> dict[str, Any]:
    """Snapshot the local hardware. Read-only; partial results on failure."""
    errors: dict[str, str] = {}

    logical = os.cpu_count() or 0
    physical = 0
    physical_source = "smt_heuristic"
    try:
        physical = _windows_physical_cores()
        physical_source = "measured"
    except Exception:  # noqa: BLE001 - fixed-category degradation
        physical = max(1, logical // 2) if logical else 0
        errors["cpu_physical_cores"] = ERROR_PROBE_FAILED

    try:
        isa = _windows_isa_features()
    except Exception:  # noqa: BLE001
        isa = {name: False for name in _PF_FEATURES}
        errors["cpu_isa"] = ERROR_PROBE_FAILED

    ram_total = ram_available = 0
    try:
        ram_total, ram_available = _windows_memory_status()
    except Exception:  # noqa: BLE001
        errors["ram"] = ERROR_PROBE_FAILED

    gpus, gpu_error = (gpu_prober or _probe_nvidia_gpus)()
    if gpu_error:
        errors["gpu"] = gpu_error

    storage: dict[str, Any] = {
        "profile_disk_free_bytes": None,
        "model_store_disk_free_bytes": None,
        "kind": "unknown",
    }
    for key, target in (
        ("profile_disk_free_bytes", profile_dir),
        ("model_store_disk_free_bytes", model_store_dir or default_ollama_model_store()),
    ):
        if not target:
            continue
        try:
            storage[key] = int(shutil.disk_usage(target).free)
        except OSError:
            errors[f"storage_{key}"] = ERROR_PROBE_UNAVAILABLE

    snapshot = {
        "schema_version": HARDWARE_SCHEMA_VERSION,
        "os": {
            "name": platform.system(),
            "version": platform.version(),
            "arch": platform.machine(),
        },
        "cpu": {
            "logical_threads": logical,
            "physical_cores": physical,
            "physical_cores_source": physical_source,
            "isa": isa,
        },
        "ram": {"total_bytes": ram_total, "available_bytes": ram_available},
        "gpus": gpus,
        "npu": "none_detected",
        "storage": storage,
        "errors": errors,
        "captured_at_ms": utc_ms(),
    }
    logger.info(
        "hardware inventory cores=%s/%s ram_total=%s gpus=%s errors=%s",
        physical,
        logical,
        ram_total,
        len(gpus),
        sorted(errors),
    )
    return snapshot


def default_ollama_model_store() -> str | None:
    override = os.environ.get("OLLAMA_MODELS")
    if override:
        return override
    home = os.path.expanduser("~")
    candidate = os.path.join(home, ".ollama", "models")
    return candidate if os.path.isdir(candidate) else None


# -- runtimes ------------------------------------------------------------


def _default_get_json(url: str, timeout: float = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def _tcp_reachable(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_ollama_binary(which: Callable[[str], str | None] = shutil.which) -> str | None:
    found = which("ollama")
    if found:
        return found
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        candidate = os.path.join(local_app_data, "Programs", "Ollama", "ollama.exe")
        if os.path.isfile(candidate):
            return candidate
    return None


def detect_ollama_runtime(
    *,
    get_json: Callable[..., dict[str, Any]] = _default_get_json,
    which: Callable[[str], str | None] = shutil.which,
    tcp_reachable: Callable[[str, int], bool] = _tcp_reachable,
) -> dict[str, Any]:
    installed = _default_ollama_binary(which) is not None
    reachable = tcp_reachable(OLLAMA_HOST, OLLAMA_PORT)
    version = ""
    healthy = False
    error_category = ""
    if reachable:
        try:
            version = str(get_json(f"{OLLAMA_ENDPOINT}/api/version").get("version") or "")
            healthy = bool(version)
        except TimeoutError:
            error_category = ERROR_PROBE_TIMEOUT
        except Exception:  # noqa: BLE001 - fixed category, no raw text
            error_category = ERROR_PROBE_FAILED
    return {
        "name": "ollama",
        "installed": installed,
        "reachable": reachable,
        "healthy": healthy,
        "version": version,
        "endpoint": OLLAMA_ENDPOINT,
        "error_category": error_category,
    }


def detect_llamacpp_runtime(
    *,
    explicit_path: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[..., subprocess.CompletedProcess[str]] = _run_bounded,
) -> dict[str, Any]:
    """Detect a llama.cpp `llama-server` binary. Never launches a server."""
    binary = None
    if explicit_path and os.path.isfile(explicit_path):
        binary = explicit_path
    if binary is None:
        binary = which("llama-server")
    installed = binary is not None
    version = ""
    error_category = ""
    if installed:
        try:
            proc = run([binary, "--version"])
            # llama.cpp prints "version: NNNN (sha)" on stderr.
            for stream in (proc.stderr or "", proc.stdout or ""):
                for line in stream.splitlines():
                    if line.strip().lower().startswith("version:"):
                        version = line.split(":", 1)[1].strip()
                        break
                if version:
                    break
            if not version and proc.returncode != 0:
                error_category = ERROR_PROBE_FAILED
        except subprocess.TimeoutExpired:
            error_category = ERROR_PROBE_TIMEOUT
        except OSError:
            error_category = ERROR_PROBE_UNAVAILABLE
    return {
        "name": "llamacpp",
        "installed": installed,
        "reachable": False,
        "healthy": bool(version),
        "version": version,
        "endpoint": "",
        "error_category": error_category,
    }


def runtime_inventory(
    *,
    llamacpp_path: str | None = None,
    colibri_cli_configured: bool = False,
    detect_ollama: Callable[[], dict[str, Any]] | None = None,
    detect_llamacpp: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    runtimes = [
        (detect_ollama or detect_ollama_runtime)(),
        (detect_llamacpp or (lambda: detect_llamacpp_runtime(explicit_path=llamacpp_path)))(),
        {
            "name": "colibri",
            "installed": colibri_cli_configured,
            "reachable": False,
            "healthy": False,
            "version": "",
            "endpoint": "",
            "error_category": "",
            "note": "deep_local.status is the authoritative Colibri probe",
        },
    ]
    logger.info(
        "runtime inventory %s",
        [(item["name"], item["installed"], item["healthy"]) for item in runtimes],
    )
    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "runtimes": runtimes,
        "captured_at_ms": utc_ms(),
    }


# -- installed models ----------------------------------------------------


def _default_post_json(url: str, payload: dict[str, Any], timeout: float = HTTP_TIMEOUT_SECONDS) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - loopback only
        return json.loads(response.read().decode("utf-8"))


def model_inventory(
    *,
    get_json: Callable[..., dict[str, Any]] = _default_get_json,
    post_json: Callable[..., dict[str, Any]] = _default_post_json,
    include_details: bool = True,
    max_detail_models: int = 32,
) -> dict[str, Any]:
    """List installed Ollama models with sizes/quantization/context.

    Read-only over the local API. `context_length_native` needs one
    `/api/show` per model, bounded by `max_detail_models`.
    """
    models: list[dict[str, Any]] = []
    error_category = ""
    try:
        tags = get_json(f"{OLLAMA_ENDPOINT}/api/tags")
    except TimeoutError:
        tags = {}
        error_category = ERROR_PROBE_TIMEOUT
    except Exception:  # noqa: BLE001
        tags = {}
        error_category = ERROR_PROBE_FAILED

    for item in tags.get("models", []) if isinstance(tags, dict) else []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        models.append(
            {
                "tag": str(item["name"]),
                "digest": str(item.get("digest") or ""),
                "format": str(details.get("format") or ""),
                "family": str(details.get("family") or ""),
                "parameter_size": str(details.get("parameter_size") or ""),
                "quantization": str(details.get("quantization_level") or ""),
                "disk_bytes": int(item.get("size") or 0),
                "context_length_native": 0,
                "capabilities": [],
            }
        )

    if include_details:
        for entry in models[:max_detail_models]:
            try:
                data = post_json(f"{OLLAMA_ENDPOINT}/api/show", {"model": entry["tag"]})
            except Exception:  # noqa: BLE001 - detail probes are best-effort
                continue
            model_info = data.get("model_info") if isinstance(data.get("model_info"), dict) else {}
            context = 0
            for key, value in model_info.items():
                if str(key).endswith(".context_length"):
                    try:
                        context = int(value)
                    except (TypeError, ValueError):
                        context = 0
                    break
            entry["context_length_native"] = context
            entry["capabilities"] = [
                str(cap).lower() for cap in data.get("capabilities") or [] if isinstance(cap, str)
            ]

    logger.info("model inventory models=%s error=%s", len(models), error_category)
    return {
        "schema_version": MODEL_SCHEMA_VERSION,
        "runtime": "ollama",
        "models": sorted(models, key=lambda entry: entry["tag"]),
        "error_category": error_category,
        "captured_at_ms": utc_ms(),
    }
