"""Tests for the memory-bounded Colibrì Stage 2A shard converter.

No test here downloads anything, runs the real model, or touches the real
``D:\\Colibri`` tree. Every fixture is a tiny synthetic safetensors shard
built in ``tmp_path``.

Two properties matter and are both proven directly rather than asserted:

* *equivalence* -- the bounded converter's output is byte-identical to
  what the pinned upstream ``convert_olmoe.py`` would have produced for
  the same tensors, including tensor names, dtypes, shapes, ordering, and
  the exact row-wise int8 / float32-scale quantization values;
* *boundedness* -- peak resident bytes stay proportional to the chunk
  budget rather than to the shard size, measured by instrumenting the
  converter's own reads.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest

torch = pytest.importorskip("torch", reason="bounded conversion requires torch")

from odysseus_desktop_backend.services import (  # noqa: E402
    colibri_stage2_bounded_convert as bounded,
)

EXPERT_PREFIX = "model.layers.0.mlp.experts.0"


# ---------------------------------------------------------------------------
# Synthetic shard construction (mirrors safetensors' own serializer)
# ---------------------------------------------------------------------------

_DTYPE_NAMES = {
    torch.float32: "F32",
    torch.bfloat16: "BF16",
    torch.float16: "F16",
    torch.int8: "I8",
    torch.int64: "I64",
}


def _raw_bytes(tensor: torch.Tensor) -> bytes:
    import ctypes

    contiguous = tensor.contiguous()
    nbytes = contiguous.numel() * contiguous.element_size()
    if nbytes == 0:
        return b""
    buffer = (ctypes.c_char * nbytes).from_address(contiguous.data_ptr())
    return bytes(memoryview(buffer))


def write_shard(path: Path, tensors: dict[str, torch.Tensor]) -> None:
    """Write a safetensors file using the format's own ordering rules."""

    entries = sorted(
        tensors.items(),
        key=lambda item: (-bounded._DTYPE_SIZES[_DTYPE_NAMES[item[1].dtype]], item[0]),
    )
    header: dict[str, object] = {}
    body = bytearray()
    for name, tensor in entries:
        payload = _raw_bytes(tensor)
        header[name] = {
            "dtype": _DTYPE_NAMES[tensor.dtype],
            "shape": list(tensor.shape),
            "data_offsets": [len(body), len(body) + len(payload)],
        }
        body.extend(payload)
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    raw += b" " * ((8 - len(raw) % 8) % 8)
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + bytes(body))


def read_shard(path: Path) -> dict[str, torch.Tensor]:
    raw = path.read_bytes()
    (header_len,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8 : 8 + header_len].decode("utf-8"))
    data = raw[8 + header_len :]
    reverse = {value: key for key, value in _DTYPE_NAMES.items()}
    out: dict[str, torch.Tensor] = {}
    for name, info in header.items():
        begin, end = info["data_offsets"]
        buffer = bytearray(data[begin:end])
        dtype = reverse[info["dtype"]]
        tensor = torch.frombuffer(buffer, dtype=dtype) if buffer else torch.empty(0, dtype=dtype)
        out[name] = tensor.reshape(info["shape"])
    return out


def _sample_tensors(rows: int = 16, columns: int = 12) -> dict[str, torch.Tensor]:
    generator = torch.Generator().manual_seed(20260729)
    return {
        f"{EXPERT_PREFIX}.gate_proj.weight": (
            torch.randn(rows, columns, generator=generator).to(torch.bfloat16)
        ),
        f"{EXPERT_PREFIX}.up_proj.weight": (
            torch.randn(rows, columns, generator=generator).to(torch.bfloat16)
        ),
        f"{EXPERT_PREFIX}.down_proj.weight": (
            torch.randn(columns, rows, generator=generator).to(torch.bfloat16)
        ),
        "model.embed_tokens.weight": torch.randn(8, columns, generator=generator).to(
            torch.bfloat16
        ),
        "model.layers.0.self_attn.q_proj.weight": torch.randn(
            6, columns, generator=generator
        ).to(torch.bfloat16),
        "lm_head.weight": torch.randn(4, columns, generator=generator).to(torch.float16),
    }


@pytest.fixture()
def shard(tmp_path: Path) -> Path:
    path = tmp_path / "model-00001-of-00003.safetensors"
    write_shard(path, _sample_tensors())
    return path


# ---------------------------------------------------------------------------
# The reference implementation: the pinned converter's own arithmetic
# ---------------------------------------------------------------------------


def reference_quantize_row(w: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """Copied verbatim from the pinned convert_olmoe.py -- the oracle."""

    w_f32 = w.float()
    row_max = w_f32.abs().amax(dim=1, keepdim=True).clamp(min=1e-12)
    scales = row_max / 127.0
    q = (w_f32 / scales).round().clamp(-128, 127).to(torch.int8)
    return q, scales.squeeze(1)


def reference_outputs(tensors: dict[str, torch.Tensor]) -> dict[str, torch.Tensor]:
    """What the pinned converter's ``out_tensors`` dict would contain."""

    out: dict[str, torch.Tensor] = {}
    for name, tensor in tensors.items():
        if bounded.is_expert_weight(name):
            q, scales = reference_quantize_row(tensor)
            out[name] = q
            out[name + ".qs"] = scales
        else:
            out[name] = tensor
    return out


# ---------------------------------------------------------------------------
# Deterministic, equivalent output
# ---------------------------------------------------------------------------


def test_converted_values_match_the_pinned_quantization_exactly(shard: Path, tmp_path: Path) -> None:
    source = _sample_tensors()
    expected = reference_outputs(source)
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=64)

    produced = read_shard(output)
    assert set(produced) == set(expected)
    for name, expected_tensor in expected.items():
        assert produced[name].dtype == expected_tensor.dtype, name
        assert produced[name].shape == expected_tensor.shape, name
        # Bit-exact, not approximate: int8 codes and float32 scales must be
        # identical to what the pinned converter computes.
        assert torch.equal(produced[name], expected_tensor), name


def test_expert_rows_are_quantized_and_dense_rows_are_passed_through(
    shard: Path, tmp_path: Path
) -> None:
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=64)
    produced = read_shard(output)

    for projection in ("gate_proj", "up_proj", "down_proj"):
        name = f"{EXPERT_PREFIX}.{projection}.weight"
        assert produced[name].dtype == torch.int8
        assert produced[name + ".qs"].dtype == torch.float32
        assert produced[name + ".qs"].shape == (produced[name].shape[0],)
    # Dense weights keep their original dtype and gain no ".qs" companion.
    assert produced["model.embed_tokens.weight"].dtype == torch.bfloat16
    assert "model.embed_tokens.weight.qs" not in produced
    assert produced["lm_head.weight"].dtype == torch.float16


def test_dense_tensor_bytes_are_copied_through_unchanged(shard: Path, tmp_path: Path) -> None:
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=64)
    produced = read_shard(output)
    for name, tensor in _sample_tensors().items():
        if not bounded.is_expert_weight(name):
            assert torch.equal(produced[name], tensor), name


@pytest.mark.parametrize("chunk_target_bytes", [1, 24, 64, 4096, 1 << 20])
def test_output_is_byte_identical_across_every_chunk_size(
    shard: Path, tmp_path: Path, chunk_target_bytes: int
) -> None:
    """The whole equivalence argument in one test: the chunk budget is a
    memory knob only. Changing it must not move a single output byte."""

    reference = tmp_path / "reference.safetensors"
    bounded.convert_shard(shard, reference, chunk_target_bytes=1 << 24)
    candidate = tmp_path / f"candidate-{chunk_target_bytes}.safetensors"
    bounded.convert_shard(shard, candidate, chunk_target_bytes=chunk_target_bytes)
    assert candidate.read_bytes() == reference.read_bytes()


def test_conversion_is_deterministic_across_repeated_runs(shard: Path, tmp_path: Path) -> None:
    first = tmp_path / "first.safetensors"
    second = tmp_path / "second.safetensors"
    bounded.convert_shard(shard, first, chunk_target_bytes=128)
    bounded.convert_shard(shard, second, chunk_target_bytes=128)
    assert first.read_bytes() == second.read_bytes()


def test_tensor_ordering_matches_the_safetensors_rule(shard: Path, tmp_path: Path) -> None:
    """Descending dtype width, then ascending name -- the exact order the
    safetensors serializer uses, so an engine reading either file walks
    the same layout."""

    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=256)
    raw = output.read_bytes()
    (header_len,) = struct.unpack("<Q", raw[:8])
    pairs = json.JSONDecoder(object_pairs_hook=lambda items: items).decode(
        raw[8 : 8 + header_len].decode("utf-8")
    )
    observed = [(name, info) for name, info in pairs]
    keys = [
        (-bounded._DTYPE_SIZES[dict(info)["dtype"]], name) for name, info in observed
    ]
    assert keys == sorted(keys)
    # Data offsets are contiguous and start at zero.
    offset = 0
    for _, info in observed:
        begin, end = dict(info)["data_offsets"]
        assert begin == offset
        offset = end
    assert 8 + header_len + offset == len(raw)


def test_header_is_padded_to_an_eight_byte_data_boundary(shard: Path, tmp_path: Path) -> None:
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=256)
    (header_len,) = struct.unpack("<Q", output.read_bytes()[:8])
    assert (8 + header_len) % 8 == 0


def test_output_is_readable_by_safetensors_itself(shard: Path, tmp_path: Path) -> None:
    """The artifact must be engine-readable, not merely self-consistent."""

    safe_open = pytest.importorskip(
        "safetensors", reason="round-trip check requires safetensors"
    ).safe_open
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=64)

    expected = reference_outputs(_sample_tensors())
    with safe_open(str(output), framework="pt") as handle:
        assert set(handle.keys()) == set(expected)
        for name, expected_tensor in expected.items():
            assert torch.equal(handle.get_tensor(name), expected_tensor), name


def test_upstream_metadata_is_dropped_exactly_as_the_pinned_script_drops_it(
    tmp_path: Path,
) -> None:
    """``load_file`` returns tensors only and the pinned script calls
    ``save_file`` without metadata, so upstream ``__metadata__`` does not
    survive its conversion either. Matching that keeps the two outputs
    byte-identical."""

    path = tmp_path / "model-00001-of-00003.safetensors"
    write_shard(path, _sample_tensors())
    raw = path.read_bytes()
    (header_len,) = struct.unpack("<Q", raw[:8])
    header = json.loads(raw[8 : 8 + header_len].decode("utf-8"))
    header["__metadata__"] = {"format": "pt", "producer": "test"}
    new_header = json.dumps(header, separators=(",", ":")).encode("utf-8")
    new_header += b" " * ((8 - len(new_header) % 8) % 8)
    path.write_bytes(struct.pack("<Q", len(new_header)) + new_header + raw[8 + header_len :])

    output = tmp_path / "out.safetensors"
    bounded.convert_shard(path, output, chunk_target_bytes=64)
    produced_raw = output.read_bytes()
    (produced_len,) = struct.unpack("<Q", produced_raw[:8])
    assert "__metadata__" not in json.loads(produced_raw[8 : 8 + produced_len].decode("utf-8"))


# ---------------------------------------------------------------------------
# Bounded memory
# ---------------------------------------------------------------------------


def test_no_single_read_exceeds_the_chunk_budget(
    shard: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct boundedness proof.

    Every read the converter performs is instrumented, with its file
    offset. Reads landing in the *data* section -- the ones whose size
    could otherwise scale with the shard -- must all stay within the chunk
    budget (or one row, whichever is larger, since a row is indivisible).
    The only read allowed to exceed that is the header, which is
    separately capped by ``_MAX_HEADER_BYTES`` and does not grow with the
    tensor payload.
    """

    chunk_target_bytes = 128
    reads: list[tuple[int, int]] = []
    real_open = Path.open

    def _tracking_open(self: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        handle = real_open(self, *args, **kwargs)
        mode = str(args[0]) if args else str(kwargs.get("mode", "r"))
        if "r" not in mode:
            return handle
        real_read = handle.read
        real_tell = handle.tell

        def _read(size: int = -1) -> bytes:
            position = real_tell()
            block = real_read(size)
            reads.append((position, len(block)))
            return block

        handle.read = _read  # type: ignore[method-assign]
        return handle

    monkeypatch.setattr(Path, "open", _tracking_open)
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=chunk_target_bytes)

    data_start = bounded.read_source_header(shard).data_start
    source_size = shard.stat().st_size
    widest_row_bytes = max(
        tensor.shape[1] * tensor.element_size()
        for name, tensor in _sample_tensors().items()
        if bounded.is_expert_weight(name)
    )
    budget = max(chunk_target_bytes, widest_row_bytes)

    data_reads = [size for position, size in reads if position >= data_start]
    assert data_reads, "the converter must actually read tensor data"
    assert max(data_reads) <= budget
    # And the whole payload really was streamed, not skipped.
    assert sum(data_reads) >= source_size - data_start


def test_peak_resident_bytes_track_the_chunk_budget_not_the_shard_size(tmp_path: Path) -> None:
    """A large-shard proof that peak memory is a function of the chunk
    budget, not of the shard: converting the same shard with a 16x smaller
    budget must not increase peak resident bytes, and the peak must stay a
    small fraction of the shard itself."""

    rows, columns = 4096, 256
    generator = torch.Generator().manual_seed(4242)
    tensors = {
        f"{EXPERT_PREFIX}.gate_proj.weight": torch.randn(
            rows, columns, generator=generator
        ).to(torch.bfloat16),
        "model.embed_tokens.weight": torch.randn(rows, columns, generator=generator).to(
            torch.bfloat16
        ),
    }
    shard = tmp_path / "model-00001-of-00003.safetensors"
    write_shard(shard, tensors)
    shard_size = shard.stat().st_size

    peaks: dict[int, int] = {}
    for index, chunk_target_bytes in enumerate((1 << 20, 1 << 16)):
        live = 0
        peak = 0
        real_frombuffer = torch.frombuffer

        def _tracked(buffer, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal live, peak
            tensor = real_frombuffer(buffer, **kwargs)
            live = len(buffer)
            # A float32 upcast of the chunk plus the int8 result is the
            # widest transient the quantizer ever holds for that chunk.
            peak = max(peak, live * 3)
            return tensor

        original = torch.frombuffer
        torch.frombuffer = _tracked  # type: ignore[assignment]
        try:
            bounded.convert_shard(
                shard, tmp_path / f"out-{index}.safetensors", chunk_target_bytes=chunk_target_bytes
            )
        finally:
            torch.frombuffer = original  # type: ignore[assignment]
        peaks[chunk_target_bytes] = peak

    # A 16x smaller budget never costs more memory, and the peak is a
    # small fraction of the shard -- the property the pinned converter
    # lacks, where peak scales with the shard itself.
    assert peaks[1 << 16] <= peaks[1 << 20]
    assert peaks[1 << 16] < shard_size // 4
    assert peaks[1 << 16] <= 3 * (1 << 16)


def test_rows_per_chunk_is_always_at_least_one_row(shard: Path, tmp_path: Path) -> None:
    """A row is indivisible, so a budget smaller than one row must still
    make progress rather than loop forever or emit a truncated tensor."""

    assert bounded.rows_per_chunk(row_bytes=1000, chunk_target_bytes=1) == 1
    assert bounded.rows_per_chunk(row_bytes=0, chunk_target_bytes=1) == 1
    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=1)
    assert torch.equal(
        read_shard(output)[f"{EXPERT_PREFIX}.gate_proj.weight"],
        reference_outputs(_sample_tensors())[f"{EXPERT_PREFIX}.gate_proj.weight"],
    )


def test_row_chunk_boundaries_cover_every_row_exactly_once() -> None:
    for rows, per_chunk in ((10, 3), (9, 3), (1, 5), (7, 1)):
        chunks = list(bounded.iter_row_chunks(rows, per_chunk))
        assert chunks[0][0] == 0
        assert chunks[-1][1] == rows
        assert all(a[1] == b[0] for a, b in zip(chunks, chunks[1:]))
        assert sum(stop - start for start, stop in chunks) == rows


def test_scale_memory_is_four_bytes_per_expert_row(shard: Path, tmp_path: Path) -> None:
    """The only whole-shard state the converter retains."""

    output = tmp_path / "out.safetensors"
    bounded.convert_shard(shard, output, chunk_target_bytes=64)
    produced = read_shard(output)
    for name, tensor in _sample_tensors().items():
        if bounded.is_expert_weight(name):
            scales = produced[name + ".qs"]
            assert scales.numel() * scales.element_size() == tensor.shape[0] * 4


# ---------------------------------------------------------------------------
# No-overwrite and malformed-input safety
# ---------------------------------------------------------------------------


def test_convert_shard_never_overwrites_an_existing_output(shard: Path, tmp_path: Path) -> None:
    output = tmp_path / "out.safetensors"
    output.write_bytes(b"pre-existing artifact")
    with pytest.raises(bounded.BoundedConversionError) as excinfo:
        bounded.convert_shard(shard, output, chunk_target_bytes=64)
    assert excinfo.value.reason == "output_already_exists"
    assert output.read_bytes() == b"pre-existing artifact"


def test_rejects_a_truncated_source_file(shard: Path, tmp_path: Path) -> None:
    raw = shard.read_bytes()
    truncated = tmp_path / "truncated.safetensors"
    truncated.write_bytes(raw[: len(raw) // 2])
    with pytest.raises(bounded.BoundedConversionError):
        bounded.convert_shard(truncated, tmp_path / "out.safetensors", chunk_target_bytes=64)


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"\x00" * 4,
        struct.pack("<Q", 0),
        struct.pack("<Q", 1 << 40),
        struct.pack("<Q", 4) + b"{{{{",
    ],
)
def test_rejects_malformed_headers(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "bad.safetensors"
    path.write_bytes(payload)
    with pytest.raises(bounded.BoundedConversionError):
        bounded.read_source_header(path)


def test_rejects_offsets_that_disagree_with_shape_and_dtype(tmp_path: Path) -> None:
    header = {"a": {"dtype": "F32", "shape": [4, 4], "data_offsets": [0, 8]}}
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    raw += b" " * ((8 - len(raw) % 8) % 8)
    path = tmp_path / "bad.safetensors"
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\x00" * 8)
    with pytest.raises(bounded.BoundedConversionError):
        bounded.read_source_header(path)


def test_rejects_offsets_pointing_past_the_end_of_the_file(tmp_path: Path) -> None:
    header = {"a": {"dtype": "F32", "shape": [4, 4], "data_offsets": [0, 64]}}
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    raw += b" " * ((8 - len(raw) % 8) % 8)
    path = tmp_path / "bad.safetensors"
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\x00" * 8)
    with pytest.raises(bounded.BoundedConversionError):
        bounded.read_source_header(path)


def test_rejects_a_one_dimensional_expert_weight(tmp_path: Path) -> None:
    """Row-wise quantization is only defined for a 2-D weight."""

    path = tmp_path / "model-00001-of-00003.safetensors"
    write_shard(path, {f"{EXPERT_PREFIX}.gate_proj.weight": torch.zeros(8, dtype=torch.bfloat16)})
    with pytest.raises(bounded.BoundedConversionError):
        bounded.convert_shard(path, tmp_path / "out.safetensors")


def test_expert_key_regex_matches_the_pinned_converter() -> None:
    assert bounded.EXPERT_KEY_RE == (
        r"model\.layers\.\d+\.mlp\.experts\.\d+\.(gate_proj|up_proj|down_proj)\.weight"
    )
    assert bounded.is_expert_weight("model.layers.11.mlp.experts.7.up_proj.weight")
    assert bounded.is_expert_weight("model.layers.0.mlp.experts.0.down_proj.weight")
    assert not bounded.is_expert_weight("model.layers.0.self_attn.q_proj.weight")
    assert not bounded.is_expert_weight("model.layers.0.mlp.experts.0.gate_proj.bias")


def test_quantization_constants_match_the_pinned_converter() -> None:
    assert bounded.QUANT_DIVISOR == 127.0
    assert (bounded.QUANT_CLAMP_MIN, bounded.QUANT_CLAMP_MAX) == (-128, 127)
    assert bounded.SCALE_FLOOR == 1e-12
    assert bounded.SCALE_SUFFIX == ".qs"


def test_zero_weight_rows_use_the_scale_floor_exactly_like_upstream() -> None:
    """An all-zero row would divide by zero without the 1e-12 clamp; the
    pinned script's behaviour here is preserved bit-for-bit."""

    weights = torch.zeros(2, 4, dtype=torch.bfloat16)
    q, scales = bounded._quantize_rows(weights, torch)
    expected_q, expected_scales = reference_quantize_row(weights)
    assert torch.equal(q, expected_q)
    assert torch.equal(scales, expected_scales)
    assert torch.all(scales == 1e-12 / 127.0)


def test_convert_model_dir_copies_config_and_converts_each_shard(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    config_payload = b'{"model_type": "olmoe"}'
    (model_dir / "config.json").write_bytes(config_payload)
    write_shard(model_dir / "model-00001-of-00003.safetensors", _sample_tensors())

    out_dir = tmp_path / "out"
    stats = bounded.convert_model_dir(model_dir, out_dir, chunk_target_bytes=64)

    assert (out_dir / "config.json").read_bytes() == config_payload
    assert (out_dir / "model-00001-of-00003.safetensors").exists()
    assert len(stats) == 1
    assert stats[0].expert_tensor_count == 3


def test_convert_model_dir_requires_a_config(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    write_shard(model_dir / "model-00001-of-00003.safetensors", _sample_tensors())
    with pytest.raises(bounded.BoundedConversionError):
        bounded.convert_model_dir(model_dir, tmp_path / "out")


def test_cli_has_no_repo_option_and_never_reaches_the_network(tmp_path: Path) -> None:
    """Unlike the pinned script, this converter has no ``--repo`` path and
    imports no downloader -- it can only ever read a local directory."""

    # Checked against the parsed module rather than raw text, so a mention
    # in a docstring cannot satisfy or break this.
    import ast

    tree = ast.parse(Path(bounded.__file__).read_text(encoding="utf-8"))
    string_literals = {
        node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    option_literals = {value for value in string_literals if value.startswith("--")}
    assert option_literals == {"--model", "--out", "--chunk-bytes"}

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if "huggingface" in name or "urllib" in name}
    assert not {name for name in imported if name.startswith("odysseus_desktop_backend")}

    model_dir = tmp_path / "model"
    model_dir.mkdir()
    (model_dir / "config.json").write_bytes(b"{}")
    write_shard(model_dir / "model-00001-of-00003.safetensors", _sample_tensors())
    assert bounded.main(["--model", str(model_dir), "--out", str(tmp_path / "out")]) == 0


def test_cli_reports_a_closed_reason_without_leaking_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    model_dir = tmp_path / "missing-model-directory"
    exit_code = bounded.main(["--model", str(model_dir), "--out", str(tmp_path / "out")])
    assert exit_code == bounded.EXIT_CONVERSION_ERROR
    captured = capsys.readouterr()
    assert "model_dir_missing" in captured.err
    assert str(model_dir) not in captured.err
    assert "missing-model-directory" not in captured.err


def test_planned_output_sizes_are_exact(shard: Path) -> None:
    """The header is written before any tensor data, so every declared
    size must be right the first time -- there is no rewind."""

    parsed = bounded.read_source_header(shard)
    entries = bounded.plan_output_entries(parsed.tensors)
    header, ranges = bounded.build_header(entries)
    assert len(ranges) == len(entries)
    for entry, (begin, end) in zip(entries, ranges):
        assert end - begin == entry.nbytes
        assert entry.nbytes == math.prod(entry.shape) * bounded._DTYPE_SIZES[entry.dtype]
    assert len(header) % 8 == 0
