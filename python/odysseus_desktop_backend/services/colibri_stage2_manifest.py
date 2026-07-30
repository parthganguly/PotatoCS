"""Reviewed converted-model manifest gate for Colibrì Stage 2A (OLMoE).

A manifest binds one reviewed converted OLMoE model directory to the exact
identities of every input the real one-token runner is allowed to touch: the
pinned model repository/revision, the pinned Colibrì commit, the native
engine, the converter that actually produced the artifacts (by kind *and*
full reviewed identity), the source config, the three converted shards, the
derived one-token reference, and the token contract itself (cap, bits, the
prompt token ids, and the single expected generated token id).

``REVIEWED_OLMOE_MODEL_REGISTRY`` now holds exactly one entry: the converted
``allenai/OLMoE-1B-7B-0125-Instruct`` revision
``b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e`` artifact set that passed
independent on-disk attestation. Every value in that entry is a literal in
this reviewed source file. There is no parameter, environment variable,
configuration file, or registry-substitution seam anywhere in Stage 2 that
can add, replace, or relax an entry: callers supply a model revision and a
Colibrì commit and get back either the one reviewed record or a closed
failure.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services.colibri_stage2_common import (
    ALLOWED_CONVERSION_DEPENDENCY_NAMES,
    BITS_ARGUMENT,
    CAP_ARGUMENT,
    CONVERTER_KIND_BOUNDED,
    CONVERTER_KINDS,
    EXPECTED_CONFIG_BASENAME,
    EXPECTED_GENERATED_TOKEN_ID,
    EXPECTED_REF_BASENAME,
    EXPECTED_SHARD_BASENAMES,
    MANIFEST_EVIDENCE_SCHEMA_VERSION,
    PINNED_COLIBRI_COMMIT,
    PINNED_LICENSE_IDENTIFIER,
    PINNED_MODEL_REPOSITORY,
    PINNED_MODEL_REVISION,
    PROMPT_TOKEN_IDS,
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
    converter_kind: str
    converter_basename: str
    converter_size_bytes: int
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
    cap_argument: str
    bits_argument: str
    prompt_token_ids: tuple[int, ...]
    expected_generated_token_id: int
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

        # The converter identity must equal, exactly and in full, the one
        # reviewed identity for the *kind* of converter that actually ran --
        # a well-formed-but-arbitrary 64-character hash is not enough, and
        # neither is a correct hash paired with the other converter's kind.
        # The kind is the only selector a manifest may state; the basename,
        # size, and digest are then dictated by the closed kind -> identity
        # mapping in `common`, so no manifest can invent a converter and the
        # bounded converter can never be recorded as the upstream script (or
        # vice versa). Looked up live via the `common` module so tests can
        # monkeypatch the reviewed identities.
        if self.converter_kind not in CONVERTER_KINDS:
            raise ValueError("manifest converter_kind is not a reviewed converter kind")
        reviewed_converter = common.reviewed_identity_for_converter_kind(self.converter_kind)
        if self.converter_basename != reviewed_converter.basename:
            raise ValueError("manifest converter_basename does not match the reviewed converter identity")
        if self.converter_size_bytes != reviewed_converter.size_bytes:
            raise ValueError("manifest converter_size_bytes does not match the reviewed converter identity")
        if self.converter_source_sha256 != reviewed_converter.sha256:
            raise ValueError("manifest converter_source_sha256 does not match the reviewed converter identity")
        # Only the pinned upstream script carries its own `colibri_commit`
        # (the bounded converter lives in this repository and is pinned by
        # digest alone). Where that field exists it must agree with this
        # manifest, so a manifest can never bind to a converter reviewed
        # against a different Colibrì commit.
        converter_commit = getattr(reviewed_converter, "colibri_commit", None)
        if converter_commit is not None and converter_commit != self.colibri_commit:
            raise ValueError("reviewed converter identity colibri_commit does not match the manifest")

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

        # The token contract is part of the reviewed record, not a runtime
        # parameter: a manifest cannot authorize a different cap, a different
        # quantization width, a different prompt, or a different expected
        # token. This is what makes the token oracle closed -- there is no
        # caller-supplied expected token anywhere in Stage 2.
        if self.cap_argument != CAP_ARGUMENT:
            raise ValueError("manifest cap_argument does not match the pinned cap")
        if self.bits_argument != BITS_ARGUMENT:
            raise ValueError("manifest bits_argument does not match the pinned bits")
        if not isinstance(self.prompt_token_ids, tuple) or tuple(self.prompt_token_ids) != PROMPT_TOKEN_IDS:
            raise ValueError("manifest prompt_token_ids do not match the pinned prompt token ids")
        if (
            isinstance(self.expected_generated_token_id, bool)
            or not isinstance(self.expected_generated_token_id, int)
            or self.expected_generated_token_id != EXPECTED_GENERATED_TOKEN_ID
        ):
            raise ValueError("manifest expected_generated_token_id does not match the pinned expected token")

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

    @property
    def expected_direct_child_basenames(self) -> frozenset[str]:
        """Every file the converted model directory is *required* to contain.

        Exactly the reviewed config plus the three reviewed shards. Anything
        else in that directory is either an explicitly tolerated by-product
        (the conversion resume ledger, which this manifest deliberately does
        not attest and which is never read as authority) or an unknown
        artifact that must be rejected.
        """

        return frozenset((self.config_basename, *self.shard_basenames))


def _require_bounded_size(value: int, maximum: int, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"manifest {field} is out of bounds")


# --- The one reviewed converted-model record --------------------------------

# The derived one-token reference is produced in-process from the embedded
# reviewed token arrays (see `colibri_stage2_reference`), never read from a
# tokenizer or a caller-supplied path. These two literals pin what that
# derivation must produce; `test_registry_reference_identity_matches_the_
# derived_reference` recomputes them from the reference module, so a change
# to either side fails the suite instead of silently drifting.
REVIEWED_REFERENCE_SIZE_BYTES = 78
REVIEWED_REFERENCE_SHA256 = "eb27ccf4ab02b54ada485f719c117265e2196c68c57dcca38a9b8886bfb28b1c"

# The independently disk-attested converted artifact set: exact sizes and
# SHA-256 digests of the config and the three shards produced by the
# reviewed *bounded* converter (`converter_kind == "bounded"`) from the
# pinned model revision. Every value here is a literal in reviewed source.
#
# `conversion_dependency_versions` is deliberately empty: the attested facts
# for this artifact set are its sizes and digests, and no python/torch/
# safetensors version was independently reviewed alongside them. Recording an
# unreviewed version string in an immutable registry entry would assert
# provenance nobody checked, which is worse than asserting none -- the
# artifact digests are the authority either way.
REVIEWED_OLMOE_CONVERTED_MODEL = OlmoeModelManifest(
    model_repository=PINNED_MODEL_REPOSITORY,
    model_revision=PINNED_MODEL_REVISION,
    license_identifier=PINNED_LICENSE_IDENTIFIER,
    colibri_commit=PINNED_COLIBRI_COMMIT,
    converter_kind=CONVERTER_KIND_BOUNDED,
    converter_basename=common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.basename,
    converter_size_bytes=common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.size_bytes,
    converter_source_sha256=common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.sha256,
    engine_basename=common.REVIEWED_ENGINE_IDENTITY.basename,
    engine_size_bytes=common.REVIEWED_ENGINE_IDENTITY.size_bytes,
    engine_sha256=common.REVIEWED_ENGINE_IDENTITY.sha256,
    config_basename=EXPECTED_CONFIG_BASENAME,
    config_size_bytes=828,
    config_sha256="272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce",
    shard_basenames=EXPECTED_SHARD_BASENAMES,
    shard_size_bytes=(2_709_555_648, 2_606_561_600, 2_097_277_536),
    shard_sha256=(
        "3b9ad7f9dd39448887c61d590f84e69138e09f6c2e0f337970f4453f5c0f61b2",
        "8f6861509c003f44c395044736a4052651b68fc6e095a11f351cf106330d416f",
        "06aa55f9ffb055dfb2e51ee3b6c2297061eb98e5beeb94172ed27900e57e4af9",
    ),
    ref_basename=EXPECTED_REF_BASENAME,
    ref_size_bytes=REVIEWED_REFERENCE_SIZE_BYTES,
    ref_sha256=REVIEWED_REFERENCE_SHA256,
    cap_argument=CAP_ARGUMENT,
    bits_argument=BITS_ARGUMENT,
    prompt_token_ids=PROMPT_TOKEN_IDS,
    expected_generated_token_id=EXPECTED_GENERATED_TOKEN_ID,
    conversion_dependency_versions={},
    evidence_schema_version=MANIFEST_EVIDENCE_SCHEMA_VERSION,
)

# Immutable, and keyed only by the pinned model revision. Adding, replacing,
# or relaxing an entry requires editing this reviewed source file: there is
# no caller-supplied path, hash, regex, or registry parameter anywhere in
# Stage 2 that could reach it.
REVIEWED_OLMOE_MODEL_REGISTRY: Mapping[str, OlmoeModelManifest] = MappingProxyType(
    {PINNED_MODEL_REVISION: REVIEWED_OLMOE_CONVERTED_MODEL}
)


def reviewed_manifest_for_revision(model_revision: str) -> OlmoeModelManifest | None:
    """Look up a reviewed manifest by its pinned model revision only."""

    if not isinstance(model_revision, str):
        return None
    return REVIEWED_OLMOE_MODEL_REGISTRY.get(model_revision)


def require_wellformed_registry() -> None:
    """Fail closed unless the whole registry is a single, self-consistent,
    pinned record.

    A successful lookup proves only that *one* key matched. This proves the
    registry as a whole has not been substituted for something broader: it
    must be an immutable mapping holding at most one entry, that entry must
    be an ``OlmoeModelManifest``, and its key must equal its own
    ``model_revision``, which in turn must be the pinned revision. A
    substituted registry carrying a second (or foreign-keyed) model is
    rejected even when the pinned lookup itself would have succeeded.
    """

    registry = REVIEWED_OLMOE_MODEL_REGISTRY
    if not isinstance(registry, Mapping):
        raise ColibriStage2Failure("malformed_registry")
    if len(registry) > 1:
        raise ColibriStage2Failure("malformed_registry")
    for key, entry in registry.items():
        if not isinstance(entry, OlmoeModelManifest):
            raise ColibriStage2Failure("malformed_registry")
        if key != entry.model_revision or key != PINNED_MODEL_REVISION:
            raise ColibriStage2Failure("malformed_registry")


def require_reviewed_manifest(model_revision: str, colibri_commit: str) -> OlmoeModelManifest:
    """Fail closed unless a reviewed manifest exists for the exact pins.

    This is the single gate the real token runner calls before doing
    anything else. It never accepts a caller-supplied override.
    """

    manifest = reviewed_manifest_for_revision(model_revision)
    if manifest is None:
        raise ColibriStage2Failure("reviewed_model_manifest_unavailable")
    # Checked only after a lookup has already matched, so a registry whose
    # single entry sits under a foreign key still fails closed as
    # `reviewed_model_manifest_unavailable` (nothing was authorized) rather
    # than being reclassified.
    require_wellformed_registry()
    if manifest.model_revision != model_revision or manifest.colibri_commit != colibri_commit:
        raise ColibriStage2Failure("manifest_pin_mismatch")
    return manifest
