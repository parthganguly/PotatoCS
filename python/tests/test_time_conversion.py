"""Deterministic time conversion: recognition, slots, clarification and DST.

Pure-module expectations, frozen before the parser was written. Chat, job and
lifecycle integration lives in test_chat_turn_contracts.py.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from odysseus_desktop_backend.services import time_conversion as tc


def run(text: str, pending: dict[str, Any] | None = None, message_id: str = "m1") -> tc.Outcome:
    outcome = tc.respond(text, pending, message_id=message_id)
    assert outcome is not None, f"expected local handling: {text!r}"
    return outcome


def chain(*texts: str) -> tc.Outcome:
    """Each reply continues from the previous outcome's committed state."""
    pending = None
    outcome = None
    for number, text in enumerate(texts):
        outcome = run(text, pending, message_id=f"m{number}")
        pending = tc.pending_from_metadata({"time_conversion": outcome.state})
    assert outcome is not None
    return outcome


def converted(outcome: tc.Outcome, *parts: str) -> None:
    assert outcome.status == "converted", outcome.reply
    for part in parts:
        assert part in outcome.reply, (part, outcome.reply)
    assert "locally" in outcome.reply


def clarified(outcome: tc.Outcome, *parts: str) -> None:
    assert outcome.status == "clarified", outcome.reply
    for part in parts:
        assert part.lower() in outcome.reply.lower(), (part, outcome.reply)


# ------------------------------------------------- 1. complete requests


def test_region_to_utc() -> None:
    out = run("Convert 2026-11-02 09:00 from America/New_York to UTC")
    converted(out, "2026-11-02", "09:00", "America/New_York (UTC-05:00)", "14:00", "UTC")
    assert out.state["status"] == "converted"
    assert tc.pending_from_metadata({"time_conversion": out.state}) is None


def test_region_to_region_with_pm_and_spelled_month() -> None:
    out = run("Please convert 9:30 PM on March 5, 2027 from Asia/Kolkata to America/Los_Angeles.")
    converted(out, "2027-03-05", "21:30", "Asia/Kolkata (UTC+05:30)", "08:00", "America/Los_Angeles (UTC-08:00)")


def test_fixed_offset_quarter_hour_seconds_and_year_rollover() -> None:
    out = run("What time is 2026-12-31 23:30:15 in UTC+05:45 in Pacific/Kiritimati?")
    converted(out, "2026-12-31", "23:30:15", "2027-01-01", "07:45:15", "UTC+14:00", "next day", "fixed offset")


def test_negative_half_hour_offset() -> None:
    out = run("Convert 2026-07-01 00:15 from UTC-09:30 to UTC")
    converted(out, "2026-07-01", "00:15", "09:45", "UTC-09:30")


def test_quoted_operand_and_politeness() -> None:
    out = run("Could you please convert “2026-04-10 08:00” from Europe/London to Asia/Tokyo, please?")
    converted(out, "Europe/London (UTC+01:00)", "16:00", "Asia/Tokyo (UTC+09:00)")


def test_non_hour_dst_zone_and_previous_day() -> None:
    out = run("Can you convert 7:05 am on 1 January 2026 from Australia/Lord_Howe to UTC?")
    converted(out, "2026-01-01", "07:05", "UTC+11:00", "2025-12-31", "20:05", "previous day")


def test_source_zone_written_next_to_the_time() -> None:
    out = run("Convert 12:00 2026-06-01 America/Chicago to Europe/Berlin")
    converted(out, "America/Chicago (UTC-05:00)", "19:00", "Europe/Berlin (UTC+02:00)")


def test_when_it_is_phrasing() -> None:
    out = run("What time will it be in Asia/Tokyo when it is 18:00 on 2026-02-10 in Europe/Paris?")
    converted(out, "Europe/Paris (UTC+01:00)", "2026-02-11", "02:00", "Asia/Tokyo")


def test_calculated_seconds_are_shown_even_for_minute_input() -> None:
    out = run("Convert 1850-01-01 12:00 from America/New_York to UTC")
    converted(out, "16:56:02", "UTC-04:56:02")


def test_case_insensitive_identifier_is_canonicalized() -> None:
    out = run("convert 2026-11-02 09:00 from america/new_york to utc")
    converted(out, "America/New_York")


# ----------------------------------------- 2. missing / invalid / conflicts


D2 = "Convert “9:00 tomorrow” to UTC. That is all the information I have given you."


def test_d2_asks_for_date_and_source_without_any_conversion() -> None:
    out = run(D2)
    clarified(out, "calendar date", "tomorrow", "source time zone", "YYYY-MM-DD")
    assert not re.search(r"\d{4}-\d{2}-\d{2}", out.reply)  # no guessed date
    assert not re.search(r"UTC[+-]\d", out.reply)  # no guessed or example offset
    assert "/" not in out.reply.replace("Area/Location", "")  # no example zone
    assert out.state["fields"] == {"time": "09:00", "target": "UTC"}
    assert out.state["needs"] == ["date", "source"]
    assert out.state["refs"] == {"time": "m1", "target": "m1"}


@pytest.mark.parametrize(
    ("text", "needs"),
    [
        ("Convert 10:00 on 2026-05-05 from Asia/Tokyo", ["target"]),
        ("Convert 2026-05-05 from Asia/Tokyo to UTC", ["time"]),
        ("Convert 9 on 2026-05-05 from UTC to Asia/Tokyo", ["time"]),
        ("Convert 9 to UTC", ["date", "time", "source"]),
        ("Convert 03/04/2026 10:00 from UTC to Asia/Tokyo", ["date"]),
        ("Convert March 4 10:00 from UTC to Asia/Tokyo", ["date"]),
        ("Convert next Friday at 10:00 from UTC to Asia/Tokyo", ["date"]),
        ("Convert today 10:00 from UTC to Asia/Tokyo", ["date"]),
        ("Convert 2026-02-30 10:00 from UTC to Asia/Tokyo", ["date"]),
        ("Convert 2026-03-04 24:00 from UTC to Asia/Tokyo", ["time"]),
        ("Convert 2026-03-04 10:60 from UTC to Asia/Tokyo", ["time"]),
        ("Convert 2026-12-31 23:59:60 from UTC to Asia/Tokyo", ["time"]),
        ("Convert 2026-03-04 13:00 pm from UTC to Asia/Tokyo", ["time"]),
        ("Convert 2026-03-04 10:00 from CST to UTC", ["source"]),
        ("Convert 2026-03-04 10:00 EST to UTC", ["source"]),
        ("Convert 2026-03-04 10:00 from London to Asia/Tokyo", ["source"]),
        ("Convert 2026-03-04 10:00 from New York to Asia/Tokyo", ["source"]),
        ("Convert 2026-03-04 10:00 from Mars/Olympus to UTC", ["source"]),
        ("Convert 2026-03-04 10:00 from Japan to UTC", ["source"]),
        ("Convert 2026-03-04 10:00 from UTC+15:00 to Asia/Tokyo", ["source"]),
        ("Convert 2026-03-04 10:00 from UTC+05:75 to Asia/Tokyo", ["source"]),
        ("Convert 09:00 at 10:00 on 2026-01-01 from UTC to Asia/Tokyo", ["time"]),
        ("Convert Monday, March 3, 2026 10:00 from UTC to Asia/Tokyo", ["date"]),
        ("Convert 2026-01-01 10:00 to IST", ["source", "target"]),
    ],
)
def test_unresolved_fields_are_named_and_nothing_is_converted(text: str, needs: list[str]) -> None:
    out = run(text)
    assert out.status == "clarified", out.reply
    assert out.state["needs"] == needs
    assert "locally" not in out.reply or "=" not in out.reply
    assert "→" not in out.reply  # the conversion arrow appears only in answers


def test_abbreviation_and_city_are_not_resolved_to_offsets() -> None:
    out = run("Convert 2026-03-04 10:00 from CST to UTC")
    clarified(out, "CST", "IANA", "UTC offset")
    assert not re.search(r"[+-]0?6", out.reply)


def test_valid_fields_are_retained_when_another_is_invalid() -> None:
    out = run("Convert 2026-03-04 10:00 from Mars/Olympus to Asia/Tokyo")
    assert out.state["fields"] == {"date": "2026-03-04", "time": "10:00", "target": "Asia/Tokyo"}
    clarified(out, "Mars/Olympus")


# --------------------------------------------- 3. multi-turn clarification


def test_d2_completes_with_one_combined_follow_up() -> None:
    out = chain(D2, "2026-11-02 America/New_York")
    converted(out, "2026-11-02", "09:00", "America/New_York (UTC-05:00)", "14:00")
    assert out.state["refs"] == {"time": "m0", "target": "m0", "date": "m1", "source": "m1"}


def test_one_field_at_a_time_with_role_question() -> None:
    first = chain("Convert 9:00 on 2026-11-02")
    assert first.state["needs"] == ["source", "target"]
    role = chain("Convert 9:00 on 2026-11-02", "America/New_York")
    clarified(role, "source", "destination")
    assert "source" not in role.state["fields"] and "target" not in role.state["fields"]
    source = chain("Convert 9:00 on 2026-11-02", "America/New_York", "source")
    assert source.state["fields"]["source"] == "America/New_York"
    assert source.state["needs"] == ["target"]
    done = chain("Convert 9:00 on 2026-11-02", "America/New_York", "source", "UTC")
    converted(done, "14:00")


def test_labeled_reply_with_several_fields() -> None:
    out = chain("Convert 08:15 to Europe/Madrid", "source: Asia/Kolkata; date: 2026-01-15")
    converted(out, "Asia/Kolkata (UTC+05:30)", "03:45", "Europe/Madrid (UTC+01:00)")


def test_from_to_reply_fragment_assigns_roles() -> None:
    out = chain("Convert 2026-08-01 10:00", "from Asia/Singapore to UTC")
    converted(out, "02:00")


def test_unlabeled_conflict_needs_confirmation_and_label_resolves_it() -> None:
    conflict = chain("Convert 09:00 to UTC", "10:00")
    clarified(conflict, "09:00", "10:00")
    assert "time" in conflict.state["needs"]
    resolved = chain("Convert 09:00 to UTC", "10:00", "time: 10:00")
    assert resolved.state["fields"]["time"] == "10:00"


def test_labeled_correction_replaces_a_field() -> None:
    out = chain("Convert 09:00 on 2026-03-01 to UTC", "time: 11:30", "source: Europe/Rome")
    converted(out, "11:30", "10:30")


def test_single_word_while_zone_missing_is_a_zone_attempt() -> None:
    out = chain("Convert 09:00 on 2026-03-01 to UTC", "London")
    clarified(out, "London", "IANA")
    abbreviation = chain("Convert 09:00 on 2026-03-01 to UTC", "CST")
    clarified(abbreviation, "CST")


@pytest.mark.parametrize("text", ["thanks", "Thank you!", "ok", "Hello", "bye.", "yes", "no"])
def test_acknowledgments_follow_normal_chat(text: str) -> None:
    pending = tc.pending_from_metadata({"time_conversion": run("Convert 09:00 on 2026-03-01 to UTC").state})
    assert pending is not None
    assert tc.respond(text, pending, message_id="m2") is None


def test_multiword_unlabeled_place_follows_normal_chat_but_labeled_is_an_attempt() -> None:
    pending = tc.pending_from_metadata({"time_conversion": run("Convert 09:00 on 2026-03-01 to UTC").state})
    assert tc.respond("New York", pending, message_id="m2") is None
    clarified(run("source: New York", pending), "New York")


def test_cancel_conversion() -> None:
    out = chain("Convert 09:00 to UTC", "cancel conversion")
    assert out.status == "cancelled"
    assert tc.pending_from_metadata({"time_conversion": out.state}) is None


@pytest.mark.parametrize(
    "text",
    [
        "Different question: what is the capital of France?",
        "What is the capital of France?",
        "Never mind, tell me a joke",
        "Can you explain how DST works?",
    ],
)
def test_topic_change_follows_normal_chat(text: str) -> None:
    pending = tc.pending_from_metadata({"time_conversion": run("Convert 09:00 to UTC").state})
    assert tc.respond(text, pending, message_id="m2") is None


def test_fresh_request_replaces_pending_state_using_only_new_fields() -> None:
    out = chain("Convert 09:00 on 2026-03-01 from Europe/Rome", "Convert 10:00 to UTC")
    assert out.state["fields"] == {"time": "10:00", "target": "UTC"}


def test_word_when_no_zone_is_missing_follows_normal_chat() -> None:
    pending = tc.pending_from_metadata({"time_conversion": run("Convert 09:00 from UTC to Asia/Tokyo").state})
    assert pending is not None and pending["needs"] == ["date"]
    assert tc.respond("banana", pending, message_id="m2") is None


@pytest.mark.parametrize("text", ["2026-11-02", "London", "America/New_York", "earlier", "10:00", "source: UTC"])
def test_no_short_reply_interception_without_pending_state(text: str) -> None:
    assert tc.respond(text, None, message_id="m1") is None


def test_completed_conversion_is_never_reused() -> None:
    done = run("Convert 2026-11-02 09:00 from America/New_York to UTC")
    assert tc.pending_from_metadata({"time_conversion": done.state}) is None


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"time_conversion": "x"},
        {"time_conversion": {"version": 99, "status": "clarified", "fields": {}}},
        {"time_conversion": {"version": 1, "status": "clarified", "fields": {"date": "2026-02-30"}}},
        {"time_conversion": {"version": 1, "status": "clarified", "fields": {"source": "../../etc"}}},
        {"time_conversion": {"version": 1, "status": "clarified", "fields": []}},
    ],
)
def test_malformed_metadata_is_not_pending_state(metadata: dict[str, Any]) -> None:
    assert tc.pending_from_metadata(metadata) is None


# ------------------------------------------------------------ 4. DST


def test_gap_is_reported_not_shifted() -> None:
    out = run("Convert 2026-03-08 02:30 from America/New_York to UTC")
    clarified(out, "does not exist", "America/New_York")
    assert "07:30" not in out.reply and "06:30" not in out.reply and "03:30" not in out.reply


def test_gap_then_replacement_time_converts() -> None:
    out = chain("Convert 2026-03-08 02:30 from America/New_York to UTC", "03:30")
    converted(out, "03:30", "07:30", "UTC-04:00")


def test_fold_asks_for_occurrence_with_both_offsets() -> None:
    out = run("Convert 2026-11-01 01:30 from America/New_York to UTC")
    clarified(out, "earlier", "later", "UTC-04:00", "UTC-05:00")
    assert out.state["needs"] == ["occurrence"]
    assert "05:30" not in out.reply and "06:30" not in out.reply


@pytest.mark.parametrize(("choice", "utc"), [("earlier", "05:30"), ("later", "06:30"), ("the second one", "06:30")])
def test_both_fold_occurrences(choice: str, utc: str) -> None:
    out = chain("Convert 2026-11-01 01:30 from America/New_York to UTC", choice)
    converted(out, utc)


def test_non_hour_fold_and_gap() -> None:
    fold = run("Convert 2026-04-05 01:45 from Australia/Lord_Howe to UTC")
    clarified(fold, "UTC+11:00", "UTC+10:30")
    later = chain("Convert 2026-04-05 01:45 from Australia/Lord_Howe to UTC", "later")
    converted(later, "2026-04-04", "15:15")
    gap = run("Convert 2026-10-04 02:15 from Australia/Lord_Howe to UTC")
    clarified(gap, "does not exist")


def test_changing_time_after_occurrence_choice_revalidates() -> None:
    chosen = chain("Convert 2026-11-01 01:30 from America/New_York", "earlier")
    assert chosen.state["occurrence"] == "earlier"
    assert chosen.state["needs"] == ["target"]
    changed = chain("Convert 2026-11-01 01:30 from America/New_York", "earlier", "time: 01:15; to UTC")
    clarified(changed, "earlier", "later")
    assert changed.state.get("occurrence") is None
    target_only = chain("Convert 2026-11-01 01:30 from America/New_York", "earlier", "to UTC")
    converted(target_only, "05:30")


@pytest.mark.parametrize(("utc", "offset"), [("05:30", "UTC-04:00"), ("06:30", "UTC-05:00")])
def test_destination_fold_is_resolved_by_the_source_instant(utc: str, offset: str) -> None:
    out = run(f"Convert 2026-11-01 {utc} from UTC to America/New_York")
    converted(out, "01:30", f"America/New_York ({offset})")


def test_fixed_offset_has_no_dst_ambiguity() -> None:
    converted(run("Convert 2026-11-01 01:30 from UTC-05:00 to UTC"), "06:30")


@pytest.mark.parametrize(
    "text",
    ["Convert 0001-01-01 00:30 from UTC+05:00 to UTC", "Convert 9999-12-31 23:30 from UTC to UTC+05:00"],
)
def test_out_of_range_asks_for_supported_date(text: str) -> None:
    clarified(run(text), "supported")


def test_missing_time_zone_data_is_honestly_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tc, "_zone_index", lambda: {})
    out = run("Convert 2026-11-02 09:00 from America/New_York to UTC")
    assert out.status == "unavailable"
    assert "14:00" not in out.reply and "UTC-05" not in out.reply
    # Fixed offsets need no zone database.
    converted(run("Convert 2026-11-02 09:00 from UTC-05:00 to UTC"), "14:00")


def test_corrupt_zone_data_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(key: str) -> Any:
        raise ValueError("corrupt")

    monkeypatch.setattr(tc, "_load_region", broken)
    assert run("Convert 2026-11-02 09:00 from America/New_York to UTC").status == "unavailable"


# ------------------------------------------- 5. non-interception


@pytest.mark.parametrize(
    "text",
    [
        "How do time zones work?",
        "What time is it in Tokyo?",
        "What is 5 in binary?",
        "What is 3/4 in decimal?",
        "Translate 'Convert 9:00 from UTC to Asia/Tokyo' into French",
        "Rewrite this sentence: convert 9:00 tomorrow to UTC",
        "\"Convert 9:00 tomorrow to UTC\"",
        "```python\nconvert('9:00', 'UTC')\n```",
        "Convert 2026-01-01 09:00 from UTC to Asia/Tokyo and also write a poem",
        "Convert 2026-01-01 09:00 from UTC to Asia/Tokyo. Then summarize the French Revolution.",
        "Convert 5 kilometers to miles",
        "Convert 17:00 to 12-hour time",
        "Convert 5pm to military time",
        "Convert this PDF to text",
        "Is EST the same as UTC-5?",
        "I need to convert 9:00 tomorrow to UTC for my meeting notes",
        "My flight leaves at 09:00 tomorrow from London",
        "That is all the information I have given you.",
    ],
)
def test_ordinary_messages_are_not_intercepted(text: str) -> None:
    assert tc.respond(text, None, message_id="m1") is None
