"""Correction regressions for neutral information tails (2026-10-26).

The frozen expectations in test_time_conversion.py stay unchanged; these add
the grammatical variants of a neutral "no further details" statement.
"""

from __future__ import annotations

import pytest

from odysseus_desktop_backend.services import time_conversion as tc

REQUEST = "Convert 9:00 tomorrow to UTC."


@pytest.mark.parametrize(
    "tail",
    [
        "No other details are supplied.",
        "No other details were provided.",
        "No further details have been given.",
        "No additional information has been provided.",
        "No more information is available.",
        "No other details.",
        "There are no other details.",
        "There were no further details provided.",
        "No other details are supplied to you.",
        "No other data is available.",
        "Nothing else has been provided.",
        "Nothing more is known.",
        "Nothing else will be supplied.",
        "Those are all the details I have.",
        "This is all the information I have.",
        "I do not have any other details.",
    ],
)
def test_neutral_tails_in_any_grammatical_number_keep_the_local_clarification(tail: str) -> None:
    out = tc.respond(f"{REQUEST} {tail}", None, message_id="m1")
    assert out is not None and out.status == "clarified", tail
    assert out.state["fields"] == {"time": "09:00", "target": "UTC"}
    assert out.state["needs"] == ["date", "source"]


def test_neutral_plural_tail_after_a_complete_request_converts() -> None:
    out = tc.respond(
        "Convert 2026-11-02 09:00 from America/New_York to UTC. No other details are supplied.", None, message_id="m1"
    )
    assert out is not None and out.status == "converted"
    assert "14:00" in out.reply


@pytest.mark.parametrize(
    "text",
    [
        f"{REQUEST} No other details are supplied, so write a poem about it.",
        f"{REQUEST} Other details are supplied below.",
        f"{REQUEST} The details are supplied in the attached notes.",
        f"{REQUEST} No other details are supplied. Then summarize the French Revolution.",
        f"{REQUEST} Then summarize the French Revolution. No other details are supplied.",
        f"{REQUEST} Also explain daylight saving time. No further details have been given.",
        f"{REQUEST} Details are supplied by my manager.",
        "No other details are supplied.",
        "Nothing else has been provided.",
    ],
)
def test_additional_instructions_or_bare_tails_stay_ordinary(text: str) -> None:
    assert tc.respond(text, None, message_id="m1") is None
