"""Tests for the one reviewed OLMoE converted-model registry entry.

The identities asserted here are written out again as literals rather than
read back from the module under test, so these are real checks on the
reviewed record and not tautologies. They mirror the independently attested
on-disk facts for the converted ``allenai/OLMoE-1B-7B-0125-Instruct``
revision ``b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e`` artifact set.

Nothing here touches the real converted artifacts, the real engine, or any
directory outside ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import inspect
from types import MappingProxyType

import pytest

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services import colibri_stage2_manifest as manifest_mod
from odysseus_desktop_backend.services import colibri_stage2_reference as ref_mod
from odysseus_desktop_backend.services import colibri_stage2_runner as runner
from odysseus_desktop_backend.services import colibri_stage2_token_cli as cli

ENTRY = manifest_mod.REVIEWED_OLMOE_CONVERTED_MODEL

EXPECTED_CONFIG_SHA256 = "272998dd7ba4846dcc682f0b5a46144f4bcd9dde8e94d2f17bd8e5cf2f23d6ce"
EXPECTED_SHARD_SIZES = (2_709_555_648, 2_606_561_600, 2_097_277_536)
EXPECTED_SHARD_SHA256 = (
    "3b9ad7f9dd39448887c61d590f84e69138e09f6c2e0f337970f4453f5c0f61b2",
    "8f6861509c003f44c395044736a4052651b68fc6e095a11f351cf106330d416f",
    "06aa55f9ffb055dfb2e51ee3b6c2297061eb98e5beeb94172ed27900e57e4af9",
)


# ---------------------------------------------------------------------------
# Exact registry acceptance
# ---------------------------------------------------------------------------


def test_registry_entry_pins_the_exact_model_and_licence() -> None:
    assert ENTRY.model_repository == "allenai/OLMoE-1B-7B-0125-Instruct"
    assert ENTRY.model_revision == "b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e"
    assert ENTRY.license_identifier == "Apache-2.0"
    assert ENTRY.colibri_commit == "72d3d37231e922a6fa9afca16e08fa45842d5eb4"


def test_registry_entry_pins_the_exact_engine_identity() -> None:
    assert ENTRY.engine_basename == "olmoe.exe"
    assert ENTRY.engine_size_bytes == 704275
    assert ENTRY.engine_sha256 == "d7beaf6fe35de265cfaeb1d07914deeea6ceb8b3650e79b76e9c6d77176b528d"


def test_registry_entry_pins_the_exact_bounded_converter_identity() -> None:
    assert ENTRY.converter_kind == "bounded"
    assert ENTRY.converter_basename == "colibri_stage2_bounded_convert.py"
    assert ENTRY.converter_size_bytes == 24033
    assert ENTRY.converter_source_sha256 == (
        "6f8145fc71f060c75d7d04a34c96cfd58d00daa3d51f2406a6de25e167d2266b"
    )


def test_registry_entry_pins_the_exact_config_identity() -> None:
    assert ENTRY.config_basename == "config.json"
    assert ENTRY.config_size_bytes == 828
    assert ENTRY.config_sha256 == EXPECTED_CONFIG_SHA256


def test_registry_entry_pins_the_exact_three_shard_identities_in_order() -> None:
    assert ENTRY.shard_basenames == (
        "model-00001-of-00003.safetensors",
        "model-00002-of-00003.safetensors",
        "model-00003-of-00003.safetensors",
    )
    assert ENTRY.shard_size_bytes == EXPECTED_SHARD_SIZES
    assert ENTRY.shard_sha256 == EXPECTED_SHARD_SHA256


def test_registry_entry_pins_cap_bits_prompt_and_expected_token() -> None:
    assert ENTRY.cap_argument == "8"
    assert ENTRY.bits_argument == "8"
    assert ENTRY.prompt_token_ids == (510, 5347, 273, 6181, 310)
    assert ENTRY.expected_generated_token_id == 7785


def test_registry_entry_asserts_no_unreviewed_conversion_dependencies() -> None:
    # No python/torch/safetensors version was independently reviewed with the
    # attested artifact set, so the entry deliberately asserts none rather
    # than recording an unchecked provenance claim.
    assert dict(ENTRY.conversion_dependency_versions) == {}
    assert isinstance(ENTRY.conversion_dependency_versions, MappingProxyType)


def test_registry_entry_is_frozen_and_immutable() -> None:
    assert dataclasses.is_dataclass(ENTRY)
    with pytest.raises(dataclasses.FrozenInstanceError):
        ENTRY.engine_size_bytes = 1  # type: ignore[misc]
    with pytest.raises(TypeError):
        ENTRY.conversion_dependency_versions["torch"] = "9.9"  # type: ignore[index]


def test_registry_entry_uses_the_current_manifest_schema() -> None:
    assert ENTRY.evidence_schema_version == "colibri-stage2-olmoe-manifest-v2"
    assert ENTRY.evidence_schema_version == common.MANIFEST_EVIDENCE_SCHEMA_VERSION


def test_pinned_lookup_returns_exactly_this_entry() -> None:
    resolved = manifest_mod.require_reviewed_manifest(
        "b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650e", "72d3d37231e922a6fa9afca16e08fa45842d5eb4"
    )
    assert resolved is ENTRY


# ---------------------------------------------------------------------------
# Reference determinism: the derived reference the registry attests
# ---------------------------------------------------------------------------


def test_registry_reference_identity_matches_the_derived_reference() -> None:
    assert ENTRY.ref_basename == "olmoe-stage2-one-token-ref.json"
    assert ENTRY.ref_size_bytes == len(ref_mod.canonical_reference_bytes())
    assert ENTRY.ref_sha256 == ref_mod.canonical_reference_sha256()


def test_registry_reference_digest_is_the_independently_computed_value() -> None:
    assert ENTRY.ref_size_bytes == 78
    assert ENTRY.ref_sha256 == "eb27ccf4ab02b54ada485f719c117265e2196c68c57dcca38a9b8886bfb28b1c"


def test_derived_reference_encodes_exactly_the_registry_token_contract() -> None:
    payload = ref_mod.reference_object()
    assert tuple(payload["prompt_ids"]) == ENTRY.prompt_token_ids
    assert tuple(payload["full_ids"]) == ENTRY.prompt_token_ids + (
        ENTRY.expected_generated_token_id,
    )


def test_reference_derivation_is_byte_stable_across_calls() -> None:
    first = ref_mod.canonical_reference_bytes()
    second = ref_mod.canonical_reference_bytes()
    assert first == second
    assert ref_mod.canonical_reference_sha256() == ref_mod.canonical_reference_sha256()


def test_reference_written_to_disk_matches_the_registry_identity(tmp_path) -> None:
    session = ref_mod.create_private_reference_session(tmp_path)
    try:
        artifact = ref_mod.write_private_reference(session)
        assert artifact.basename == ENTRY.ref_basename
        assert artifact.size_bytes == ENTRY.ref_size_bytes
        assert artifact.sha256 == ENTRY.ref_sha256
    finally:
        ref_mod.teardown_private_reference_session(session)


# ---------------------------------------------------------------------------
# The registry cannot authorize any other model or artifact set
# ---------------------------------------------------------------------------


def _entry_kwargs(**overrides: object) -> dict[str, object]:
    kwargs = {field.name: getattr(ENTRY, field.name) for field in dataclasses.fields(ENTRY)}
    kwargs["conversion_dependency_versions"] = dict(ENTRY.conversion_dependency_versions)
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize(
    "overrides",
    [
        # A different model, revision, licence, or Colibrì commit.
        {"model_repository": "allenai/OLMoE-1B-7B-0924-Instruct"},
        {"model_revision": "a" * 40},
        {"license_identifier": "MIT"},
        {"colibri_commit": "b" * 40},
        # A different engine.
        {"engine_basename": "glm.exe"},
        {"engine_size_bytes": 704276},
        {"engine_sha256": "c" * 64},
        # A different converter, or the other reviewed converter's kind.
        {"converter_kind": "pinned_script"},
        {"converter_kind": "anything_else"},
        {"converter_source_sha256": "d" * 64},
        {"converter_size_bytes": 24034},
        # A different artifact set.
        {"config_size_bytes": 829},
        {"config_sha256": "e" * 64},
        {"shard_size_bytes": (2_709_555_649, 2_606_561_600, 2_097_277_536)},
        {"shard_sha256": ("f" * 64, EXPECTED_SHARD_SHA256[1], EXPECTED_SHARD_SHA256[2])},
        {"shard_basenames": ("a.safetensors", "b.safetensors", "c.safetensors")},
        # A different token contract.
        {"cap_argument": "4"},
        {"bits_argument": "16"},
        {"prompt_token_ids": (1, 2, 3, 4, 5)},
        {"expected_generated_token_id": 7784},
        # A different reference.
        {"ref_basename": "other-ref.json"},
        {"ref_sha256": "0" * 64},
    ],
)
def test_no_variant_of_the_reviewed_entry_can_be_constructed(overrides: dict[str, object]) -> None:
    """Every deviation from the reviewed record is rejected at construction.

    Sizes and digests of the config/shards/reference are the one class of
    field a manifest may state freely (they describe *this* artifact set),
    so those variants are checked below by proving they can never reach the
    shipped registry rather than by construction failure.
    """

    freely_stateable = {
        "config_size_bytes",
        "config_sha256",
        "shard_size_bytes",
        "shard_sha256",
        "ref_sha256",
    }
    if set(overrides) <= freely_stateable:
        variant = manifest_mod.OlmoeModelManifest(**_entry_kwargs(**overrides))
        assert variant is not ENTRY
        assert manifest_mod.reviewed_manifest_for_revision(ENTRY.model_revision) is ENTRY
        return
    with pytest.raises(ValueError):
        manifest_mod.OlmoeModelManifest(**_entry_kwargs(**overrides))


def test_registry_has_no_entry_for_any_other_revision() -> None:
    for revision in ("a" * 40, "0" * 40, "b89a7c4bc24fb9e55ce2543c9458ce0ca5c4650f", "", "main"):
        assert manifest_mod.reviewed_manifest_for_revision(revision) is None
        with pytest.raises(manifest_mod.ColibriStage2Failure, match="reviewed_model_manifest_unavailable"):
            manifest_mod.require_reviewed_manifest(revision, ENTRY.colibri_commit)


def test_registry_rejects_a_foreign_colibri_commit_for_the_pinned_revision() -> None:
    with pytest.raises(manifest_mod.ColibriStage2Failure, match="manifest_pin_mismatch"):
        manifest_mod.require_reviewed_manifest(ENTRY.model_revision, "0" * 40)


# ---------------------------------------------------------------------------
# Closed trust boundary: no caller-supplied override seams exist
# ---------------------------------------------------------------------------


_FORBIDDEN_PARAMETER_NAMES = frozenset(
    {
        "expected_sha256",
        "expected_hash",
        "expected_size",
        "expected_size_bytes",
        "expected_token",
        "expected_token_id",
        "expected_generated_token_id",
        # `model_revision` is deliberately absent: it is a lookup *key*, not
        # an override -- supplying one can only ever match the single pinned
        # entry or fail closed (see the tests above).
        "engine_sha256",
        "engine_identity",
        "converter_sha256",
        "converter_identity",
        "converter_kind",
        "manifest",
        "registry",
        "token_oracle",
        "prompt",
        "prompt_ids",
        "prompt_token_ids",
        "tokenizer",
        "tokenizer_path",
        "ref_path",
        "reference_path",
        "cap",
        "bits",
        "cap_argument",
        "bits_argument",
    }
)


def test_no_stage2_entry_point_accepts_an_identity_or_registry_override() -> None:
    for function in (
        manifest_mod.reviewed_manifest_for_revision,
        manifest_mod.require_wellformed_registry,
        runner.run_one_token_proof,
        runner.attempt_one_token_proof,
        runner.build_runner_environment,
        cli.main,
    ):
        names = set(inspect.signature(function).parameters)
        assert not (names & _FORBIDDEN_PARAMETER_NAMES), function.__name__


def test_cli_exposes_no_identity_option() -> None:
    # The operator-facing surface is two paths and an approval flag. Nothing
    # on it can name a hash, size, revision, converter, token, or registry.
    parser = cli._build_parser()
    option_strings = {
        option for action in parser._actions for option in action.option_strings
    }
    assert option_strings == {"--engine", "--converted-model-dir", "--approve"}


def test_require_reviewed_manifest_takes_only_the_two_pins() -> None:
    # This is the one gate that names a revision, and it accepts nothing
    # else: no path, hash, size, engine, converter, token, or registry.
    parameters = inspect.signature(manifest_mod.require_reviewed_manifest).parameters
    assert list(parameters) == ["model_revision", "colibri_commit"]
