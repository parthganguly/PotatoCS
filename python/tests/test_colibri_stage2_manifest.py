"""Tests for the Colibrì Stage 2A reviewed model manifest gate (Part 2)."""

from __future__ import annotations

from types import MappingProxyType

import pytest

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services import colibri_stage2_manifest as manifest_mod

HASH64 = "a" * 64
HASH64_B = "b" * 64
HASH64_C = "c" * 64
HASH64_D = "d" * 64
HASH64_E = "e" * 64


def _valid_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = dict(
        model_repository=common.PINNED_MODEL_REPOSITORY,
        model_revision=common.PINNED_MODEL_REVISION,
        license_identifier=common.PINNED_LICENSE_IDENTIFIER,
        colibri_commit=common.PINNED_COLIBRI_COMMIT,
        converter_source_sha256=HASH64,
        engine_basename=common.EXPECTED_ENGINE_BASENAME,
        engine_size_bytes=1024,
        engine_sha256=HASH64_B,
        config_basename=common.EXPECTED_CONFIG_BASENAME,
        config_size_bytes=64,
        config_sha256=HASH64_C,
        shard_basenames=common.EXPECTED_SHARD_BASENAMES,
        shard_size_bytes=(10, 20, 30),
        shard_sha256=(HASH64_D, HASH64_E, "f" * 64),
        ref_basename=common.EXPECTED_REF_BASENAME,
        ref_size_bytes=78,
        ref_sha256="0" * 64,
        conversion_dependency_versions={"python": "3.11.9"},
        evidence_schema_version=common.MANIFEST_EVIDENCE_SCHEMA_VERSION,
    )
    kwargs.update(overrides)
    return kwargs


def test_registry_starts_empty_and_immutable() -> None:
    assert dict(manifest_mod.REVIEWED_OLMOE_MODEL_REGISTRY) == {}
    assert isinstance(manifest_mod.REVIEWED_OLMOE_MODEL_REGISTRY, MappingProxyType)
    with pytest.raises(TypeError):
        manifest_mod.REVIEWED_OLMOE_MODEL_REGISTRY["x"] = None  # type: ignore[index]


def test_valid_manifest_constructs() -> None:
    manifest = manifest_mod.OlmoeModelManifest(**_valid_kwargs())
    assert manifest.model_repository == common.PINNED_MODEL_REPOSITORY
    assert isinstance(manifest.conversion_dependency_versions, MappingProxyType)


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_repository": "someone/other-model"},
        {"model_revision": "0" * 39 + "x"},
        {"model_revision": "1" * 40},
        {"license_identifier": "MIT"},
        {"colibri_commit": "1" * 40},
        {"converter_source_sha256": "not-a-hash"},
        {"engine_basename": "olmoe.exe.bak"},
        {"engine_size_bytes": 0},
        {"engine_size_bytes": -1},
        {"engine_sha256": "short"},
        {"config_basename": "config.yaml"},
        {"config_sha256": "zz" * 32},
        {"shard_basenames": ("a.safetensors", "b.safetensors", "c.safetensors")},
        {"shard_basenames": common.EXPECTED_SHARD_BASENAMES[:2]},
        {"shard_size_bytes": (10, 20)},
        {"shard_sha256": (HASH64_D, HASH64_D, HASH64_E)},
        {"ref_basename": "ref.json"},
        {"ref_sha256": "nothex"},
        {"conversion_dependency_versions": {"unknown-dep": "1.0"}},
        {"conversion_dependency_versions": {"python": "not a version!"}},
        {"evidence_schema_version": "v0"},
    ],
)
def test_malformed_manifest_fields_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        manifest_mod.OlmoeModelManifest(**_valid_kwargs(**overrides))


def test_reviewed_manifest_for_revision_returns_none_when_absent() -> None:
    assert manifest_mod.reviewed_manifest_for_revision(common.PINNED_MODEL_REVISION) is None
    assert manifest_mod.reviewed_manifest_for_revision("not-a-revision") is None
    assert manifest_mod.reviewed_manifest_for_revision(None) is None  # type: ignore[arg-type]


def test_require_reviewed_manifest_fails_closed_when_registry_empty() -> None:
    with pytest.raises(manifest_mod.ColibriStage2Failure, match="reviewed_model_manifest_unavailable"):
        manifest_mod.require_reviewed_manifest(common.PINNED_MODEL_REVISION, common.PINNED_COLIBRI_COMMIT)


def test_require_reviewed_manifest_rejects_malformed_registry_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    # A manifest registered under a key that does not match its own
    # model_revision is a malformed registry, not a usable entry.
    manifest = manifest_mod.OlmoeModelManifest(**_valid_kwargs())
    monkeypatch.setattr(
        manifest_mod, "REVIEWED_OLMOE_MODEL_REGISTRY", MappingProxyType({"wrong-key": manifest})
    )
    assert manifest_mod.reviewed_manifest_for_revision(common.PINNED_MODEL_REVISION) is None
    with pytest.raises(manifest_mod.ColibriStage2Failure, match="reviewed_model_manifest_unavailable"):
        manifest_mod.require_reviewed_manifest(common.PINNED_MODEL_REVISION, common.PINNED_COLIBRI_COMMIT)


def test_require_reviewed_manifest_rejects_colibri_commit_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = manifest_mod.OlmoeModelManifest(**_valid_kwargs())
    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: manifest}),
    )
    with pytest.raises(manifest_mod.ColibriStage2Failure, match="manifest_pin_mismatch"):
        manifest_mod.require_reviewed_manifest(common.PINNED_MODEL_REVISION, "0" * 40)


def test_require_reviewed_manifest_succeeds_with_valid_registered_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = manifest_mod.OlmoeModelManifest(**_valid_kwargs())
    monkeypatch.setattr(
        manifest_mod,
        "REVIEWED_OLMOE_MODEL_REGISTRY",
        MappingProxyType({common.PINNED_MODEL_REVISION: manifest}),
    )
    resolved = manifest_mod.require_reviewed_manifest(common.PINNED_MODEL_REVISION, common.PINNED_COLIBRI_COMMIT)
    assert resolved is manifest


def test_stage2_failure_rejects_unknown_category() -> None:
    with pytest.raises(ValueError):
        manifest_mod.ColibriStage2Failure("not_a_real_category")


def test_stage2_failure_rejects_non_numeric_metadata() -> None:
    with pytest.raises(ValueError):
        manifest_mod.ColibriStage2Failure("reviewed_model_manifest_unavailable", detail="leaky string")  # type: ignore[arg-type]
