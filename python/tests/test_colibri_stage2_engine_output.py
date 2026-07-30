"""Tests for the strict pinned-engine output parser and independent oracle.

Every fixture uses the real pinned dialect. No engine is launched.
"""

from __future__ import annotations

import pytest

from odysseus_desktop_backend.services import colibri_stage2_engine_output as parser
from odysseus_desktop_backend.services.colibri_stage2_common import ColibriStage2Failure

EXPECTED_TOKEN = 7785


def engine_output(
    *,
    reference_ids: str = "7785 ",
    generated_ids: str = "7785 ",
    matched: str = "1",
    expected: str = "1",
    model_load_seconds: str = "12.5",
    rss_after_load_gb: str = "6.42",
    peak_rss_gb: str = "6.51",
    rate: str = "1.85",
    generation_seconds: str = "0.5",
    generated_count: str = "1",
    banner: bool = True,
    model_load_line: str | None = None,
) -> bytes:
    """The exact stdout shape of the pinned ``c/olmoe.c`` at commit 72d3d372.

    Transcribed from its ``main``: the streaming-engine banner, the complete
    resident-weights/RSS line, a blank line, the ``Reference``/``C engine``
    pair (each emitted as ``printf("%d ")`` per token, hence the trailing
    space), ``Matching tokens``, a blank line, ``PEAK RSS``, the cache-hit
    line, and ``Speed``.
    """

    lines: list[str] = []
    if banner:
        lines.append("== Streaming C engine, cache = 8 experts/layer, experts @ 8-bit ==")
    if model_load_line is None:
        model_load_line = (
            f"resident weights loaded in {model_load_seconds}s"
            f" | RSS after load: {rss_after_load_gb} GB"
        )
    lines.append(model_load_line)
    lines.append("")
    lines.append(f"Reference: {reference_ids}")
    lines.append(f"C engine : {generated_ids}")
    lines.append(f"Matching tokens: {matched}/{expected}")
    lines.append("")
    lines.append(f"PEAK RSS: {peak_rss_gb} GB")
    lines.append("Expert cache hit rate: 92.3%  (hit=1187 miss=98)")
    lines.append(f"Speed: {rate} tok/s ({generation_seconds}s for {generated_count} tokens)")
    return ("\n".join(lines) + "\n").encode("utf-8")


# The shortened line the tests previously used. The real engine never prints
# it, and the parser must reject it.
SHORTENED_MODEL_LOAD_LINE = "resident weights loaded in 12.5s"


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_parses_the_full_reviewed_dialect() -> None:
    parsed = parser.parse_engine_output(engine_output())
    assert parsed.reference_token_ids == (7785,)
    assert parsed.generated_token_ids == (7785,)
    assert parsed.matched_count == 1
    assert parsed.expected_count == 1
    assert parsed.model_load_seconds == pytest.approx(12.5)
    assert parsed.rss_after_load_gb == pytest.approx(6.42)
    assert parsed.generation_seconds == pytest.approx(0.5)
    assert parsed.generation_rate_tokens_per_second == pytest.approx(1.85)
    assert parsed.reported_generated_token_count == 1


def test_the_real_source_line_shape_is_what_is_accepted() -> None:
    # Byte-for-byte the line `c/olmoe.c` prints at the pinned commit:
    #   printf("resident weights loaded in %.1fs | RSS after load: %.2f GB\n", ...)
    payload = engine_output()
    assert b"resident weights loaded in 12.5s | RSS after load: 6.42 GB\n" in payload
    assert b"== Streaming C engine, cache = 8 experts/layer, experts @ 8-bit ==\n" in payload
    assert b"\nReference: 7785 \n" in payload
    assert b"C engine : 7785 \n" in payload
    assert b"\nPEAK RSS: 6.51 GB\n" in payload
    assert b"Speed: 1.85 tok/s (0.5s for 1 tokens)\n" in payload
    parser.parse_engine_output(payload)


def test_shortened_model_load_line_is_rejected() -> None:
    # Regression: the parser previously required the line to END after the
    # seconds value, which the real engine never prints -- so every real
    # successful run would have failed as timing_evidence_invalid. The
    # shortened form must now be rejected outright.
    shortened = engine_output(model_load_line=SHORTENED_MODEL_LOAD_LINE)
    assert b"resident weights loaded in 12.5s\n" in shortened
    assert b"RSS after load" not in shortened
    with pytest.raises(ColibriStage2Failure, match="timing_evidence_invalid"):
        parser.parse_engine_output(shortened)


@pytest.mark.parametrize(
    "line",
    [
        "resident weights loaded in 12.5s |",
        "resident weights loaded in 12.5s | RSS after load:",
        "resident weights loaded in 12.5s | RSS after load: 6.42",
        "resident weights loaded in 12.5s | RSS after load: 6.42 MB",
        "resident weights loaded in 12.5s | RSS after load: 6.42 GB extra",
        "resident weights loaded in | RSS after load: 6.42 GB",
    ],
)
def test_partial_or_extended_model_load_lines_are_rejected(line: str) -> None:
    with pytest.raises(ColibriStage2Failure, match="timing_evidence_invalid"):
        parser.parse_engine_output(engine_output(model_load_line=line))


def test_rss_after_load_is_validated_and_recorded() -> None:
    parsed = parser.parse_engine_output(engine_output(rss_after_load_gb="0.00"))
    assert parsed.rss_after_load_gb == 0.0
    parsed = parser.parse_engine_output(engine_output(rss_after_load_gb="13.75"))
    assert parsed.rss_after_load_gb == pytest.approx(13.75)


@pytest.mark.parametrize("rss", ["nan", "inf", "-1.0", "99999999"])
def test_out_of_bounds_rss_is_rejected(rss: str) -> None:
    with pytest.raises(ColibriStage2Failure, match="timing_evidence_invalid"):
        parser.parse_engine_output(engine_output(rss_after_load_gb=rss))


def test_peak_rss_and_cache_hit_lines_are_tolerated_not_parsed() -> None:
    # They are real engine output but carry no authority here: the peak-memory
    # figure that matters comes from the owning Job Object.
    parsed = parser.parse_engine_output(engine_output(peak_rss_gb="123.45"))
    serialized = repr(parsed)
    assert "123.45" not in serialized
    assert "cache hit" not in serialized
    assert "92.3" not in serialized


def test_banner_lines_are_tolerated_and_retained_nowhere() -> None:
    noisy = (
        b"olmoe build 2026-07-30\nloading snap...\n"
        + engine_output(banner=False)
        + b"goodbye\n"
    )
    parsed = parser.parse_engine_output(noisy)
    assert parsed.generated_token_ids == (7785,)
    serialized = repr(parsed)
    assert "olmoe build" not in serialized
    assert "loading snap" not in serialized
    assert "goodbye" not in serialized


def test_crlf_line_endings_parse() -> None:
    crlf = engine_output().replace(b"\n", b"\r\n")
    parsed = parser.parse_engine_output(crlf)
    assert parsed.generated_token_ids == (7785,)


def test_verify_returns_the_parsed_generated_token() -> None:
    parsed = parser.parse_engine_output(engine_output())
    assert parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN) == 7785


# ---------------------------------------------------------------------------
# The oracle is independent of the engine's own match count
# ---------------------------------------------------------------------------


def test_engine_claiming_a_match_for_a_different_token_is_rejected() -> None:
    # The exact case a match-line-only oracle would have accepted.
    parsed = parser.parse_engine_output(engine_output(generated_ids="7786", matched="1"))
    with pytest.raises(ColibriStage2Failure, match="token_identity_mismatch"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_engine_contradicting_its_own_token_lines_is_rejected() -> None:
    parsed = parser.parse_engine_output(engine_output(matched="0"))
    with pytest.raises(ColibriStage2Failure, match="output_internally_inconsistent"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_expected_count_disagreeing_with_the_reference_line_is_rejected() -> None:
    parsed = parser.parse_engine_output(engine_output(expected="2"))
    with pytest.raises(ColibriStage2Failure, match="output_internally_inconsistent"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_wrong_reference_line_is_rejected_before_any_match_count_is_believed() -> None:
    parsed = parser.parse_engine_output(engine_output(reference_ids="99", generated_ids="99"))
    with pytest.raises(ColibriStage2Failure, match="reference_line_mismatch"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_multi_token_reference_is_rejected() -> None:
    parsed = parser.parse_engine_output(
        engine_output(reference_ids="7785 7785", generated_ids="7785 7785", matched="2", expected="2")
    )
    with pytest.raises(ColibriStage2Failure, match="reference_line_mismatch"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_extra_generated_tokens_are_rejected() -> None:
    parsed = parser.parse_engine_output(engine_output(generated_ids="7785 12"))
    with pytest.raises(ColibriStage2Failure, match="generated_token_count_unexpected"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_speed_line_claiming_more_than_one_token_is_rejected() -> None:
    # Structurally well-formed, so it parses -- but the one-token contract
    # rejects an engine that says it generated four tokens.
    parsed = parser.parse_engine_output(engine_output(generated_count="4"))
    assert parsed.reported_generated_token_count == 4
    with pytest.raises(ColibriStage2Failure, match="timing_evidence_invalid"):
        parser.verify_one_token_output(parsed, expected_token_id=EXPECTED_TOKEN)


def test_verify_never_substitutes_the_expected_token() -> None:
    # Given output whose generated token is the expected one, the returned
    # value must still have come from the C-engine line. Proven by feeding a
    # different expected token and requiring a mismatch rather than a pass.
    parsed = parser.parse_engine_output(engine_output(generated_ids="7785"))
    with pytest.raises(ColibriStage2Failure, match="reference_line_mismatch"):
        parser.verify_one_token_output(parsed, expected_token_id=4242)


# ---------------------------------------------------------------------------
# Missing, duplicate, and malformed lines
# ---------------------------------------------------------------------------


def _without(prefix: bytes) -> bytes:
    return b"\n".join(line for line in engine_output().split(b"\n") if not line.startswith(prefix))


@pytest.mark.parametrize(
    ("prefix", "category"),
    [
        (b"Reference:", "malformed_output"),
        (b"C engine", "malformed_output"),
        (b"Matching tokens:", "malformed_output"),
        (b"resident weights", "timing_evidence_invalid"),
        (b"Speed:", "timing_evidence_invalid"),
    ],
)
def test_each_required_line_is_required(prefix: bytes, category: str) -> None:
    with pytest.raises(ColibriStage2Failure, match=category):
        parser.parse_engine_output(_without(prefix))


@pytest.mark.parametrize(
    ("extra", "category"),
    [
        (b"Reference: 7785\n", "duplicate_output_line"),
        (b"C engine : 7785\n", "duplicate_output_line"),
        (b"Matching tokens: 1/1\n", "duplicate_match_line"),
        (b"resident weights loaded in 1.0s | RSS after load: 1.00 GB\n", "timing_evidence_invalid"),
        (b"Speed: 1.0 tok/s (1.0s for 1 tokens)\n", "timing_evidence_invalid"),
    ],
)
def test_each_required_line_must_appear_exactly_once(extra: bytes, category: str) -> None:
    with pytest.raises(ColibriStage2Failure, match=category):
        parser.parse_engine_output(engine_output() + extra)


def test_empty_output_is_rejected() -> None:
    with pytest.raises(ColibriStage2Failure, match="malformed_output"):
        parser.parse_engine_output(b"")


def test_banner_only_output_is_rejected() -> None:
    with pytest.raises(ColibriStage2Failure, match="malformed_output"):
        parser.parse_engine_output(b"olmoe starting\nnothing else\n")


def test_non_numeric_token_ids_are_rejected() -> None:
    with pytest.raises(ColibriStage2Failure, match="malformed_output"):
        parser.parse_engine_output(engine_output(generated_ids="seven"))
    with pytest.raises(ColibriStage2Failure, match="malformed_output"):
        parser.parse_engine_output(engine_output(reference_ids="-1"))


def test_empty_reference_and_generated_lines_are_rejected() -> None:
    with pytest.raises(ColibriStage2Failure, match="malformed_output"):
        parser.parse_engine_output(engine_output(reference_ids=""))
    with pytest.raises(ColibriStage2Failure, match="generated_token_count_unexpected"):
        parser.parse_engine_output(engine_output(generated_ids=""))


def test_absurdly_long_token_lists_are_rejected() -> None:
    many = " ".join(["7785"] * 64)
    with pytest.raises(ColibriStage2Failure, match="malformed_output"):
        parser.parse_engine_output(engine_output(reference_ids=many))


# ---------------------------------------------------------------------------
# Strict decoding and timing bounds
# ---------------------------------------------------------------------------


def test_decoding_is_strict() -> None:
    with pytest.raises(ColibriStage2Failure, match="output_decode_failed"):
        parser.decode_engine_output(b"\xff\xfe\x00bad")
    with pytest.raises(ColibriStage2Failure, match="output_decode_failed"):
        parser.decode_engine_output("already text")  # type: ignore[arg-type]


def test_undecodable_output_never_reaches_the_line_parser() -> None:
    payload = engine_output()[:-1] + b"\xc3\x28\n"
    with pytest.raises(ColibriStage2Failure, match="output_decode_failed"):
        parser.parse_engine_output(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"model_load_seconds": "nan"},
        {"model_load_seconds": "inf"},
        {"model_load_seconds": "-inf"},
        {"model_load_seconds": "-2.0"},
        {"model_load_seconds": "90000"},
        {"generation_seconds": "nan"},
        {"generation_seconds": "-1"},
        {"generation_seconds": "100000"},
        {"rate": "nan"},
        {"rate": "inf"},
        {"rate": "-1"},
        {"rate": "2000000000"},
    ],
)
def test_non_finite_negative_or_absurd_timings_are_rejected(overrides: dict[str, str]) -> None:
    with pytest.raises(ColibriStage2Failure, match="timing_evidence_invalid"):
        parser.parse_engine_output(engine_output(**overrides))


def test_zero_timings_are_accepted_as_real_measurements() -> None:
    parsed = parser.parse_engine_output(
        engine_output(model_load_seconds="0", generation_seconds="0", rate="0")
    )
    assert parsed.model_load_seconds == 0.0
    assert parsed.generation_seconds == 0.0
    assert parsed.generation_rate_tokens_per_second == 0.0


def test_parsed_record_is_frozen() -> None:
    import dataclasses

    parsed = parser.parse_engine_output(engine_output())
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.matched_count = 0  # type: ignore[misc]
