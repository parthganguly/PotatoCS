"""Strict parser for the pinned Colibrì OLMoE engine's one-token output.

The dialect below is transcribed from the reviewed ``c/olmoe.c`` at the
pinned Colibrì commit ``72d3d372``, not inferred from a sample run. Its
``main`` emits, in order::

    == Streaming C engine, cache = <cap> experts/layer, experts @ <bits>-bit ==
    resident weights loaded in <seconds>s | RSS after load: <gb> GB

    Reference: <token ids>
    C engine : <generated token ids>
    Matching tokens: <matched>/<expected>

    PEAK RSS: <gb> GB
    Expert cache hit rate: <pct>%  (hit=<n> miss=<n>)
    Speed: <rate> tok/s (<seconds>s for <n> tokens)

Five of those lines are required and parsed. The ``resident weights`` line is
matched **complete**, including its ``| RSS after load: <gb> GB`` half: the
source prints one line, so a pattern that stopped at the seconds value would
reject every real successful run. The banner, ``PEAK RSS``, and cache-hit
lines are tolerated and parsed for nothing -- the authoritative peak-memory
figure comes from the owning Job Object, not from the engine's self-report.

Note that ``Reference:`` and ``C engine :`` are emitted as ``printf("%d ")``
per token, so both carry a trailing space and both are terminated by the
*next* line's leading newline. Both shapes are handled.

This module turns those bytes into a closed structured record and then
*independently* compares the engine's own generated token against the
reviewed expected token. The engine's ``Matching tokens`` line is treated as
a redundant engine-side consistency proof, never as the oracle: a run is
accepted only when this module's own comparison of the two token lines
agrees with the count the engine printed.

Banner and other unrecognised lines are tolerated (the engine's banner is
not part of the reviewed dialect and cannot be enumerated), but each of the
five required lines must appear exactly once and be exactly well-formed.

Nothing here retains the raw streams: callers pass bounded bytes in and get a
small record of integers and floats out.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from odysseus_desktop_backend.services.colibri_stage2_common import (
    MAX_ENGINE_REPORTED_RATE,
    MAX_ENGINE_REPORTED_RSS_GB,
    MAX_ENGINE_REPORTED_SECONDS,
    ColibriStage2Failure,
)

# Anchored, whitespace-tolerant only where the engine's own formatting is
# known to vary (the padding around `C engine :`). Everything else is exact.
#
# The model-load pattern deliberately requires the *complete* source line:
#   printf("resident weights loaded in %.1fs | RSS after load: %.2f GB\n", ...)
# A pattern ending after the seconds value matches nothing the real engine
# ever prints, so it would turn every successful run into
# `timing_evidence_invalid`.
_MODEL_LOAD_LINE = re.compile(
    r"^resident weights loaded in (\d+(?:\.\d+)?)s \| RSS after load: (\d+(?:\.\d+)?) GB$"
)
_REFERENCE_LINE = re.compile(r"^Reference:[ \t]*(.*)$")
_C_ENGINE_LINE = re.compile(r"^C engine[ \t]*:[ \t]*(.*)$")
_MATCH_LINE = re.compile(r"^Matching tokens: (\d+)/(\d+)$")
_SPEED_LINE = re.compile(
    r"^Speed: (\d+(?:\.\d+)?) tok/s \((\d+(?:\.\d+)?)s for (\d+) tokens\)$"
)

_TOKEN_ID = re.compile(r"^\d+$")
_MAX_TOKEN_ID = 2**31 - 1
# A one-token proof prints one reference token and one generated token. A
# line carrying more is a different contract, not a parsing nuisance.
_MAX_TOKEN_IDS_PER_LINE = 8


@dataclass(frozen=True, slots=True)
class ParsedEngineOutput:
    """Everything the reviewed engine dialect positively stated.

    Only integers and floats: no line text, no path, no prompt, and no part
    of the raw stream survives parsing.
    """

    reference_token_ids: tuple[int, ...]
    generated_token_ids: tuple[int, ...]
    matched_count: int
    expected_count: int
    model_load_seconds: float
    rss_after_load_gb: float
    generation_seconds: float
    generation_rate_tokens_per_second: float
    reported_generated_token_count: int


def decode_engine_output(data: bytes) -> str:
    """Decode engine stdout strictly.

    Strict UTF-8, no replacement characters: a stream this parser cannot read
    exactly is a malformed run, not something to guess at. Lenient decoding
    would let a corrupted `Reference:` line silently become a plausible one.
    """

    if not isinstance(data, (bytes, bytearray)):
        raise ColibriStage2Failure("output_decode_failed")
    try:
        return bytes(data).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ColibriStage2Failure("output_decode_failed") from exc


def _parse_token_ids(text: str, *, empty_category: str) -> tuple[int, ...]:
    fields = text.split()
    if not fields:
        raise ColibriStage2Failure(empty_category)
    if len(fields) > _MAX_TOKEN_IDS_PER_LINE:
        raise ColibriStage2Failure(empty_category)
    ids: list[int] = []
    for field in fields:
        if not _TOKEN_ID.fullmatch(field):
            raise ColibriStage2Failure("malformed_output")
        value = int(field)
        if value > _MAX_TOKEN_ID:
            raise ColibriStage2Failure("malformed_output")
        ids.append(value)
    return tuple(ids)


def _require_bounded_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise ColibriStage2Failure("timing_evidence_invalid") from exc
    if not math.isfinite(value) or value < 0.0 or value > MAX_ENGINE_REPORTED_SECONDS:
        raise ColibriStage2Failure("timing_evidence_invalid")
    return value


def _require_bounded_rate(text: str) -> float:
    try:
        value = float(text)
    except ValueError as exc:
        raise ColibriStage2Failure("timing_evidence_invalid") from exc
    if not math.isfinite(value) or value < 0.0 or value > MAX_ENGINE_REPORTED_RATE:
        raise ColibriStage2Failure("timing_evidence_invalid")
    return value


def _require_bounded_rss_gb(text: str) -> float:
    """Validate the engine's reported resident-set size.

    Recorded as engine-reported evidence rather than discarded, but it is
    never the authoritative memory figure -- that comes from the owning Job
    Object. Non-finite, negative, or absurd readings are rejected outright.
    """

    try:
        value = float(text)
    except ValueError as exc:
        raise ColibriStage2Failure("timing_evidence_invalid") from exc
    if not math.isfinite(value) or value < 0.0 or value > MAX_ENGINE_REPORTED_RSS_GB:
        raise ColibriStage2Failure("timing_evidence_invalid")
    return value


def _single(matches: list[object], *, missing_category: str, duplicate_category: str) -> object:
    if not matches:
        raise ColibriStage2Failure(missing_category)
    if len(matches) > 1:
        raise ColibriStage2Failure(duplicate_category)
    return matches[0]


def parse_engine_output(data: bytes) -> ParsedEngineOutput:
    """Parse the reviewed dialect, requiring exactly one of each line.

    Raises a closed Stage 2 failure for a stream that cannot be decoded, is
    missing a required line, repeats one, malforms one, carries an
    out-of-bounds timing, or contradicts itself.
    """

    text = decode_engine_output(data)
    lines = [line.strip("\r") for line in text.split("\n")]

    model_load: list[re.Match[str]] = []
    reference: list[re.Match[str]] = []
    c_engine: list[re.Match[str]] = []
    match_counts: list[re.Match[str]] = []
    speed: list[re.Match[str]] = []

    for line in lines:
        if not line:
            continue
        for pattern, sink in (
            (_MODEL_LOAD_LINE, model_load),
            (_REFERENCE_LINE, reference),
            (_C_ENGINE_LINE, c_engine),
            (_MATCH_LINE, match_counts),
            (_SPEED_LINE, speed),
        ):
            found = pattern.fullmatch(line)
            if found is not None:
                sink.append(found)
                break
        # Unrecognised lines are engine banner text. They are counted for
        # nothing and retained nowhere.

    # The three token-comparison lines are required first: a stream missing
    # them is malformed at the level that matters, and reporting a timing
    # defect for output that never stated a token at all would misdescribe it.
    reference_match = _single(
        reference, missing_category="malformed_output", duplicate_category="duplicate_output_line"
    )
    c_engine_match = _single(
        c_engine, missing_category="malformed_output", duplicate_category="duplicate_output_line"
    )
    match_line = _single(
        match_counts, missing_category="malformed_output", duplicate_category="duplicate_match_line"
    )
    model_load_match = _single(
        model_load, missing_category="timing_evidence_invalid", duplicate_category="timing_evidence_invalid"
    )
    speed_match = _single(
        speed, missing_category="timing_evidence_invalid", duplicate_category="timing_evidence_invalid"
    )

    reference_ids = _parse_token_ids(
        reference_match.group(1), empty_category="malformed_output"  # type: ignore[union-attr]
    )
    generated_ids = _parse_token_ids(
        c_engine_match.group(1),  # type: ignore[union-attr]
        empty_category="generated_token_count_unexpected",
    )

    matched_count = int(match_line.group(1))  # type: ignore[union-attr]
    expected_count = int(match_line.group(2))  # type: ignore[union-attr]
    if matched_count > _MAX_TOKEN_IDS_PER_LINE or expected_count > _MAX_TOKEN_IDS_PER_LINE:
        raise ColibriStage2Failure("malformed_output")

    model_load_seconds = _require_bounded_seconds(model_load_match.group(1))  # type: ignore[union-attr]
    rss_after_load_gb = _require_bounded_rss_gb(model_load_match.group(2))  # type: ignore[union-attr]
    generation_rate = _require_bounded_rate(speed_match.group(1))  # type: ignore[union-attr]
    generation_seconds = _require_bounded_seconds(speed_match.group(2))  # type: ignore[union-attr]
    reported_generated = int(speed_match.group(3))  # type: ignore[union-attr]
    if reported_generated > _MAX_TOKEN_IDS_PER_LINE:
        raise ColibriStage2Failure("timing_evidence_invalid")

    return ParsedEngineOutput(
        reference_token_ids=reference_ids,
        generated_token_ids=generated_ids,
        matched_count=matched_count,
        expected_count=expected_count,
        model_load_seconds=model_load_seconds,
        rss_after_load_gb=rss_after_load_gb,
        generation_seconds=generation_seconds,
        generation_rate_tokens_per_second=generation_rate,
        reported_generated_token_count=reported_generated,
    )


def verify_one_token_output(parsed: ParsedEngineOutput, *, expected_token_id: int) -> int:
    """Independently verify the run and return the *parsed* generated token.

    The order of checks is the whole point:

    1. the engine's reference line must state exactly the one reviewed
       expected token -- if the engine compared against something else, its
       own match count is meaningless;
    2. the engine must have generated exactly one token;
    3. *this* function compares that generated token to the reviewed
       expected token. This is the oracle;
    4. the engine's own ``Matching tokens`` line must then agree, both with
       the ``1/1`` the reviewed contract requires and with the comparison
       just performed independently. An engine claiming ``1/1`` while its two
       token lines disagree is internally inconsistent and rejected.

    The returned value is always read from the engine's ``C engine`` line.
    It is never substituted from ``expected_token_id``.
    """

    if parsed.reference_token_ids != (expected_token_id,):
        raise ColibriStage2Failure("reference_line_mismatch")
    if len(parsed.generated_token_ids) != 1:
        raise ColibriStage2Failure("generated_token_count_unexpected")
    if parsed.reported_generated_token_count != 1:
        raise ColibriStage2Failure("timing_evidence_invalid")

    generated_token_id = parsed.generated_token_ids[0]
    if generated_token_id != expected_token_id:
        raise ColibriStage2Failure("token_identity_mismatch")

    # Independently recomputed from the two token lines, then required to
    # equal what the engine printed.
    independent_matches = sum(
        1
        for reference_id, generated_id in zip(parsed.reference_token_ids, parsed.generated_token_ids)
        if reference_id == generated_id
    )
    if parsed.expected_count != len(parsed.reference_token_ids):
        raise ColibriStage2Failure("output_internally_inconsistent")
    if parsed.matched_count != independent_matches:
        raise ColibriStage2Failure("output_internally_inconsistent")
    if parsed.matched_count != 1 or parsed.expected_count != 1:
        raise ColibriStage2Failure(
            "match_count_mismatch",
            matched_count=max(0, min(parsed.matched_count, 2**31 - 1)),
            expected_count=max(0, min(parsed.expected_count, 2**31 - 1)),
        )
    return generated_token_id
