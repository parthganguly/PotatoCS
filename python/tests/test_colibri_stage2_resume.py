"""Tests for Colibrì Stage 2A safe resumability and closed failure evidence.

Nothing here downloads a model, runs a real converter, or touches the real
``D:\\Colibri`` tree. Every downloader and converter is a synthetic fake,
and every file is a few bytes in ``tmp_path``.

The invariants under test are the ones the real 16 GiB failure exposed:

* a verified source file is reused, so a crashed conversion never costs
  another 5 GB download;
* a corrupt, wrong-hash, wrong-size, or partial file is *never* reused
  and never silently repaired;
* an existing converted artifact is never overwritten;
* a failure leaves the source shard intact and the run restartable;
* failure evidence distinguishes timeout, nonzero exit, and process
  crash, and carries only bounded numbers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from odysseus_desktop_backend.services import colibri_stage2_common as common
from odysseus_desktop_backend.services import colibri_stage2_conversion as conv

CONFIG_BASENAME = common.EXPECTED_CONFIG_BASENAME
SHARD_BASENAMES = common.EXPECTED_SHARD_BASENAMES


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(basename: str, data: bytes) -> conv.SourceShardEntry:
    return conv.SourceShardEntry(basename=basename, size_bytes=len(data), sha256=_sha256(data))


class _CountingDownloader:
    """Records every basename it is asked for, so a test can prove a
    download did *not* happen."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def download(
        self, *, basename: str, expected_size_bytes: int, expected_sha256: str, destination: Path
    ) -> None:
        self.calls.append(basename)
        destination.write_bytes(self.payloads[basename])


class _CountingConverter:
    def __init__(self, converted_payload: bytes) -> None:
        self.converted_payload = converted_payload
        self.calls: list[Path] = []

    def convert(self, *, model_dir: Path, output_dir: Path) -> None:
        self.calls.append(output_dir)
        (output_dir / CONFIG_BASENAME).write_bytes((model_dir / CONFIG_BASENAME).read_bytes())
        shards = [e.name for e in model_dir.iterdir() if e.name != CONFIG_BASENAME]
        assert len(shards) == 1
        (output_dir / shards[0]).write_bytes(self.converted_payload)
        return None


class _CrashingConverter:
    """Stands in for the real observed failure: the converter process dies
    without producing output, leaving the verified source shard on disk."""

    def __init__(self, category: str = "conversion_process_crashed", **metadata: int) -> None:
        self.category = category
        self.metadata = metadata
        self.calls = 0

    def convert(self, *, model_dir: Path, output_dir: Path) -> None:
        self.calls += 1
        raise conv.ColibriStage2Failure(self.category, **self.metadata)


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    converted = tmp_path / "converted"
    scratch = tmp_path / "scratch"
    for directory in (source, converted, scratch):
        directory.mkdir()
    return source, converted, scratch


# ---------------------------------------------------------------------------
# Verified-source resume
# ---------------------------------------------------------------------------


def test_verified_existing_source_shard_is_reused_without_redownloading(tmp_path: Path) -> None:
    """The headline requirement: a previously verified 5 GB shard is never
    fetched twice."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"a-source-shard-payload"
    entry = _entry(SHARD_BASENAMES[0], payload)
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload)

    downloader = _CountingDownloader({})  # any call would KeyError
    converter = _CountingConverter(b"converted-bytes")
    result = conv.run_shard_transaction(
        expected_source=entry,
        destination_dir=source,
        config_path=source / CONFIG_BASENAME,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        downloader=downloader,
        converter=converter,
    )

    assert downloader.calls == [], "a verified shard must never be downloaded again"
    assert result.source_reused is True
    assert result.converted_reused is False
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == b"converted-bytes"
    assert not (source / SHARD_BASENAMES[0]).exists()


def test_verified_existing_config_is_reused_without_redownloading(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    payload = b'{"config": true}'
    (source / CONFIG_BASENAME).write_bytes(payload)
    downloader = _CountingDownloader({})

    path, reused = conv.download_and_verify_config(
        expected_config=_entry(CONFIG_BASENAME, payload),
        destination_dir=source,
        downloader=downloader,
    )
    assert reused is True
    assert downloader.calls == []
    assert path.read_bytes() == payload


def test_resume_reuses_an_already_converted_shard_and_leaves_it_untouched(tmp_path: Path) -> None:
    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    converted_payload = b"already-converted"
    entry = _entry(SHARD_BASENAMES[0], payload)
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (converted / SHARD_BASENAMES[0]).write_bytes(converted_payload)
    record = conv.ConvertedShardRecord(
        basename=SHARD_BASENAMES[0],
        size_bytes=len(converted_payload),
        sha256=_sha256(converted_payload),
    )

    downloader = _CountingDownloader({})
    converter = _CountingConverter(b"should-not-run")
    result = conv.run_shard_transaction(
        expected_source=entry,
        destination_dir=source,
        config_path=source / CONFIG_BASENAME,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        downloader=downloader,
        converter=converter,
        converted_record=record,
    )

    assert result.converted_reused is True
    assert downloader.calls == []
    assert converter.calls == []
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == converted_payload


def test_resume_finishes_a_transaction_interrupted_between_move_and_delete(
    tmp_path: Path,
) -> None:
    """A crash can land between placing the converted artifact (3h) and
    deleting the source shard (3i). Resuming must finish that transaction
    -- otherwise the shard reports ``source_deleted=False`` forever and no
    capture can ever be built for the run."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    converted_payload = b"converted-payload"
    entry = _entry(SHARD_BASENAMES[0], payload)
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload)  # not yet deleted
    (converted / SHARD_BASENAMES[0]).write_bytes(converted_payload)  # already moved
    record = conv.ConvertedShardRecord(
        basename=SHARD_BASENAMES[0],
        size_bytes=len(converted_payload),
        sha256=_sha256(converted_payload),
    )

    result = conv.run_shard_transaction(
        expected_source=entry,
        destination_dir=source,
        config_path=source / CONFIG_BASENAME,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        downloader=_CountingDownloader({}),
        converter=_CountingConverter(b"must-not-run"),
        converted_record=record,
    )

    assert result.converted_reused is True
    assert result.source_deleted is True
    assert not (source / SHARD_BASENAMES[0]).exists()
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == converted_payload
    # And the result is capture-eligible, which is the point.
    capture = conv.build_conversion_capture(
        source_config=_entry(CONFIG_BASENAME, b"{}"),
        source_config_verified=True,
        source_config_moved_to_final=True,
        converted_config_sha256="c" * 64,
        converted_config_size_bytes=2,
        shard_results=[
            result,
            conv.ShardTransactionResult(
                source_basename=SHARD_BASENAMES[1],
                source_size_bytes=1,
                source_sha256="a" * 64,
                source_verified=True,
                source_deleted=True,
                converted_basename=SHARD_BASENAMES[1],
                converted_size_bytes=1,
                converted_sha256="b" * 64,
                partial_cleanup_complete=True,
                temporary_output_cleanup_complete=True,
                elapsed_ms=1,
            ),
            conv.ShardTransactionResult(
                source_basename=SHARD_BASENAMES[2],
                source_size_bytes=1,
                source_sha256="a" * 64,
                source_verified=True,
                source_deleted=True,
                converted_basename=SHARD_BASENAMES[2],
                converted_size_bytes=1,
                converted_sha256="b" * 64,
                partial_cleanup_complete=True,
                temporary_output_cleanup_complete=True,
                elapsed_ms=1,
            ),
        ],
        dependency_versions={"torch": "2.8.0"},
        total_elapsed_ms=1,
        cleanup_complete=True,
    )
    assert capture["shards"][0]["converted_reused"] is True


def test_resume_will_not_delete_a_leftover_source_that_fails_verification(
    tmp_path: Path,
) -> None:
    """Finishing an interrupted transaction must never delete a file that
    is not provably the shard that was converted."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    converted_payload = b"converted-payload"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    unexpected = b"something-else-entirely"
    (source / SHARD_BASENAMES[0]).write_bytes(unexpected)
    (converted / SHARD_BASENAMES[0]).write_bytes(converted_payload)
    record = conv.ConvertedShardRecord(
        basename=SHARD_BASENAMES[0],
        size_bytes=len(converted_payload),
        sha256=_sha256(converted_payload),
    )

    with pytest.raises(conv.ColibriStage2Failure):
        conv.run_shard_transaction(
            expected_source=_entry(SHARD_BASENAMES[0], payload),
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=_CountingDownloader({}),
            converter=_CountingConverter(b"x"),
            converted_record=record,
        )
    assert (source / SHARD_BASENAMES[0]).read_bytes() == unexpected
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == converted_payload


# ---------------------------------------------------------------------------
# Corrupt / wrong-hash / stale-partial rejection
# ---------------------------------------------------------------------------


def test_wrong_hash_source_is_rejected_and_not_silently_replaced(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    real_payload = b"the-correct-payload"
    corrupt = b"the-corrupt-payload"  # same length, different bytes
    assert len(corrupt) == len(real_payload)
    path = source / SHARD_BASENAMES[0]
    path.write_bytes(corrupt)

    with pytest.raises(conv.ColibriStage2Failure, match="shard_verification_failed"):
        conv.verify_existing_source_file(path, _entry(SHARD_BASENAMES[0], real_payload))
    assert path.read_bytes() == corrupt, "a rejected file must be left for the operator to see"


def test_stale_partial_source_is_rejected_by_size_before_being_hashed(tmp_path: Path) -> None:
    """A partial download is the exact state the real crash could leave.
    It must fail on the exact-size check -- never be treated as resumable
    and never be extended in place."""

    source, _, _ = _setup(tmp_path)
    full = b"complete-shard-payload"
    path = source / SHARD_BASENAMES[0]
    path.write_bytes(full[: len(full) // 2])

    with pytest.raises(conv.ColibriStage2Failure, match="stale_source_file_rejected"):
        conv.verify_existing_source_file(path, _entry(SHARD_BASENAMES[0], full))
    assert path.read_bytes() == full[: len(full) // 2]


def test_oversized_source_file_is_rejected(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    expected = b"expected-payload"
    path = source / SHARD_BASENAMES[0]
    path.write_bytes(expected + b"-plus-trailing-garbage")
    with pytest.raises(conv.ColibriStage2Failure, match="stale_source_file_rejected"):
        conv.verify_existing_source_file(path, _entry(SHARD_BASENAMES[0], expected))


def test_a_directory_in_place_of_a_source_file_is_rejected(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    (source / SHARD_BASENAMES[0]).mkdir()
    with pytest.raises(conv.ColibriStage2Failure, match="stale_source_file_rejected"):
        conv.verify_existing_source_file(
            source / SHARD_BASENAMES[0], _entry(SHARD_BASENAMES[0], b"payload")
        )


def test_absent_source_file_is_simply_absent_not_an_error(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    assert (
        conv.verify_existing_source_file(
            source / SHARD_BASENAMES[0], _entry(SHARD_BASENAMES[0], b"payload")
        )
        is False
    )


def test_shard_transaction_rejects_a_stale_partial_rather_than_redownloading(
    tmp_path: Path,
) -> None:
    source, converted, scratch = _setup(tmp_path)
    payload = b"a-full-source-payload"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload[:5])

    downloader = _CountingDownloader({SHARD_BASENAMES[0]: payload})
    with pytest.raises(conv.ColibriStage2Failure, match="stale_source_file_rejected"):
        conv.run_shard_transaction(
            expected_source=_entry(SHARD_BASENAMES[0], payload),
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=downloader,
            converter=_CountingConverter(b"x"),
        )
    assert downloader.calls == [], "a stale partial must not trigger an overwrite download"
    assert (source / SHARD_BASENAMES[0]).read_bytes() == payload[:5]


def test_symlinked_source_file_is_rejected(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    payload = b"payload-bytes"
    real = tmp_path / "outside.bin"
    real.write_bytes(payload)
    link = source / SHARD_BASENAMES[0]
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation requires privileges unavailable here")
    with pytest.raises(conv.ColibriStage2Failure, match="stale_source_file_rejected"):
        conv.verify_existing_source_file(link, _entry(SHARD_BASENAMES[0], payload))


def test_verify_rejects_a_basename_that_is_not_the_pinned_one(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    payload = b"payload"
    other = source / "unexpected.bin"
    other.write_bytes(payload)
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_basename_rejected"):
        conv.verify_existing_source_file(other, _entry(SHARD_BASENAMES[0], payload))


# ---------------------------------------------------------------------------
# No-overwrite invariants
# ---------------------------------------------------------------------------


def test_unrecorded_existing_converted_shard_is_never_overwritten(tmp_path: Path) -> None:
    """Without a recorded identity, an existing artifact is neither
    trusted nor replaced -- it fails closed."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload)
    (converted / SHARD_BASENAMES[0]).write_bytes(b"pre-existing-artifact")

    with pytest.raises(conv.ColibriStage2Failure, match="converted_shard_already_exists"):
        conv.run_shard_transaction(
            expected_source=_entry(SHARD_BASENAMES[0], payload),
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=_CountingDownloader({}),
            converter=_CountingConverter(b"new"),
        )
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == b"pre-existing-artifact"


def test_converted_shard_not_matching_its_record_is_rejected_not_overwritten(
    tmp_path: Path,
) -> None:
    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload)
    on_disk = b"tampered-converted"
    (converted / SHARD_BASENAMES[0]).write_bytes(on_disk)
    record = conv.ConvertedShardRecord(
        basename=SHARD_BASENAMES[0], size_bytes=len(on_disk), sha256=_sha256(b"different-content")
    )

    with pytest.raises(conv.ColibriStage2Failure, match="resume_state_invalid"):
        conv.run_shard_transaction(
            expected_source=_entry(SHARD_BASENAMES[0], payload),
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=_CountingDownloader({}),
            converter=_CountingConverter(b"new"),
            converted_record=record,
        )
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == on_disk


def test_existing_config_is_never_overwritten_when_it_fails_verification(tmp_path: Path) -> None:
    source, _, _ = _setup(tmp_path)
    on_disk = b'{"wrong": true}'
    (source / CONFIG_BASENAME).write_bytes(on_disk)
    downloader = _CountingDownloader({CONFIG_BASENAME: b'{"right": 1}'})

    with pytest.raises(conv.ColibriStage2Failure):
        conv.download_and_verify_config(
            expected_config=_entry(CONFIG_BASENAME, b'{"right": 1}'),
            destination_dir=source,
            downloader=downloader,
        )
    assert (source / CONFIG_BASENAME).read_bytes() == on_disk
    assert downloader.calls == []


# ---------------------------------------------------------------------------
# Failure preservation and retry
# ---------------------------------------------------------------------------


def test_crashed_conversion_preserves_the_verified_source_for_retry(tmp_path: Path) -> None:
    """The exact real-world scenario: the converter dies, and the 5 GB
    source shard must still be there and still verify."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"an-expensive-to-download-shard"
    entry = _entry(SHARD_BASENAMES[0], payload)
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    downloader = _CountingDownloader({SHARD_BASENAMES[0]: payload})

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_process_crashed"):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=downloader,
            converter=_CrashingConverter(),
        )

    assert downloader.calls == [SHARD_BASENAMES[0]]
    assert (source / SHARD_BASENAMES[0]).read_bytes() == payload
    assert conv.verify_existing_source_file(source / SHARD_BASENAMES[0], entry) is True
    assert not (converted / SHARD_BASENAMES[0]).exists()
    assert list(scratch.iterdir()) == [], "the temp output must be cleaned up even on crash"


def test_retry_after_a_crash_downloads_nothing_and_completes(tmp_path: Path) -> None:
    """Attempt 1 crashes; attempt 2 succeeds using the shard already on
    disk. This is the whole point of the change."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"an-expensive-to-download-shard"
    entry = _entry(SHARD_BASENAMES[0], payload)
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    downloader = _CountingDownloader({SHARD_BASENAMES[0]: payload})

    with pytest.raises(conv.ColibriStage2Failure):
        conv.run_shard_transaction(
            expected_source=entry,
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=downloader,
            converter=_CrashingConverter(),
        )
    assert downloader.calls == [SHARD_BASENAMES[0]]

    result = conv.run_shard_transaction(
        expected_source=entry,
        destination_dir=source,
        config_path=source / CONFIG_BASENAME,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        downloader=downloader,
        converter=_CountingConverter(b"converted-on-retry"),
    )
    assert downloader.calls == [SHARD_BASENAMES[0]], "the retry must not download again"
    assert result.source_reused is True
    assert (converted / SHARD_BASENAMES[0]).read_bytes() == b"converted-on-retry"


# ---------------------------------------------------------------------------
# The resume ledger
# ---------------------------------------------------------------------------


def test_ledger_round_trips_and_preserves_pinned_shard_order(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    records = {
        basename: conv.ConvertedShardRecord(
            basename=basename, size_bytes=index + 1, sha256=_sha256(basename.encode())
        )
        for index, basename in enumerate(SHARD_BASENAMES)
    }
    conv.write_resume_ledger(converted, records)
    reloaded = conv.read_resume_ledger(converted)
    assert reloaded == records
    document = json.loads((converted / common.RESUME_LEDGER_BASENAME).read_text(encoding="utf-8"))
    assert [item["basename"] for item in document["converted_shards"]] == list(SHARD_BASENAMES)


def test_absent_ledger_reads_as_empty(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    assert conv.read_resume_ledger(converted) == {}


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": "some-other-version"},
        {"model_revision": "0" * 40},
        {"colibri_commit": "f" * 40},
        {"model_repository": "someone-else/other-model"},
        {"converted_shards": "not-a-list"},
    ],
)
def test_ledger_written_for_a_different_pin_is_rejected(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    _, converted, _ = _setup(tmp_path)
    conv.write_resume_ledger(converted, {})
    path = converted / common.RESUME_LEDGER_BASENAME
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(mutation)
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(conv.ColibriStage2Failure, match="resume_state_invalid"):
        conv.read_resume_ledger(converted)


def test_malformed_ledger_fails_closed_rather_than_being_ignored(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    (converted / common.RESUME_LEDGER_BASENAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(conv.ColibriStage2Failure, match="resume_state_invalid"):
        conv.read_resume_ledger(converted)


def test_ledger_records_reject_an_unpinned_basename() -> None:
    with pytest.raises(ValueError):
        conv.ConvertedShardRecord(basename="evil.safetensors", size_bytes=1, sha256="a" * 64)
    with pytest.raises(ValueError):
        conv.ConvertedShardRecord(basename=SHARD_BASENAMES[0], size_bytes=1, sha256="not-a-hash")


def test_a_tampered_ledger_cannot_cause_a_wrong_reuse(tmp_path: Path) -> None:
    """The ledger is a hint, never authority: even a well-formed entry is
    re-verified against the bytes on disk."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload)
    real_artifact = b"the-real-converted-artifact"
    (converted / SHARD_BASENAMES[0]).write_bytes(real_artifact)
    lying_record = conv.ConvertedShardRecord(
        basename=SHARD_BASENAMES[0],
        size_bytes=len(real_artifact),
        sha256=_sha256(b"a-completely-different-artifact"),
    )
    with pytest.raises(conv.ColibriStage2Failure, match="resume_state_invalid"):
        conv.run_shard_transaction(
            expected_source=_entry(SHARD_BASENAMES[0], payload),
            destination_dir=source,
            config_path=source / CONFIG_BASENAME,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            downloader=_CountingDownloader({}),
            converter=_CountingConverter(b"x"),
            converted_record=lying_record,
        )


# ---------------------------------------------------------------------------
# Full-sequence resume
# ---------------------------------------------------------------------------


def _reviewed(monkeypatch: pytest.MonkeyPatch, payloads: dict[str, bytes]) -> None:
    from types import MappingProxyType

    registry = MappingProxyType(
        {basename: _entry(basename, data) for basename, data in payloads.items()}
    )
    monkeypatch.setattr(conv, "REVIEWED_SOURCE_SHARD_MANIFEST", registry)


def _payloads() -> dict[str, bytes]:
    return {
        CONFIG_BASENAME: b'{"config": 1}',
        SHARD_BASENAMES[0]: b"source-shard-one",
        SHARD_BASENAMES[1]: b"source-shard-two",
        SHARD_BASENAMES[2]: b"source-shard-three",
    }


def test_full_run_resumes_after_a_crash_on_the_second_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: shard 1 converts, shard 2 crashes. The retry
    re-downloads nothing, reuses shard 1's artifact, and finishes."""

    payloads = _payloads()
    _reviewed(monkeypatch, payloads)
    source, converted, scratch = _setup(tmp_path)

    class _CrashOnSecond:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def convert(self, *, model_dir: Path, output_dir: Path):  # type: ignore[no-untyped-def]
            shards = [e.name for e in model_dir.iterdir() if e.name != CONFIG_BASENAME]
            assert len(shards) == 1
            self.seen.append(shards[0])
            if shards[0] == SHARD_BASENAMES[1]:
                raise conv.ColibriStage2Failure("conversion_process_crashed", win32_code=0xC0000005)
            (output_dir / CONFIG_BASENAME).write_bytes((model_dir / CONFIG_BASENAME).read_bytes())
            (output_dir / shards[0]).write_bytes(b"converted-" + shards[0].encode())
            return None

    downloader = _CountingDownloader(payloads)
    kwargs = dict(
        interactive_check=lambda: True,
        approved=True,
        destination_dir=source,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        free_bytes_probe=lambda path: 2**40,
        isolated_python_env_ready=True,
        dependency_versions={"torch": "2.8.0", "safetensors": "0.5.3"},
        downloader=downloader,
    )

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_process_crashed"):
        conv.run_approved_conversion(converter=_CrashOnSecond(), **kwargs)

    # Shard 1 is done and recorded; shard 2's source is downloaded and intact.
    assert (converted / SHARD_BASENAMES[0]).exists()
    assert conv.read_resume_ledger(converted).keys() == {SHARD_BASENAMES[0]}
    assert (source / SHARD_BASENAMES[1]).read_bytes() == payloads[SHARD_BASENAMES[1]]
    downloads_before_retry = list(downloader.calls)
    assert SHARD_BASENAMES[2] not in downloads_before_retry

    capture = conv.run_approved_conversion(converter=_CountingConverter(b"x"), **kwargs)

    # The retry re-downloaded neither the config, nor shard 1, nor shard 2.
    retry_downloads = downloader.calls[len(downloads_before_retry) :]
    assert retry_downloads == [SHARD_BASENAMES[2]]
    assert capture["shards"][0]["converted_reused"] is True
    assert capture["shards"][1]["source_reused"] is True
    assert capture["source_config_reused"] is True
    for basename in SHARD_BASENAMES:
        assert (converted / basename).exists()
    assert (converted / CONFIG_BASENAME).read_bytes() == payloads[CONFIG_BASENAME]


def test_resume_disabled_keeps_the_original_empty_destination_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = _payloads()
    _reviewed(monkeypatch, payloads)
    source, converted, _ = _setup(tmp_path)
    (source / SHARD_BASENAMES[0]).write_bytes(payloads[SHARD_BASENAMES[0]])

    with pytest.raises(conv.ColibriStage2Failure, match="destination_not_empty"):
        conv.check_approved_preconditions(
            interactive_check=lambda: True,
            approved=True,
            destination_dir=source,
            converted_dir=converted,
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.8.0", "safetensors": "0.5.3"},
        )


def test_resume_relaxes_only_the_emptiness_check_never_an_identity_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = _payloads()
    _reviewed(monkeypatch, payloads)
    source, converted, scratch = _setup(tmp_path)
    # A corrupt leftover shard: resume must still refuse it.
    (source / CONFIG_BASENAME).write_bytes(payloads[CONFIG_BASENAME])
    (source / SHARD_BASENAMES[0]).write_bytes(b"x" * len(payloads[SHARD_BASENAMES[0]]))

    conv.check_approved_preconditions(
        interactive_check=lambda: True,
        approved=True,
        destination_dir=source,
        converted_dir=converted,
        free_bytes_probe=lambda path: 2**40,
        isolated_python_env_ready=True,
        dependency_versions={"torch": "2.8.0", "safetensors": "0.5.3"},
        allow_resume=True,
    )
    with pytest.raises(conv.ColibriStage2Failure, match="shard_verification_failed"):
        conv.run_approved_conversion(
            interactive_check=lambda: True,
            approved=True,
            destination_dir=source,
            final_converted_dir=converted,
            temp_output_parent=scratch,
            free_bytes_probe=lambda path: 2**40,
            isolated_python_env_ready=True,
            dependency_versions={"torch": "2.8.0", "safetensors": "0.5.3"},
            downloader=_CountingDownloader(payloads),
            converter=_CountingConverter(b"x"),
        )


# ---------------------------------------------------------------------------
# Closed failure evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("returncode", "expected_category", "expected_key"),
    [
        (0, "ok", None),
        (1, "conversion_nonzero_exit", "exit_code"),
        (2, "conversion_nonzero_exit", "exit_code"),
        (0xC0000005, "conversion_process_crashed", "win32_code"),  # access violation
        (0xC0000409, "conversion_process_crashed", "win32_code"),  # stack buffer overrun
        (0x80000003, "conversion_process_crashed", "win32_code"),
        (-11, "conversion_process_crashed", "exit_code"),  # POSIX SIGSEGV
    ],
)
def test_exit_codes_are_classified_into_distinct_categories(
    returncode: int, expected_category: str, expected_key: str | None
) -> None:
    category, metadata = conv.classify_process_exit(returncode)
    assert category == expected_category
    if expected_key is None:
        assert metadata == {}
    else:
        assert expected_key in metadata


def test_the_real_observed_access_violation_is_reported_as_a_crash_not_a_nonzero_exit() -> None:
    """3221225477 == 0xC0000005 is what Windows reports for the crash
    actually observed in torch_cpu.dll. Reporting that as an ordinary
    nonzero exit would have hidden the real cause."""

    category, metadata = conv.classify_process_exit(3221225477)
    assert category == "conversion_process_crashed"
    assert metadata == {"win32_code": 0xC0000005}


def test_timeout_nonzero_exit_and_crash_are_three_different_categories() -> None:
    categories = {
        "conversion_timeout",
        "conversion_nonzero_exit",
        "conversion_process_crashed",
    }
    assert categories <= common.STAGE2_FAILURE_CATEGORIES
    assert len(categories) == 3


def test_a_real_child_nonzero_exit_is_captured_with_bounded_evidence(tmp_path: Path) -> None:
    import sys

    with pytest.raises(conv.ColibriStage2Failure) as excinfo:
        conv.run_converter_child([sys.executable, "-c", "raise SystemExit(3)"], deadline_seconds=60)
    failure = excinfo.value
    assert failure.category == "conversion_nonzero_exit"
    assert failure.numeric_metadata["exit_code"] == 3
    assert "elapsed_ms" in failure.numeric_metadata


def test_a_real_child_timeout_is_captured_and_the_child_is_killed() -> None:
    import sys

    with pytest.raises(conv.ColibriStage2Failure) as excinfo:
        conv.run_converter_child(
            [sys.executable, "-c", "import time; time.sleep(30)"], deadline_seconds=1.0
        )
    failure = excinfo.value
    assert failure.category == "conversion_timeout"
    assert failure.numeric_metadata["timeout_ms"] == 1000


def test_failure_evidence_carries_only_bounded_numbers_never_text() -> None:
    """The privacy property: a failure can only ever carry small
    non-negative integers from a fixed key set."""

    failure = conv.ColibriStage2Failure(
        "conversion_process_crashed", win32_code=0xC0000005, elapsed_ms=1234, peak_memory_bytes=999
    )
    record = failure.as_record()
    assert set(record) == {"category", "numeric_metadata"}
    assert all(isinstance(value, int) for value in record["numeric_metadata"].values())
    assert set(record["numeric_metadata"]) <= common.FAILURE_NUMERIC_METADATA_KEYS

    with pytest.raises(ValueError):
        conv.ColibriStage2Failure("conversion_failed", stderr_text="secret")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        conv.ColibriStage2Failure("conversion_failed", exit_code=-1)


def test_peak_memory_keys_are_part_of_the_closed_numeric_vocabulary() -> None:
    assert {"peak_memory_bytes", "peak_commit_bytes"} <= common.FAILURE_NUMERIC_METADATA_KEYS


def test_converter_child_discards_stdout_and_stderr(tmp_path: Path) -> None:
    """Converter output is never captured, so it can never reach evidence."""

    import subprocess
    import sys

    recorded: dict[str, object] = {}
    real_popen = subprocess.Popen

    class _Recording(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            recorded.update(kwargs)
            super().__init__(argv, **kwargs)

    subprocess.Popen = _Recording  # type: ignore[misc]
    try:
        conv.run_converter_child(
            [sys.executable, "-c", "import sys; print('secret'); print('secret', file=sys.stderr)"],
            deadline_seconds=60,
        )
    finally:
        subprocess.Popen = real_popen  # type: ignore[misc]

    assert recorded["stdout"] is subprocess.DEVNULL
    assert recorded["stderr"] is subprocess.DEVNULL
    assert recorded["stdin"] is subprocess.DEVNULL
    assert recorded["shell"] is False


def test_capture_records_resume_and_peak_memory_fields(tmp_path: Path) -> None:
    results = [
        conv.ShardTransactionResult(
            source_basename=basename,
            source_size_bytes=10,
            source_sha256="a" * 64,
            source_verified=True,
            source_deleted=True,
            converted_basename=basename,
            converted_size_bytes=20,
            converted_sha256="b" * 64,
            partial_cleanup_complete=True,
            temporary_output_cleanup_complete=True,
            elapsed_ms=5,
            source_reused=True,
            converted_reused=False,
            conversion_peak_memory_bytes=205_520_896,
            conversion_peak_commit_bytes=206_569_472,
        )
        for basename in SHARD_BASENAMES
    ]
    capture = conv.build_conversion_capture(
        source_config=_entry(CONFIG_BASENAME, b"{}"),
        source_config_verified=True,
        source_config_moved_to_final=True,
        converted_config_sha256="c" * 64,
        converted_config_size_bytes=2,
        shard_results=results,
        dependency_versions={"python": "3.13.12", "torch": "2.8.0", "safetensors": "0.5.3"},
        total_elapsed_ms=100,
        cleanup_complete=True,
        source_config_reused=True,
    )
    assert capture["schema_version"] == "colibri-stage2-olmoe-conversion-capture-v2"
    assert capture["source_config_reused"] is True
    for shard in capture["shards"]:
        assert shard["source_reused"] is True
        assert shard["converted_reused"] is False
        assert shard["conversion_peak_memory_bytes"] == 205_520_896

    # Still privacy-safe: no path, username, or free text anywhere.
    serialized = json.dumps(capture)
    assert "C:\\" not in serialized and "/Users/" not in serialized
    assert "Parth" not in serialized


def test_capture_still_never_validates_as_a_reviewed_manifest() -> None:
    from odysseus_desktop_backend.services import colibri_stage2_manifest as manifest_mod

    results = [
        conv.ShardTransactionResult(
            source_basename=basename,
            source_size_bytes=10,
            source_sha256="a" * 64,
            source_verified=True,
            source_deleted=True,
            converted_basename=basename,
            converted_size_bytes=20,
            converted_sha256="b" * 64,
            partial_cleanup_complete=True,
            temporary_output_cleanup_complete=True,
            elapsed_ms=5,
        )
        for basename in SHARD_BASENAMES
    ]
    capture = conv.build_conversion_capture(
        source_config=_entry(CONFIG_BASENAME, b"{}"),
        source_config_verified=True,
        source_config_moved_to_final=True,
        converted_config_sha256="c" * 64,
        converted_config_size_bytes=2,
        shard_results=results,
        dependency_versions={"torch": "2.8.0"},
        total_elapsed_ms=100,
        cleanup_complete=True,
    )
    assert capture["state"] == "unreviewed_conversion_capture"
    with pytest.raises(TypeError):
        manifest_mod.OlmoeModelManifest(**capture)


# ---------------------------------------------------------------------------
# Path safety for the new surfaces
# ---------------------------------------------------------------------------


def test_ledger_path_is_a_direct_child_and_never_escapes(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    conv.write_resume_ledger(converted, {})
    written = list(converted.iterdir())
    assert [path.name for path in written] == [common.RESUME_LEDGER_BASENAME]
    assert written[0].parent == converted


def test_ledger_write_leaves_no_temporary_file_behind(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    conv.write_resume_ledger(converted, {})
    assert not (converted / f"{common.RESUME_LEDGER_BASENAME}.tmp").exists()


def test_bounded_converter_script_path_is_the_in_repo_module(tmp_path: Path) -> None:
    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    resolved = conv.bounded_converter_script_path()
    assert resolved == Path(bounded.__file__).resolve()
    assert resolved.name == "colibri_stage2_bounded_convert.py"
    assert resolved.is_absolute()


def test_bounded_converter_takes_no_caller_supplied_script_path() -> None:
    """There must be no parameter anywhere that could redirect the bounded
    converter at a different script."""

    import inspect

    signature = inspect.signature(conv.BoundedScriptConverter)
    assert set(signature.parameters) == {"chunk_target_bytes", "absolute_deadline_seconds"}
    assert not inspect.signature(conv.bounded_converter_script_path).parameters


def test_bounded_converter_builds_shell_free_argv_with_the_current_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import subprocess
    import sys

    recorded: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            recorded["argv"] = list(argv)
            recorded["kwargs"] = kwargs
            self._handle = None

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            return 0

        def kill(self) -> None:
            return None

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    conv.BoundedScriptConverter(chunk_target_bytes=1024).convert(
        model_dir=tmp_path / "m", output_dir=tmp_path / "o"
    )

    argv = recorded["argv"]
    assert argv[0] == sys.executable
    assert argv[1] == str(conv.bounded_converter_script_path())
    assert argv[argv.index("--model") + 1] == str(tmp_path / "m")
    assert argv[argv.index("--out") + 1] == str(tmp_path / "o")
    assert argv[argv.index("--chunk-bytes") + 1] == "1024"
    assert "--repo" not in argv
    assert recorded["kwargs"]["shell"] is False
