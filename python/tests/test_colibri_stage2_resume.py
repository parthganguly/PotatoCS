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
import sys
import time
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
    # Synthetic fakes must still declare which reviewed converter they
    # stand in for; a converter that cannot say is not capture-eligible.
    converter_kind = common.CONVERTER_KIND_BOUNDED

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


def _patch_job_ownership(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for Windows job ownership when ``Popen`` itself is a fake.

    A synthetic Popen has no real process handle, so the genuine
    assignment/resume path cannot apply to it. Tests using this helper are
    about argv and converter identity, not ownership -- ownership has its
    own tests, which use real processes.
    """

    monkeypatch.setattr(conv, "_create_owning_job", lambda: object())
    monkeypatch.setattr(conv, "_assign_process_to_job", lambda job, process: True)
    monkeypatch.setattr(conv, "_resume_process_tree", lambda pid: True)
    monkeypatch.setattr(conv, "_peak_job_memory", lambda job: (None, None))
    monkeypatch.setattr(conv, "_close_job", lambda job: None)


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
        converter_kind=common.CONVERTER_KIND_BOUNDED,
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
        converter_kind=common.CONVERTER_KIND_BOUNDED,
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
                converter_kind=common.CONVERTER_KIND_BOUNDED,
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
                converter_kind=common.CONVERTER_KIND_BOUNDED,
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
        converter_kind=common.CONVERTER_KIND_BOUNDED,
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
        basename=SHARD_BASENAMES[0], size_bytes=len(on_disk), sha256=_sha256(b"different-content"),
        converter_kind=common.CONVERTER_KIND_BOUNDED,
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
            basename=basename,
            size_bytes=index + 1,
            sha256=_sha256(basename.encode()),
            converter_kind=common.CONVERTER_KIND_BOUNDED,
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
        conv.ConvertedShardRecord(basename="evil.safetensors", size_bytes=1, sha256="a" * 64,
        converter_kind=common.CONVERTER_KIND_BOUNDED,
    )
    with pytest.raises(ValueError):
        conv.ConvertedShardRecord(basename=SHARD_BASENAMES[0], size_bytes=1, sha256="not-a-hash",
        converter_kind=common.CONVERTER_KIND_BOUNDED,
    )


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
        converter_kind=common.CONVERTER_KIND_BOUNDED,
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
        converter_kind = common.CONVERTER_KIND_BOUNDED

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
            conversion_peak_memory_state=conv.MEMORY_ACCOUNTING_MEASURED,
            converter_kind=common.CONVERTER_KIND_BOUNDED,
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
    assert capture["schema_version"] == "colibri-stage2-olmoe-conversion-capture-v3"
    assert capture["source_config_reused"] is True
    for shard in capture["shards"]:
        assert shard["source_reused"] is True
        assert shard["converted_reused"] is False
        assert shard["conversion_peak_memory_bytes"] == 205_520_896
        assert shard["conversion_peak_memory_state"] == "measured"

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
            converter_kind=common.CONVERTER_KIND_BOUNDED,
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
    assert set(signature.parameters) == {
        "chunk_target_bytes",
        "absolute_deadline_seconds",
        "converter_kind",
    }
    # No parameter names a script, and the kind is fixed to the bounded
    # converter -- it selects an identity, never supplies one.
    assert not [name for name in signature.parameters if "path" in name or "script" in name]
    assert conv.BoundedScriptConverter().converter_kind == common.CONVERTER_KIND_BOUNDED
    assert not inspect.signature(conv.bounded_converter_script_path).parameters


# ---------------------------------------------------------------------------
# Reviewed bounded-converter identity
# ---------------------------------------------------------------------------


def _copy_bounded_converter(destination_dir: Path) -> Path:
    """A byte-identical copy of the real reviewed converter, so a test can
    tamper with the copy without ever touching the repository file."""

    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    destination = destination_dir / common.EXPECTED_BOUNDED_CONVERTER_BASENAME
    destination.write_bytes(Path(bounded.__file__).read_bytes())
    return destination


def test_reviewed_bounded_converter_identity_matches_the_file_on_disk() -> None:
    """The pin must describe the real file. If the converter is edited
    without updating the reviewed identity, this fails loudly rather than
    letting the gate silently reject every launch."""

    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    identity = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY
    data = Path(bounded.__file__).read_bytes()
    assert Path(bounded.__file__).name == identity.basename
    assert len(data) == identity.size_bytes
    assert hashlib.sha256(data).hexdigest() == identity.sha256


def test_reviewed_bounded_converter_identity_is_immutable_and_validated() -> None:
    identity = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY
    with pytest.raises(AttributeError):
        identity.sha256 = "b" * 64  # type: ignore[misc]
    with pytest.raises(ValueError):
        common.ReviewedBoundedConverterIdentity(
            basename="something_else.py", size_bytes=1, sha256="a" * 64
        )
    with pytest.raises(ValueError):
        common.ReviewedBoundedConverterIdentity(
            basename=common.EXPECTED_BOUNDED_CONVERTER_BASENAME, size_bytes=0, sha256="a" * 64
        )
    with pytest.raises(ValueError):
        common.ReviewedBoundedConverterIdentity(
            basename=common.EXPECTED_BOUNDED_CONVERTER_BASENAME,
            size_bytes=1,
            sha256="not-a-sha256",
        )


def test_the_real_converter_passes_its_own_reviewed_identity_gate() -> None:
    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    resolved = conv.require_reviewed_bounded_converter_identity(Path(bounded.__file__).resolve())
    assert resolved == Path(bounded.__file__).resolve()


def test_modified_bounded_converter_is_rejected_before_subprocess_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single appended byte must stop the launch -- and must stop it
    *before* any process is created, not after."""

    import subprocess

    script = _copy_bounded_converter(tmp_path)
    script.write_bytes(script.read_bytes() + b"\n# injected\n")

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("no subprocess may be created for a modified converter")

    monkeypatch.setattr(subprocess, "Popen", _never)
    monkeypatch.setattr(conv, "bounded_converter_script_path", lambda: script)

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.require_reviewed_bounded_converter_identity(script)


def test_bounded_converter_launch_is_blocked_when_the_module_file_is_modified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the adapter: a tampered converter never reaches
    ``Popen``."""

    import subprocess

    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    script = _copy_bounded_converter(tmp_path)
    script.write_bytes(script.read_bytes().replace(b"row_max / QUANT_DIVISOR", b"row_max / 64.0"))
    monkeypatch.setattr(bounded, "__file__", str(script))

    def _never(*args: object, **kwargs: object) -> None:
        raise AssertionError("no subprocess may be created for a modified converter")

    monkeypatch.setattr(subprocess, "Popen", _never)
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.BoundedScriptConverter().convert(model_dir=tmp_path / "m", output_dir=tmp_path / "o")


def test_wrong_bounded_converter_size_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Right digest pin, wrong length: truncation must be caught."""

    script = _copy_bounded_converter(tmp_path)
    script.write_bytes(script.read_bytes()[:-100])
    assert script.stat().st_size != common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.size_bytes
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.require_reviewed_bounded_converter_identity(script)


def test_wrong_bounded_converter_hash_at_identical_size_is_rejected(
    tmp_path: Path,
) -> None:
    """The size check alone is not the gate: a same-length substitution
    must still fail on the digest."""

    script = _copy_bounded_converter(tmp_path)
    data = bytearray(script.read_bytes())
    # Flip one byte inside a comment, preserving the exact length.
    for index, byte in enumerate(data):
        if byte == ord("#"):
            data[index] = ord("!")
            break
    script.write_bytes(bytes(data))
    assert len(data) == common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.size_bytes
    assert hashlib.sha256(bytes(data)).hexdigest() != (
        common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.sha256
    )
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.require_reviewed_bounded_converter_identity(script)


def test_wrong_bounded_converter_basename_is_rejected(tmp_path: Path) -> None:
    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    renamed = tmp_path / "not_the_bounded_converter.py"
    renamed.write_bytes(Path(bounded.__file__).read_bytes())
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        conv.require_reviewed_bounded_converter_identity(renamed)


def test_relative_bounded_converter_path_is_rejected() -> None:
    with pytest.raises(conv.ColibriStage2Failure, match="unsafe_directory_rejected"):
        conv.require_reviewed_bounded_converter_identity(
            Path(common.EXPECTED_BOUNDED_CONVERTER_BASENAME)
        )


def test_bounded_converter_identity_is_rechecked_immediately_before_each_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recheck must re-read and re-hash the file every time. A first
    successful launch must never license a second one."""

    import subprocess

    from odysseus_desktop_backend.services import colibri_stage2_bounded_convert as bounded

    script = _copy_bounded_converter(tmp_path)
    monkeypatch.setattr(bounded, "__file__", str(script))

    launches = 0

    class _FakePopen:
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal launches
            launches += 1
            self._handle = None
            self.pid = 4242

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            return 0

        def kill(self) -> None:
            return None

    _patch_job_ownership(monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", _FakePopen)
    converter = conv.BoundedScriptConverter()
    converter.convert(model_dir=tmp_path / "m", output_dir=tmp_path / "o")
    assert launches == 1

    # Tamper AFTER the first successful launch, preserving the byte count
    # so only the digest recheck can catch it.
    data = bytearray(script.read_bytes())
    data[-2] = ord("X") if data[-2] != ord("X") else ord("Y")
    script.write_bytes(bytes(data))
    assert script.stat().st_size == common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.size_bytes

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_failed"):
        converter.convert(model_dir=tmp_path / "m", output_dir=tmp_path / "o")
    assert launches == 1, "the tampered second launch must never have created a process"


def test_bounded_converter_identity_has_no_caller_supplied_expected_hash() -> None:
    """There must be no parameter through which a caller could supply the
    hash to compare against."""

    import inspect

    parameters = inspect.signature(conv.require_reviewed_bounded_converter_identity).parameters
    assert list(parameters) == ["script_path"]


# ---------------------------------------------------------------------------
# Job Object accounting boundary
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_assignment_failure_cannot_launch_an_unowned_converter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the converter cannot be brought under ownership it must never be
    allowed to run at all.

    The process is created suspended, so at the moment assignment fails it
    has executed no instruction and spawned nothing. Proven here by having
    the child write a marker file: a converter that was permitted to run
    would leave one behind."""

    marker = tmp_path / "the-converter-ran.txt"
    monkeypatch.setattr(conv, "_assign_process_to_job", lambda job, process: False)

    def _must_not_query(job: object) -> tuple[int, int]:
        raise AssertionError("peak memory must not be queried without a confirmed assignment")

    monkeypatch.setattr(conv, "_peak_job_memory", _must_not_query)

    with pytest.raises(conv.ColibriStage2Failure, match="job_assignment_failed"):
        conv.run_converter_child(
            [sys.executable, "-c", f"open(r'{marker}','w').write('ran')"],
            deadline_seconds=60,
        )

    time.sleep(0.5)
    assert not marker.exists(), "an unowned converter must never have executed"


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_assignment_failure_reports_a_closed_category_without_peak_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(conv, "_assign_process_to_job", lambda job, process: False)
    with pytest.raises(conv.ColibriStage2Failure) as excinfo:
        conv.run_converter_child([sys.executable, "-c", "raise SystemExit(4)"], deadline_seconds=60)
    failure = excinfo.value
    assert failure.category == "job_assignment_failed"
    # No memory claim of any kind accompanies an unowned run.
    assert "peak_memory_bytes" not in failure.numeric_metadata
    assert "peak_commit_bytes" not in failure.numeric_metadata


def test_confirmed_assignment_reports_measured_peak_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setattr(conv, "_assign_process_to_job", lambda job, process: True)
    monkeypatch.setattr(conv, "_peak_job_memory", lambda job: (205_520_896, 206_569_472))

    evidence = conv.run_converter_child([sys.executable, "-c", "pass"], deadline_seconds=60)
    assert evidence.peak_memory_state == conv.MEMORY_ACCOUNTING_MEASURED
    assert evidence.peak_memory_bytes == 205_520_896
    assert evidence.peak_commit_bytes == 206_569_472


def test_a_confirmed_assignment_whose_query_fails_is_still_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Assignment succeeding does not guarantee the query does. The state
    is derived from what was actually obtained, never from intent."""

    import sys

    monkeypatch.setattr(conv, "_assign_process_to_job", lambda job, process: True)
    monkeypatch.setattr(conv, "_peak_job_memory", lambda job: (None, None))

    evidence = conv.run_converter_child([sys.executable, "-c", "pass"], deadline_seconds=60)
    assert evidence.peak_memory_state == conv.MEMORY_ACCOUNTING_UNAVAILABLE
    assert evidence.peak_memory_bytes is None


def test_assign_process_to_job_returns_false_when_the_api_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The defect being fixed: a false return from
    ``AssignProcessToJobObject`` must be observed, not discarded."""

    assert conv._assign_process_to_job(None, object()) is False

    class _NoHandle:
        _handle = None

    assert conv._assign_process_to_job(1234, _NoHandle()) is False


def test_evidence_cannot_be_constructed_claiming_unavailable_with_values() -> None:
    """The dataclass itself refuses the incoherent combination, so no code
    path can assemble a misleading record."""

    with pytest.raises(ValueError):
        conv.ConversionRunEvidence(
            elapsed_ms=1,
            peak_memory_bytes=123,
            peak_commit_bytes=456,
            peak_memory_state=conv.MEMORY_ACCOUNTING_UNAVAILABLE,
        )
    with pytest.raises(ValueError):
        conv.ConversionRunEvidence(
            elapsed_ms=1, peak_memory_bytes=None, peak_commit_bytes=None, peak_memory_state="guess"
        )


def test_capture_rejects_unavailable_accounting_that_carries_peak_values() -> None:
    """Same boundary at the capture layer, so a hand-built shard result
    cannot smuggle an unconfirmed number into evidence."""

    results = [
        conv.ShardTransactionResult(
            source_basename=basename,
            source_size_bytes=1,
            source_sha256="a" * 64,
            source_verified=True,
            source_deleted=True,
            converted_basename=basename,
            converted_size_bytes=1,
            converted_sha256="b" * 64,
            partial_cleanup_complete=True,
            temporary_output_cleanup_complete=True,
            elapsed_ms=1,
            conversion_peak_memory_bytes=999,
            conversion_peak_memory_state=conv.MEMORY_ACCOUNTING_UNAVAILABLE,
            converter_kind=common.CONVERTER_KIND_BOUNDED,
        )
        for basename in SHARD_BASENAMES
    ]
    with pytest.raises(ValueError, match="unavailable memory accounting"):
        conv.build_conversion_capture(
            source_config=_entry(CONFIG_BASENAME, b"{}"),
            source_config_verified=True,
            source_config_moved_to_final=True,
            converted_config_sha256="c" * 64,
            converted_config_size_bytes=2,
            shard_results=results,
            dependency_versions={"torch": "2.8.0"},
            total_elapsed_ms=1,
            cleanup_complete=True,
        )


def test_capture_defaults_to_unavailable_accounting_for_a_reused_shard() -> None:
    """A reused shard ran no converter, so it has no memory measurement --
    and must not imply one."""

    results = [
        conv.ShardTransactionResult(
            source_basename=basename,
            source_size_bytes=1,
            source_sha256="a" * 64,
            source_verified=True,
            source_deleted=True,
            converted_basename=basename,
            converted_size_bytes=1,
            converted_sha256="b" * 64,
            partial_cleanup_complete=True,
            temporary_output_cleanup_complete=True,
            elapsed_ms=1,
            converted_reused=True,
            converter_kind=common.CONVERTER_KIND_BOUNDED,
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
        total_elapsed_ms=1,
        cleanup_complete=True,
    )
    for shard in capture["shards"]:
        assert shard["conversion_peak_memory_state"] == "unavailable"
        assert shard["conversion_peak_memory_bytes"] is None
        assert shard["conversion_peak_commit_bytes"] is None


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
            self.pid = 4242

        def wait(self, timeout=None):  # type: ignore[no-untyped-def]
            return 0

        def kill(self) -> None:
            return None

    _patch_job_ownership(monkeypatch)
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


# ---------------------------------------------------------------------------
# Converter provenance: the capture names the converter that actually ran
# ---------------------------------------------------------------------------


class _KindedConverter:
    """A synthetic converter reporting a chosen reviewed kind, standing in
    for whichever adapter a test wants to have executed."""

    def __init__(self, kind: str, payload: bytes = b"converted") -> None:
        self.converter_kind = kind
        self.payload = payload

    def convert(self, *, model_dir: Path, output_dir: Path) -> conv.ConversionRunEvidence:
        (output_dir / CONFIG_BASENAME).write_bytes((model_dir / CONFIG_BASENAME).read_bytes())
        shards = [e.name for e in model_dir.iterdir() if e.name != CONFIG_BASENAME]
        assert len(shards) == 1
        (output_dir / shards[0]).write_bytes(self.payload)
        return conv.ConversionRunEvidence(
            elapsed_ms=1,
            peak_memory_bytes=None,
            peak_commit_bytes=None,
            converter_kind=self.converter_kind,
        )


def _run_one_shard(tmp_path: Path, converter: object) -> conv.ShardTransactionResult:
    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (source / SHARD_BASENAMES[0]).write_bytes(payload)
    return conv.run_shard_transaction(
        expected_source=_entry(SHARD_BASENAMES[0], payload),
        destination_dir=source,
        config_path=source / CONFIG_BASENAME,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        downloader=_CountingDownloader({}),
        converter=converter,
    )


def _capture_for(results: list[conv.ShardTransactionResult]) -> dict:
    return conv.build_conversion_capture(
        source_config=_entry(CONFIG_BASENAME, b"{}"),
        source_config_verified=True,
        source_config_moved_to_final=True,
        converted_config_sha256="c" * 64,
        converted_config_size_bytes=2,
        shard_results=results,
        dependency_versions={"torch": "2.8.0"},
        total_elapsed_ms=1,
        cleanup_complete=True,
    )


def _result_for(basename: str, kind: str | None) -> conv.ShardTransactionResult:
    return conv.ShardTransactionResult(
        source_basename=basename,
        source_size_bytes=1,
        source_sha256="a" * 64,
        source_verified=True,
        source_deleted=True,
        converted_basename=basename,
        converted_size_bytes=1,
        converted_sha256="b" * 64,
        partial_cleanup_complete=True,
        temporary_output_cleanup_complete=True,
        elapsed_ms=1,
        converter_kind=kind,
    )


def test_bounded_execution_captures_only_the_bounded_identity(tmp_path: Path) -> None:
    """The defect being fixed: a bounded conversion previously claimed the
    upstream script's identity."""

    result = _run_one_shard(tmp_path, _KindedConverter(common.CONVERTER_KIND_BOUNDED))
    assert result.converter_kind == common.CONVERTER_KIND_BOUNDED

    capture = _capture_for(
        [_result_for(name, common.CONVERTER_KIND_BOUNDED) for name in SHARD_BASENAMES]
    )
    bounded_identity = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY
    upstream = common.REVIEWED_CONVERTER_IDENTITY

    assert capture["converters"] == [
        {
            "converter_kind": common.CONVERTER_KIND_BOUNDED,
            "basename": bounded_identity.basename,
            "size_bytes": bounded_identity.size_bytes,
            "sha256": bounded_identity.sha256,
        }
    ]
    for shard in capture["shards"]:
        assert shard["converter_kind"] == common.CONVERTER_KIND_BOUNDED
        assert shard["converter_basename"] == bounded_identity.basename
        assert shard["converter_sha256"] == bounded_identity.sha256
    # The upstream script's identity appears nowhere at all.
    serialized = json.dumps(capture)
    assert upstream.sha256 not in serialized
    assert upstream.basename not in serialized


def test_pinned_script_execution_captures_only_the_upstream_identity(tmp_path: Path) -> None:
    result = _run_one_shard(tmp_path, _KindedConverter(common.CONVERTER_KIND_PINNED_SCRIPT))
    assert result.converter_kind == common.CONVERTER_KIND_PINNED_SCRIPT

    capture = _capture_for(
        [_result_for(name, common.CONVERTER_KIND_PINNED_SCRIPT) for name in SHARD_BASENAMES]
    )
    upstream = common.REVIEWED_CONVERTER_IDENTITY
    bounded_identity = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY

    assert capture["converters"] == [
        {
            "converter_kind": common.CONVERTER_KIND_PINNED_SCRIPT,
            "basename": upstream.basename,
            "size_bytes": upstream.size_bytes,
            "sha256": upstream.sha256,
        }
    ]
    for shard in capture["shards"]:
        assert shard["converter_sha256"] == upstream.sha256
    assert bounded_identity.sha256 not in json.dumps(capture)


def test_the_two_converter_identities_are_actually_distinct() -> None:
    """Without this the provenance tests above would be vacuous."""

    bounded_identity = common.REVIEWED_BOUNDED_CONVERTER_IDENTITY
    upstream = common.REVIEWED_CONVERTER_IDENTITY
    assert bounded_identity.basename != upstream.basename
    assert bounded_identity.sha256 != upstream.sha256
    assert bounded_identity.size_bytes != upstream.size_bytes


def test_converter_identities_cannot_be_swapped_or_caller_overridden() -> None:
    """A caller may select a closed *kind*; it may never supply a
    basename, size, hash, or identity object."""

    import inspect

    parameters = set(inspect.signature(conv.build_conversion_capture).parameters)
    assert not [
        name for name in parameters if "converter" in name
    ], "build_conversion_capture must accept no converter identity parameter"

    # Only reviewed kinds resolve at all.
    with pytest.raises(ValueError):
        common.reviewed_identity_for_converter_kind("something_else")
    with pytest.raises(ValueError):
        _result_for(SHARD_BASENAMES[0], "forged_converter")

    # And the mapping itself is immutable.
    with pytest.raises(TypeError):
        common.REVIEWED_CONVERTER_IDENTITY_BY_KIND[  # type: ignore[index]
            common.CONVERTER_KIND_BOUNDED
        ] = object()


def test_a_shard_that_cannot_name_its_converter_is_rejected() -> None:
    """No default, no fallback: an unattributable conversion cannot be
    captured at all."""

    results = [_result_for(name, common.CONVERTER_KIND_BOUNDED) for name in SHARD_BASENAMES]
    results[0] = _result_for(SHARD_BASENAMES[0], None)
    with pytest.raises(ValueError, match="which converter produced it"):
        _capture_for(results)


def test_resumed_runs_retain_the_identity_of_the_converter_that_made_them(
    tmp_path: Path,
) -> None:
    """A resumed run may use a different converter than the interrupted
    one -- the upstream-script -> bounded migration this PR enables. Each
    shard must keep the identity of whichever converter actually produced
    it."""

    source, converted, scratch = _setup(tmp_path)
    payload = b"source-payload"
    artifact = b"made-by-the-upstream-script"
    (source / CONFIG_BASENAME).write_bytes(b'{"c": 1}')
    (converted / SHARD_BASENAMES[0]).write_bytes(artifact)
    record = conv.ConvertedShardRecord(
        basename=SHARD_BASENAMES[0],
        size_bytes=len(artifact),
        sha256=_sha256(artifact),
        converter_kind=common.CONVERTER_KIND_PINNED_SCRIPT,
    )

    # This run is configured with the *bounded* converter, but the shard
    # being resumed was made by the upstream script.
    result = conv.run_shard_transaction(
        expected_source=_entry(SHARD_BASENAMES[0], payload),
        destination_dir=source,
        config_path=source / CONFIG_BASENAME,
        final_converted_dir=converted,
        temp_output_parent=scratch,
        downloader=_CountingDownloader({}),
        converter=_KindedConverter(common.CONVERTER_KIND_BOUNDED),
        converted_record=record,
    )
    assert result.converted_reused is True
    assert result.converter_kind == common.CONVERTER_KIND_PINNED_SCRIPT

    capture = _capture_for(
        [
            result,
            _result_for(SHARD_BASENAMES[1], common.CONVERTER_KIND_BOUNDED),
            _result_for(SHARD_BASENAMES[2], common.CONVERTER_KIND_BOUNDED),
        ]
    )
    # Both converters really did contribute, and the capture says so.
    assert [entry["converter_kind"] for entry in capture["converters"]] == [
        common.CONVERTER_KIND_BOUNDED,
        common.CONVERTER_KIND_PINNED_SCRIPT,
    ]
    assert capture["shards"][0]["converter_sha256"] == common.REVIEWED_CONVERTER_IDENTITY.sha256
    assert (
        capture["shards"][1]["converter_sha256"]
        == common.REVIEWED_BOUNDED_CONVERTER_IDENTITY.sha256
    )


def test_the_resume_ledger_persists_the_producing_converter(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    records = {
        SHARD_BASENAMES[0]: conv.ConvertedShardRecord(
            basename=SHARD_BASENAMES[0],
            size_bytes=1,
            sha256="a" * 64,
            converter_kind=common.CONVERTER_KIND_PINNED_SCRIPT,
        )
    }
    conv.write_resume_ledger(converted, records)
    document = json.loads((converted / common.RESUME_LEDGER_BASENAME).read_text(encoding="utf-8"))
    assert document["converted_shards"][0]["converter_kind"] == (
        common.CONVERTER_KIND_PINNED_SCRIPT
    )
    assert conv.read_resume_ledger(converted) == records


def test_a_ledger_without_a_converter_kind_fails_closed(tmp_path: Path) -> None:
    """A pre-existing ledger that cannot say which converter made its
    artifacts is unusable rather than silently defaulted."""

    _, converted, _ = _setup(tmp_path)
    conv.write_resume_ledger(
        converted,
        {
            SHARD_BASENAMES[0]: conv.ConvertedShardRecord(
                basename=SHARD_BASENAMES[0],
                size_bytes=1,
                sha256="a" * 64,
                converter_kind=common.CONVERTER_KIND_BOUNDED,
            )
        },
    )
    path = converted / common.RESUME_LEDGER_BASENAME
    document = json.loads(path.read_text(encoding="utf-8"))
    del document["converted_shards"][0]["converter_kind"]
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(conv.ColibriStage2Failure, match="resume_state_invalid"):
        conv.read_resume_ledger(converted)


def test_a_ledger_naming_an_unreviewed_converter_fails_closed(tmp_path: Path) -> None:
    _, converted, _ = _setup(tmp_path)
    conv.write_resume_ledger(
        converted,
        {
            SHARD_BASENAMES[0]: conv.ConvertedShardRecord(
                basename=SHARD_BASENAMES[0],
                size_bytes=1,
                sha256="a" * 64,
                converter_kind=common.CONVERTER_KIND_BOUNDED,
            )
        },
    )
    path = converted / common.RESUME_LEDGER_BASENAME
    document = json.loads(path.read_text(encoding="utf-8"))
    document["converted_shards"][0]["converter_kind"] = "some_other_converter"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(conv.ColibriStage2Failure, match="resume_state_invalid"):
        conv.read_resume_ledger(converted)


def test_real_adapters_declare_their_own_fixed_kind() -> None:
    """The kind is a property of the adapter class, not something the
    caller passes in at conversion time."""

    assert conv.BoundedScriptConverter().converter_kind == common.CONVERTER_KIND_BOUNDED
    pinned = conv.PinnedScriptConverter(converter_script_path=Path("C:/x/convert_olmoe.py"))
    assert pinned.converter_kind == common.CONVERTER_KIND_PINNED_SCRIPT
    assert set(common.CONVERTER_KINDS) == {
        common.CONVERTER_KIND_BOUNDED,
        common.CONVERTER_KIND_PINNED_SCRIPT,
    }


# ---------------------------------------------------------------------------
# Process-tree ownership
# ---------------------------------------------------------------------------

_TREE_CHILD = (
    "import subprocess, sys, time\n"
    "marker, grandchild_src = sys.argv[1], sys.argv[2]\n"
    "subprocess.Popen([sys.executable, '-c', open(grandchild_src).read(), marker])\n"
    "time.sleep(120)\n"
)

_GRANDCHILD = (
    "import sys, time\n"
    "path = sys.argv[1]\n"
    "for index in range(100000):\n"
    "    open(path, 'w').write(str(index))\n"
    "    time.sleep(0.05)\n"
)


def _tree_argv(tmp_path: Path) -> tuple[list[str], Path]:
    marker = tmp_path / "grandchild-progress.txt"
    grandchild_src = tmp_path / "grandchild.py"
    grandchild_src.write_text(_GRANDCHILD, encoding="utf-8")
    return [sys.executable, "-c", _TREE_CHILD, str(marker), str(grandchild_src)], marker


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_timeout_kills_the_full_process_tree(tmp_path: Path) -> None:
    """A converter that spawns a grandchild -- exactly what the venv
    launcher does -- must be killed in its entirety by the timeout."""

    argv, marker = _tree_argv(tmp_path)
    with pytest.raises(conv.ColibriStage2Failure) as excinfo:
        conv.run_converter_child(argv, deadline_seconds=2.0)
    assert excinfo.value.category == "conversion_timeout"

    # The grandchild really did start, so the test is not vacuous.
    assert marker.exists(), "the grandchild must have started for this test to mean anything"


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_a_grandchild_cannot_continue_writing_after_timeout_returns(tmp_path: Path) -> None:
    """The load-bearing ordering: cleanup deletes the temporary output
    directory right after this returns, so nothing may still be writing
    into it."""

    argv, marker = _tree_argv(tmp_path)
    with pytest.raises(conv.ColibriStage2Failure, match="conversion_timeout"):
        conv.run_converter_child(argv, deadline_seconds=2.0)

    assert marker.exists()
    # Sample the grandchild's output well after the call returned. A
    # survivor would still be incrementing it.
    first = marker.read_bytes()
    time.sleep(1.5)
    assert marker.read_bytes() == first, "a surviving grandchild is still writing"


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_cleanup_occurs_only_after_full_tree_termination(tmp_path: Path) -> None:
    """Deleting the temp directory must succeed immediately after the
    timeout, which it cannot do while a grandchild still holds files in
    it."""

    work = tmp_path / "temp-output"
    work.mkdir()
    marker = work / "grandchild-progress.txt"
    grandchild_src = tmp_path / "grandchild.py"
    grandchild_src.write_text(_GRANDCHILD, encoding="utf-8")
    argv = [sys.executable, "-c", _TREE_CHILD, str(marker), str(grandchild_src)]

    with pytest.raises(conv.ColibriStage2Failure, match="conversion_timeout"):
        conv.run_converter_child(argv, deadline_seconds=2.0)

    # This is what run_shard_transaction does next.
    import shutil

    shutil.rmtree(work, ignore_errors=False)
    assert not work.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_the_job_owns_every_member_of_the_tree(tmp_path: Path) -> None:
    """Directly observe that the grandchild is inside the job, which is
    what makes whole-tree termination and accounting meaningful."""

    argv, marker = _tree_argv(tmp_path)
    observed: dict[str, int | None] = {}
    real_await = conv._await_empty_job

    def _observing_await(job, **kwargs):  # type: ignore[no-untyped-def]
        observed["members_before_wait"] = conv._job_process_count(job)
        return real_await(job, **kwargs)

    conv._await_empty_job = _observing_await  # type: ignore[assignment]
    try:
        with pytest.raises(conv.ColibriStage2Failure, match="conversion_timeout"):
            conv.run_converter_child(argv, deadline_seconds=2.5)
    finally:
        conv._await_empty_job = real_await  # type: ignore[assignment]

    assert marker.exists()
    # Terminate is asynchronous, so the count at that instant may already
    # be dropping; what matters is that the job was tracking members and
    # ended empty.
    assert observed["members_before_wait"] is not None


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_normal_successful_execution_is_unchanged(tmp_path: Path) -> None:
    """The ownership machinery must not disturb the ordinary path: a
    quick, clean converter still succeeds, still runs, and still reports
    measured whole-tree memory."""

    marker = tmp_path / "ran.txt"
    evidence = conv.run_converter_child(
        [sys.executable, "-c", f"open(r'{marker}','w').write('done')"],
        deadline_seconds=60,
        converter_kind=common.CONVERTER_KIND_BOUNDED,
    )
    assert marker.read_text(encoding="utf-8") == "done"
    assert evidence.converter_kind == common.CONVERTER_KIND_BOUNDED
    assert evidence.peak_memory_state == conv.MEMORY_ACCOUNTING_MEASURED
    assert evidence.peak_memory_bytes and evidence.peak_memory_bytes > 0


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_a_nonzero_exit_still_classifies_normally_under_ownership() -> None:
    with pytest.raises(conv.ColibriStage2Failure) as excinfo:
        conv.run_converter_child(
            [sys.executable, "-c", "raise SystemExit(5)"], deadline_seconds=60
        )
    assert excinfo.value.category == "conversion_nonzero_exit"
    assert excinfo.value.numeric_metadata["exit_code"] == 5


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_the_converter_is_created_suspended_and_resumed_only_after_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ordering proof: assignment must happen while the process is still
    suspended, i.e. strictly before it is resumed."""

    import subprocess

    order: list[str] = []
    real_popen = subprocess.Popen

    class _OrderedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, argv, **kwargs):  # type: ignore[no-untyped-def]
            order.append(f"create:{hex(kwargs.get('creationflags', 0))}")
            super().__init__(argv, **kwargs)

    real_assign = conv._assign_process_to_job
    real_resume = conv._resume_process_tree

    def _assign(job, process):  # type: ignore[no-untyped-def]
        order.append("assign")
        return real_assign(job, process)

    def _resume(pid):  # type: ignore[no-untyped-def]
        order.append("resume")
        return real_resume(pid)

    monkeypatch.setattr(subprocess, "Popen", _OrderedPopen)
    monkeypatch.setattr(conv, "_assign_process_to_job", _assign)
    monkeypatch.setattr(conv, "_resume_process_tree", _resume)

    marker = tmp_path / "ok.txt"
    conv.run_converter_child(
        [sys.executable, "-c", f"open(r'{marker}','w').write('x')"], deadline_seconds=60
    )

    assert order[0].startswith("create:")
    assert hex(conv._CREATE_SUSPENDED) in order[0]
    assert order[1:3] == ["assign", "resume"]
    assert marker.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_a_missing_job_fails_closed_rather_than_running_unowned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "must-not-run.txt"
    monkeypatch.setattr(conv, "_create_owning_job", lambda: None)
    with pytest.raises(conv.ColibriStage2Failure, match="job_create_failed"):
        conv.run_converter_child(
            [sys.executable, "-c", f"open(r'{marker}','w').write('ran')"], deadline_seconds=60
        )
    time.sleep(0.4)
    assert not marker.exists(), "no process may be created without an owning job"


@pytest.mark.skipif(sys.platform != "win32", reason="Job Object ownership is Windows-only")
def test_a_failed_resume_terminates_the_tree_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "must-not-run.txt"
    monkeypatch.setattr(conv, "_resume_process_tree", lambda pid: False)
    with pytest.raises(conv.ColibriStage2Failure, match="process_resume_failed"):
        conv.run_converter_child(
            [sys.executable, "-c", f"open(r'{marker}','w').write('ran')"], deadline_seconds=60
        )
    time.sleep(0.4)
    assert not marker.exists(), "a never-resumed converter must never have run"


def test_the_owning_job_is_configured_to_kill_on_close() -> None:
    """The final safeguard: whatever remains inside the job dies when the
    last handle to it closes."""

    assert conv._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE == 0x00002000
    if sys.platform != "win32":
        assert conv._create_owning_job() is None
        return
    job = conv._create_owning_job()
    assert job is not None
    try:
        assert conv._job_process_count(job) == 0
    finally:
        conv._close_job(job)
