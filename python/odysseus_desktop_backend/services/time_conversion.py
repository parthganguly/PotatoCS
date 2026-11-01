"""Deterministic local time conversion inside ordinary text chat.

Recognizes a deliberately narrow set of explicit English conversion requests,
collects the four required fields (local calendar date, local clock time,
source zone, destination zone) across clarification turns, and converts with
the standard-library zoneinfo and the installed tz database. Nothing here
calls a model or the network, reads the machine clock or zone, or guesses a
zone from a place name or abbreviation.

Anything that is not wholly recognized returns None so the caller keeps its
ordinary chat behavior. Resumable state lives only in the `time_conversion`
metadata object of the committed clarification (see `pending_from_metadata`).
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import zoneinfo
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone, tzinfo
from functools import lru_cache
from typing import Any

METADATA_KEY = "time_conversion"
VERSION = 1
CAPABILITY = "time_conversion"
ENGINE = "local_time_conversion"
FIELDS = ("date", "time", "source", "target")
OCCURRENCES = ("earlier", "later")
OUTCOMES = ("clarified", "converted", "cancelled", "unavailable")

FIELD_NAMES = {"date": "date", "time": "time", "source": "source zone", "target": "destination zone"}
FIELD_ASK = {
    "date": "the local calendar date (YYYY-MM-DD or Month D, YYYY)",
    "time": "the local clock time (HH:MM in 24-hour time, or with AM/PM)",
    "source": "the source time zone, as an IANA identifier (Area/Location) or an explicit UTC offset (UTC±HH:MM)",
    "target": "the destination time zone, as an IANA identifier (Area/Location) or an explicit UTC offset (UTC±HH:MM)",
}
REPLY_LABELS = {"date": "date", "time": "time", "source": "source", "target": "destination"}
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass
class Outcome:
    status: str  # clarified | converted | cancelled | unavailable
    reply: str
    state: dict[str, Any]
    time_zone_data: str = ""  # set only when region rules were consulted


class ZoneDataUnavailable(Exception):
    def __init__(self, zone: str):
        super().__init__(zone)
        self.zone = zone


# --------------------------------------------------------------- zone data


@lru_cache(maxsize=1)
def _available_zones() -> dict[str, str]:
    try:
        return {key.lower(): key for key in zoneinfo.available_timezones()}
    except Exception:  # noqa: BLE001 - any failure means the data cannot be used
        return {}


def _zone_index() -> dict[str, str]:
    """Lower-case IANA key -> canonical key; empty when no tz data is installed."""
    return _available_zones()


def _load_region(key: str) -> tzinfo:
    return zoneinfo.ZoneInfo(key)


def time_zone_data_label() -> str:
    if any(os.path.isdir(path) for path in zoneinfo.TZPATH):
        return "system tz database"
    try:
        return f"tzdata {importlib.metadata.version('tzdata')}"
    except importlib.metadata.PackageNotFoundError:
        return "tz database"


def _fixed_zone(canonical: str) -> tzinfo | None:
    if canonical in {"UTC", "GMT"}:
        return timezone.utc
    match = re.fullmatch(r"UTC([+-])(\d{2}):(\d{2})", canonical)
    if not match:
        return None
    delta = timedelta(hours=int(match.group(2)), minutes=int(match.group(3)))
    return timezone(-delta if match.group(1) == "-" else delta)


def _tzinfo(canonical: str) -> tzinfo:
    fixed = _fixed_zone(canonical)
    if fixed is not None:
        return fixed
    if canonical not in _zone_index().values():
        raise ZoneDataUnavailable(canonical)
    try:
        return _load_region(canonical)
    except Exception as exc:  # noqa: BLE001 - missing or corrupt data is never guessed around
        raise ZoneDataUnavailable(canonical) from exc


# ------------------------------------------------------------- attempts


@dataclass
class Attempt:
    field: str  # date | time | source | target | zone | occurrence | role
    value: str | None
    error: str = ""
    raw: str = ""
    labeled: bool = False
    strong: bool = True


_IANA = re.compile(r"[A-Za-z][A-Za-z0-9_+-]*(?:/[A-Za-z0-9_+-]+)+")
_UTC_OFFSET = re.compile(r"(?:utc|gmt)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?", re.I)
_BARE_OFFSET = re.compile(r"([+-])(\d{2}):?(\d{2})")
_ZONE_WORDS = re.compile(r"[A-Za-z][A-Za-z.'-]*(?: [A-Za-z][A-Za-z.'-]*){0,3}")
# Words that mark a format/unit request ("military time", "unix time"), not a zone.
_NOT_ZONE_WORDS = {
    "military", "unix", "epoch", "decimal", "binary", "hex", "hexadecimal", "format", "notation", "words",
    "text", "seconds", "minutes", "hours", "milliseconds", "timestamp", "iso", "roman", "numerals", "digital",
    "analog", "string", "number", "numbers", "minute", "hour", "second", "fraction", "percent",
}
_ZONE_NOT_IDENTIFIER = (
    "“{raw}” is not a time zone identifier I can use; place names and abbreviations can be ambiguous, "
    "so I don't guess their offsets."
)


def _zone_attempt(raw: str, role: str = "zone") -> Attempt | None:
    """Classify a zone operand; None when it does not look like a zone attempt at all."""
    text = raw.strip().strip("\"'").strip()
    text = re.sub(r"^the\s+", "", text, flags=re.I)
    text = re.sub(r"\s+(?:time\s*zone|timezone|zone)$", "", text, flags=re.I)
    shorter = re.fullmatch(r"(.+?)\s+time", text, re.I)
    if shorter and _strong_zone_shape(shorter.group(1)):
        text = shorter.group(1)
    if not text:
        return None
    if re.fullmatch(r"utc|gmt", text, re.I):
        return Attempt(role, text.upper(), raw=text)
    offset = _UTC_OFFSET.fullmatch(text) or _BARE_OFFSET.fullmatch(text)
    if offset:
        sign, hours, minutes = offset.group(1), int(offset.group(2)), int(offset.group(3) or 0)
        total = (hours * 60 + minutes) * (-1 if sign == "-" else 1)
        if minutes >= 60 or not -12 * 60 <= total <= 14 * 60:
            return Attempt(
                role, None, f"“{text}” is not a valid UTC offset (real offsets run from UTC-12:00 to UTC+14:00).", text
            )
        if total == 0:
            return Attempt(role, "UTC", raw=text)
        return Attempt(role, f"UTC{'-' if total < 0 else '+'}{abs(total) // 60:02}:{abs(total) % 60:02}", raw=text)
    if _IANA.fullmatch(text):
        index = _zone_index()
        if not index:  # cannot be checked; conversion will report the data as unavailable
            return Attempt(role, text, raw=text)
        canonical = index.get(text.lower())
        if canonical is None:
            return Attempt(role, None, f"“{text}” is not a known IANA time zone identifier.", text)
        return Attempt(role, canonical, raw=text)
    if _ZONE_WORDS.fullmatch(text) and not _NOT_ZONE_WORDS & set(text.lower().split()):
        return Attempt(role, None, _ZONE_NOT_IDENTIFIER.format(raw=text), text, strong=False)
    return None


def _strong_zone_shape(text: str) -> bool:
    return bool(
        re.fullmatch(r"utc|gmt", text, re.I)
        or _UTC_OFFSET.fullmatch(text)
        or _BARE_OFFSET.fullmatch(text)
        or _IANA.fullmatch(text)
    )


def _valid_canonical_zone(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if _fixed_zone(value) is not None:
        return True
    if not _IANA.fullmatch(value):
        return False
    index = _zone_index()
    return not index or value in index.values()


# -------------------------------------------------------------- scanning

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3, "apr": 4,
    "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3, "fri": 4, "sat": 5, "sun": 6,
}
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WD = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))
_AMPM = r"(?P<ap>[ap])\.?\s?m\.?(?![a-z])"

_TOKENS: list[tuple[str, re.Pattern[str]]] = [
    (kind, re.compile(pattern, re.I))
    for kind, pattern in [
        ("iso", r"(?P<y>\d{4})-(?P<m>\d{1,2})-(?P<d>\d{1,2})(?!\d)(?:t(?=\d))?"),
        ("ymd_other", r"\d{4}[/.]\d{1,2}[/.]\d{1,2}(?!\d)"),
        ("numeric", r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}(?!\d)"),
        (
            "month_first",
            rf"(?:(?P<wd>{_WD})\.?,?\s+)?(?P<mon>{_MON})\.?\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?"
            rf"(?:,?\s*(?P<y>\d{{4}}))?(?![\d:])",
        ),
        (
            "day_first",
            rf"(?:(?P<wd>{_WD})\.?,?\s+)?(?:the\s+)?(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{_MON})\.?"
            rf"(?:,?\s*(?P<y>\d{{4}}))?(?![\d:a-z])",
        ),
        ("clock", rf"(?P<h>\d{{1,2}}):(?P<mi>\d{{2}})(?::(?P<s>\d{{2}}))?(?:\s*{_AMPM})?(?![\d:])"),
        ("hour_ampm", rf"(?P<h>\d{{1,2}})\s*{_AMPM}"),
        ("noon", r"(?:12\s*)?(?:noon|midday)(?![a-z])"),
        ("midnight", r"midnight(?![a-z])"),
        (
            "relative",
            rf"(?:(?:the\s+)?day\s+(?:after\s+tomorrow|before\s+yesterday)"
            rf"|(?:tomorrow|today|tonight|yesterday)(?:\s+(?:morning|afternoon|evening|night))?"
            rf"|(?:this|next|last|coming)\s+(?:week|month|year|weekend|morning|afternoon|evening|night|{_WD}))(?![a-z])",
        ),
        ("weekday", rf"(?P<wd>{_WD})\.?(?![a-z])"),
        ("bare_hour", r"(?P<h>\d{1,2})(?:\s*o'?clock)?(?![\d:a-z])"),
    ]
]
_ZONE_TOKENS: list[tuple[str, re.Pattern[str]]] = [
    ("zone", re.compile(r"[a-z][a-z0-9_+-]*(?:/[a-z0-9_+-]+)+", re.I)),
    ("zone", re.compile(r"(?:utc|gmt)(?:\s*[+-]\s*\d{1,2}(?::?\d{2})?)?(?![a-z0-9])", re.I)),
    ("zone", re.compile(r"[+-]\d{2}:?\d{2}(?!\d)")),
    ("abbreviation", re.compile(r"[A-Z]{2,5}(?![A-Za-z])")),  # case-sensitive: "EST", not "est"
]
_OCCURRENCE_TOKEN = re.compile(
    r"(?:the\s+)?(?P<o>earlier|later|first|second|1st|2nd)(?:\s+(?:one|occurrence|instance|time))?(?![a-z])", re.I
)
_SEPARATOR = re.compile(r"\s+|,|(?:at|on)(?![a-z])", re.I)


@dataclass
class _Scan:
    dates: list[Attempt] = field(default_factory=list)
    times: list[Attempt] = field(default_factory=list)
    zones: list[Attempt] = field(default_factory=list)
    occurrences: list[str] = field(default_factory=list)
    weekdays: list[tuple[str, int]] = field(default_factory=list)
    strong: bool = False
    weak: bool = False

    def empty(self) -> bool:
        return not (self.dates or self.times or self.zones or self.occurrences or self.weekdays)


def _scan(text: str, *, zones: bool, occurrence: bool = False) -> _Scan | None:
    """Tokenize date/time material; None if anything in `text` is not recognized."""
    text = text.replace('"', " ").strip().strip("'").strip()
    result = _Scan()
    position = 0
    while position < len(text):
        separator = _SEPARATOR.match(text, position)
        if separator and separator.end() > position:
            position = separator.end()
            continue
        for kind, pattern in _TOKENS + (_ZONE_TOKENS if zones else []):
            match = pattern.match(text, position)
            if match:
                _take(result, kind, match)
                position = match.end()
                break
        else:
            match = _OCCURRENCE_TOKEN.match(text, position) if occurrence else None
            if not match:
                return None
            result.occurrences.append("later" if match.group("o").lower() in {"later", "second", "2nd"} else "earlier")
            position = match.end()
    _check_weekdays(result)
    return result


def _take(result: _Scan, kind: str, match: re.Match[str]) -> None:
    raw = match.group(0).strip()
    groups = match.groupdict()
    if kind in {"iso", "month_first", "day_first"}:
        month = int(groups["m"]) if kind == "iso" else _MONTHS[groups["mon"].lower()]
        if groups.get("wd"):
            result.weekdays.append((groups["wd"], _WEEKDAYS[groups["wd"].lower()]))
        result.strong = True
        if not groups.get("y"):
            result.dates.append(Attempt("date", None, f"“{raw}” has no year.", raw))
            return
        try:
            value = date(int(groups["y"]), month, int(groups["d"])).isoformat()
        except ValueError:
            result.dates.append(Attempt("date", None, f"“{raw}” is not a real calendar date.", raw))
            return
        result.dates.append(Attempt("date", value, raw=raw))
    elif kind == "ymd_other":
        result.strong = True
        result.dates.append(Attempt("date", None, f"Please write “{raw}” as YYYY-MM-DD.", raw))
    elif kind == "numeric":
        result.strong = True
        result.dates.append(Attempt("date", None, f"“{raw}” is ambiguous because day/month order varies.", raw))
    elif kind in {"relative", "weekday"}:
        if kind == "weekday":
            result.weekdays.append((groups["wd"], _WEEKDAYS[groups["wd"].lower()]))
            result.weak = True
            return
        result.strong = True
        result.dates.append(Attempt("date", None, f"“{raw}” is relative, so I need the actual calendar date.", raw))
    elif kind in {"clock", "hour_ampm"}:
        result.strong = True
        result.times.append(_clock_attempt(raw, groups))
    elif kind == "noon":
        result.strong = True
        result.times.append(Attempt("time", "12:00", raw=raw))
    elif kind == "midnight":
        result.strong = True
        result.times.append(Attempt("time", None, "“midnight” could mean the start or the end of a day.", raw))
    elif kind == "bare_hour":
        result.weak = True
        result.times.append(Attempt("time", None, f"“{raw}” needs minutes or AM/PM.", raw, strong=False))
    elif kind in {"zone", "abbreviation"}:
        attempt = _zone_attempt(raw)
        if attempt is not None:
            result.zones.append(attempt)


def _clock_attempt(raw: str, groups: dict[str, Any]) -> Attempt:
    hour, minute = int(groups["h"]), int(groups.get("mi") or 0)
    second = groups.get("s")
    meridiem = (groups.get("ap") or "").lower()
    if second is not None and int(second) == 60:
        return Attempt("time", None, f"“{raw}” uses second 60 (a leap second), which can't be converted.", raw)
    invalid = minute > 59 or (second is not None and int(second) > 59)
    if meridiem:
        invalid = invalid or not 1 <= hour <= 12
        hour = hour % 12 + (12 if meridiem == "p" else 0)
    else:
        invalid = invalid or hour > 23
    if invalid:
        return Attempt("time", None, f"“{raw}” is not a valid clock time.", raw)
    value = f"{hour:02}:{minute:02}" + (f":{int(second):02}" if second is not None else "")
    return Attempt("time", value, raw=raw)


def _check_weekdays(result: _Scan) -> None:
    if not result.weekdays:
        return
    valid = [item for item in result.dates if item.value]
    if not result.dates:
        name = result.weekdays[0][0]
        result.dates.append(Attempt("date", None, f"“{name}” is relative, so I need the actual calendar date.", name))
        result.strong = True
        return
    if len(valid) != 1:
        return
    actual = date.fromisoformat(str(valid[0].value)).weekday()
    for name, weekday in result.weekdays:
        if weekday != actual:
            index = result.dates.index(valid[0])
            result.dates[index] = Attempt(
                "date",
                None,
                f"{name.capitalize()} does not match {valid[0].value}, which is a {WEEKDAY_NAMES[actual]}.",
                valid[0].raw,
            )
            return


def _merge(attempts: list[Attempt], name: str) -> Attempt | None:
    if not attempts:
        return None
    distinct: list[Attempt] = []
    for attempt in attempts:
        if not any((attempt.value or attempt.raw) == (item.value or item.raw) for item in distinct):
            distinct.append(attempt)
    if len(distinct) == 1:
        return distinct[0]
    listed = " and ".join(f"“{item.raw}”" for item in distinct)
    return Attempt(distinct[0].field, None, f"I found more than one {name} ({listed}). Which one should I use?", listed)


# --------------------------------------------------------------- requests

_PUNCTUATION = str.maketrans(
    {"“": '"', "”": '"', "„": '"', "‘": "'", "’": "'", "−": "-", " ": " "}
)
# Neutral "no further information" statements: any auxiliary agreeing with a
# singular or plural subject, followed by a passive participle.
_INFO = r"(?:information|info|details?|context|data)"
_AUX = r"(?:is|are|was|were|will be|(?:has|have|had) been|(?:is|are) being)"
_GIVEN = r"(?:available|given|provided|supplied|known|included|specified)(?: (?:to )?you)?"
_NEUTRAL = (
    rf"(?:(?:that|this)(?:'s| is| was) (?:all|everything)(?: the)?(?: {_INFO})?"
    r"(?: (?:that )?(?:i have|i've got|i can give(?: you)?|i know|there is|i was given|you get|you have))?"
    r"(?: (?:given|provided|supplied|told)(?: (?:to )?you)?)?"
    rf"|(?:those|these) (?:are|were) all the {_INFO}(?: (?:i have|i've got|i know))?(?: {_AUX} {_GIVEN})?"
    r"|i (?:have|know|can give) nothing (?:more|else)"
    rf"|i (?:don't|do not) have (?:any )?(?:more|other|further|additional) {_INFO}"
    rf"|(?:there (?:is|are|was|were) |i have )?no (?:more|other|further|additional) {_INFO}"
    rf"(?: (?:{_AUX} )?{_GIVEN})?"
    rf"|nothing (?:more|else) (?:{_AUX} )?{_GIVEN})"
)
_NEUTRAL_TAIL = re.compile(rf"(?:^|(?<=[.!?]))\s*{_NEUTRAL}\s*[.!?]*\s*$", re.I)
_POLITE_HEAD = re.compile(
    r"^(?:(?:hi|hello|hey)[,!]?\s+)?(?:(?:please|kindly)\s+|(?:could|can|would|will)\s+you\s+(?:please\s+|kindly\s+)?"
    r"|i(?:'d| would) like (?:you )?to\s+|help me\s+)?",
    re.I,
)
_POLITE_TAIL = re.compile(r"(?:,?\s+(?:please|for me|thanks|thank you))+$", re.I)
_OP = r"(?P<op>.+?)"
_REQUESTS = [
    re.compile(pattern, re.I)
    for pattern in [
        rf"convert {_OP} (?:from|in) (?P<src>.+?) (?:to|into) (?P<dst>.+)",
        rf"convert {_OP} (?:to|into) (?P<dst>.+?) from (?P<src>.+)",
        rf"convert {_OP} (?:to|into) (?P<dst>.+)",
        rf"convert {_OP} (?:from|in) (?P<src>.+)",
        r"convert (?P<op>.+)",
        rf"what(?: time)? is {_OP} (?:in|from) (?P<src>.+?) (?:in|to) (?P<dst>.+)",
        rf"what(?: time)? is {_OP} (?:in|to) (?P<dst>.+)",
        r"what time (?:is it|will it be|would it be) in (?P<dst>.+?) (?:when|if) it(?:'s| is| will be| would be) "
        r"(?P<op>.+?)(?: (?:in|from) (?P<src>.+))?",
    ]
]
_CANCEL = re.compile(r"(?:please\s+)?(?:cancel|stop|abort)(?: the| this)?(?: time)?(?: conversion)?[.!]*", re.I)
_ACK = re.compile(r"(?:hi|hello|hey|thanks|thank you|ok|okay|yes|no|bye)\s*[.!?]*", re.I)
_TOPIC_CHANGE = re.compile(
    r"^(?:(?:different|new|unrelated|another|separate|other) (?:question|topic|subject|thing)"
    r"|(?:change of|changing the) (?:topic|subject)|never ?mind|forget (?:it|that|about it|the conversion)"
    r"|anyway|by the way|unrelated|on another note)\b",
    re.I,
)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_PUNCTUATION)).strip()


def _parse_request(text: str) -> list[Attempt] | None:
    """Attempts of one wholly recognized, explicit conversion request; else None."""
    normalized = _normalize(text)
    if not normalized or normalized[0] in "\"'" or re.search(r"[`{}<>\[\]|\\=;]", normalized):
        return None
    for _ in range(3):
        stripped = _NEUTRAL_TAIL.sub("", normalized, count=1).strip()
        if stripped == normalized:
            break
        normalized = stripped
    normalized = _POLITE_TAIL.sub("", normalized.rstrip(" .!?")).rstrip(" .!?,")
    normalized = _POLITE_HEAD.sub("", normalized, count=1)
    normalized = re.sub(r"^what's\b", "what is", normalized, flags=re.I)
    for pattern in _REQUESTS:
        match = pattern.fullmatch(normalized)
        if match:
            attempts = _assemble(match.group("op"), match.groupdict().get("src"), match.groupdict().get("dst"))
            if attempts is not None:
                return attempts
    return None


def _assemble(operand: str, source: str | None, target: str | None) -> list[Attempt] | None:
    scan = _scan(operand, zones=True)
    if scan is None or scan.empty():
        return None
    sources = list(scan.zones)
    explicit: list[Attempt] = []
    for raw, role in ((source, "source"), (target, "target")):
        if raw is None:
            continue
        attempt = _zone_attempt(raw, role)
        if attempt is None:
            return None
        explicit.append(attempt)
        if role == "source":
            sources.append(attempt)
    if not (scan.strong or (scan.weak and any(item.strong for item in sources + explicit))):
        return None
    attempts = [item for item in (_merge(scan.dates, "date"), _merge(scan.times, "time")) if item is not None]
    for item in sources:
        item.field = "source"
    merged_source = _merge(sources, "source zone")
    if merged_source is not None:
        attempts.append(merged_source)
    attempts.extend(item for item in explicit if item.field == "target")
    return attempts


# ---------------------------------------------------------------- replies

_LABEL_FIELDS = [
    ("zone", r"time\s*zone|timezone|zone"),
    ("source", r"source(?:\s+time)?(?:\s*zone|\s*timezone)?|origin(?:\s+zone)?|from(?:\s+zone)?|starting\s+zone"),
    ("target", r"destination(?:\s+time)?(?:\s*zone|\s*timezone)?|target(?:\s+time)?(?:\s*zone)?|to(?:\s+zone)?|into"),
    ("date", r"(?:local\s+|calendar\s+)?date|day"),
    ("time", r"(?:local\s+|clock\s+)?time"),
    ("occurrence", r"occurrence"),
]
_LABEL = re.compile(
    r"^(?:the\s+)?(?P<label>" + "|".join(f"(?P<{name}>{pattern})" for name, pattern in _LABEL_FIELDS) + r")"
    r"(?P<sep>\s*[:=]\s*|\s+is\s+|\s+)(?P<value>\S.*)$",
    re.I,
)
_LABEL_SPLIT = re.compile(
    r"[,\s]+(?=(?:" + "|".join(pattern for _, pattern in _LABEL_FIELDS) + r")\s*[:=])", re.I
)
_FROM_TO = re.compile(r"(?:from|in) (?P<src>.+?) (?:to|into) (?P<dst>.+)", re.I)
_ROLE = re.compile(
    r"(?:it(?:'s| is) )?(?:the )?(?P<role>source|origin|from|destination|target|to)(?: (?:one|zone|time zone))?", re.I
)
_REPLY_HEAD = re.compile(r"^(?:(?:ok(?:ay)?|sure|sorry|right|well)[,!]?\s+)?(?:it(?:'s| is)\s+|use\s+|i mean\s+)?", re.I)
_SINGLE_WORD = re.compile(r"[A-Za-z][A-Za-z'.-]*")


def _parse_reply(text: str, state: _State) -> list[Attempt] | None:
    """Attempts if the whole reply is a recognized slot answer; else None."""
    normalized = _normalize(text)
    if re.search(r"[`{}<>\[\]|\\]", normalized):
        return None
    normalized = _POLITE_TAIL.sub("", normalized.rstrip(" .!?")).rstrip(" .!?,")
    normalized = _REPLY_HEAD.sub("", normalized, count=1)
    if not normalized:
        return None
    role = _ROLE.fullmatch(normalized)
    if role and state.role_zone:
        name = role.group("role").lower()
        return [Attempt("source" if name in {"source", "origin", "from"} else "target", state.role_zone, labeled=True)]
    attempts: list[Attempt] = []
    for piece in re.split(r"\s*;\s*|\s+and\s+", _LABEL_SPLIT.sub(";", normalized)):
        piece = piece.strip(" ,")
        if not piece:
            continue
        parsed = _parse_piece(piece)
        if parsed is None:
            return None
        attempts.extend(parsed)
    if attempts:
        return attempts
    return None


def _parse_piece(piece: str) -> list[Attempt] | None:
    pair = _FROM_TO.fullmatch(piece)
    if pair:
        found = [_zone_attempt(pair.group("src"), "source"), _zone_attempt(pair.group("dst"), "target")]
        if all(item is not None and item.strong for item in found):
            for item in found:
                item.labeled = True  # type: ignore[union-attr]
            return found  # type: ignore[return-value]
    label = _LABEL.match(piece)
    if label:
        parsed = _labeled(label)
        if parsed is not None:
            return [parsed]
    scan = _scan(piece, zones=True, occurrence=True)
    if scan is None or scan.empty() or scan.weekdays and not scan.dates:
        return None
    attempts = [item for item in (_merge(scan.dates, "date"), _merge(scan.times, "time")) if item is not None]
    attempts.extend(scan.zones)
    attempts.extend(Attempt("occurrence", value) for value in scan.occurrences)
    return attempts


def _labeled(match: re.Match[str]) -> Attempt | None:
    name = next(key for key, _ in _LABEL_FIELDS if match.group(key))
    value = match.group("value").strip()
    explicit = bool(match.group("sep").strip())  # ":", "=" or "is"
    preposition = match.group(name).lower() in {"from", "to", "into"}
    if name in {"zone", "source", "target"}:
        attempt = _zone_attempt(value, name)
        if attempt is None:
            if not explicit:
                return None
            attempt = Attempt(name, None, _ZONE_NOT_IDENTIFIER.format(raw=value), value)
        if not explicit and not attempt.strong and not (preposition and _SINGLE_WORD.fullmatch(value)):
            return None
        attempt.labeled = name != "zone"
        return attempt
    if name == "occurrence":
        occurrence = _OCCURRENCE_TOKEN.fullmatch(value)
        if occurrence:
            word = occurrence.group("o").lower()
            return Attempt("occurrence", "later" if word in {"later", "second", "2nd"} else "earlier", labeled=True)
        return Attempt("occurrence", None, f"Please answer “earlier” or “later” rather than “{value}”.", value)
    scan = _scan(value, zones=False)
    attempts = (scan.dates if name == "date" else scan.times) if scan is not None else []
    other = (scan.times if name == "date" else scan.dates) if scan is not None else []
    if scan is None or not attempts or other:
        if not explicit:
            return None
        return Attempt(name, None, f"I couldn't read “{value}” as a {FIELD_NAMES[name]}.", value, labeled=True)
    attempt = _merge(attempts, FIELD_NAMES[name])
    assert attempt is not None
    attempt.labeled = True
    return attempt


# ------------------------------------------------------------------ state


@dataclass
class _State:
    fields: dict[str, str] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)
    occurrence: str | None = None
    role_zone: str | None = None
    open: set[str] = field(default_factory=set)
    expecting_occurrence: bool = False
    issues: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    @classmethod
    def from_pending(cls, pending: dict[str, Any]) -> _State:
        return cls(
            fields=dict(pending["fields"]),
            refs=dict(pending["refs"]),
            occurrence=pending.get("occurrence"),
            role_zone=pending.get("role_zone"),
            open=set(pending.get("open") or []),
            expecting_occurrence="occurrence" in pending["needs"],
        )

    def apply(self, attempt: Attempt, message_id: str) -> None:
        name = attempt.field
        current = self.fields.get(name)
        if attempt.error:
            self.fields.pop(name, None)
            self.refs.pop(name, None)
            self.issues[name] = attempt.error
        elif current is None or current == attempt.value or attempt.labeled or name in self.open:
            if current != attempt.value:
                self.fields[name] = str(attempt.value)
                self.refs[name] = message_id
        else:
            self.fields.pop(name, None)
            self.refs.pop(name, None)
            self.issues[name] = (
                f"You gave {current} earlier and {attempt.value} now. Which should I use? "
                f"Reply with just one, or label it (such as “{REPLY_LABELS[name]}: …”)."
            )
        if name in {"date", "time", "source"} and self.fields.get(name) != current:
            self.occurrence = None
            self.expecting_occurrence = False

    def apply_all(self, attempts: list[Attempt], message_id: str) -> None:
        unassigned: list[Attempt] = []
        for attempt in attempts:
            if attempt.field in FIELDS:
                self.apply(attempt, message_id)
            elif attempt.field == "zone":
                unassigned.append(attempt)
            elif attempt.field == "occurrence":
                if attempt.error:
                    self.notes.append(attempt.error)
                elif self.expecting_occurrence:
                    self.occurrence = attempt.value
                else:
                    self.notes.append(
                        "There is no repeated local time to choose between, so I didn't use “earlier” or “later”."
                    )
        self._assign_zones(unassigned, message_id)
        if self.role_zone in self.fields.values() or (not unassigned and {"source", "target"} <= self.fields.keys()):
            self.role_zone = None

    def _assign_zones(self, zones: list[Attempt], message_id: str) -> None:
        missing = [name for name in ("source", "target") if name not in self.fields]
        valid = [item for item in zones if not item.error and item.value not in self.fields.values()]
        for item in zones:
            if item.error:
                if len(missing) == 1:
                    self.issues[missing[0]] = item.error
                else:
                    self.notes.append(item.error)
        if len(valid) == 1 and len(missing) == 1:
            valid[0].field = missing[0]
            self.apply(valid[0], message_id)
            self.role_zone = None
        elif len(valid) == 1:
            self.role_zone = valid[0].value
        elif len(valid) > 1:
            listed = " and ".join(str(item.value) for item in valid)
            self.notes.append(
                f"I can't tell which of {listed} is the source and which is the destination. "
                "Please label them (“source: …; destination: …”)."
            )

    def snapshot(self, status: str, needs: list[str]) -> dict[str, Any]:
        state: dict[str, Any] = {
            "version": VERSION,
            "status": status,
            "fields": dict(self.fields),
            "refs": {name: ref for name, ref in self.refs.items() if name in self.fields},
            "needs": needs,
        }
        if self.occurrence:
            state["occurrence"] = self.occurrence
        if status == "clarified":
            if self.role_zone:
                state["role_zone"] = self.role_zone
            if self.open:
                state["open"] = sorted(self.open)
        return state


def pending_from_metadata(metadata: Any) -> dict[str, Any] | None:
    """The resumable clarification state saved on a committed answer, or None."""
    data = metadata.get(METADATA_KEY) if isinstance(metadata, dict) else None
    if not isinstance(data, dict) or data.get("version") != VERSION or data.get("status") != "clarified":
        return None
    fields, refs, needs = data.get("fields"), data.get("refs", {}), data.get("needs", [])
    if not isinstance(fields, dict) or not isinstance(refs, dict) or not isinstance(needs, list):
        return None
    for name, value in fields.items():
        if name not in FIELDS or not isinstance(value, str):
            return None
        if name == "date" and not _valid_iso_date(value):
            return None
        if name == "time" and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?", value):
            return None
        if name in {"source", "target"} and not _valid_canonical_zone(value):
            return None
    if not all(isinstance(key, str) and isinstance(value, str) for key, value in refs.items()):
        return None
    if not all(isinstance(item, str) for item in needs):
        return None
    occurrence = data.get("occurrence")
    role_zone = data.get("role_zone")
    open_fields = data.get("open", [])
    if occurrence is not None and occurrence not in OCCURRENCES:
        return None
    if role_zone is not None and not _valid_canonical_zone(role_zone):
        return None
    if not isinstance(open_fields, list) or not set(open_fields) <= {"date", "time"}:
        return None
    return {
        "fields": dict(fields),
        "refs": {key: value for key, value in refs.items() if key in fields},
        "needs": list(needs),
        "occurrence": occurrence,
        "role_zone": role_zone,
        "open": list(open_fields),
    }


def _valid_iso_date(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


# ----------------------------------------------------------------- driver


def respond(text: str, pending: dict[str, Any] | None, *, message_id: str) -> Outcome | None:
    """Handle one user message; None means the message follows ordinary chat."""
    normalized = _normalize(text)
    if pending is not None and _CANCEL.fullmatch(normalized):
        return Outcome("cancelled", "Okay, I've cancelled that time conversion.", {"version": VERSION, "status": "cancelled"})
    request = _parse_request(text)
    if request is not None:
        state = _State()
        state.apply_all(request, message_id)
        return _finish(state)
    if pending is None or _ACK.fullmatch(normalized) or _TOPIC_CHANGE.match(normalized):
        return None
    state = _State.from_pending(pending)
    attempts = _parse_reply(text, state)
    if attempts is None:
        if _SINGLE_WORD.fullmatch(normalized.rstrip(".!?")) and not {"source", "target"} <= state.fields.keys():
            word = normalized.rstrip(".!?")
            attempts = [Attempt("zone", None, _ZONE_NOT_IDENTIFIER.format(raw=word), word, strong=False)]
        else:
            return None
    state.apply_all(attempts, message_id)
    return _finish(state)


def _candidates(naive: datetime, zone: tzinfo) -> list[datetime]:
    """Distinct real instants for a local wall time: fold=0/1, round-tripped, deduplicated by UTC."""
    found: dict[datetime, datetime] = {}
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        instant = local.astimezone(timezone.utc)
        if instant.astimezone(zone).replace(tzinfo=None) == naive:
            found.setdefault(instant.replace(tzinfo=None), local)
    return [found[key] for key in sorted(found)]


def _finish(state: _State) -> Outcome:
    fields = state.fields
    resolution: list[str] = []
    region_used = any(_fixed_zone(fields.get(name, "UTC")) is None for name in ("source", "target") if name in fields)
    source_local: datetime | None = None
    result: datetime | None = None
    ambiguous: list[datetime] = []
    state.open = set()
    try:
        if {"date", "time", "source"} <= fields.keys():
            naive = datetime.fromisoformat(f"{fields['date']}T{fields['time']}")
            zone = _tzinfo(fields["source"])
            try:
                found = _candidates(naive, zone)
            except OverflowError:
                found = None
            if found is None:
                state.issues["date"] = _range_issue(fields)
                state.open = {"date", "time"}
                resolution.append("date")
            elif not found:
                state.issues["time"] = (
                    f"{fields['date']} {fields['time']} does not exist in {fields['source']}: the clocks skip over it "
                    "on that date. Please give a different local time or date."
                )
                state.open = {"date", "time"}
                resolution.append("time")
            elif len(found) == 2 and state.occurrence is None:
                ambiguous = found
                resolution.append("occurrence")
            else:
                source_local = found[-1] if len(found) == 2 and state.occurrence == "later" else found[0]
                if len(found) == 1:
                    state.occurrence = None
        if "target" in fields:
            target_zone = _tzinfo(fields["target"])
            if source_local is not None:
                try:
                    result = source_local.astimezone(target_zone)
                except OverflowError:
                    state.issues["date"] = _range_issue(fields)
                    state.open = {"date", "time"}
                    resolution.append("date")
                    source_local = None
    except ZoneDataUnavailable as exc:
        reply = (
            f"I can't do this conversion right now: the time-zone database could not be loaded, so I can't look up "
            f"the rules for {exc.zone}. I haven't guessed an offset. Explicit UTC offsets (UTC±HH:MM) can still "
            "be converted without it."
        )
        return Outcome("unavailable", reply, state.snapshot("unavailable", []))
    needs = [name for name in FIELDS if name not in fields]
    needs += [item for item in resolution if item not in needs]
    if state.role_zone:
        needs.append("zone_role")
    data_label = time_zone_data_label() if region_used else ""
    if not needs and source_local is not None and result is not None:
        return Outcome("converted", _converted_reply(state, source_local, result, data_label), state.snapshot("converted", []), data_label)
    state.expecting_occurrence = "occurrence" in needs
    return Outcome("clarified", _clarification(state, needs, ambiguous), state.snapshot("clarified", needs), data_label)


def _range_issue(fields: dict[str, str]) -> str:
    return (
        f"{fields['date']} {fields['time']} falls outside the range I can calculate (years 0001 to 9999 in both zones). "
        "Please give a supported date."
    )


# -------------------------------------------------------------- rendering


def _format_offset(offset: timedelta | None) -> str:
    seconds = int((offset or timedelta()).total_seconds())
    sign = "-" if seconds < 0 else "+"
    hours, rest = divmod(abs(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return f"UTC{sign}{hours:02}:{minutes:02}" + (f":{secs:02}" if secs else "")


def _zone_label(canonical: str, moment: datetime) -> str:
    if canonical == "UTC":
        return "UTC (UTC+00:00)"
    if canonical == "GMT":
        return "GMT (UTC+00:00)"
    if _fixed_zone(canonical) is not None:
        return f"{canonical} (fixed offset)"
    return f"{canonical} ({_format_offset(moment.utcoffset())})"


def _format_moment(moment: datetime, seconds: bool) -> str:
    clock = f"{moment.hour:02}:{moment.minute:02}" + (f":{moment.second:02}" if seconds else "")
    return f"{moment.year:04}-{moment.month:02}-{moment.day:02} ({WEEKDAY_NAMES[moment.weekday()]}) {clock}"


def _converted_reply(state: _State, source: datetime, result: datetime, data_label: str) -> str:
    fields = state.fields
    seconds = len(fields["time"]) > 5 or bool(result.second) or any(
        int((value.utcoffset() or timedelta()).total_seconds()) % 60 for value in (source, result)
    )
    days = (result.date() - source.date()).days
    rollover = {0: "", 1: " (next day)", -1: " (previous day)"}.get(
        days, f" ({abs(days)} days {'later' if days > 0 else 'earlier'})"
    )
    lines = [
        f"{_format_moment(source, seconds)} in {_zone_label(fields['source'], source)}",
        f"→ {_format_moment(result, seconds)} in {_zone_label(fields['target'], result)}{rollover}",
        "",
    ]
    if state.occurrence:
        lines.append(
            f"You chose the {state.occurrence} of the two {fields['time']} occurrences in {fields['source']} "
            f"({_format_offset(source.utcoffset())})."
        )
    fixed = [fields[name] for name in ("source", "target") if fields[name].startswith("UTC") and fields[name] != "UTC"]
    for canonical in dict.fromkeys(fixed):
        lines.append(f"{canonical} is a fixed offset; no daylight-saving rules apply to it.")
    if data_label:
        lines.append(f"Region offsets, including daylight-saving changes, come from the installed {data_label}.")
    lines.append(
        "Calculated locally and deterministically from the date, time and zones you gave; "
        "no model, web lookup or location check was used."
    )
    return "\n".join(lines)


def _clarification(state: _State, needs: list[str], ambiguous: list[datetime]) -> str:
    fields = state.fields
    lines = ["To finish this time conversion I still need:"]
    for name in needs:
        if name in {"source", "target"} and state.role_zone:
            continue
        if name == "zone_role":
            lines.append(
                f"- the role of {state.role_zone}: is it the source or the destination time zone? "
                "Reply “source” or “destination”."
            )
        elif name == "occurrence":
            earlier, later = (_format_offset(item.utcoffset()) for item in ambiguous)
            lines.append(
                f"- which occurrence: {fields['date']} {fields['time']} happens twice in {fields['source']}. "
                f"The earlier occurrence is at {earlier} and the later one at {later}. Reply “earlier” or “later”."
            )
        elif name in state.issues and name in fields:  # a resolution problem with otherwise valid fields
            lines.append(f"- {state.issues[name]}")
        else:
            issue = state.issues.get(name)
            lines.append(f"- {FIELD_ASK[name]}." + (f" {issue}" if issue else ""))
    lines.extend(f"- {note}" for note in state.notes)
    known = [f"{FIELD_NAMES[name]} {fields[name]}" for name in FIELDS if name in fields]
    if known:
        lines.extend(["", "So far I have: " + "; ".join(known) + "."])
    lines.extend(
        [
            "",
            "Reply with just these details (labels such as “date:” or “source:” are fine), "
            "or say “cancel conversion”.",
        ]
    )
    return "\n".join(lines)
