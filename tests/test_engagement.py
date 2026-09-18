"""Tests for the engagement scan using an in-memory stub client (offline)."""

import csv
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from fetlife import engagement
from fetlife.exceptions import FetLifeError, RateLimitedError
from fetlife.models import Member, Story

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)
TARGET = Member(id="21621264", nickname="Xanadu_Kink", url="https://fetlife.com/Xanadu_Kink")


def _m(nick, id=None):
    return Member(id=id, nickname=nick, url=f"https://fetlife.com/{nick}")


def _post(uid, days_ago, loves, comments, owner="Xanadu_Kink", kind="pictures"):
    return Story(
        id=uid, type="picture_created", uid=uid, kind="Picture", content_id=uid,
        url=f"https://fetlife.com/{owner}/{kind}/{uid}",
        created_at=(NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z"),
        loves=loves, comments=comments,
    )


class StubClient:
    def __init__(self, relations, posts, lovers, commenters):
        self.relations = relations      # section -> [Member]
        self.posts = posts              # [Story], newest first
        self.lovers = lovers            # uid -> [Member]
        self.commenters = commenters    # uid -> [Member] (one per comment)
        self.calls = []

    def iter_members(self, ident, section):
        self.calls.append(("members", ident, section))
        yield from self.relations.get(section, [])

    def iter_posts(self, ident, since=None):
        self.calls.append(("posts", ident, since))
        for s in self.posts:
            if since and s.created() < since:
                return
            yield s

    def get_story_lovers(self, uid):
        self.calls.append(("lovers", uid))
        found = self.lovers.get(uid, [])
        if isinstance(found, Exception):
            raise found
        return found

    def iter_story_commenters(self, uid):
        self.calls.append(("commenters", uid))
        yield from self.commenters.get(uid, [])


def _build():
    relations = {
        "friends": [_m("alice", "1"), _m("bob", "2")],
        "followers": [_m("carol", "3"), _m("Bob", "2")],   # bob both friend + follower
        "following": [_m("dave", "4")],
    }
    posts = [
        _post("p1", 1, loves=3, comments=2),
        _post("p2", 5, loves=0, comments=0),               # nothing to fetch
        _post("shared", 6, loves=2, comments=0, owner="Someone_Else"),  # not theirs
        _post("p3", 40, loves=9, comments=9),              # older than the window
    ]
    lovers = {
        "p1": [_m("alice"), _m("Eve"), _m("Xanadu_Kink")],  # own love is ignored
        "shared": [_m("mallory")],
    }
    commenters = {
        "p1": [_m("eve", "5"), _m("frank", "6")],           # eve: love + comment
    }
    return StubClient(relations, posts, lovers, commenters)


def test_scan_partitions_engagers_by_connection():
    client = _build()
    report = engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)

    assert report.target == "Xanadu_Kink"
    assert (report.friends, report.followers, report.following) == (2, 2, 1)
    assert report.posts == 2       # p1 and p2; the shared post and p3 excluded
    assert report.skipped == 0

    by_name = {e.nickname.lower(): e for e in report.engagers}
    assert set(by_name) == {"alice", "eve", "frank"}

    assert by_name["alice"].connected and by_name["alice"].relation == "friend"
    assert by_name["alice"].loves == 1 and by_name["alice"].comments == 0

    eve = by_name["eve"]
    assert not eve.connected and eve.relation is None
    assert eve.loves == 1 and eve.comments == 1 and eve.total == 2
    assert eve.id == "5"                     # id learned from the comment
    assert eve.posts == ["https://fetlife.com/Xanadu_Kink/pictures/p1"]

    assert [e.nickname for e in report.strangers] == ["Eve", "frank"]  # most engaged first
    assert report.engagers[0].nickname == "Eve"


def test_scan_skips_fetches_the_feed_says_are_empty():
    client = _build()
    engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    fetched = [c for c in client.calls if c[0] in ("lovers", "commenters")]
    assert ("lovers", "p2") not in fetched and ("commenters", "p2") not in fetched
    assert ("lovers", "shared") not in fetched
    assert ("lovers", "p3") not in fetched


def test_scan_fetches_when_counts_are_unknown():
    client = _build()
    client.posts = [_post("p1", 1, loves=None, comments=None)]
    engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    assert ("lovers", "p1") in client.calls and ("commenters", "p1") in client.calls


def test_relation_lists_every_list_a_member_is_in():
    client = _build()
    client.lovers["p1"].append(_m("bob"))
    report = engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    bob = next(e for e in report.engagers if e.nickname == "bob")
    assert bob.relation == "friend, follower"


def test_unreadable_post_is_skipped_not_fatal():
    client = _build()
    client.lovers["p1"] = FetLifeError("gone")
    report = engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    assert report.skipped == 1
    assert report.engagers == []   # p1's comments were never reached either


def test_rate_limit_propagates():
    client = _build()
    client.lovers["p1"] = RateLimitedError("429")
    with pytest.raises(RateLimitedError):
        engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)


def test_progress_callback_reports_phases():
    client = _build()
    phases = []
    engagement.scan(client, TARGET, NOW - timedelta(days=30),
                    on_progress=lambda p: phases.append(p.phase), now=NOW)
    assert phases[0] == "friends" and phases[-1] == "done"
    assert "posts" in phases


def test_report_to_dict_is_json_serializable():
    client = _build()
    report = engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    data = json.loads(json.dumps(report.to_dict()))
    assert data["target"] == "Xanadu_Kink"
    assert [e["nickname"] for e in data["strangers"]] == ["Eve", "frank"]
    assert len(data["engagers"]) == 3


def test_parse_since_accepts_dates_and_durations():
    assert engagement.parse_since("2026-09-01", NOW) == datetime(2026, 9, 1, tzinfo=timezone.utc)
    assert engagement.parse_since("2026-09-01T06:30:00Z", NOW) == datetime(
        2026, 9, 1, 6, 30, tzinfo=timezone.utc
    )
    assert engagement.parse_since("2 weeks", NOW) == NOW - timedelta(days=14)
    assert engagement.parse_since("30d", NOW) == NOW - timedelta(days=30)
    with pytest.raises(ValueError):
        engagement.parse_since("yesterday-ish", NOW)
    with pytest.raises(ValueError):
        engagement.parse_since("any", NOW)  # "no cutoff" makes no sense here


def test_scan_state_round_trip(tmp_path):
    state = engagement.ScanState(tmp_path, "Xanadu_Kink")
    assert state.last_scan is None
    client = _build()
    report = engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    state.record(report)

    again = engagement.ScanState(tmp_path, "xanadu_kink")  # case-insensitive key
    assert again.last_scan == NOW
    assert again.path == tmp_path / "xanadu_kink.json"
    saved = json.loads(again.path.read_text())
    assert saved["posts"] == 2 and saved["strangers"] == 2


def test_scan_state_ignores_corrupt_file(tmp_path):
    (tmp_path / "x.json").write_text("{not json")
    assert engagement.ScanState(tmp_path, "X").last_scan is None
    assert engagement.ScanState(None, "X").last_scan is None


def test_write_csv():
    client = _build()
    report = engagement.scan(client, TARGET, NOW - timedelta(days=30), now=NOW)
    out = io.StringIO()
    engagement.write_csv(report.strangers, out)
    rows = list(csv.DictReader(io.StringIO(out.getvalue())))
    assert [r["nickname"] for r in rows] == ["Eve", "frank"]
    eve = rows[0]
    assert (eve["loves"], eve["comments"], eve["posts"]) == ("1", "1", "1")
    assert eve["connected"] == "no" and eve["relation"] == ""
    assert eve["url"] == "https://fetlife.com/Eve"
    assert eve["post_urls"] == "https://fetlife.com/Xanadu_Kink/pictures/p1"

    out = io.StringIO()
    engagement.write_csv(report.engagers, out)
    alice = next(r for r in csv.DictReader(io.StringIO(out.getvalue())) if r["nickname"] == "alice")
    assert alice["connected"] == "yes" and alice["relation"] == "friend"
