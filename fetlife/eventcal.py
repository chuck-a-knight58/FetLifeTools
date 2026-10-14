"""The events within a distance of a place, as the data for an HTML calendar.

Pure orchestration over a :class:`~fetlife.client.FetLifeClient`, like
:mod:`fetlife.groups`: the client walks FetLife's near-me event search, this
module keeps the events that fall in the requested dates and writes them as
a data script (``community_calendar.js``) for the static calendar page,
``community_calendar.html`` at the root of this repo.
"""

from __future__ import annotations

import base64
import calendar
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from .exceptions import RateLimitedError
from .models import Event

METERS_PER_UNIT = {"mi": 1609.344, "km": 1000.0}

# The page's colors per FetLife event category; categories not listed here
# take the next fallback color in the order they first appear.
CATEGORY_COLORS = {
    "BDSM Party": "#d9534f",
    "Sex Party": "#c98ad6",
    "Social": "#daa520",
    "Educational": "#7fb3d5",
    "Conference / Festival": "#5fb3a1",
}
FALLBACK_COLORS = ("#a3d977", "#f0a868", "#e8a3a3", "#b0a0ff", "#9ad1d4")


@dataclass
class Progress:
    """Passed to the ``on_progress`` callback after every page."""

    pages: int
    events: int
    # The latest day the search has listed so far (YYYY-MM-DD).
    through: str | None


@dataclass
class Result:
    events: list[Event] = field(default_factory=list)
    pages: int = 0
    # True when FetLife's rate limit ended the walk before *end* was reached;
    # the events from ``through`` onward may then be incomplete.
    rate_limited: bool = False
    # The latest day the search listed (YYYY-MM-DD), possibly past *end*.
    through: str | None = None


def _iso(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def default_end(start: date, months: int = 3) -> date:
    """The last day of a span of *months* months from *start*: the day before
    the same date *months* months on (Oct 1 → Dec 31, Oct 13 → Jan 12).

    When that month is too short to have the date, the span ends with it
    instead (Jan 31 → Feb 28).
    """
    month = start.month - 1 + months  # zero-based month index, counted from year 0
    year, month = start.year + month // 12, month % 12 + 1
    last = calendar.monthrange(year, month)[1]
    if start.day > last:
        return date(year, month, last)
    return date(year, month, start.day) - timedelta(days=1)


def gather_events(
    fl,
    center: tuple[float, float],
    radius: float,
    units: str,
    start: date,
    end: date,
    place_name: str | None = None,
    on_progress: Optional[Callable[[Progress], None]] = None,
) -> Result:
    """Every event within *radius* of *center* that runs on any day from
    *start* to *end* (inclusive), in start-date order.

    A multi-day event that began before *start* but is still running counts.
    Pages are read until one lists only days past *end* (a page can add
    more events to days an earlier page already showed). If the search runs out
    before *end* (FetLife only lists so far ahead in one go), it is started
    again from the day after the last one listed. A :class:`RateLimitedError`
    ends the walk instead of propagating: what was read is returned with
    ``rate_limited`` set.
    """
    lo, hi = _iso(start), _iso(end)
    found: dict[str, Event] = {}
    result = Result()
    range_meters = radius * METERS_PER_UNIT[units]
    from_date = start
    try:
        while from_date <= end:
            listed_any = False
            for events in fl.iter_events_near(center[0], center[1], range_meters,
                                              _iso(from_date), place_name=place_name):
                result.pages += 1
                listed_in_range = False
                for event in events:
                    listed_any = True
                    day = event.meta.get("listed_on")
                    if day and (result.through is None or day > result.through):
                        result.through = day
                    if day and day <= hi:
                        listed_in_range = True
                    if (event.start or "") > hi or (event.end or event.start or "") < lo:
                        continue
                    # A full card beats the slim continuation of the same event.
                    have = found.get(event.url)
                    if have is None or (have.meta.get("continuation")
                                        and not event.meta.get("continuation")):
                        found[event.url] = event
                if on_progress:
                    on_progress(Progress(result.pages, len(found), result.through))
                # Later pages can still add to days already shown, so stop only
                # once a page has nothing on or before *end*.
                if result.through and result.through > hi and not listed_in_range:
                    break
            if not listed_any or not result.through or result.through >= hi:
                break
            # The search ended short of *end*: pick up the day after its last.
            restart = datetime.strptime(result.through, "%Y-%m-%d").date() + timedelta(days=1)
            if restart <= from_date:
                break
            from_date = restart
    except RateLimitedError:
        result.rate_limited = True

    # Sorted by start date; events on the same day keep FetLife's (time) order.
    result.events = sorted(found.values(), key=lambda e: e.start or "")
    return result


def embed_images(session, events: list[Event], referer: str,
                 on_progress: Optional[Callable[[int, int], None]] = None) -> int:
    """Replace each event's picture URL with the image itself (a ``data:`` URI).

    The page can't simply link the pictures: FetLife's CDN refuses requests
    that don't come from a FetLife page ("Not Allowed: Hot Linking"), and
    the links are signed to expire within days anyway. *referer* (a FetLife
    URL) is sent so the CDN serves them. A picture that can't be fetched
    keeps its URL, which the page leaves out. Returns how many were embedded.
    """
    todo = [e for e in events if e.image_url and not e.image_url.startswith("data:")]
    done = 0
    for i, event in enumerate(todo, 1):
        try:
            resp = session.get(event.image_url, timeout=30, headers={"Referer": referer})
        except Exception:  # network trouble: leave this picture out
            resp = None
        ctype = resp.headers.get("content-type", "").split(";")[0] if resp is not None else ""
        if resp is not None and resp.status_code == 200 and ctype.startswith("image/"):
            event.image_url = f"data:{ctype};base64,{base64.b64encode(resp.content).decode()}"
            done += 1
        if on_progress:
            on_progress(i, len(todo))
    return done


def _payload(event: Event) -> dict:
    return {
        "url": event.url,
        "name": event.name,
        "start": event.start,
        "end": event.end or event.start,
        "category": event.category or "Other",
        "cost": event.cost,
        "when": event.when,
        "venue": event.location,
        "city": event.address,
        "rsvps": event.going_count,
        # Only embedded pictures: a plain CDN link won't load outside FetLife.
        "image": event.image_url if (event.image_url or "").startswith("data:") else None,
    }


def category_colors(events: list[Event]) -> dict[str, str]:
    """The color of every category among *events*, in first-seen order."""
    colors: dict[str, str] = {}
    spare = iter(FALLBACK_COLORS * (1 + len(events) // len(FALLBACK_COLORS)))
    for event in events:
        name = event.category or "Other"
        if name not in colors:
            colors[name] = CATEGORY_COLORS.get(name) or next(spare)
    return colors


def _script_json(value) -> str:
    """JSON safe to place inside a <script> element, one item per line."""
    return json.dumps(value, ensure_ascii=False, indent=4).replace("</", "<\\/")


def render_data_js(
    events: list[Event],
    start: date,
    end: date,
    place: str,
    radius: float,
    units: str,
    title: str | None = None,
    generated: datetime | None = None,
) -> str:
    """The calendar's data script: *events* (from :func:`gather_events`) as
    ``EVENTS`` and the page's heading, dates and colors as ``CONFIG``.
    """
    generated = generated or datetime.now().astimezone()
    span = (f"{start:%b} {start.day} – {end:%b} {end.day}, {end.year}" if start.year == end.year
            else f"{start:%b} {start.day}, {start.year} – {end:%b} {end.day}, {end.year}")
    count = f"{len(events)} event{'s' if len(events) != 1 else ''}"
    config = {
        "title": title or f"Events near {place}",
        "subtitle": f"Within {radius:g} {units} of {place} · {span} · {count}",
        "generated": (f"{generated:%B} {generated.day}, {generated.year} at "
                      + generated.strftime("%I:%M %p").lstrip("0")),
        "start": _iso(start),
        "end": _iso(end),
        "colors": category_colors(events),
    }
    return (DATA_HEADER
            + f"var EVENTS = {_script_json([_payload(e) for e in events])};\n"
            + f"var CONFIG = {_script_json(config)};\n")


DATA_HEADER = """\
// Data for community_calendar.html (keep the two in the same folder),
// written by FetLifeTools' `fetlife events-calendar`. Each run replaces
// this file.
//
// EVENTS: one per event, in start-date order.
//   url, name   - the event on FetLife
//   start, end  - "YYYY-MM-DD" (end = start for a one-day event)
//   category    - FetLife's category (colors and the legend come from it)
//   cost, when, venue, city, rsvps - as the event's card shows them (or null)
//   image       - an embedded picture (data: URI), or null
// CONFIG: the page's heading (title, subtitle, generated), the dates it
//   covers (start, end), and the color of each category (colors).

"""
