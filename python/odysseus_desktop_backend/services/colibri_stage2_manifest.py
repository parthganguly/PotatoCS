"""Reviewed converted-model manifest gate for Colibrì Stage 2A (OLMoE).

A manifest binds one reviewed converted OLMoE model directory to the exact
identities of every file the real one-token runner is allowed to touch:
the native engine, the source config, the three converted shards, and the
derived one-token reference. ``REVIEWED_OLMOE_MODEL_REGISTRY`` starts (and,
in this commit, remains) empty because the converted files referenced by a
manifest do not exist yet — no download or conversion has been performed.
Populating one entry requires a separate, human-reviewed commit.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services.colibri_stage2_common import (
    ALLOWED_CONVERSION_DEPENDENCY_NAMES,
    EXPECTED_CONFIG_BASENAME,
    EXPECTED_REF_BASENAME,
    EXPECTED_SHARD_BASENAMES,
    MANIFEST_EVIDENCE_SCHEMA_VERSION,
    PINNED_COLIBRI_COMMIT,
    PINNED_LICENSE_IDENTIFIER,
    PINNED_MODEL_REPOSITORY,
    PINNED_MODEL_REVISION,
    ColibriStage2Failure,
    is_hex40,
    is_hex64,
    is_safe_basename,
    is_simple_version,
)

_MAX_CONFIG_BYTES = 1 * 1024 * 1024
_MAX_SHARD_BYTES = 20 * 1024 * 1024 * 1024
_MAX_REF_BYTES = 4 * 1024


@dataclass(frozen=True, slots=True)
class OlmoeModelManifest:
    """One closed, immutable record of a reviewed converted OLMoE model.

    Every field is validated at construction time; there is no way to build
    an instance that points at the wrong repository, revision, Colibrì
    commit, or an unexpected file set.
    """

    model_repository: str
    model_revision: str
    license_identifier: str
    colibri_commit: str
    converter_source_sha256: str
    engine_basename: str
    engine_size_bytes: int
    engine_sha256: str
    config_basename: str
    config_size_bytes: int
    config_sha256: str
    shard_basenames: tuple[str, str, str]
    shard_size_bytes: tuple[int, int, int]
    shard_sha256: tuple[str, str, str]
    ref_basename: str
    ref_size_bytes: int
    ref_sha256: str
    conversion_dependency_versions: Mapping[str, str]
    evidence_schema_version: str

    def __post_init__(self) -> None:
        if self.model_repository != PINNED_MODEL_REPOSITORY:
            raise ValueError("manifest model_repository does not match the pinned repository")
        if not is_hex40(self.model_revision) or self.model_revision != PINNED_MODEL_REVISION:
            raise ValueError("manifest model_revision does not match the pinned immutable revision")
        if self.license_identifier != PINNED_LICENSE_IDENTIFIER:
            raise ValueError("manifest license_identifier does not match the pinned license")
        if not is_hex40(self.colibri_commit) or self.colibri_commit != PINNED_COLIBRI_COMMIT:
            raise ValueError("manifest colibri_commit does not match the pinned Colibrì commit")
        if not is_hex64(self.converter_source_sha256):
            raise ValueError("manifest converter_source_sha256 is not a SHA-256")

        # The engine identity must equal the one reviewed, real,
        # deterministic-build result exactly -- a caller cannot authorize
        # some other engine by supplying an arbitrary (but well-formed)
        # basename/size/hash. Looked up live via the `common` module (not
        # a name imported at module load time) so tests can monkeypatch
        # `common.REVIEWED_ENGINE_IDENTITY` for their own synthetic fixtures.
        reviewed_engine = common.REVIEWED_ENGINE_IDENTITY
        if self.engine_basename != reviewed_engine.basename:
            raise ValueError("manifest engine_basename does not match the reviewed engine identity")
        if self.engine_size_bytes != reviewed_engine.size_bytes:
            raise ValueError("manifest engine_size_bytes does not match the reviewed engine identity")
        if self.engine_sha256 != reviewed_engine.sha256:
            raise ValueError("manifest engine_sha256 does not match the reviewed engine identity")

        if self.config_basename != EXPECTED_CONFIG_BASENAME:
            raise ValueError("manifest config_basename does not match the expected config")
        _require_bounded_size(self.config_size_bytes, _MAX_CONFIG_BYTES, "config_size_bytes")
        if not is_hex64(self.config_sha256):
            raise ValueError("manifest config_sha256 is not a SHA-256")

        if (
            not isinstance(self.shard_basenames, tuple)
            or len(self.shard_basenames) != 3
            or tuple(self.shard_basenames) != EXPECTED_SHARD_BASENAMES
        ):
            raise ValueError("manifest shard_basenames must be exactly the three pinned shard names, in order")
        if not isinstance(self.shard_size_bytes, tuple) or len(self.shard_size_bytes) != 3:
            raise ValueError("manifest shard_size_bytes must have exactly three entries")
        for size in self.shard_size_bytes:
            _require_bounded_size(size, _MAX_SHARD_BYTES, "shard_size_bytes")
        if not isinstance(self.shard_sha256, tuple) or len(self.shard_sha256) != 3:
            raise ValueError("manifest shard_sha256 must have exactly three entries")
        if not all(is_hex64(value) for value in self.shard_sha256):
            raise ValueError("manifest shard_sha256 entries must be SHA-256 values")
        if len(set(self.shard_sha256)) != 3:
            raise ValueError("manifest shard_sha256 entries must be distinct")

        if self.ref_basename != EXPECTED_REF_BASENAME:
            raise ValueError("manifest ref_basename does not match the expected derived reference")
        _require_bounded_size(self.ref_size_bytes, _MAX_REF_BYTES, "ref_size_bytes")
        if not is_hex64(self.ref_sha256):
            raise ValueError("manifest ref_sha256 is not a SHA-256")

        if not isinstance(self.conversion_dependency_versions, Mapping):
            raise ValueError("manifest conversion_dependency_versions must be a mapping")
        unknown = set(self.conversion_dependency_versions) - ALLOWED_CONVERSION_DEPENDENCY_NAMES
        if unknown:
            raise ValueError(f"manifest conversion_dependency_versions has unknown keys: {sorted(unknown)}")
        for name, version in self.conversion_dependency_versions.items():
            if not is_safe_basename(name) or not is_simple_version(version):
                raise ValueError(f"manifest conversion dependency {name!r} has an invalid version")
        object.__setattr__(
            self, "conversion_dependency_versions", MappingProxyType(dict(self.conversion_dependency_versions))
        )

        if self.evidence_schema_version != MANIFEST_EVIDENCE_SCHEMA_VERSION:
            raise ValueError("manifest evidence_schema_version does not match the supported schema")

    @property
    def all_shard_basenames(self) -> frozenset[str]:
        return frozenset(self.shard_basenames)


def _require_bounded_size(value: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"manifest {field} is out of bounds")


# Immutable and, in this implementation commit, empty: the converted model
# files a manifest would describe do not exist on disk yet. Registering an
# entry requires a dedicated, separately reviewed commit that adds exactly
# one ``OlmoeModelManifest`` keyed by its pinned model revision — never a
# caller-supplied path, hash, or regex.
REVIEWED_OLMOE_MODEL_REGISTRY: Mapping[str, OlmoeModelManifest] = MappingProxyType({})


def reviewed_manifest_for_revision(model_revision: str) -> OlmoeModelManifest | None:
    """Look up a reviewed manifest by its pinned model revision only."""

    if not isinstance(model_revision, str):
        return None
    return REVIEWED_OLMOE_MODEL_REGISTRY.get(model_revision)


def require_reviewed_manifest(model_revision: str, colibri_commit: str) -> OlmoeModelManifest:
    """Fail closed unless a reviewed manifest exists for the exact pins.

    This is the single gate the real token runner calls before doing
    anything else. It never accepts a caller-supplied override.
    """

    manifest = reviewed_manifest_for_revision(model_revision)
    if manifest is None:
        raise ColibriStage2Failure("reviewed_model_manifest_unavailable")
    if manifest.model_revision != model_revision or manifest.colibri_commit != colibri_commit:
        raise ColibriStage2Failure("manifest_pin_mismatch")
    return manifest
