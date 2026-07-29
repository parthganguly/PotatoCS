#!/usr/bin/env python3
"""Memory-bounded, equivalence-proven OLMoE shard converter for Colibrì
Stage 2A.

Why this exists
---------------
The pinned upstream ``convert_olmoe.py`` (Colibrì commit
``72d3d37231e922a6fa9afca16e08fa45842d5eb4``) is correct but not usable on
a 16 GiB machine, for two independent reasons measured on the real target
host (Windows 11, 15.42 GiB RAM, Python 3.13.12, torch 2.8.0+cpu,
safetensors 0.5.3):

1. *Unbounded peak memory.* It calls ``load_file(shard)``, which
   materialises the whole 4.65 GiB source shard as torch tensors, then
   accumulates every converted tensor into a second ``out_tensors`` dict
   while the source dict stays alive, then calls ``save_file`` -- whose
   ``_flatten`` step makes one more full ``bytes`` copy of every output
   tensor before a single byte reaches disk. Peak resident set is
   therefore roughly ``source shard + converted shard + converted shard``
   again, about 9-10 GiB for shard 1 alone, which exhausts commit charge
   on a 16 GiB host and kills the process with a native access violation
   (``0xc0000005``) inside ``torch_cpu.dll``.
2. *A hard dependency this environment does not have.* ``save_file`` ->
   ``_flatten`` -> ``_tobytes`` does ``import numpy``; numpy is not
   installed in the isolated Stage 2 environment, so the pinned script
   could not have finished the write even with unlimited RAM.

This module is the bounded replacement. It is deliberately **equivalent,
not different**: it reproduces the pinned script's quantization
arithmetic verbatim and produces a safetensors file byte-identical to the
one ``safetensors.serialize_file`` would have written for the same tensor
set. What changes is only *how much memory is resident at once*.

Preserved exactly
-----------------
* Row-wise int8 expert quantization with float32 scales -- ``_quantize_rows``
  below is a line-for-line copy of the pinned ``quantize_row``, and row-wise
  reductions make chunking bit-exact (a row's scale and quantized values
  depend on that row alone, and max/divide/round/clamp are all exactly
  determined by IEEE-754).
* The expert key regex, the ``.qs`` scale suffix, and the ``--model`` /
  ``--out`` argument grammar.
* Tensor names, dtypes, and shapes; non-expert ("dense") tensors are
  copied through byte-for-byte.
* Tensor ordering and on-disk layout: safetensors orders entries by
  descending dtype size then ascending name, emits a compact JSON header
  space-padded so the data section starts on an 8-byte boundary, and
  concatenates tensor data contiguously in that same order.
* Dropping the source shard's ``__metadata__`` -- the pinned script does
  the same, because ``load_file`` returns tensors only and it calls
  ``save_file`` without a ``metadata`` argument.

Bounded by construction
-----------------------
Peak additional memory is ``O(chunk_bytes)``, not ``O(shard_bytes)``:

* dense tensors never enter torch at all -- their bytes are copied from
  the source file range straight to the output file in bounded chunks;
* expert tensors are read, quantized, and written one row-chunk at a
  time, so the float32 intermediates exist only for that chunk;
* the only whole-shard retained state is the float32 scale vectors, which
  are four bytes per expert row (about 7 MB for a 4.65 GiB shard).

Standalone on purpose
---------------------
This module imports nothing from ``odysseus_desktop_backend``. That keeps
it a drop-in positional equivalent of the pinned converter -- a plain
script taking ``--model`` and ``--out`` -- so the orchestrator can run it
as an isolated child process through the very same reviewed
subprocess/deadline/evidence path, and a crash in torch can never take
the orchestrator down with it. It never downloads anything: unlike the
pinned script there is deliberately no ``--repo`` option and no
``huggingface_hub`` import.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import re
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Iterator

# Verbatim from the pinned convert_olmoe.py.
EXPERT_KEY_RE = r"model\.layers\.\d+\.mlp\.experts\.\d+\.(gate_proj|up_proj|down_proj)\.weight"

_EXPERT_KEY = re.compile(EXPERT_KEY_RE)

SCALE_SUFFIX = ".qs"
QUANT_DIVISOR = 127.0
QUANT_CLAMP_MIN = -128
QUANT_CLAMP_MAX = 127
SCALE_FLOOR = 1e-12

CONFIG_BASENAME = "config.json"
DEFAULT_CHUNK_TARGET_BYTES = 32 * 1024 * 1024

# The safetensors dtype vocabulary, with the byte width the format's own
# ordering rule sorts on.
_DTYPE_SIZES: dict[str, int] = {
    "BOOL": 1,
    "U8": 1,
    "I8": 1,
    "F8_E5M2": 1,
    "F8_E4M3": 1,
    "U16": 2,
    "I16": 2,
    "F16": 2,
    "BF16": 2,
    "U32": 4,
    "I32": 4,
    "F32": 4,
    "U64": 8,
    "I64": 8,
    "F64": 8,
}

_MAX_HEADER_BYTES = 100 * 1024 * 1024
_ASCII_NAME = re.compile(r"^[\x20-\x7e]{1,512}$")

EXIT_CONVERSION_ERROR = 2


class BoundedConversionError(RuntimeError):
    """A closed, message-free-by-convention converter failure.

    Only a short fixed reason string is ever carried; no path, no tensor
    payload, and no environment value.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# Source header parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceTensor:
    """One tensor as described by the source shard's own header."""

    name: str
    dtype: str
    shape: tuple[int, ...]
    begin: int
    end: int

    @property
    def nbytes(self) -> int:
        return self.end - self.begin


@dataclass(frozen=True, slots=True)
class SourceShard:
    path: Path
    data_start: int
    file_size: int
    tensors: tuple[SourceTensor, ...]


def _torch_dtype(module: Any, dtype: str) -> Any:
    mapping = {
        "BOOL": "bool",
        "U8": "uint8",
        "I8": "int8",
        "I16": "int16",
        "I32": "int32",
        "I64": "int64",
        "F16": "float16",
        "BF16": "bfloat16",
        "F32": "float32",
        "F64": "float64",
    }
    attribute = mapping.get(dtype)
    if attribute is None:
        raise BoundedConversionError("unsupported_source_dtype")
    return getattr(module, attribute)


def read_source_header(path: Path) -> SourceShard:
    """Parse a safetensors header without reading a single tensor byte.

    Every field is validated before use: the declared header length must
    fit a sane bound, each dtype must be one this converter knows, each
    shape must be non-negative integers whose element count times the
    dtype width exactly equals the declared byte range, and every range
    must lie inside the file.
    """

    file_size = path.stat().st_size
    with path.open("rb") as handle:
        prefix = handle.read(8)
        if len(prefix) != 8:
            raise BoundedConversionError("source_header_truncated")
        (header_len,) = struct.unpack("<Q", prefix)
        if not 0 < header_len <= _MAX_HEADER_BYTES or 8 + header_len > file_size:
            raise BoundedConversionError("source_header_length_invalid")
        raw = handle.read(header_len)
    if len(raw) != header_len:
        raise BoundedConversionError("source_header_truncated")

    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BoundedConversionError("source_header_unparsable") from exc
    if not isinstance(document, dict):
        raise BoundedConversionError("source_header_unparsable")

    data_start = 8 + header_len
    data_bytes = file_size - data_start
    tensors: list[SourceTensor] = []
    for name, info in document.items():
        # ``load_file`` returns tensors only, and the pinned script then
        # calls ``save_file`` without metadata -- so upstream metadata is
        # dropped by the pinned converter too, and dropping it here is
        # what keeps the two outputs identical.
        if name == "__metadata__":
            continue
        if not isinstance(name, str) or not _ASCII_NAME.fullmatch(name):
            raise BoundedConversionError("source_tensor_name_invalid")
        if not isinstance(info, dict):
            raise BoundedConversionError("source_header_unparsable")
        dtype = info.get("dtype")
        shape = info.get("shape")
        offsets = info.get("data_offsets")
        if dtype not in _DTYPE_SIZES:
            raise BoundedConversionError("unsupported_source_dtype")
        if not isinstance(shape, list) or any(
            isinstance(dim, bool) or not isinstance(dim, int) or dim < 0 for dim in shape
        ):
            raise BoundedConversionError("source_shape_invalid")
        if (
            not isinstance(offsets, list)
            or len(offsets) != 2
            or any(isinstance(value, bool) or not isinstance(value, int) for value in offsets)
        ):
            raise BoundedConversionError("source_offsets_invalid")
        begin, end = offsets
        if not 0 <= begin <= end or end > data_bytes:
            raise BoundedConversionError("source_offsets_invalid")
        if math.prod(shape) * _DTYPE_SIZES[dtype] != end - begin:
            raise BoundedConversionError("source_offsets_invalid")
        tensors.append(
            SourceTensor(name=name, dtype=dtype, shape=tuple(shape), begin=begin, end=end)
        )

    if not tensors:
        raise BoundedConversionError("source_header_empty")
    return SourceShard(
        path=path, data_start=data_start, file_size=file_size, tensors=tuple(tensors)
    )


def is_expert_weight(name: str) -> bool:
    """Verbatim semantics from the pinned converter's ``is_expert_weight``."""

    return bool(_EXPERT_KEY.search(name))


# ---------------------------------------------------------------------------
# Output planning: names, dtypes, shapes, and the exact safetensors ordering
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OutputEntry:
    """One tensor the converted shard will contain.

    ``kind`` is one of ``dense`` (copied through byte-for-byte),
    ``expert_q`` (row-wise int8), or ``expert_scales`` (float32 scales).
    """

    name: str
    dtype: str
    shape: tuple[int, ...]
    kind: str
    source: SourceTensor

    @property
    def nbytes(self) -> int:
        return math.prod(self.shape) * _DTYPE_SIZES[self.dtype]


def plan_output_entries(tensors: tuple[SourceTensor, ...]) -> tuple[OutputEntry, ...]:
    """Decide every output tensor's name, dtype, shape, and position.

    The ordering is safetensors' own and is reproduced exactly: descending
    dtype byte width, then ascending name. Names are ASCII-validated at
    parse time, so Python's code-point ordering and the Rust
    implementation's byte ordering agree.
    """

    entries: list[OutputEntry] = []
    for tensor in tensors:
        if is_expert_weight(tensor.name):
            if len(tensor.shape) != 2:
                raise BoundedConversionError("expert_tensor_not_2d")
            entries.append(
                OutputEntry(
                    name=tensor.name,
                    dtype="I8",
                    shape=tensor.shape,
                    kind="expert_q",
                    source=tensor,
                )
            )
            entries.append(
                OutputEntry(
                    name=tensor.name + SCALE_SUFFIX,
                    dtype="F32",
                    shape=(tensor.shape[0],),
                    kind="expert_scales",
                    source=tensor,
                )
            )
        else:
            entries.append(
                OutputEntry(
                    name=tensor.name,
                    dtype=tensor.dtype,
                    shape=tensor.shape,
                    kind="dense",
                    source=tensor,
                )
            )

    names = [entry.name for entry in entries]
    if len(set(names)) != len(names):
        raise BoundedConversionError("duplicate_output_tensor_name")
    entries.sort(key=lambda entry: (-_DTYPE_SIZES[entry.dtype], entry.name))
    return tuple(entries)


def build_header(entries: tuple[OutputEntry, ...]) -> tuple[bytes, tuple[tuple[int, int], ...]]:
    """Render the exact safetensors header bytes for ``entries``.

    Compact separators, ``dtype``/``shape``/``data_offsets`` in that key
    order, contiguous data ranges in entry order, and trailing spaces so
    that the data section begins on an 8-byte boundary -- all matching
    ``safetensors::tensor::serialize``.
    """

    document: dict[str, dict[str, Any]] = {}
    ranges: list[tuple[int, int]] = []
    offset = 0
    for entry in entries:
        end = offset + entry.nbytes
        document[entry.name] = {
            "dtype": entry.dtype,
            "shape": list(entry.shape),
            "data_offsets": [offset, end],
        }
        ranges.append((offset, end))
        offset = end

    raw = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    padding = (8 - len(raw) % 8) % 8
    raw += b" " * padding
    return struct.pack("<Q", len(raw)) + raw, tuple(ranges)


# ---------------------------------------------------------------------------
# Bit-exact quantization, applied to a bounded slice of rows
# ---------------------------------------------------------------------------


def _quantize_rows(w: Any, torch: Any) -> tuple[Any, Any]:
    """Row-wise int8 quantization. Returns (int8_weights, float32_scales).

    Identical arithmetic to the pinned ``convert_olmoe.quantize_row``.
    Because every operation here is either row-local (``amax(dim=1)``) or
    elementwise, applying it to a contiguous slice of rows yields exactly
    the values the pinned whole-tensor call would have produced for those
    same rows -- no accumulation order and no cross-row state exists that
    a chunk boundary could perturb.
    """

    w_f32 = w.float()
    row_max = w_f32.abs().amax(dim=1, keepdim=True).clamp(min=SCALE_FLOOR)
    scales = row_max / QUANT_DIVISOR
    q = (w_f32 / scales).round().clamp(QUANT_CLAMP_MIN, QUANT_CLAMP_MAX).to(torch.int8)
    return q, scales.squeeze(1)


def rows_per_chunk(row_bytes: int, chunk_target_bytes: int) -> int:
    """At least one row, and otherwise as many whole rows as fit the budget."""

    if row_bytes <= 0:
        return 1
    return max(1, chunk_target_bytes // row_bytes)


def iter_row_chunks(rows: int, per_chunk: int) -> Iterator[tuple[int, int]]:
    for start in range(0, rows, per_chunk):
        yield start, min(start + per_chunk, rows)


# ---------------------------------------------------------------------------
# Bounded IO helpers
# ---------------------------------------------------------------------------


def _write_tensor_bytes(handle: BinaryIO, tensor: Any) -> int:
    """Write ``tensor``'s raw little-endian buffer with no intermediate copy.

    ``ctypes`` is used to expose the tensor's own storage as a buffer --
    the same technique safetensors itself uses -- because the alternative
    (``tensor.numpy().tobytes()``) both requires numpy, which this
    environment does not have, and duplicates the tensor in memory, which
    is the exact defect this module exists to remove. ``tensor`` stays
    referenced for the whole call, so the buffer can never outlive it.
    """

    contiguous = tensor.contiguous()
    nbytes = contiguous.numel() * contiguous.element_size()
    if nbytes == 0:
        return 0
    buffer = (ctypes.c_char * nbytes).from_address(contiguous.data_ptr())
    handle.write(memoryview(buffer))
    del buffer
    return nbytes


def _copy_range(
    source: BinaryIO, destination: BinaryIO, offset: int, nbytes: int, chunk_target_bytes: int
) -> int:
    """Copy a byte range with a bounded buffer and no torch involvement."""

    source.seek(offset)
    remaining = nbytes
    while remaining > 0:
        block = source.read(min(chunk_target_bytes, remaining))
        if not block:
            raise BoundedConversionError("source_truncated")
        destination.write(block)
        remaining -= len(block)
    return nbytes


def _read_rows(
    handle: BinaryIO, shard: SourceShard, tensor: SourceTensor, start: int, stop: int, torch: Any
) -> Any:
    columns = tensor.shape[1]
    row_bytes = columns * _DTYPE_SIZES[tensor.dtype]
    handle.seek(shard.data_start + tensor.begin + start * row_bytes)
    wanted = (stop - start) * row_bytes
    raw = bytearray(handle.read(wanted))
    if len(raw) != wanted:
        raise BoundedConversionError("source_truncated")
    return torch.frombuffer(raw, dtype=_torch_dtype(torch, tensor.dtype)).reshape(
        stop - start, columns
    )


# ---------------------------------------------------------------------------
# The bounded conversion itself
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ShardConversionStats:
    tensor_count: int
    expert_tensor_count: int
    output_bytes: int
    max_chunk_bytes: int


def convert_shard(
    source_path: Path,
    output_path: Path,
    *,
    chunk_target_bytes: int = DEFAULT_CHUNK_TARGET_BYTES,
) -> ShardConversionStats:
    """Convert one safetensors shard with ``O(chunk_target_bytes)`` memory.

    Three bounded passes over the source file:

    1. header only -- names, dtypes, shapes, byte ranges;
    2. expert scales -- one row-chunk at a time, retaining only the tiny
       float32 scale vectors (4 bytes per expert row);
    3. the write -- entries emitted in exact safetensors order, dense
       tensors copied byte-for-byte through a bounded buffer, expert rows
       re-read and quantized one chunk at a time.

    Never overwrites: ``output_path`` is opened with exclusive creation.
    """

    import torch

    if chunk_target_bytes <= 0:
        raise BoundedConversionError("chunk_target_invalid")

    shard = read_source_header(source_path)
    entries = plan_output_entries(shard.tensors)
    header, ranges = build_header(entries)

    scales_by_name: dict[str, Any] = {}
    max_chunk_bytes = 0

    with source_path.open("rb") as source:
        # Pass 2: scales only. Nothing else from the source survives this
        # loop, so peak memory here is one row-chunk of float32.
        for entry in entries:
            if entry.kind != "expert_scales":
                continue
            tensor = entry.source
            row_bytes = tensor.shape[1] * _DTYPE_SIZES[tensor.dtype]
            per_chunk = rows_per_chunk(row_bytes, chunk_target_bytes)
            max_chunk_bytes = max(max_chunk_bytes, per_chunk * row_bytes)
            pieces = []
            for start, stop in iter_row_chunks(tensor.shape[0], per_chunk):
                rows = _read_rows(source, shard, tensor, start, stop, torch)
                _, scales = _quantize_rows(rows, torch)
                pieces.append(scales)
                del rows
            scales_by_name[entry.name] = torch.cat(pieces) if len(pieces) > 1 else pieces[0]
            del pieces

        # Pass 3: the write. Exclusive creation ("xb", never "wb") is the
        # no-overwrite guarantee: an existing artifact is never truncated.
        try:
            destination_handle = output_path.open("xb")
        except FileExistsError as exc:
            raise BoundedConversionError("output_already_exists") from exc
        with destination_handle as destination:
            destination.write(header)
            written = 0
            for entry, (begin, end) in zip(entries, ranges, strict=True):
                if written != begin:
                    raise BoundedConversionError("output_offset_desynchronised")
                if entry.kind == "dense":
                    written += _copy_range(
                        source,
                        destination,
                        shard.data_start + entry.source.begin,
                        entry.source.nbytes,
                        chunk_target_bytes,
                    )
                elif entry.kind == "expert_scales":
                    written += _write_tensor_bytes(destination, scales_by_name[entry.name])
                elif entry.kind == "expert_q":
                    tensor = entry.source
                    row_bytes = tensor.shape[1] * _DTYPE_SIZES[tensor.dtype]
                    per_chunk = rows_per_chunk(row_bytes, chunk_target_bytes)
                    for start, stop in iter_row_chunks(tensor.shape[0], per_chunk):
                        rows = _read_rows(source, shard, tensor, start, stop, torch)
                        quantized, _ = _quantize_rows(rows, torch)
                        written += _write_tensor_bytes(destination, quantized)
                        del rows, quantized
                else:  # pragma: no cover - plan_output_entries is closed
                    raise BoundedConversionError("unknown_output_entry_kind")
                if written != end:
                    raise BoundedConversionError("output_offset_desynchronised")

    expected_size = len(header) + (ranges[-1][1] if ranges else 0)
    if output_path.stat().st_size != expected_size:
        raise BoundedConversionError("output_size_mismatch")

    return ShardConversionStats(
        tensor_count=len(entries),
        expert_tensor_count=sum(1 for entry in entries if entry.kind == "expert_q"),
        output_bytes=expected_size,
        max_chunk_bytes=max_chunk_bytes,
    )


def convert_model_dir(
    model_dir: Path, output_dir: Path, *, chunk_target_bytes: int = DEFAULT_CHUNK_TARGET_BYTES
) -> tuple[ShardConversionStats, ...]:
    """The pinned converter's ``--model`` behaviour, memory-bounded.

    Same contract: require ``config.json``, copy it through unchanged,
    then convert every ``*.safetensors`` in sorted order. Stage 2A's
    orchestrator always presents exactly one shard at a time.
    """

    if not model_dir.is_dir():
        raise BoundedConversionError("model_dir_missing")
    config_path = model_dir / CONFIG_BASENAME
    if not config_path.is_file():
        raise BoundedConversionError("config_missing")

    output_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(config_path, output_dir / CONFIG_BASENAME)

    shards = sorted(model_dir.glob("*.safetensors"))
    if not shards:
        raise BoundedConversionError("no_shards_found")
    return tuple(
        convert_shard(shard, output_dir / shard.name, chunk_target_bytes=chunk_target_bytes)
        for shard in shards
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Memory-bounded OLMoE HF -> Colibrì converter (int8 experts, f32 scales)"
    )
    # Deliberately no --repo: this converter never reaches the network.
    parser.add_argument("--model", required=True, help="Local HF checkpoint directory")
    parser.add_argument("--out", required=True, help="Output directory for the converted model")
    parser.add_argument(
        "--chunk-bytes",
        type=int,
        default=DEFAULT_CHUNK_TARGET_BYTES,
        help="Target bytes held resident per row-chunk (default 32 MiB)",
    )
    args = parser.parse_args(argv)

    try:
        stats = convert_model_dir(
            Path(args.model), Path(args.out), chunk_target_bytes=args.chunk_bytes
        )
    except BoundedConversionError as exc:
        # A short fixed reason only -- never a path or a tensor payload.
        print(f"bounded-convert failed: {exc.reason}", file=sys.stderr)
        return EXIT_CONVERSION_ERROR

    for index, entry in enumerate(stats, 1):
        print(
            f"[{index}/{len(stats)}] {entry.tensor_count} tensors, "
            f"{entry.expert_tensor_count} experts quantized, {entry.output_bytes} bytes"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
