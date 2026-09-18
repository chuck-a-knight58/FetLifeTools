"""Tests for the friend-request runner using an in-memory stub client (offline)."""

import io
import json
from datetime import datetime, timezone

import pytest

from fetlife import friending
from fetlife.exceptions import FetLifeError, NotFoundError, RateLimitedError
from fetlife.models import ProfileRelation

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


def _open(uid):
    return ProfileRelation(user_id=uid, can_friend_request=True,
                           request_path=f"/requests?user_id={uid}", labels=["Follow", "Add as Friend"])


def _closed(uid):
    return ProfileRelation(user_id=uid, can_friend_request=False, labels=["Friends"])


class StubClient:
    class config:
        base_url = "https://fetlife.com"

    def __init__(self, relations, after=None):
        self.relations = relations   # nickname -> ProfileRelation | Exception
        self.after = after or {}     # uid -> relation returned after posting
        self.posted = []

    def get_profile_relation(self, nickname):
        rel = self.relations[nickname]
        if isinstance(rel, Exception):
            raise rel
        return rel, "tok"

    def send_friend_request(self, relation, token):
        assert token == "tok"
        self.posted.append(relation.user_id)
        result = self.after.get(relation.user_id)
        if isinstance(result, Exception):
            raise result
        return result


def _run(client, names, log=None, **kw):
    sleeps = []
    log = log or friending.RequestLog(None)
    outcomes = list(friending.run(client, names, log, sleep=sleeps.append, now=lambda: NOW, **kw))
    return outcomes, sleeps


def test_read_nicknames_uses_the_nickname_column_or_first_column():
    csv_with_header = "loves,nickname,url\n3,Alice,https://x/Alice\n1,bob,\n0,alice,\n\n"
    assert friending.read_nicknames(io.StringIO(csv_with_header)) == ["Alice", "bob"]
    assert friending.read_nicknames(io.StringIO("carol\ndave\n")) == ["carol", "dave"]
    assert friending.read_nicknames(io.StringIO("")) == []


def test_run_sends_only_where_offered_and_logs():
    client = StubClient({"alice": _open("1"), "pal": _closed("2"), "ghost": NotFoundError("404"),
                         "broken": FetLifeError("boom")})
    log = friending.RequestLog(None)
    outcomes, sleeps = _run(client, ["alice", "pal", "ghost", "broken"], log)
    assert [(o.nickname, o.action) for o in outcomes] == [
        ("alice", "sent"), ("pal", "skipped"), ("ghost", "skipped"), ("broken", "failed"),
    ]
    assert outcomes[1].reason == "no 'Add as Friend' (Friends)"
    assert outcomes[2].reason == "profile not found"
    assert outcomes[0].at == NOW.isoformat() and outcomes[0].url == "https://fetlife.com/alice"
    assert client.posted == ["1"]
    assert log.sent == {"alice": NOW.isoformat()}


def test_run_respects_limit_and_pauses_between_sends():
    client = StubClient({n: _open(str(i)) for i, n in enumerate(["a", "b", "c", "d"])})
    outcomes, sleeps = _run(client, ["a", "b", "c", "d"], limit=3, pause=30)
    assert [o.action for o in outcomes] == ["sent", "sent", "sent", "skipped"]
    assert outcomes[3].reason == "over --limit 3"
    assert client.posted == ["0", "1", "2"]
    assert sleeps == [30, 30]          # before the 2nd and 3rd send only


def test_dry_run_sends_nothing_but_counts_against_limit():
    client = StubClient({"a": _open("1"), "b": _closed("2"), "c": _open("3")})
    outcomes, sleeps = _run(client, ["a", "b", "c"], limit=1, dry_run=True)
    assert [o.action for o in outcomes] == ["would send", "skipped", "skipped"]
    assert client.posted == [] and sleeps == []


def test_already_requested_members_are_skipped_unless_resend(tmp_path):
    path = tmp_path / "log.jsonl"
    path.write_text(json.dumps({"nickname": "Alice", "action": "sent", "at": "2026-09-01T00:00:00+00:00"}) + "\n"
                    + json.dumps({"nickname": "bob", "action": "failed"}) + "\n"
                    + "not json\n")
    client = StubClient({"alice": _open("1"), "bob": _open("2")})
    log = friending.RequestLog(path)
    assert log.sent == {"alice": "2026-09-01T00:00:00+00:00"}

    outcomes, _ = _run(client, ["alice", "bob"], log)
    assert [(o.action, o.reason) for o in outcomes] == [
        ("skipped", "already requested 2026-09-01"), ("sent", ""),
    ]
    assert client.posted == ["2"]
    lines = [json.loads(l) for l in path.read_text().splitlines() if l.startswith("{")]
    assert lines[-1]["nickname"] == "bob" and lines[-1]["action"] == "sent"

    client.posted.clear()
    outcomes, _ = _run(client, ["alice"], friending.RequestLog(path), resend=True)
    assert outcomes[0].action == "sent" and client.posted == ["1"]


def test_failed_post_and_unchanged_button_are_reported():
    client = StubClient({"a": _open("1"), "b": _open("2"), "c": _open("3")},
                        after={"1": FetLifeError("HTTP 422"), "2": _open("2"), "3": _closed("3")})
    log = friending.RequestLog(None)
    outcomes, _ = _run(client, ["a", "b", "c"], log)
    assert [o.action for o in outcomes] == ["failed", "failed", "sent"]
    assert "HTTP 422" in outcomes[0].reason
    assert "still offers" in outcomes[1].reason
    assert log.sent == {"c": NOW.isoformat()}


def test_rate_limit_propagates():
    client = StubClient({"a": RateLimitedError("429")})
    with pytest.raises(RateLimitedError):
        _run(client, ["a"])
