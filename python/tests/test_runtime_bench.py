from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from odysseus_desktop_backend.runtime_bench import (
    ARTIFACT_SCHEMA_VERSION,
    BENCHMARK_SHAPES,
    capability,
    quality_check,
    redaction_violations,
    runtime_capability_matrix,
    validate_artifact,
    write_artifact,
)
from odysseus_desktop_backend.runtime_bench import harness
from odysseus_desktop_backend.runtime_bench.shapes import (
    LONG_CONTEXT_CODEWORD,
    TINY_TOKEN,
)


def _minimal_run(**overrides) -> dict:
    run = {
        "run_index": 0,
        "cold": False,
        "options": {"temperature": 0},
        "timings_ms": {"total": 10.0, "load": 1.0, "prompt_eval": 2.0, "generation": 3.0, "first_token": 4.0},
        "tokens": {"prompt": 10, "generated": 5, "prompt_tps": 100.0, "generation_tps": 50.0},
        "memory": {"runtime_peak_rss_bytes": 1, "system_min_available_bytes": 1, "vram_peak_used_bytes": None, "sampler_interval_ms": 250},
        "quality_check": "passed",
        "error_category": "",
    }
    run.update(overrides)
    return run


def _minimal_artifact(**overrides) -> dict:
    artifact = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "batch_id": "test-batch",
        "captured_at": "2026-07-17T00:00:00Z",
        "hardware": {"cpu": {"logical_threads": 12}, "ram": {"total_bytes": 1}},
        "runtime": {"name": "ollama", "version": "0.31.1", "server_env": {}},
        "model": {"tag": "llama3.2:1b", "quantization": "Q8_0"},
        "shape": "tiny",
        "mode": "interactive",
        "engine_kind": "real",
        "runs": [_minimal_run()],
    }
    artifact.update(overrides)
    return artifact


# -- schema --------------------------------------------------------------


def test_artifact_schema_valid_case() -> None:
    assert validate_artifact(_minimal_artifact()) == []


def test_artifact_schema_rejects_missing_keys() -> None:
    bad = _minimal_artifact()
    del bad["hardware"]
    assert any("missing top-level" in problem for problem in validate_artifact(bad))


def test_artifact_schema_rejects_bad_shape_and_engine_kind() -> None:
    problems = validate_artifact(_minimal_artifact(shape="giant", engine_kind="imaginary"))
    assert any("invalid shape" in problem for problem in problems)
    assert any("invalid engine_kind" in problem for problem in problems)


def test_artifact_schema_rejects_non_allowlisted_server_env() -> None:
    artifact = _minimal_artifact()
    artifact["runtime"]["server_env"] = {"OLLAMA_API_KEY": "secret"}
    assert any("not allow-listed" in problem for problem in validate_artifact(artifact))


def test_artifact_schema_rejects_embedded_prompt_or_output() -> None:
    artifact = _minimal_artifact(runs=[_minimal_run(prompt="hello"), _minimal_run(output="world")])
    problems = validate_artifact(artifact)
    assert any("run 0 keys not allow-listed" in problem for problem in problems)
    assert any("run 1 keys not allow-listed" in problem for problem in problems)


def test_artifact_schema_rejects_passed_quality_with_error() -> None:
    artifact = _minimal_artifact(runs=[_minimal_run(error_category="timeout")])
    assert any("cannot both pass" in problem for problem in validate_artifact(artifact))


def test_failed_run_is_recorded_not_skipped() -> None:
    artifact = _minimal_artifact(
        runs=[_minimal_run(quality_check="not_applicable", error_category="timeout")]
    )
    assert validate_artifact(artifact) == []


def test_cold_flag_must_be_boolean() -> None:
    artifact = _minimal_artifact(runs=[_minimal_run(cold="yes")])
    assert any("cold must be boolean" in problem for problem in validate_artifact(artifact))


# -- redaction -----------------------------------------------------------


def test_redaction_sentinel_flags_home_directory() -> None:
    home = os.path.expanduser("~")
    assert redaction_violations(f"data at {home}\\models") != []


def test_redaction_sentinel_flags_windows_user_paths() -> None:
    assert redaction_violations(r"C:\\Users\\SomeoneElse\\secret.gguf") != []
    assert redaction_violations(r"C:\Users\Other\file") != []


def test_redaction_sentinel_accepts_clean_payload() -> None:
    payload = json.dumps(_minimal_artifact())
    assert redaction_violations(payload) == []


def test_write_artifact_refuses_home_directory_content(tmp_path) -> None:
    artifact = _minimal_artifact(batch_id="bad-batch")
    artifact["model"]["format"] = "model at " + os.path.expanduser("~")
    # Rejected at the schema layer (forbidden separators) or the
    # redaction layer (home directory) — either way nothing is written.
    with pytest.raises(ValueError, match="forbidden|redaction"):
        write_artifact(artifact, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_write_artifact_round_trip(tmp_path) -> None:
    path = write_artifact(_minimal_artifact(), tmp_path)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["batch_id"] == "test-batch"
    assert validate_artifact(loaded) == []


# -- hostile inputs (review finding 7 / brief item 8) --------------------


def test_batch_id_traversal_is_rejected(tmp_path) -> None:
    artifact = _minimal_artifact(batch_id="../../escape")
    with pytest.raises(ValueError, match="schema invalid"):
        write_artifact(artifact, tmp_path)
    assert list(tmp_path.parent.glob("escape*")) == []


@pytest.mark.parametrize(
    "bad_id",
    ["../../escape", "..", "a/b", "a\\b", "C:evil", "UPPER", "sp ace", "a" * 120, "", ".hidden".upper()],
)
def test_unsafe_batch_ids_rejected(bad_id) -> None:
    problems = validate_artifact(_minimal_artifact(batch_id=bad_id))
    assert any("batch_id" in problem for problem in problems), bad_id


def test_write_artifact_target_must_stay_inside_directory(tmp_path) -> None:
    # Even if validation were bypassed, the writer re-proves containment.
    artifact = _minimal_artifact()
    artifact["batch_id"] = "ok-slug"
    path = write_artifact(artifact, tmp_path)
    assert path.parent == tmp_path.resolve()


@pytest.mark.parametrize(
    "hostile",
    [
        r"D:\private\models\x.gguf",
        r"\\fileserver\share\model.gguf",
        "/home/user/model.gguf",
        "/Users/someone/Documents/x",
        "E:/other/drive/path",
        "/opt/private/model.gguf",
        "/srv/models/x.gguf",
        "/data/user/x",
        "/custom-root/nested/file.bin",
        "/secret",
        "/opt/private model/x.gguf",
        "/データ/秘密/モデル.gguf",
    ],
)
def test_pathlike_content_rejected_in_any_string_field(hostile, tmp_path) -> None:
    artifact = _minimal_artifact()
    artifact["model"]["format"] = f"loaded from {hostile}"
    problems = validate_artifact(artifact)
    assert any("forbidden characters" in problem for problem in problems), hostile
    with pytest.raises(ValueError):
        write_artifact(artifact, tmp_path)


def test_pathlike_content_rejected_in_model_tag(tmp_path) -> None:
    artifact = _minimal_artifact()
    artifact["model"]["tag"] = r"D:\private\models\x.gguf"
    assert any("forbidden characters" in problem for problem in validate_artifact(artifact))
    with pytest.raises(ValueError):
        write_artifact(artifact, tmp_path)


def test_pathlike_and_illegal_server_env_values_rejected(tmp_path) -> None:
    artifact = _minimal_artifact()
    artifact["runtime"]["server_env"] = {"OLLAMA_KV_CACHE_TYPE": r"\\unc\share\x"}
    problems = validate_artifact(artifact)
    assert any("server_env value" in problem or "forbidden" in problem for problem in problems)
    artifact2 = _minimal_artifact()
    artifact2["runtime"]["server_env"] = {"OLLAMA_KEEP_ALIVE": "5m; rm -rf ~ && echo " + "x" * 40}
    assert any("server_env value" in problem for problem in validate_artifact(artifact2))


def test_notes_field_is_no_longer_accepted() -> None:
    artifact = _minimal_artifact()
    artifact["notes"] = "free-form provenance text"
    assert any("top-level keys not allow-listed" in problem for problem in validate_artifact(artifact))


def test_top_level_prompt_output_secret_rejected() -> None:
    """The schema is closed: arbitrary top-level keys can never ride
    along (review round 3, finding 4)."""
    for key in ("prompt", "output", "secret", "user_document"):
        artifact = _minimal_artifact()
        artifact[key] = "private text that must not be storable"
        problems = validate_artifact(artifact)
        assert any("top-level keys not allow-listed" in problem for problem in problems), key


def test_run_level_extras_rejected() -> None:
    for key in ("prompt", "output", "content", "messages", "secret"):
        artifact = _minimal_artifact(runs=[_minimal_run(**{key: "x"})])
        problems = validate_artifact(artifact)
        assert any(f"run 0 keys not allow-listed" in problem for problem in problems), key


def test_unknown_nested_keys_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["model"]["private_path"] = "x"
    assert any("model keys" in problem for problem in validate_artifact(artifact))
    artifact2 = _minimal_artifact()
    artifact2["runtime"]["command_line"] = "x"
    assert any("runtime keys" in problem for problem in validate_artifact(artifact2))
    artifact3 = _minimal_artifact()
    artifact3["hardware"]["hostname"] = "x"
    assert any("hardware keys" in problem for problem in validate_artifact(artifact3))


def test_nested_hardware_and_run_extras_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["hardware"]["cpu"]["serial_number"] = "x"
    assert any("hardware.cpu keys" in problem for problem in validate_artifact(artifact))
    artifact2 = _minimal_artifact()
    artifact2["runs"][0]["memory"]["heap_dump"] = "x"
    assert any("run 0 memory keys" in problem for problem in validate_artifact(artifact2))
    artifact3 = _minimal_artifact()
    artifact3["runs"][0]["options"]["system_prompt"] = "x"
    assert any("run 0 options keys" in problem for problem in validate_artifact(artifact3))


# -- numeric type/range validation (review round 5) ----------------------


def test_string_in_numeric_model_field_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["model"]["disk_bytes"] = "not-a-number"
    assert any("model.disk_bytes must be a finite number" in problem for problem in validate_artifact(artifact))


def test_string_in_tps_field_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["runs"][0]["tokens"]["generation_tps"] = "secret"
    problems = validate_artifact(artifact)
    assert any("tokens.generation_tps must be a finite number" in problem for problem in problems)


def test_nan_and_infinity_rejected() -> None:
    for bad in (float("nan"), float("inf"), float("-inf")):
        artifact = _minimal_artifact()
        artifact["runs"][0]["timings_ms"]["total"] = bad
        problems = validate_artifact(artifact)
        assert any("timings_ms.total must be a finite number" in problem for problem in problems), bad


def test_negative_values_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["hardware"]["ram"] = {"total_bytes": -1, "available_bytes": 4}
    assert any("ram.total_bytes must be >= 0" in problem for problem in validate_artifact(artifact))
    artifact2 = _minimal_artifact()
    artifact2["runs"][0]["tokens"]["generated"] = -5
    assert any("tokens.generated must be >= 0" in problem for problem in validate_artifact(artifact2))
    artifact3 = _minimal_artifact()
    artifact3["runs"][0]["residency"] = {
        "size_bytes": -100,
        "size_vram_bytes": 0,
        "gpu_fraction": 0.5,
        "context_length": 4096,
    }
    assert any("residency.size_bytes must be >= 0" in problem for problem in validate_artifact(artifact3))


def test_gpu_fraction_outside_unit_interval_rejected() -> None:
    for bad in (-0.1, 1.5, 99):
        artifact = _minimal_artifact()
        artifact["runs"][0]["residency"] = {
            "size_bytes": 100,
            "size_vram_bytes": 100,
            "gpu_fraction": bad,
            "context_length": 4096,
        }
        problems = validate_artifact(artifact)
        assert any("gpu_fraction" in problem for problem in problems), bad


def test_booleans_rejected_in_numeric_fields() -> None:
    artifact = _minimal_artifact()
    artifact["model"]["disk_bytes"] = True
    assert any("model.disk_bytes must be a finite number" in problem for problem in validate_artifact(artifact))
    artifact2 = _minimal_artifact()
    artifact2["runs"][0]["tokens"]["prompt"] = False
    assert any("tokens.prompt must be a finite number" in problem for problem in validate_artifact(artifact2))
    artifact3 = _minimal_artifact()
    artifact3["hardware"]["cpu"]["logical_threads"] = True
    assert any("cpu.logical_threads must be a finite number" in problem for problem in validate_artifact(artifact3))


def test_float_where_integer_expected_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["model"]["disk_bytes"] = 1.5
    assert any("model.disk_bytes must be an integer" in problem for problem in validate_artifact(artifact))


def test_malformed_hardware_timestamp_rejected() -> None:
    artifact = _minimal_artifact()
    artifact["hardware"]["captured_at_ms"] = "yesterday"
    assert any("hardware.captured_at_ms must be a finite number" in problem for problem in validate_artifact(artifact))


def test_numeric_options_validated() -> None:
    artifact = _minimal_artifact()
    artifact["runs"][0]["options"]["num_ctx"] = "big"
    assert any("options.num_ctx must be a finite number" in problem for problem in validate_artifact(artifact))


def test_redaction_sentinel_rejects_any_path_separator() -> None:
    assert "windows_absolute_path" in redaction_violations(json.dumps({"x": r"D:\private\models\x.gguf"}))
    for hostile in (
        "E:/other/path",
        r"\\server\share",
        "/home/user/model.gguf",
        "/opt/private/model.gguf",
        "/srv/models/x.gguf",
        "/data/user/x",
        "/secret",
        "/opt/private model/x.gguf",
    ):
        assert "path_separator" in redaction_violations(json.dumps({"x": hostile})), hostile
    assert redaction_violations(json.dumps({"x": "llama3.2:1b", "t": "2026-07-17T00:00:00Z"})) == []


# -- shapes / quality ----------------------------------------------------


def test_all_shapes_present_with_prompts() -> None:
    assert set(BENCHMARK_SHAPES) == {"tiny", "medium", "long_context", "repeat", "grounded", "overcommit"}
    for spec in BENCHMARK_SHAPES.values():
        assert spec["prompt"].strip()
        assert spec["num_predict"] > 0


def test_shapes_are_deterministic() -> None:
    from odysseus_desktop_backend.runtime_bench import shapes as shapes_module

    assert shapes_module.BENCHMARK_SHAPES["medium"]["prompt"] == BENCHMARK_SHAPES["repeat"]["prompt"]
    assert BENCHMARK_SHAPES["long_context"]["prompt"].count(LONG_CONTEXT_CODEWORD) == 1


def test_quality_checks() -> None:
    assert quality_check("tiny", f"Sure! {TINY_TOKEN}") == "passed"
    assert quality_check("tiny", "no token here") == "failed"
    assert quality_check("long_context", f"The codeword is {LONG_CONTEXT_CODEWORD}.") == "passed"
    assert quality_check("long_context", "I cannot find it") == "failed"
    assert quality_check("grounded", "The fee is 4,750 rupees per community_hall_minutes.pdf") == "passed"
    assert quality_check("grounded", "The fee is 9,999 rupees") == "failed"
    long_summary = "The committee prioritized lighthouse repairs and harbor dredging before winter storms arrive." * 2
    assert quality_check("medium", long_summary) == "passed"
    assert quality_check("overcommit", "anything") == "not_applicable"
    with pytest.raises(ValueError):
        quality_check("nonexistent", "text")


# -- capability matrix ---------------------------------------------------


def test_capability_matrix_shape() -> None:
    matrix = runtime_capability_matrix()
    assert set(matrix) == {"ollama", "llamacpp", "colibri"}
    for caps in matrix.values():
        for entry in caps.values():
            assert entry["state"] in {"supported", "unsupported", "unknown"}
            assert entry["evidence"] in {"binary_help", "live_probe", "measured", "unknown"}


def test_capability_lookup_degrades_to_unknown() -> None:
    assert capability("ollama", "speculative_decoding")["state"] == "unsupported"
    assert capability("ollama", "made_up")["state"] == "unknown"
    assert capability("made_up_runtime", "batch_size")["state"] == "unknown"


def test_unknown_capabilities_have_no_fake_evidence() -> None:
    for caps in runtime_capability_matrix().values():
        for entry in caps.values():
            if entry["state"] == "unknown":
                assert entry["evidence"] == "unknown"


# -- streaming client against a fake loopback server ---------------------


class _FakeOllamaHandler(BaseHTTPRequestHandler):
    behavior = "ok"

    def log_message(self, *args) -> None:  # noqa: A003 - silence
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/api/ps":
            body = json.dumps({"models": []}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        if self.behavior == "ok":
            chunks = [
                {"message": {"role": "assistant", "content": TINY_TOKEN}, "done": False},
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "load_duration": 1_000_000,
                    "prompt_eval_count": 12,
                    "prompt_eval_duration": 2_000_000,
                    "eval_count": 4,
                    "eval_duration": 500_000_000,
                },
            ]
            for chunk in chunks:
                self.wfile.write((json.dumps(chunk) + "\n").encode())
        elif self.behavior == "garbage":
            self.wfile.write(b"this is not json\n")


@pytest.fixture()
def fake_ollama():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeOllamaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def test_run_ollama_shape_success(fake_ollama: str) -> None:
    _FakeOllamaHandler.behavior = "ok"
    record = harness.run_ollama_shape(
        model="fake-model",
        shape="tiny",
        endpoint=fake_ollama,
        timeout=10,
        cold=True,
        sample_vram=False,
    )
    assert record["error_category"] == ""
    assert record["quality_check"] == "passed"
    assert record["cold"] is True
    assert record["tokens"] == {"prompt": 12, "generated": 4, "prompt_tps": 6000.0, "generation_tps": 8.0}
    assert record["timings_ms"]["first_token"] is not None
    assert record["timings_ms"]["load"] == 1.0
    assert record["memory"]["sampler_interval_ms"] == 250


def test_run_ollama_shape_malformed_stream(fake_ollama: str) -> None:
    _FakeOllamaHandler.behavior = "garbage"
    record = harness.run_ollama_shape(
        model="fake-model",
        shape="tiny",
        endpoint=fake_ollama,
        timeout=10,
        sample_vram=False,
    )
    assert record["error_category"] == harness.ERROR_MALFORMED
    assert record["quality_check"] == "not_applicable"


def test_run_ollama_shape_connection_refused() -> None:
    record = harness.run_ollama_shape(
        model="fake-model",
        shape="tiny",
        endpoint="http://127.0.0.1:9",  # discard port: nothing listens
        timeout=3,
        sample_vram=False,
    )
    assert record["error_category"] in {harness.ERROR_CONNECTION, harness.ERROR_TIMEOUT}
    assert record["quality_check"] == "not_applicable"


def test_run_record_never_contains_prompt_or_output(fake_ollama: str) -> None:
    _FakeOllamaHandler.behavior = "ok"
    record = harness.run_ollama_shape(
        model="fake-model",
        shape="tiny",
        endpoint=fake_ollama,
        timeout=10,
        sample_vram=False,
    )
    serialized = json.dumps(record)
    assert BENCHMARK_SHAPES["tiny"]["prompt"] not in serialized
    assert TINY_TOKEN not in serialized
