"""Build a tiny GLM-shaped model directory in pure Python (no torch).

Enough structure for upstream `coli plan --json` and `coli doctor --json`
(which read only config.json, tokenizer.json, and safetensors *headers*)
and for `coli serve`'s need_model() check. No real weights: tensor data is
zeros, a few hundred KB total. This is NOT a runnable model — the engine is
stubbed separately (stub_engine.py).
"""

from __future__ import annotations

import json
import struct
from pathlib import Path


def _write_safetensors(path: Path, tensors: dict[str, int]) -> None:
    """Write a valid safetensors file where each tensor is `size` zero bytes."""
    header: dict[str, dict] = {}
    offset = 0
    for name, size in tensors.items():
        header[name] = {
            "dtype": "U8",
            "shape": [size],
            "data_offsets": [offset, offset + size],
        }
        offset += size
    raw = json.dumps(header).encode("utf-8")
    with path.open("wb") as stream:
        stream.write(struct.pack("<Q", len(raw)))
        stream.write(raw)
        stream.write(b"\0" * offset)


def build_fixture_model(target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    (target / "config.json").write_text(
        json.dumps(
            {
                "architectures": ["GlmMoeDsaForCausalLM"],
                "model_type": "glm_moe_dsa",
                "hidden_size": 1024,
                "num_hidden_layers": 4,
                "first_k_dense_replace": 1,
                "n_routed_experts": 4,
                "num_experts_per_tok": 2,
                "vocab_size": 8192,
            }
        ),
        encoding="utf-8",
    )
    (target / "tokenizer.json").write_text(
        json.dumps({"version": "1.0", "model": {"type": "BPE", "vocab": {}, "merges": []}}),
        encoding="utf-8",
    )
    tensors: dict[str, int] = {"model.embed_tokens.weight": 16384}
    for layer in range(1, 4):  # layers 1..3 are MoE (first_k_dense_replace=1)
        for expert in range(4):
            for proj in ("gate_proj", "up_proj", "down_proj"):
                tensors[f"model.layers.{layer}.mlp.experts.{expert}.{proj}.weight"] = 4096
    _write_safetensors(target / "model-00001-of-00001.safetensors", tensors)
    return target


if __name__ == "__main__":
    import sys

    build_fixture_model(Path(sys.argv[1] if len(sys.argv) > 1 else "glm_fixture_model"))
