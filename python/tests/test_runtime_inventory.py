from __future__ import annotations

import json
import os
import subprocess

import pytest

from odysseus_desktop_backend.services import runtime_inventory as ri


# -- hardware ------------------------------------------------------------


def test_hardware_inventory_schema_and_local_values() -> None:
    snapshot = ri.hardware_inventory(gpu_prober=lambda: ([], ""))
    assert snapshot["schema_version"] == ri.HARDWARE_SCHEMA_VERSION
    assert set(snapshot) >= {"os", "cpu", "ram", "gpus", "npu", "storage", "errors", "captured_at_ms"}
    cpu = snapshot["cpu"]
    assert cpu["logical_threads"] >= 1
    assert 1 <= cpu["physical_cores"] <= cpu["logical_threads"]
    assert cpu["physical_cores_source"] in {"measured", "smt_heuristic"}
    assert set(cpu["isa"]) == {"ssse3", "sse4_1", "sse4_2", "avx", "avx2", "avx512f"}
    assert all(isinstance(value, bool) for value in cpu["isa"].values())
    assert snapshot["ram"]["total_bytes"] > 0
    assert 0 < snapshot["ram"]["available_bytes"] <= snapshot["ram"]["total_bytes"]
    assert snapshot["npu"] == "none_detected"
    assert snapshot["storage"]["kind"] == "unknown"


def test_hardware_inventory_contains_no_home_directory_or_username() -> None:
    snapshot = ri.hardware_inventory(gpu_prober=lambda: ([], ""))
    serialized = json.dumps(snapshot)
    home = os.path.expanduser("~")
    assert home not in serialized
    assert home.replace("\\", "/") not in serialized
    username = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    if len(username) >= 2:
        assert username not in serialized


def test_gpu_probe_parses_nvidia_smi_csv() -> None:
    completed = subprocess.CompletedProcess(
        args=["nvidia-smi"],
        returncode=0,
        stdout="NVIDIA GeForce RTX 3050 Laptop GPU, 4096, 3638, 596.36\n",
        stderr="",
    )
    gpus, error = ri._probe_nvidia_gpus(run=lambda argv, **kw: completed, which=lambda name: "nvidia-smi")
    assert error == ""
    assert gpus == [
        {
            "vendor": "nvidia",
            "name": "NVIDIA GeForce RTX 3050 Laptop GPU",
            "vram_total_bytes": 4096 * 1024 * 1024,
            "vram_free_bytes": 3638 * 1024 * 1024,
            "driver_version": "596.36",
            "source": "nvidia-smi",
        }
    ]


def test_gpu_probe_timeout_yields_fixed_category() -> None:
    def raise_timeout(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    gpus, error = ri._probe_nvidia_gpus(run=raise_timeout, which=lambda name: "nvidia-smi")
    assert gpus == []
    assert error == ri.ERROR_PROBE_TIMEOUT


def test_gpu_probe_absent_tool_is_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SystemRoot", r"C:\definitely-missing-root")
    gpus, error = ri._probe_nvidia_gpus(
        run=lambda argv, **kw: pytest.fail("must not run"),
        which=lambda name: None,
    )
    assert gpus == []
    assert error == ""


def test_gpu_probe_nonzero_exit_yields_probe_failed() -> None:
    completed = subprocess.CompletedProcess(args=["nvidia-smi"], returncode=9, stdout="", stderr="boom")
    gpus, error = ri._probe_nvidia_gpus(run=lambda argv, **kw: completed, which=lambda name: "nvidia-smi")
    assert gpus == []
    assert error == ri.ERROR_PROBE_FAILED


# -- redaction -----------------------------------------------------------


def test_redact_local_identifiers_strips_home_and_username(monkeypatch: pytest.MonkeyPatch) -> None:
    home = os.path.expanduser("~")
    message = f"failed to open {home}\\secret\\file.gguf"
    cleaned = ri.redact_local_identifiers(message)
    assert home not in cleaned
    assert "<home>" in cleaned
    monkeypatch.setenv("USERNAME", "SentinelUser")
    assert "SentinelUser" not in ri.redact_local_identifiers("hello SentinelUser goodbye")


# -- runtimes ------------------------------------------------------------


def test_detect_ollama_healthy_via_fakes() -> None:
    status = ri.detect_ollama_runtime(
        get_json=lambda url, **kw: {"version": "0.31.1"},
        which=lambda name: r"C:\fake\ollama.exe",
        tcp_reachable=lambda host, port: True,
    )
    assert status == {
        "name": "ollama",
        "installed": True,
        "reachable": True,
        "healthy": True,
        "version": "0.31.1",
        "endpoint": ri.OLLAMA_ENDPOINT,
        "error_category": "",
    }


def test_detect_ollama_version_timeout_category() -> None:
    def raise_timeout(url, **kw):
        raise TimeoutError("slow")

    status = ri.detect_ollama_runtime(
        get_json=raise_timeout,
        which=lambda name: None,
        tcp_reachable=lambda host, port: True,
    )
    assert status["reachable"] is True
    assert status["healthy"] is False
    assert status["error_category"] == ri.ERROR_PROBE_TIMEOUT


def test_detect_ollama_error_uses_fixed_category_without_detail() -> None:
    def explode(url, **kw):
        raise RuntimeError(r"C:\Users\SomeBody\private\path")

    status = ri.detect_ollama_runtime(
        get_json=explode,
        which=lambda name: None,
        tcp_reachable=lambda host, port: True,
    )
    assert status["error_category"] == ri.ERROR_PROBE_FAILED
    assert "SomeBody" not in json.dumps(status)


def test_detect_llamacpp_parses_version_from_stderr(tmp_path) -> None:
    binary = tmp_path / "llama-server.exe"
    binary.write_bytes(b"fake")
    completed = subprocess.CompletedProcess(
        args=[str(binary)],
        returncode=0,
        stdout="",
        stderr="register_backend: CUDA\nversion: 6543 (abc1234)\n",
    )
    status = ri.detect_llamacpp_runtime(explicit_path=str(binary), run=lambda argv, **kw: completed)
    assert status["installed"] is True
    assert status["healthy"] is True
    assert status["version"] == "6543 (abc1234)"


def test_detect_llamacpp_absent_binary() -> None:
    status = ri.detect_llamacpp_runtime(explicit_path=None, which=lambda name: None)
    assert status == {
        "name": "llamacpp",
        "installed": False,
        "reachable": False,
        "healthy": False,
        "version": "",
        "endpoint": "",
        "error_category": "",
    }


def test_detect_llamacpp_timeout_category(tmp_path) -> None:
    binary = tmp_path / "llama-server.exe"
    binary.write_bytes(b"fake")

    def raise_timeout(argv, **kw):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)

    status = ri.detect_llamacpp_runtime(explicit_path=str(binary), run=raise_timeout)
    assert status["installed"] is True
    assert status["healthy"] is False
    assert status["error_category"] == ri.ERROR_PROBE_TIMEOUT


def test_runtime_inventory_composition_and_privacy() -> None:
    inventory = ri.runtime_inventory(
        detect_ollama=lambda: {
            "name": "ollama",
            "installed": True,
            "reachable": False,
            "healthy": False,
            "version": "",
            "endpoint": ri.OLLAMA_ENDPOINT,
            "error_category": "",
        },
        detect_llamacpp=lambda: {
            "name": "llamacpp",
            "installed": False,
            "reachable": False,
            "healthy": False,
            "version": "",
            "endpoint": "",
            "error_category": "",
        },
    )
    assert inventory["schema_version"] == ri.RUNTIME_SCHEMA_VERSION
    names = [runtime["name"] for runtime in inventory["runtimes"]]
    assert names == ["ollama", "llamacpp", "colibri"]
    serialized = json.dumps(inventory)
    home = os.path.expanduser("~")
    assert home not in serialized


# -- models --------------------------------------------------------------


def _fake_tags(url: str, **kw) -> dict:
    assert url.endswith("/api/tags")
    return {
        "models": [
            {
                "name": "qwen3:8b",
                "digest": "sha256:aaa",
                "size": 5_200_000_000,
                "details": {
                    "format": "gguf",
                    "family": "qwen3",
                    "parameter_size": "8.2B",
                    "quantization_level": "Q4_K_M",
                },
            },
            {
                "name": "llama3.2:1b",
                "digest": "sha256:bbb",
                "size": 1_321_098_329,
                "details": {
                    "format": "gguf",
                    "family": "llama",
                    "parameter_size": "1.2B",
                    "quantization_level": "Q8_0",
                },
            },
            {"not_a_model": True},
        ]
    }


def _fake_show(url: str, payload: dict, **kw) -> dict:
    assert url.endswith("/api/show")
    return {
        "model_info": {
            "llama.context_length": 131072,
            "llama.block_count": 16,
            "llama.attention.head_count": 32,
            "llama.attention.head_count_kv": 8,
            "llama.attention.key_length": 64,
            "llama.attention.value_length": 64,
        },
        "capabilities": ["completion", "Tools"],
    }


def test_model_inventory_parses_and_sorts() -> None:
    inventory = ri.model_inventory(get_json=_fake_tags, post_json=_fake_show)
    assert inventory["schema_version"] == ri.MODEL_SCHEMA_VERSION
    assert inventory["error_category"] == ""
    assert inventory["details_complete"] is True
    tags = [model["tag"] for model in inventory["models"]]
    assert tags == ["llama3.2:1b", "qwen3:8b"]
    first = inventory["models"][0]
    assert first["quantization"] == "Q8_0"
    assert first["disk_bytes"] == 1_321_098_329
    assert first["context_length_native"] == 131072
    assert first["capabilities"] == ["completion", "tools"]
    assert first["kv_geometry"] == {"layers": 16, "kv_heads": 8, "key_length": 64, "value_length": 64}
    assert first["detail_status"] == ri.DETAIL_COMPLETE


def test_extract_kv_geometry_qwen_prefix() -> None:
    geometry = ri.extract_kv_geometry(
        {
            "qwen3.block_count": 36,
            "qwen3.attention.head_count": 32,
            "qwen3.attention.head_count_kv": 8,
            "qwen3.attention.key_length": 128,
            "qwen3.attention.value_length": 128,
        }
    )
    assert geometry == {"layers": 36, "kv_heads": 8, "key_length": 128, "value_length": 128}


def test_extract_kv_geometry_head_dim_fallback() -> None:
    geometry = ri.extract_kv_geometry(
        {
            "llama.block_count": 28,
            "llama.attention.head_count": 24,
            "llama.attention.head_count_kv": 8,
            "llama.embedding_length": 3072,
        }
    )
    assert geometry == {"layers": 28, "kv_heads": 8, "key_length": 128, "value_length": 128}


def test_extract_kv_geometry_partial_metadata_is_none() -> None:
    assert ri.extract_kv_geometry({}) is None
    assert ri.extract_kv_geometry({"llama.block_count": 16}) is None
    assert (
        ri.extract_kv_geometry({"llama.block_count": 16, "llama.attention.head_count_kv": 8}) is None
    )
    assert ri.extract_kv_geometry("not a dict") is None


def test_model_inventory_tags_failure_uses_fixed_category() -> None:
    def explode(url, **kw):
        raise RuntimeError("connection refused at /home/someuser")

    inventory = ri.model_inventory(get_json=explode, post_json=_fake_show)
    assert inventory["models"] == []
    assert inventory["error_category"] == ri.ERROR_PROBE_FAILED
    assert "someuser" not in json.dumps(inventory)


def test_model_inventory_failed_probes_are_not_complete() -> None:
    """Attempted is not complete (review round 2, finding 3): failed
    probes carry a fixed per-model status and the snapshot is partial."""

    def explode(url, payload, **kw):
        raise RuntimeError("show failed")

    inventory = ri.model_inventory(get_json=_fake_tags, post_json=explode)
    assert [model["tag"] for model in inventory["models"]] == ["llama3.2:1b", "qwen3:8b"]
    assert all(model["context_length_native"] == 0 for model in inventory["models"])
    assert all(model["detail_status"] == ri.DETAIL_FAILED for model in inventory["models"])
    assert inventory["details_complete"] is False


def test_model_inventory_probe_cap_marks_incomplete_with_status() -> None:
    many = {
        "models": [
            {"name": f"m{i:02d}:latest", "digest": f"sha256:{i}", "size": 1, "details": {}} for i in range(12)
        ]
    }
    inventory = ri.model_inventory(
        get_json=lambda url, **kw: many,
        post_json=_fake_show,
        max_detail_models=4,
    )
    assert len(inventory["models"]) == 12
    assert inventory["details_complete"] is False
    statuses = [model["detail_status"] for model in inventory["models"]]
    assert statuses[:4] == [ri.DETAIL_COMPLETE] * 4
    assert statuses[4:] == [ri.DETAIL_PROBE_CAP_REACHED] * 8


def test_model_inventory_mixed_outcomes_have_per_model_status() -> None:
    calls = {"count": 0}

    def flaky(url, payload, **kw):
        calls["count"] += 1
        if payload["model"] == "llama3.2:1b":
            return _fake_show(url, payload, **kw)
        raise RuntimeError("boom")

    inventory = ri.model_inventory(get_json=_fake_tags, post_json=flaky)
    by_tag = {model["tag"]: model["detail_status"] for model in inventory["models"]}
    assert by_tag == {"llama3.2:1b": ri.DETAIL_COMPLETE, "qwen3:8b": ri.DETAIL_FAILED}
    assert inventory["details_complete"] is False


def test_model_inventory_details_disabled_is_not_probed_and_incomplete() -> None:
    inventory = ri.model_inventory(
        get_json=_fake_tags,
        post_json=lambda url, payload, **kw: pytest.fail("must not be called"),
        include_details=False,
    )
    assert all(model["detail_status"] == ri.DETAIL_NOT_PROBED for model in inventory["models"])
    assert inventory["details_complete"] is False


