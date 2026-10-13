"""Tests for the near-me event parser and fetlife.eventcal (offline)."""

import json
import re
from datetime import date

from fetlife import eventcal, parsers
from fetlife.exceptions import RateLimitedError
from fetlife.models import Event


def _card(href, name, header, when_html, venue, city, rsvps=None, event_id=None, img=None):
    """A full event card as /events/near renders it (trimmed to what's parsed)."""
    frame = (f'<turbo-frame id="event_rsvp_bar_{event_id}"><form action="/events/{event_id}/my_rsvp">'
             '</form></turbo-frame>') if event_id else ""
    return f"""
    <div class="w-full" data-controller="clickable" data-clickable-url-value="{href}">
      <a href="{href}">{f'<div><img src="{img}" alt=""></div>' if img else ''}</a>
      <div><a href="{href}"><div class="flex-auto">
        <div class="pb-2">{header}</div>
        <h3 title="{name}"><span>{name}</span></h3>
        <div class="pt-2">
          <div class="flex items-start"><span><svg><use href="/icon-sprite.svg#icon-clock"></use></svg></span>
            <span class="min-w-0">{when_html}</span></div>
          <div class="flex items-start"><span><svg><use href="/icon-sprite.svg#icon-location"></use></svg></span>
            <span class="min-w-0"> {venue} <span class="block">{city}</span> </span></div>
        </div>
      </div></a>
      {f'<div><span> {rsvps} RSVPs </span></div>' if rsvps is not None else ''}
      </div>
      {frame}
    </div>"""


def _continuation(href, name, header):
    return f"""
    <div class="@container"><a href="{href}">
      <div class="pb-1.5">{header}</div>
      <div title="{name}"><span>{name}</span></div>
    </a></div>"""


def _day(key, cards="", continuations=""):
    return f"""
    <div id="events_near_day_{key}" data-day-key="{key}">
      <div id="events_near_day_{key}_cards" data-day-cards>{cards}</div>
      <div id="events_near_day_{key}_continuations" data-day-continuations>{continuations}</div>
    </div>"""


CAT = '<span data-testid="event card category">{}</span>'

NEAR_PAGE = f"""<html><body><turbo-frame id="events_near_results"><div id="events_near_list">
{_day("2026-10-13",
      cards=_card("/events/2026/10/13/munch-abc", "Tuesday Munch",
                  CAT.format("Social") + '<span> · </span>Free',
                  " Tue, Oct 13 at 6:30 PM ", "The Pub", "Phoenixville, Pennsylvania",
                  rsvps=34, event_id="2307444", img="https://cdn.example/a.jpg")
      + _card("/events/2026/10/13/week-long-xyz", "Week Long",
              'day 1 of 6<span> · </span>' + CAT.format("Sex Party"),
              '<span class="block">Oct 13 – 18</span><span class="block">Tue 6:00 PM – Sun 8:00 AM</span>',
              "Stacks", "New York City, New York", rsvps=1, event_id="2324671"),
      continuations=_continuation("/events/2026/10/10/festival-q1", "Fall Festival",
                                  'day 4 of 7<span> · </span>' + CAT.format("Conference / Festival")))}
</div></turbo-frame>
<turbo-frame loading="lazy" id="events_near_pagination_loader"
  src="/events/near.turbo_stream?from_date=2026-10-13&amp;page=2&amp;search%5Brange%5D=40234"></turbo-frame>
</body></html>"""

NEAR_STREAM = f"""
<turbo-stream action="append" target="events_near_list"><template>
{_day("2026-10-20", cards=_card("/events/2026/10/20/class-d4", "Rope Class",
                                CAT.format("Educational") + '<span> · </span>$20',
                                " Tue, Oct 20 at 7:00 PM ", "Studio", "Easton, Pennsylvania"))}
</template></turbo-stream>
<turbo-stream action="update" target="events_near_pagination"><template> </template></turbo-stream>"""


def test_events_near_reads_cards_and_continuations():
    events, next_path = parsers.events_near_from_html(NEAR_PAGE, base_url="https://fetlife.com")
    assert next_path == "/events/near.turbo_stream?from_date=2026-10-13&page=2&search%5Brange%5D=40234"
    munch, week, fest = events

    assert munch.id == "2307444" and munch.name == "Tuesday Munch"
    assert munch.url == "https://fetlife.com/events/2026/10/13/munch-abc"
    assert (munch.start, munch.end) == ("2026-10-13", "2026-10-13")
    assert (munch.category, munch.cost) == ("Social", "Free")
    assert munch.when == "Tue, Oct 13 at 6:30 PM"
    assert (munch.location, munch.address) == ("The Pub", "Phoenixville, Pennsylvania")
    assert munch.going_count == 34 and munch.image_url == "https://cdn.example/a.jpg"

    # "day 1 of 6" gives the end date; the two time lines are joined.
    assert (week.start, week.end, week.cost) == ("2026-10-13", "2026-10-18", None)
    assert week.when == "Oct 13 – 18 · Tue 6:00 PM – Sun 8:00 AM"
    assert week.image_url is None

    # A continuation of an event that began earlier: dates from its URL + "day N of M".
    assert fest.meta["continuation"] and fest.meta["listed_on"] == "2026-10-13"
    assert (fest.name, fest.category) == ("Fall Festival", "Conference / Festival")
    assert (fest.start, fest.end) == ("2026-10-10", "2026-10-16")
    assert fest.when is None and fest.id is None


def test_events_near_reads_turbo_stream_appends_to_days_already_shown():
    stream = f"""
<turbo-stream action="append" target="events_near_day_2026-10-16_cards"><template>
{_card("/events/2026/10/16/friday-f1", "Friday Party", CAT.format("Sex Party"),
       " Fri, Oct 16 at 9:00 PM ", "Club", "Newark, New Jersey", event_id="2400001")}
</template></turbo-stream>
<turbo-stream action="append" target="events_near_day_2026-10-17_continuations"><template>
{_continuation("/events/2026/10/16/retreat-r1", "Retreat",
               'day 2 of 3<span> · </span>' + CAT.format("Conference / Festival"))}
</template></turbo-stream>
<turbo-stream action="append" target="events_near_list"><template>
{_day("2026-10-19", cards=_card("/events/2026/10/19/munch-m1", "Monday Munch", CAT.format("Social"),
                                " Mon, Oct 19 at 7:00 PM ", "Diner", "Easton, Pennsylvania"))}
</template></turbo-stream>"""
    events, _ = parsers.events_near_from_html(stream, base_url="https://fetlife.com")
    by_name = {e.name: e for e in events}
    assert set(by_name) == {"Friday Party", "Retreat", "Monday Munch"}
    friday, retreat = by_name["Friday Party"], by_name["Retreat"]
    assert friday.meta["listed_on"] == "2026-10-16" and friday.id == "2400001"
    assert (friday.location, friday.address) == ("Club", "Newark, New Jersey")
    assert retreat.meta["continuation"] and retreat.meta["listed_on"] == "2026-10-17"
    assert (retreat.start, retreat.end) == ("2026-10-16", "2026-10-18")


def test_events_near_reads_turbo_stream_pages_and_their_end():
    events, next_path = parsers.events_near_from_html(NEAR_STREAM, base_url="https://fetlife.com")
    assert next_path is None
    (cls,) = events
    assert (cls.name, cls.category, cls.cost, cls.start) == ("Rope Class", "Educational", "$20", "2026-10-20")


# --------------------------------------------------------------------------- #
# gather_events
# --------------------------------------------------------------------------- #
def _ev(start, name, end=None, cont=False, listed=None, category="Social"):
    meta = {"listed_on": listed or start}
    if cont:
        meta["continuation"] = True
    return Event(name=name, start=start, end=end or start, category=category,
                 url=f"https://fetlife.com/events/{start.replace('-', '/')}/{name}", meta=meta)


class StubClient:
    """Serves search pages per from_date; an exception in a page list is raised."""

    def __init__(self, searches):
        self.searches = searches
        self.calls = []

    def iter_events_near(self, lat, lng, range_meters, from_date, place_name=None):
        self.calls.append((from_date, round(range_meters)))
        for page in self.searches.get(from_date, []):
            if isinstance(page, Exception):
                raise page
            yield page


def test_gather_keeps_events_overlapping_the_dates_and_stops_past_the_end():
    fl = StubClient({"2026-10-13": [
        [_ev("2026-10-10", "fest", end="2026-10-16", cont=True, listed="2026-10-13"),
         _ev("2026-10-13", "munch"), _ev("2026-10-15", "class")],
        [_ev("2026-10-20", "party"), _ev("2026-10-21", "late")],
        [_ev("2026-11-01", "past-end")],
        [_ev("2026-11-02", "never-fetched")],
    ]})
    seen = []
    r = eventcal.gather_events(fl, (40.0, -75.0), 25, "mi", date(2026, 10, 13), date(2026, 10, 20),
                               on_progress=seen.append)
    assert [e.name for e in r.events] == ["fest", "munch", "class", "party"]
    assert fl.calls == [("2026-10-13", 40234)]  # 25 mi in meters
    assert r.pages == 3 and not r.rate_limited and r.through == "2026-11-01"
    assert [(p.pages, p.events) for p in seen] == [(1, 3), (2, 4), (3, 4)]


def test_gather_reads_on_while_pages_add_to_days_already_shown():
    # Page 1 shows Oct 16-18; page 2 appends more cards to Oct 16.
    fl = StubClient({"2026-10-16": [
        [_ev("2026-10-13", "week", end="2026-10-18", listed="2026-10-16"),
         _ev("2026-10-13", "week", end="2026-10-18", cont=True, listed="2026-10-18")],
        [_ev("2026-10-16", "friday")],
        [_ev("2026-10-17", "saturday"), _ev("2026-10-19", "monday")],
        [_ev("2026-10-20", "tuesday")],
        [_ev("2026-10-21", "never-fetched")],
    ]})
    r = eventcal.gather_events(fl, (0, 0), 50, "mi", date(2026, 10, 16), date(2026, 10, 17))
    assert [e.name for e in r.events] == ["week", "friday", "saturday"]
    assert r.pages == 4


def test_gather_prefers_a_full_card_over_a_continuation():
    full = _ev("2026-10-13", "week", end="2026-10-18")
    fl = StubClient({"2026-10-14": [[_ev("2026-10-13", "week", end="2026-10-18", cont=True,
                                         listed="2026-10-14"), full], [_ev("2026-10-30", "x")]]})
    r = eventcal.gather_events(fl, (0, 0), 10, "km", date(2026, 10, 14), date(2026, 10, 20))
    assert r.events == [full]
    assert fl.calls == [("2026-10-14", 10000)]


def test_gather_restarts_after_the_last_listed_day_when_the_search_runs_out():
    fl = StubClient({
        "2026-12-20": [[_ev("2026-12-31", "nye")]],
        "2027-01-01": [[_ev("2027-01-09", "jan")]],
        "2027-01-10": [],
    })
    r = eventcal.gather_events(fl, (0, 0), 50, "mi", date(2026, 12, 20), date(2027, 1, 31))
    assert [e.name for e in r.events] == ["nye", "jan"]
    assert [c[0] for c in fl.calls] == ["2026-12-20", "2027-01-01", "2027-01-10"]


def test_gather_returns_what_it_has_when_rate_limited():
    fl = StubClient({"2026-10-13": [[_ev("2026-10-13", "munch")], RateLimitedError("429")]})
    r = eventcal.gather_events(fl, (0, 0), 50, "mi", date(2026, 10, 13), date(2026, 11, 13))
    assert [e.name for e in r.events] == ["munch"]
    assert r.rate_limited and r.through == "2026-10-13"


# --------------------------------------------------------------------------- #
# embed_images / render_html
# --------------------------------------------------------------------------- #
class _Resp:
    def __init__(self, status, ctype, content=b""):
        self.status_code, self.headers, self.content = status, {"content-type": ctype}, content


class StubSession:
    def __init__(self, responses):
        self.responses = responses
        self.headers = []

    def get(self, url, timeout=None, headers=None):
        self.headers.append(headers)
        resp = self.responses[url]
        if isinstance(resp, Exception):
            raise resp
        return resp


def test_embed_images_inlines_what_the_cdn_serves():
    ok, blocked, broken, none = (Event(name=n, image_url=f"https://cdn/{n}.jpg") for n in "abcd")
    none.image_url = None
    session = StubSession({
        "https://cdn/a.jpg": _Resp(200, "image/jpeg", b"\xff\xd8"),
        "https://cdn/b.jpg": _Resp(403, "text/plain", b"Not Allowed: Hot Linking"),
        "https://cdn/c.jpg": OSError("down"),
    })
    assert eventcal.embed_images(session, [ok, blocked, broken, none], referer="https://fetlife.com/") == 1
    assert ok.image_url == "data:image/jpeg;base64,/9g="
    assert blocked.image_url == "https://cdn/b.jpg" and none.image_url is None
    assert session.headers == [{"Referer": "https://fetlife.com/"}] * 3


def _page_data(page):
    events = re.search(r"var EVENTS = (.*);\nvar CONFIG = (.*);\n", page)
    return json.loads(events.group(1)), json.loads(events.group(2))


def test_render_html_embeds_the_events_and_settings():
    shown = Event(name="Munch </script><b>", start="2026-11-01", end="2026-11-01", category="Social",
                  url="https://fetlife.com/events/2026/11/01/m", image_url="data:image/png;base64,AA==")
    linked = Event(name="Party", start="2026-11-03", end="2026-11-04", category="Masquerade",
                   url="https://fetlife.com/events/2026/11/03/p", image_url="https://cdn/p.jpg")
    page = eventcal.render_html([shown, linked], date(2026, 11, 1), date(2026, 11, 30),
                                "Easton, PA", 25, "mi", title="Fall & Winter")
    assert "<title>Fall &amp; Winter</title>" in page
    assert "Within 25 mi of Easton, PA · Nov 1 – Nov 30, 2026 · 2 events" in page
    assert "</script><b>" not in page  # a name can't close the data script
    events, config = _page_data(page)
    assert [e["name"] for e in events] == ["Munch </script><b>", "Party"]
    assert events[0]["image"] == "data:image/png;base64,AA=="
    assert events[1]["image"] is None  # a CDN link wouldn't load outside FetLife
    assert config["start"] == "2026-11-01" and config["end"] == "2026-11-30"
    assert config["colors"] == {"Social": "#daa520", "Masquerade": eventcal.FALLBACK_COLORS[0]}
    # The calendar's views, and dates that open a day's list in a new tab.
    for sub in ("overview", "month", "week", "day"):
        assert f'data-sub="{sub}"' in page
    assert "'#day=' + k" in page and "target=\"_blank\"" in page
    # Hidden words live in the browser's storage.
    assert 'id="exclude-form"' in page and "localStorage.setItem(WORDS_KEY" in page


def test_default_end_spans_three_months():
    assert eventcal.default_end(date(2026, 10, 1)) == date(2026, 12, 31)   # three whole months
    assert eventcal.default_end(date(2026, 11, 1)) == date(2027, 1, 31)    # across the year
    assert eventcal.default_end(date(2026, 10, 13)) == date(2027, 1, 12)   # mid-month start
    assert eventcal.default_end(date(2026, 11, 30)) == date(2027, 2, 28)   # short last month
    assert eventcal.default_end(date(2027, 11, 30)) == date(2028, 2, 29)   # leap year
    assert eventcal.default_end(date(2026, 10, 13), months=1) == date(2026, 11, 12)
