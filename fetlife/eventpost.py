"""Turn the pieces of an event into what FetLife's New Event form posts, and
read/update the event entries of a site data file like Xanadu's ``events-data.js``.

The form (``/events/new``) is a multipart POST to ``/events``: the cover
picture rides along as a file, the description is editor HTML (the same
format :func:`fetlife.writing.text_html` builds), co-hosts are user ids, and
the start/end times are split into Rails' month/day/year/hour/minute/AM-PM
fields. :class:`~fetlife.client.FetLifeClient` does the requests.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from .exceptions import FetLifeError

# The form's category menu: what to type (a prefix of the site's label) → its value.
CATEGORIES = {
    "Educational": "educational",
    "Social": "social",
    "Sex Party": "sex_party",
    "BDSM Party": "bdsm_party",
    "Conference": "conference_festival",
}
EVENT_TYPES = {
    "in-person": "physical",
    "virtual": "virtual",
    "virtual-local": "virtual_local",
}
COST_TYPES = ("free", "paid")
PRIVACY_LEVELS = ("public", "fetlifers", "friends", "private")
DEFAULT_TIMEZONE = "America/New_York"


def category_value(text: str, offered: dict[str, str] | None = None) -> str:
    """The form value for a category typed as a prefix of its label.

    "BDSM" and "bdsm party" both match "BDSM Party (fetish party, …)";
    "Conferences" matches "Conference / Festival". *offered* is the live
    form's ``{value: label}``; without it the known menu is used.
    """
    offered = offered or {v: k for k, v in CATEGORIES.items()}
    want = " ".join(text.split()).casefold()
    if not want:
        raise FetLifeError("The category is empty.")
    for value, label in offered.items():
        short = re.split(r" \(| /", label)[0].casefold()
        if value.casefold() == want or label.casefold().startswith(want) or want.startswith(short):
            return value
    raise FetLifeError(f"{text!r} isn't one of FetLife's event categories: "
                       + ", ".join(re.split(r" \(", label)[0] for label in offered.values()) + ".")


def timezone_value(text: str, offered: dict[str, str]) -> str:
    """The form's timezone value for an IANA name ("America/New_York") or a
    unique piece of the menu's label ("Eastern Time")."""
    want = text.strip().casefold()
    for value in offered:
        if value.casefold() == want:
            return value
    hits = [value for value, label in offered.items() if want and want in label.casefold()]
    if len(hits) == 1:
        return hits[0]
    raise FetLifeError(f"{text!r} doesn't name one of FetLife's timezones"
                       + (f" (it matches {', '.join(hits)})." if hits else
                          "; use an IANA name such as America/New_York."))


_CLOCK = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s*m\.?", re.I)


def _clock(hour: str, minute: str | None, ampm: str) -> tuple[int, int]:
    h, m = int(hour), int(minute or 0)
    if not 1 <= h <= 12 or m > 59:
        raise FetLifeError(f"{hour}:{minute or '00'}{ampm}m isn't a time of day.")
    return h % 12 + (12 if ampm.lower() == "p" else 0), m


def parse_when(text: str) -> datetime:
    """A start or end time: "2026-11-07 14:00", "2026-11-07 2:00pm" or "2026-11-07T14:00"."""
    m = re.fullmatch(r"\s*(\d{4}-\d{2}-\d{2})[ T]+(.+?)\s*", text)
    if m:
        day = date.fromisoformat(m.group(1))
        clock = _CLOCK.fullmatch(m.group(2))
        if clock:
            return datetime(day.year, day.month, day.day, *_clock(*clock.groups()))
        hm = re.fullmatch(r"(\d{1,2}):(\d{2})", m.group(2))
        if hm and int(hm.group(1)) < 24 and int(hm.group(2)) < 60:
            return datetime(day.year, day.month, day.day, int(hm.group(1)), int(hm.group(2)))
    raise FetLifeError(f"{text!r} isn't a date and time like 2026-11-07 14:00 or 2026-11-07 2:00pm.")


def times_from_range(day: str, time_range: str) -> tuple[datetime, datetime]:
    """Start and end from a date and a range like "7:00pm - 1:00am Est.".

    An end at or before the start is taken to be on the next day, so a party
    running past midnight (and an all-day "12:00am - 12:00am") ends after it starts.
    """
    clocks = _CLOCK.findall(time_range)
    if len(clocks) != 2:
        raise FetLifeError(f"{time_range!r} isn't a time range like 8:00pm - 10:00pm.")
    base = datetime.combine(date.fromisoformat(day), datetime.min.time())
    (sh, sm), (eh, em) = (_clock(*c) for c in clocks)
    start, end = base.replace(hour=sh, minute=sm), base.replace(hour=eh, minute=em)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def datetime_fields(name: str, when: datetime) -> list[tuple[str, str]]:
    """Rails' multi-part fields for *name* (``start_date_time`` / ``end_date_time``)."""
    hour = when.hour % 12 or 12
    return [
        (f"event[{name}(2i)]", f"{when.month:02d}"),
        (f"event[{name}(3i)]", f"{when.day:02d}"),
        (f"event[{name}(1i)]", str(when.year)),
        (f"event[{name}(4i)]", f"{hour:02d}"),
        (f"event[{name}(5i)]", f"{when.minute:02d}"),
        (f"event[{name}(6i)]", "AM" if when.hour < 12 else "PM"),
    ]


def _place_label(place_name: str, context: list | None) -> str:
    """Mapbox's place name without stray non-administrative regions (FetLife's placeLabel)."""
    stray = {text for entry in (context or [])
             if str(entry.get("id", "")).startswith("region.") and not entry.get("short_code")
             for text in (entry.get("text_en"), entry.get("text")) if text}
    if not stray:
        return place_name
    return ", ".join(part for part in place_name.split(", ") if part not in stray)


def location_feature(feature: dict) -> dict:
    """A Mapbox geocoding feature in the shape the form's ``event[geo_location]``
    carries (FetLife's MapboxGeocodingService.processResponse)."""
    out = {
        "id": feature["id"],
        "address": {"label": _place_label(feature["place_name"], feature.get("context")),
                    "streetLevel": " ".join(x for x in (feature.get("address"), feature.get("text")) if x)},
        "position": {"longitude": feature["center"][0], "latitude": feature["center"][1]},
        "boundingBox": feature.get("bbox"),
        "matchLevel": list(feature.get("place_type") or []),
        "context": feature.get("context"),
    }
    # JSON.stringify drops undefined values; addresses have no bounding box.
    return {k: v for k, v in out.items() if v is not None}


def within(feature: dict, place: dict) -> bool:
    """Whether the geocoded *feature* lies in the geocoded *place* (a city)."""
    return feature["id"] == place["id"] or any(
        entry.get("id") == place["id"] for entry in feature.get("context") or [])


def split_names(text: str) -> list[str]:
    """Nicknames from "A and B", "A, B" or "A & B", without repeats."""
    names: list[str] = []
    for name in re.split(r",|&|\band\b", text or ""):
        name = name.strip()
        if name and name.casefold() not in {n.casefold() for n in names}:
            names.append(name)
    return names


# --------------------------------------------------------------------------- #
# events-data.js
# --------------------------------------------------------------------------- #
#
# The file is a JavaScript array of object literals (``const EVENTS_DATA = [
# { id: '…', name: '…', … }, … ];``). It isn't JSON, so it is scanned here:
# strings, comments and braces are tracked well enough to find each event's
# object and its ``key: value`` pairs, and to rewrite one value in place
# without touching the rest of the file's formatting.

@dataclass
class _Field:
    value: object
    start: int  # span of the value's source text
    end: int


@dataclass
class _Entry:
    start: int  # the object's "{"
    end: int    # its "}"
    fields: dict[str, _Field]


_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _skip_string(src: str, i: int) -> int:
    """Index just past the string literal starting at *i*."""
    quote = src[i]
    i += 1
    while i < len(src):
        if src[i] == "\\":
            i += 2
            continue
        if src[i] == quote:
            return i + 1
        i += 1
    raise FetLifeError("The events file has an unterminated string.")


def _skip_comment(src: str, i: int) -> int:
    """Index just past a comment starting at *i*, or *i* if there is none."""
    if src.startswith("//", i):
        nl = src.find("\n", i)
        return len(src) if nl < 0 else nl + 1
    if src.startswith("/*", i):
        close = src.find("*/", i + 2)
        return len(src) if close < 0 else close + 2
    return i


def _unescape(body: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            n = body[i + 1]
            if n == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", body[i + 2:i + 6]):
                out.append(chr(int(body[i + 2:i + 6], 16)))
                i += 6
                continue
            if n == "\n":  # line continuation
                i += 2
                continue
            out.append(_ESCAPES.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _entries(src: str) -> list[_Entry]:
    """Every top-level ``{ … }`` object in the file, with its fields."""
    entries: list[_Entry] = []
    depth, start, i = 0, 0, 0
    while i < len(src):
        c = src[i]
        if c in "'\"`":
            i = _skip_string(src, i)
            continue
        j = _skip_comment(src, i)
        if j != i:
            i = j
            continue
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                entries.append(_Entry(start, i, _fields(src, start + 1, i)))
        i += 1
    return entries


_KEY = re.compile(r"""\s*(?:([A-Za-z_$][\w$]*)|(['"])(.*?)\2)\s*:\s*""", re.S)
_WORD = re.compile(r"true|false|null|-?\d+(?:\.\d+)?")


def _fields(src: str, i: int, end: int) -> dict[str, _Field]:
    """The ``key: value`` pairs of an object body; nested values are skipped."""
    fields: dict[str, _Field] = {}
    while i < end:
        j = _skip_comment(src, i)
        if j != i:
            i = j
            continue
        if src[i] in " \t\r\n,":
            i += 1
            continue
        m = _KEY.match(src, i, end)
        if not m:
            raise FetLifeError(f"Can't read the events file near: {src[i:i + 40]!r}")
        key = m.group(1) or m.group(3)
        i = m.end()
        if src[i] in "'\"`":
            stop = _skip_string(src, i)
            fields[key] = _Field(_unescape(src[i + 1:stop - 1]), i, stop)
            i = stop
            continue
        word = _WORD.match(src, i, end)
        if word:
            text = word.group()
            value = {"true": True, "false": False, "null": None}.get(text, text)
            if isinstance(value, str):
                value = float(text) if "." in text else int(text)
            fields[key] = _Field(value, i, word.end())
            i = word.end()
            continue
        # Something nested (an array or object): skip it whole.
        depth, k = 0, i
        while k < end:
            ch = src[k]
            if ch in "'\"`":
                k = _skip_string(src, k)
                continue
            if ch in "[{(":
                depth += 1
            elif ch in "]})":
                depth -= 1
            elif ch == "," and depth == 0:
                break
            k += 1
            if depth == 0:
                break
        fields[key] = _Field(None, i, k)
        i = k
    return fields


def _find(src: str, event_id: str, path) -> _Entry:
    hits = [e for e in _entries(src) if e.fields.get("id") and e.fields["id"].value == event_id]
    if not hits:
        raise FetLifeError(f"{path}: no event with id {event_id!r}.")
    if len(hits) > 1:
        raise FetLifeError(f"{path}: {len(hits)} events have the id {event_id!r}.")
    return hits[0]


def read_event(path: str | Path, event_id: str) -> dict:
    """The fields of the event with *event_id* in an events-data.js file."""
    src = Path(path).read_text(encoding="utf-8")
    return {k: f.value for k, f in _find(src, event_id, path).fields.items()}


def _js_string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def set_event_field(path: str | Path, event_id: str, key: str, value: str) -> None:
    """Set *key* of the event with *event_id* to the string *value*, in place.

    An existing value is replaced; otherwise ``key: 'value'`` is added as the
    object's last property, indented like its other properties. Nothing else
    in the file changes.
    """
    path = Path(path)
    src = path.read_text(encoding="utf-8")
    entry = _find(src, event_id, path)
    literal = _js_string(value)
    if key in entry.fields:
        field = entry.fields[key]
        src = src[:field.start] + literal + src[field.end:]
    else:
        id_line_start = src.rfind("\n", 0, entry.fields["id"].start) + 1
        indent = re.match(r"[ \t]*", src[id_line_start:]).group()
        last = entry.end
        while last > entry.start + 1 and src[last - 1].isspace():
            last -= 1
        comma = "" if src[last - 1] in ",{" else ","
        src = src[:last] + f"{comma}\n{indent}{key}: {literal}" + src[last:]
    path.write_text(src, encoding="utf-8")


def defaults_from_entry(entry: dict, assets_dir: Path) -> dict:
    """What an events-data.js entry says about the event, as post-event's options.

    name → title, subtitle → tagline, presenters → co-hosts, date + time →
    start/end, image (in *assets_dir*) → cover image, an "Online" audience →
    a virtual event, a ticket URL → a paid event, and dressCode, location,
    address and cityStateCountry → the options of those names. Only what the entry
    actually has is returned; the category is included only when it names
    one of FetLife's.
    """
    out: dict = {}
    text = {k: v.strip() for k, v in entry.items() if isinstance(v, str) and v.strip()}
    if "name" in text:
        out["title"] = text["name"]
    if "subtitle" in text:
        out["tagline"] = text["subtitle"]
    if "description" in text:
        out["description"] = entry["description"]
    if "presenters" in text:
        out["co_hosts"] = split_names(text["presenters"])
    if "category" in text:
        try:
            out["category"] = category_value(text["category"])
        except FetLifeError:
            pass
    if "date" in text and "time" in text:
        out["start"], out["end"] = times_from_range(text["date"], text["time"])
    if "image" in text:
        out["cover_image"] = str(assets_dir / text["image"])
    if "audience" in text:
        out["type"] = "virtual" if text["audience"].casefold() == "online" else "in-person"
    if "ticketUrl" in text:
        out["cost"] = "paid"
        out["ticket_url"] = text["ticketUrl"]
    elif "date" in text:
        out["cost"] = "free"
    for key, option in [("dressCode", "dress_code"), ("location", "location"),
                        ("address", "address"), ("cityStateCountry", "city")]:
        if key in text:
            out[option] = text[key]
    return out
