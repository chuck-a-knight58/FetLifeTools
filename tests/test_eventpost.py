"""Tests for post-event: fetlife.eventpost, its parsers and client calls (offline)."""

from datetime import datetime
from pathlib import Path

import pytest
import requests

from fetlife import eventpost, parsers
from fetlife.client import FetLifeClient
from fetlife.config import Config
from fetlife.exceptions import FetLifeError


def _authed(tmp_path):
    cfg = Config(username="tester", password="secret", base_url="https://fetlife.com",
                 rate_limit_min=0.0, rate_limit_max=0.0, session_path=tmp_path / "s.cookies",
                 throttle_state_path=tmp_path / "throttle.json", retry_backoff=0.0)
    fl = FetLifeClient(cfg, session=requests.Session())
    fl._authenticated = True
    return fl


# The New Event page, trimmed to what's parsed.
NEW_EVENT = """<html><head><meta name="csrf-token" content="page-tok"></head><body>
<form action="/search" method="get"><input name="q"></form>
<form action="/events" method="post" enctype="multipart/form-data">
  <input type="hidden" name="authenticity_token" value="form-tok">
  <select name="event[category]" id="event_category">
    <option value="">Select Category</option>
    <option value="educational">Educational (workshop, class, demo, etc.)</option>
    <option value="bdsm_party">BDSM Party (fetish party, dungeon party, etc.)</option>
    <option value="conference_festival">Conference / Festival</option>
  </select>
  <div data-controller="fl--listbox">
    <input type="hidden" name="event[cost_type]" id="event_cost_type">
    <ul><li role="option" data-id="free" data-label="Free"></li><li role="option" data-id="paid"></li></ul>
  </div>
  <select name="event[timezone]">
    <option value="">Select Timezone</option>
    <option value="America/Chicago">(GMT-05:00) Central Time (US &amp; Canada)</option>
    <option value="America/New_York">(GMT-04:00) Eastern Time (US &amp; Canada)</option>
  </select>
  <select name="event[type]"><option disabled value="">Select Event Type</option>
    <option value="physical">In-Person Event</option><option value="virtual">Virtual Event (worldwide)</option>
  </select>
  <div data-controller="fl--listbox">
    <input type="hidden" name="event[privacy]" id="event_privacy" value="">
    <ul><li role="option" data-id="public"></li><li role="option" data-id="private"></li></ul>
  </div>
</form></body></html>"""


def test_event_form_reads_the_token_and_the_live_choices():
    form = parsers.event_form_from_html(NEW_EVENT)
    assert form["token"] == "form-tok"
    assert list(form["categories"]) == ["educational", "bdsm_party", "conference_festival"]
    assert form["timezones"]["America/New_York"].startswith("(GMT-04:00) Eastern")
    assert form["types"] == ["physical", "virtual"]
    assert form["cost_types"] == ["free", "paid"]
    assert form["privacy_levels"] == ["public", "private"]
    assert parsers.event_form_from_html("<html><form action='/posts'></form></html>") is None


def test_categories_match_by_prefix_of_the_sites_label():
    assert eventpost.category_value("Educational") == "educational"
    assert eventpost.category_value("bdsm") == "bdsm_party"
    assert eventpost.category_value("Sex Party") == "sex_party"
    assert eventpost.category_value("Conferences") == "conference_festival"
    with pytest.raises(FetLifeError, match="isn't one of FetLife's event categories"):
        eventpost.category_value("Xanadu Reserve")


def test_timezones_match_by_name_or_a_unique_piece_of_the_label():
    zones = parsers.event_form_from_html(NEW_EVENT)["timezones"]
    assert eventpost.timezone_value("america/new_york", zones) == "America/New_York"
    assert eventpost.timezone_value("Eastern", zones) == "America/New_York"
    with pytest.raises(FetLifeError, match="matches America/Chicago, America/New_York"):
        eventpost.timezone_value("US & Canada", zones)


def test_times_parse_from_options_and_from_the_files_ranges():
    assert eventpost.parse_when("2026-11-07 2:00pm") == datetime(2026, 11, 7, 14, 0)
    assert eventpost.parse_when("2026-11-07 14:30") == datetime(2026, 11, 7, 14, 30)
    assert eventpost.parse_when("2026-11-07T12:15am") == datetime(2026, 11, 7, 0, 15)
    with pytest.raises(FetLifeError):
        eventpost.parse_when("next Tuesday")
    # Past midnight, and all day, end on the next day.
    assert eventpost.times_from_range("2027-12-31", "7:00pm - 1:00am Est.") == (
        datetime(2027, 12, 31, 19), datetime(2028, 1, 1, 1))
    assert eventpost.times_from_range("2026-11-20", "12:00am - 12:00am") == (
        datetime(2026, 11, 20), datetime(2026, 11, 21))


def test_datetime_fields_are_rails_twelve_hour_parts():
    assert eventpost.datetime_fields("start_date_time", datetime(2026, 3, 5, 0, 5)) == [
        ("event[start_date_time(2i)]", "03"), ("event[start_date_time(3i)]", "05"),
        ("event[start_date_time(1i)]", "2026"), ("event[start_date_time(4i)]", "12"),
        ("event[start_date_time(5i)]", "05"), ("event[start_date_time(6i)]", "AM")]
    assert eventpost.datetime_fields("end_date_time", datetime(2026, 3, 5, 22, 0))[3:] == [
        ("event[end_date_time(4i)]", "10"), ("event[end_date_time(5i)]", "00"),
        ("event[end_date_time(6i)]", "PM")]


EVENTS_JS = """// Event data.
// Fields: id, name, ...

const EVENTS_DATA = [
    {
        id: 'VDM102226',
        category: 'Educational',
        name: 'Virtual Dominants Meetup',
        subtitle: 'The Philosophy of Dominance',
        cohosts: 'DimpledSadist Knight_of_Xanadu',
        description: 'What does it mean?\\n\\nDiscussion: \\u201cIt\\'s {power}\\u201d',
        date: '2026-10-22',
        time: '8:00pm - 10:00pm',
        audience: 'Online',
        image: 'VDM.png',
        ticketUrl: '',
        cost: 'free',
        authRequired: false,
        wheelchairAccessible: false
    },
    {
        id: 'ETX112126',
        category: 'Xanadu Reserve',
        name: "Play Party",
        date: '2026-11-21',
        time: '7:00pm - 1:00am',
        audience: 'Members',
        ticketUrl: 'https://tickets.example/etx',
        Url: '',
        wheelchairAccessible: true,
        costDetails: '$40 per couple',
        dressCode: "Vanilla to the door, fetish inside",
        location: 'House of Xanadu',
        address: '4 Clubhouse Drive',
        cityStateCountry: 'Washington, New Jersey, United States'
    }
];
"""


def test_read_event_finds_the_entry_and_unescapes_its_strings(tmp_path):
    path = tmp_path / "events-data.js"
    path.write_text(EVENTS_JS)
    entry = eventpost.read_event(path, "VDM102226")
    assert entry["description"] == "What does it mean?\n\nDiscussion: “It's {power}”"
    assert entry["authRequired"] is False
    assert eventpost.read_event(path, "ETX112126")["name"] == "Play Party"
    with pytest.raises(FetLifeError, match="no event with id 'NOPE'"):
        eventpost.read_event(path, "NOPE")


def test_split_names_takes_any_separator():
    assert eventpost.split_names("A and B, C & D  E\tA") == ["A", "B", "C", "D", "E"]
    assert eventpost.split_names("") == []


def test_defaults_from_entry_map_the_sites_fields(tmp_path):
    path = tmp_path / "events-data.js"
    path.write_text(EVENTS_JS)
    vdm = eventpost.defaults_from_entry(eventpost.read_event(path, "VDM102226"), Path("/site/assets"))
    assert vdm["title"] == "Virtual Dominants Meetup" and vdm["tagline"] == "The Philosophy of Dominance"
    assert vdm["co_hosts"] == ["DimpledSadist", "Knight_of_Xanadu"]
    assert vdm["category"] == "educational" and vdm["type"] == "virtual" and vdm["cost"] == "free"
    assert vdm["start"] == datetime(2026, 10, 22, 20) and vdm["end"] == datetime(2026, 10, 22, 22)
    assert vdm["cover_image"] == "/site/assets/VDM.png"
    etx = eventpost.defaults_from_entry(eventpost.read_event(path, "ETX112126"), Path("/a"))
    assert "category" not in etx  # not one of FetLife's
    assert etx["type"] == "in-person" and etx["cost"] == "paid"
    assert etx["ticket_url"] == "https://tickets.example/etx"
    assert etx["end"] == datetime(2026, 11, 22, 1)
    assert etx["dress_code"] == "Vanilla to the door, fetish inside"
    assert etx["cost_details"] == "$40 per couple" and "cost_details" not in vdm
    assert etx["location"] == "House of Xanadu" and etx["address"] == "4 Clubhouse Drive"
    assert etx["city"] == "Washington, New Jersey, United States"
    assert "dress_code" not in vdm and "city" not in vdm
    assert etx["wheelchair_accessible"] is True and vdm["wheelchair_accessible"] is False


def test_defaults_from_entry_cost():
    assert eventpost.defaults_from_entry({"date": "2026-11-07"}, Path("/a"))["cost"] == "free"
    assert eventpost.defaults_from_entry({"ticketUrl": "https://t"}, Path("/a"))["cost"] == "paid"
    assert eventpost.defaults_from_entry({"ticketUrl": "https://t", "cost": "free"}, Path("/a"))["cost"] == "free"
    assert eventpost.defaults_from_entry({"cost": "Paid"}, Path("/a"))["cost"] == "paid"
    with pytest.raises(FetLifeError, match="X1's cost is 'cheap'"):
        eventpost.defaults_from_entry({"id": "X1", "cost": "cheap"}, Path("/a"))


def test_set_event_field_adds_or_replaces_only_that_value(tmp_path):
    path = tmp_path / "events-data.js"
    path.write_text(EVENTS_JS)
    eventpost.set_event_field(path, "VDM102226", "Url", "https://fetlife.com/events/1")
    eventpost.set_event_field(path, "ETX112126", "Url", "https://fetlife.com/events/it's")
    text = path.read_text()
    assert ("        wheelchairAccessible: false,\n        Url: 'https://fetlife.com/events/1'\n    },"
            in text)
    assert "        Url: 'https://fetlife.com/events/it\\'s',\n        wheelchairAccessible:" in text
    assert eventpost.read_event(path, "ETX112126")["Url"] == "https://fetlife.com/events/it's"
    # Everything else is byte-for-byte the same.
    assert text.replace(",\n        Url: 'https://fetlife.com/events/1'", "").replace(
        "Url: 'https://fetlife.com/events/it\\'s'", "Url: ''") == EVENTS_JS


class _Resp:
    def __init__(self, status, text, url):
        self.status_code, self.text, self.url = status, text, url
        self.headers = {}


def test_create_event_posts_multipart_and_returns_the_event(tmp_path, monkeypatch):
    fl = _authed(tmp_path)
    cover = tmp_path / "c.png"
    cover.write_bytes(b"\x89PNG fake")
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, path, kw))
        return _Resp(200, "<html></html>", "https://fetlife.com/events/777")

    monkeypatch.setattr(fl, "request", fake_request)
    assert fl.create_event([("event[name]", "N")], cover, "image/png") == "https://fetlife.com/events/777"
    method, path, kw = calls[0]
    assert (method, path) == ("POST", "/events")
    assert kw["retry_server_errors"] is False and "multipart" in kw

    # Events are now addressed by date and name; a page under the event counts too.
    slug = "https://fetlife.com/events/2026/11/07/rope-jam-abc123"
    for landed in (slug, slug + "/invite"):
        monkeypatch.setattr(fl, "request", lambda *a, landed=landed, **k: _Resp(200, "", landed))
        assert fl.create_event([("event[name]", "N")]) == slug

    monkeypatch.setattr(fl, "request", lambda *a, **k: _Resp(
        422, "<div id='error_explanation'><ul><li>Timezone can't be blank</li></ul></div>",
        "https://fetlife.com/events"))
    with pytest.raises(FetLifeError, match="did not create the event: Timezone can't be blank"):
        fl.create_event([("event[name]", "N")])


def test_find_co_host_matches_the_nickname_exactly(tmp_path, monkeypatch):
    fl = _authed(tmp_path)
    html = ("<div data-form-typeahead-multi-select-option data-label='Grr_Bear' data-value='11'></div>"
            "<div data-form-typeahead-multi-select-option data-label='Grr_Bear2' data-value='12'></div>")
    seen = []

    def fake_get(path, **kw):
        seen.append((path, kw["headers"]))
        return _Resp(200, html, "https://fetlife.com" + path)

    monkeypatch.setattr(fl, "get", fake_get)
    assert fl.find_co_host("grr_bear") == {"name": "Grr_Bear", "id": "11"}
    assert seen[0][0] == "/events/co_hosts/suggestions?query=grr_bear"
    assert seen[0][1]["X-Requested-With"] == "XMLHttpRequest"
    assert fl.find_co_host("Nobody") is None


def test_cli_posts_the_file_entry_and_saves_the_url_back(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from fetlife import cli
    from fetlife.models import Member

    path = tmp_path / "events-data.js"
    path.write_text(EVENTS_JS)
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets" / "VDM.png").write_bytes(b"\x89PNG fake")
    posted = {}

    class FakeClient:
        on_retry = None
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def whoami(self): return Member(nickname="Knight_of_Xanadu", id="1")
        def get_event_form(self): return parsers.event_form_from_html(NEW_EVENT)
        def find_co_host(self, name):
            return {"name": "DimpledSadist", "id": "42"} if name == "DimpledSadist" else None
        def create_event(self, fields, cover, cover_type):
            posted.update(fields=fields, cover=cover, cover_type=cover_type)
            return "https://fetlife.com/events/999"

    monkeypatch.setattr(cli, "_client", lambda ctx: FakeClient())
    extra = tmp_path / "extra.txt"
    extra.write_text("Bring questions.\n")
    result = CliRunner().invoke(cli.cli, ["--env-file", str(tmp_path / "none.env"), "post-event",
                                          "--events-file", str(path), "--event-id", "VDM102226",
                                          "--description-file", str(extra), "--keep-file-description",
                                          "--location", "https://zoom.example/j/1",
                                          "--dress-code", "Casual", "--wheelchair-accessible", "--yes"])
    assert result.exit_code == 0, result.output
    fields = posted["fields"]
    values = dict(fields)
    assert values["authenticity_token"] == "form-tok"
    assert values["event[name]"] == "Virtual Dominants Meetup"
    assert values["event[tagline]"] == "The Philosophy of Dominance"
    assert values["event[rich_description]"].startswith("<p>What does it mean?</p><p>Discussion:")
    assert values["event[rich_description]"].endswith("</p><p>Bring questions.</p>")
    assert [v for k, v in fields if k == "event[co_host_user_ids][]"] == ["", "42"]
    assert values["event[category]"] == "educational" and values["event[type]"] == "virtual"
    assert values["event[cost_type]"] == "free" and values["event[timezone]"] == "America/New_York"
    assert [v for k, v in fields if k.endswith("[wheelchair_accessible]")] == ["0", "1"]
    assert values["event[dress_code]"] == "Casual"
    assert values["event[location]"] == "https://zoom.example/j/1"
    assert values["event[start_date_time(4i)]"] == "08" and values["event[start_date_time(6i)]"] == "PM"
    assert values["event[invite_policy]"] == "organizers_and_attendees"
    assert posted["cover"].endswith("assets/VDM.png") and posted["cover_type"] == "image/png"
    assert eventpost.read_event(path, "VDM102226")["Url"] == "https://fetlife.com/events/999"

    # Posting the same entry again is refused rather than creating a duplicate.
    again = CliRunner().invoke(cli.cli, ["--env-file", str(tmp_path / "none.env"), "post-event",
                                         "--events-file", str(path), "--event-id", "VDM102226", "--yes"])
    assert again.exit_code != 0
    assert "already has a FetLife event" in str(again.exception) + again.output


def test_cli_explicit_options_override_the_file_and_missing_ones_are_named(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from fetlife import cli

    def no_network(*a, **k):
        raise AssertionError("must fail before contacting FetLife")

    monkeypatch.setattr(cli, "_client", no_network)
    path = tmp_path / "events-data.js"
    path.write_text(EVENTS_JS)
    # ETX's category isn't FetLife's, so it must be given.
    result = CliRunner().invoke(cli.cli, ["--env-file", str(tmp_path / "none.env"), "post-event",
                                          "--events-file", str(path), "--event-id", "ETX112126"])
    assert result.exit_code != 0
    assert "Missing --category" in str(result.exception)


CITY = {"id": "place.1", "type": "Feature", "place_type": ["place"], "text": "Washington",
        "place_name": "Washington, New Jersey, United States", "center": [-74.98, 40.76],
        "bbox": [-75.0, 40.7, -74.9, 40.8],
        "context": [{"id": "region.9", "text": "New Jersey", "short_code": "US-NJ"}]}
STREET = {"id": "address.7", "place_type": ["address"], "address": "4", "text": "Clubhouse Drive",
          "place_name": "4 Clubhouse Drive, Washington, New Jersey 07882, United States",
          "center": [-74.97, 40.75], "context": [{"id": "postcode.3"}, {"id": "place.1"}]}
ELSEWHERE = dict(STREET, id="address.8", context=[{"id": "place.2"}])


def test_location_feature_is_the_shape_the_form_posts():
    city = eventpost.location_feature(CITY)
    assert city["address"] == {"label": "Washington, New Jersey, United States", "streetLevel": "Washington"}
    assert city["position"] == {"longitude": -74.98, "latitude": 40.76}
    assert city["matchLevel"] == ["place"] and city["boundingBox"] == [-75.0, 40.7, -74.9, 40.8]
    street = eventpost.location_feature(STREET)
    assert street["address"]["streetLevel"] == "4 Clubhouse Drive" and "boundingBox" not in street
    assert eventpost.within(street, city) and not eventpost.within(eventpost.location_feature(ELSEWHERE), city)
    # A non-administrative region Mapbox slots in is dropped from the label.
    stray = dict(CITY, place_name="Hamburg, Lüneburg Heath, Germany",
                 context=[{"id": "region.5", "text": "Lüneburg Heath"}])
    assert eventpost.location_feature(stray)["address"]["label"] == "Hamburg, Germany"


def _in_person_client(posted, streets):
    from fetlife.models import Member

    class FakeClient:
        on_retry = None
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def whoami(self): return Member(nickname="Knight_of_Xanadu", id="1")
        def get_event_form(self):
            return dict(parsers.event_form_from_html(NEW_EVENT), mapbox_token="pk.test")
        def find_co_host(self, name): return None
        def geocode(self, token, query, types, proximity=None, limit=5):
            posted.setdefault("lookups", []).append((query, types, proximity))
            return [CITY] if "place" in types else streets
        def create_event(self, fields, cover, cover_type):
            posted.update(fields=fields)
            return "https://fetlife.com/events/1000"
    return FakeClient()


@pytest.mark.parametrize("streets, address", [([ELSEWHERE, STREET], "4 Clubhouse Drive"), ([ELSEWHERE], "")])
def test_cli_places_an_in_person_event_by_city_and_address(tmp_path, monkeypatch, streets, address):
    import json

    from click.testing import CliRunner

    from fetlife import cli

    path = tmp_path / "events-data.js"
    path.write_text(EVENTS_JS)
    posted = {}
    monkeypatch.setattr(cli, "_client", lambda ctx: _in_person_client(posted, streets))
    result = CliRunner().invoke(cli.cli, ["--env-file", str(tmp_path / "none.env"), "post-event",
                                          "--events-file", str(path), "--event-id", "ETX112126",
                                          "--category", "BDSM Party", "--yes"])
    assert result.exit_code == 0, result.output
    values = dict(posted["fields"])
    assert posted["lookups"][0] == ("Washington, New Jersey, United States", ["place", "locality"], None)
    assert posted["lookups"][1] == ("4 Clubhouse Drive", ["address", "postcode"], (-74.98, 40.76))
    geo = json.loads(values["event[geo_location]"])
    assert values["event[address]"] == address
    assert geo["id"] == ("address.7" if address else "place.1")
    assert values["event[location]"] == "House of Xanadu"
    assert values["event[dress_code]"] == "Vanilla to the door, fetish inside"
