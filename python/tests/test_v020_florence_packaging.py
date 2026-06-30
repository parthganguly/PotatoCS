from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REQUIRED_FILES = [
    "LICENSE",
    "README.md",
    "config.json",
    "model.safetensors",
    "preprocessor_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "vocab.json",
]


def write_pack(pack_dir: Path, *, corrupt_hash: bool = False) -> None:
    pack_dir.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in REQUIRED_FILES:
        payload = (f"{name}\n").encode("utf-8")
        (pack_dir / name).write_bytes(payload)
        digest = hashlib.sha256(payload).hexdigest()
        if corrupt_hash and name == "config.json":
            digest = "0" * 64
        files[name] = {"size_bytes": len(payload), "sha256": digest}
    manifest = {
        "pack_id": "florence2-base-ft",
        "model_id": "microsoft/Florence-2-base-ft",
        "revision": "f6c1a25888ffc1d945ee8a1a77ac833c7303d46e",
        "license": "MIT",
        "trust_remote_code": False,
        "normal_runtime_downloads": False,
        "created_at": "2026-06-19T00:00:00Z",
        "files": files,
    }
    (pack_dir / "odysseus-florence2-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def run_stage(source: Path, output_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(REPO_ROOT / "scripts" / "stage-florence2-resources.ps1"),
            "-SourceModelDir",
            str(source),
            "-OutputRoot",
            str(output_root),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_stage_florence_resources_copies_required_files(tmp_path: Path):
    source = tmp_path / "source pack with spaces"
    output_root = tmp_path / "generated resources"
    write_pack(source)

    result = run_stage(source, output_root)

    assert result.returncode == 0, result.stderr + result.stdout
    staged = output_root / "models" / "florence2-base-ft"
    assert (staged / "manifest.json").exists()
    assert not (staged / "odysseus-florence2-manifest.json").exists()
    assert sorted(path.name for path in staged.iterdir()) == sorted(REQUIRED_FILES + ["manifest.json"])
    assert (source / "odysseus-florence2-manifest.json").exists()


def test_stage_florence_resources_rejects_checksum_mismatch(tmp_path: Path):
    source = tmp_path / "bad source"
    output_root = tmp_path / "generated resources"
    write_pack(source, corrupt_hash=True)

    result = run_stage(source, output_root)

    assert result.returncode != 0
    assert "checksum mismatch" in (result.stderr + result.stdout).lower()
    assert not output_root.exists()


def test_standard_release_build_is_florence_enabled_and_core_only_is_explicit():
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    scripts = package["scripts"]

    assert scripts["tauri:build"] == "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-release.ps1"
    assert scripts["tauri:build:core"] == "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-core.ps1"


def test_tauri_resource_configs_are_variant_specific():
    config = json.loads((REPO_ROOT / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    core = json.loads((REPO_ROOT / "src-tauri" / "tauri.core.conf.json").read_text(encoding="utf-8"))
    florence = json.loads((REPO_ROOT / "src-tauri" / "tauri.florence.conf.json").read_text(encoding="utf-8"))

    assert "resources" not in config["bundle"]
    assert core["bundle"]["resources"]["../python-runtime-core"] == "python-runtime"
    assert "../python-runtime-florence" not in core["bundle"]["resources"]
    assert "generated-resources/models/florence2-base-ft" not in core["bundle"]["resources"]
    assert florence["bundle"]["resources"]["../python-runtime-florence"] == "python-runtime"
    assert florence["bundle"]["resources"]["generated-resources/models/florence2-base-ft"] == "models/florence2-base-ft"
