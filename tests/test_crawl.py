"""Tests for the BFS crawl using in-memory stubs (fully offline)."""

from datetime import datetime, timedelta, timezone

import pytest

from fetlife import crawl, geo
from fetlife.models import Member, Relationship

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _member(nick, location, ds=False):
    rels = [Relationship(kind="D/s", status="owned", with_nickname="X")] if ds else []
    return Member(nickname=nick, location=location, url=f"https://fetlife.com/{nick}",
                  age=30, gender="F", role="submissive", relationships=rels)


# center (0,0), radius 50mi:  (0.1,0.1)~9.8mi in · (0.2,0.2)~19.6mi in · (5,5) far out
CENTER = (0.0, 0.0)
GEO = {
    "NearCity": ((0.1, 0.1), geo.OK),
    "FarCity": ((5.0, 5.0), geo.OK),
    "Pennsylvania": ((0.2, 0.2), geo.STATE_ONLY),
    "Atlantis": (None, geo.NOT_FOUND),
}


class StubGeocoder:
    def locate(self, query):
        if not query:
            return (None, geo.NOT_FOUND)
        return GEO.get(query, (None, geo.NOT_FOUND))

    def locate_member(self, member):
        return self.locate(member.location)


# Last-active times relative to NOW (used only by the activity-filter test).
LAST_ACTIVE = {
    "alice": NOW - timedelta(days=5),    # recent
    "dave": NOW - timedelta(days=60),    # stale
    "eve": NOW - timedelta(days=10),     # recent
    "notfound": NOW - timedelta(days=2),  # recent
    "stateonly": None,                    # unknown -> treated as inactive
}


class StubClient:
    def __init__(self, members, friends):
        self.members = members
        self.friends = friends
        self.member_fetches = []

    def whoami(self):
        return Member(nickname="seed", location="NearCity")

    def get_member(self, ident):
        self.member_fetches.append(ident)
        return self.members[ident]

    def get_friends(self, ident, page=1):
        return self.friends.get(ident, []) if page == 1 else []

    def get_followers(self, ident, page=1):
        return []

    def get_last_active(self, ident):
        return LAST_ACTIVE.get(ident)


def _build():
    members = {
        "alice": _member("alice", "NearCity", ds=True),
        "bob": _member("bob", "FarCity", ds=False),
        "carol": _member("carol", "NearCity", ds=False),
        "dave": _member("dave", "NearCity", ds=True),
        "eve": _member("eve", "NearCity", ds=True),
        "stateonly": _member("stateonly", "Pennsylvania", ds=True),
        "notfound": _member("notfound", "Atlantis", ds=True),
        "zed": _member("zed", "NearCity", ds=True),
    }
    friends = {
        "seed": [members[n] for n in ("alice", "bob", "carol", "stateonly", "notfound")],
        "alice": [members["dave"]],   # in-area -> expanded
        "carol": [members["eve"]],    # in-area (no D/s) -> still expanded
        "bob": [members["zed"]],      # out-of-area -> NOT expanded
    }
    return StubClient(members, friends), StubGeocoder()


def _run(ds_only):
    client, gc = _build()
    rows = list(crawl.discover(client, gc, CENTER, 50.0, seed="seed",
                               ds_only=ds_only, max_pages=2))
    return client, {r.fet_name: r for r in rows}


def test_ds_only_yields_in_area_ds_members():
    _, by_name = _run(ds_only=True)
    # alice/dave/eve reached & D/s; stateonly/notfound flagged; carol(no D/s) & bob(far) absent.
    assert set(by_name) == {"alice", "dave", "eve", "stateonly", "notfound"}
    assert all(by_name[n].ds for n in by_name)
    # zed is only reachable through out-of-area bob, so must not appear.
    assert "zed" not in by_name


def test_out_of_area_not_expanded_even_without_ds_filter():
    _, by_name = _run(ds_only=False)
    # carol now shown (ds False); bob excluded (out of area); zed still unreachable.
    assert "carol" in by_name and by_name["carol"].ds is False
    assert "bob" not in by_name
    assert "zed" not in by_name


def test_location_flags_and_gps_formatting():
    _, by_name = _run(ds_only=True)
    assert by_name["alice"].location_flag == geo.OK
    assert by_name["alice"].gps == "0.1000, 0.1000"
    assert by_name["stateonly"].location_flag == geo.STATE_ONLY
    assert "state-only" in by_name["stateonly"].gps
    assert by_name["notfound"].location_flag == geo.NOT_FOUND
    assert by_name["notfound"].gps == "not found"


def test_max_visits_caps_crawl():
    client, gc = _build()
    rows = list(crawl.discover(client, gc, CENTER, 50.0, seed="seed",
                               ds_only=False, max_visits=1, max_pages=2))
    # Only one candidate is visited, so at most one row is produced.
    assert len(rows) <= 1


def test_active_within_filters_stale_and_unknown():
    client, gc = _build()
    rows = list(crawl.discover(
        client, gc, CENTER, 50.0, seed="seed", ds_only=True,
        active_within=timedelta(days=30), max_pages=2, now=NOW,
    ))
    by_name = {r.fet_name: r for r in rows}
    # dave is stale (60d) and stateonly is unknown -> both filtered out.
    assert set(by_name) == {"alice", "eve", "notfound"}
    assert by_name["alice"].last_active == (NOW - timedelta(days=5)).isoformat()


def test_active_within_disabled_shows_all_ds():
    client, gc = _build()
    rows = list(crawl.discover(
        client, gc, CENTER, 50.0, seed="seed", ds_only=True,
        active_within=None, max_pages=2, now=NOW,
    ))
    # No activity filter -> dave (stale) and stateonly (unknown) are back.
    assert {r.fet_name for r in rows} == {"alice", "dave", "eve", "stateonly", "notfound"}


@pytest.mark.parametrize("text,days", [
    ("1 month", 30), ("2 weeks", 14), ("30d", 30), ("90 days", 90),
    ("6m", 180), ("1y", 365), ("48h", 2),
])
def test_parse_duration(text, days):
    assert crawl.parse_duration(text) == timedelta(days=days)


@pytest.mark.parametrize("text", ["any", "all", "none", "off", "0"])
def test_parse_duration_disabled(text):
    assert crawl.parse_duration(text) is None


def test_parse_duration_invalid():
    with pytest.raises(ValueError):
        crawl.parse_duration("soon")


def test_crawl_state_roundtrip_and_dedup(tmp_path):
    path = tmp_path / "state.json"
    st = crawl.CrawlState(path, params={"radius": 50})
    st.mark_visited("seed")
    cand = crawl.Candidate(key="a", nickname="A", id=None, url="/A", location_str="NearCity")
    st.enqueue(cand)
    st.enqueue(cand)  # duplicate key -> ignored
    st.enqueue(crawl.Candidate("seed", "seed", None, None, None))  # already visited -> ignored
    assert len(st.queue) == 1
    st.save()

    reloaded = crawl.CrawlState.load(path)
    assert reloaded.visited == {"seed"}
    assert [c.key for c in reloaded.queue] == ["a"]
    assert reloaded.params["radius"] == 50


def test_resume_continues_and_matches_single_run(tmp_path):
    # A single uninterrupted crawl (the ground truth).
    client, gc = _build()
    full = {r.fet_name for r in crawl.discover(
        client, gc, CENTER, 50.0, seed="seed", ds_only=False, active_within=None,
        now=NOW, max_pages=2)}

    # The same crawl in two chunks, persisted to a state file.
    path = tmp_path / "state.json"
    c1, g1 = _build()
    st = crawl.CrawlState(path, params={
        "center": list(CENTER), "radius": 50, "units": "mi", "seed": "seed"})
    part1 = {r.fet_name for r in crawl.discover(
        c1, g1, CENTER, 50.0, seed="seed", ds_only=False, active_within=None,
        now=NOW, max_pages=2, max_visits=2, state=st)}
    assert st.queue  # suspended: frontier remains

    c2, g2 = _build()
    resumed = crawl.CrawlState.load(path)      # picks up visited + frontier
    part2 = {r.fet_name for r in crawl.discover(
        c2, g2, CENTER, 50.0, seed="seed", ds_only=False, active_within=None,
        now=NOW, max_pages=2, state=resumed)}

    assert not resumed.queue                    # completed on resume
    assert (part1 | part2) == full              # union == the single-run result
    assert resumed.visited >= st.visited        # never lost/revisited progress
