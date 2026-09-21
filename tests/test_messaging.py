"""Tests for the bulk-message runner using an in-memory stub client (offline)."""

import json
from datetime import datetime, timezone

import pytest

from fetlife import friending, messaging
from fetlife.exceptions import FetLifeError, NotFoundError, RateLimitedError
from fetlife.models import ProfileRelation

NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)
FORM = {"authenticity_token": "tok", "with[]": ["1"]}


class StubClient:
    class config:
        base_url = "https://fetlife.com"

    def __init__(self, ids, forms, results=None):
        self.ids = ids            # nickname -> user id | Exception
        self.forms = forms        # user id -> form dict | None
        self.results = results or {}  # user id -> Exception (else success)
        self.sent = []

    def get_profile_relation(self, nickname):
        found = self.ids[nickname]
        if isinstance(found, Exception):
            raise found
        return ProfileRelation(user_id=found), "tok"

    def get_message_form(self, user_id):
        return self.forms.get(user_id)

    def send_message(self, user_id, subject, body, form=None):
        assert form is self.forms[user_id]
        if user_id in self.results:
            raise self.results[user_id]
        self.sent.append((user_id, subject, body))
        return f"Your message has been successfully sent to {user_id}"


def _run(client, names, log=None, **kw):
    sleeps = []
    log = log or friending.RequestLog(None)
    outcomes = list(messaging.run(client, names, log, "Hi {nickname}", "Hello {nickname}!",
                                  sleep=sleeps.append, now=lambda: NOW, **kw))
    return outcomes, sleeps


def test_render_fills_nickname_only():
    assert messaging.render("Hi {nickname}, {other}", "Eve") == "Hi Eve, {other}"


def test_run_sends_rendered_messages_where_allowed_and_logs():
    client = StubClient(
        ids={"eve": "1", "closed": "2", "ghost": NotFoundError("404"), "42": "42", "bad": FetLifeError("x")},
        forms={"1": FORM, "2": None, "42": {"authenticity_token": "t", "with[]": ["42"]}},
    )
    log = friending.RequestLog(None)
    outcomes, sleeps = _run(client, ["eve", "closed", "ghost", "42", "bad"], log)
    assert [(o.nickname, o.action) for o in outcomes] == [
        ("eve", "sent"), ("closed", "skipped"), ("ghost", "skipped"), ("42", "sent"), ("bad", "failed"),
    ]
    assert client.sent == [("1", "Hi eve", "Hello eve!"), ("42", "Hi 42", "Hello 42!")]
    assert outcomes[1].reason == "doesn't accept messages from this account"
    assert outcomes[0].reason.startswith("Your message has been successfully sent")
    assert sleeps == [30.0]                       # only between the two sends
    assert set(log.sent) == {"eve", "42"}


def test_limit_and_dry_run():
    client = StubClient(ids={"a": "1", "b": "2", "c": "3"}, forms={"1": FORM, "2": FORM, "3": FORM})
    outcomes, sleeps = _run(client, ["a", "b", "c"], limit=2, dry_run=True)
    assert [o.action for o in outcomes] == ["would send", "would send", "skipped"]
    assert outcomes[2].reason == "over --limit 2"
    assert client.sent == [] and sleeps == []


def test_already_messaged_are_skipped_unless_resend(tmp_path):
    path = tmp_path / "messages.jsonl"
    path.write_text(json.dumps({"nickname": "Eve", "action": "sent", "at": "2026-09-20T00:00:00+00:00"}) + "\n")
    client = StubClient(ids={"eve": "1"}, forms={"1": FORM})
    outcomes, _ = _run(client, ["eve"], friending.RequestLog(path))
    assert outcomes[0].action == "skipped" and outcomes[0].reason == "already messaged 2026-09-20"
    assert client.sent == []
    outcomes, _ = _run(client, ["eve"], friending.RequestLog(path), resend=True)
    assert outcomes[0].action == "sent" and len(client.sent) == 1
    assert [json.loads(l)["action"] for l in path.read_text().splitlines()] == ["sent", "sent"]


def test_failed_send_is_logged_but_not_counted():
    client = StubClient(ids={"a": "1", "b": "2"}, forms={"1": FORM, "2": FORM},
                        results={"1": FetLifeError("did not confirm")})
    log = friending.RequestLog(None)
    outcomes, sleeps = _run(client, ["a", "b"], log)
    assert [o.action for o in outcomes] == ["failed", "sent"]
    assert "did not confirm" in outcomes[0].reason
    assert sleeps == []                           # first send failed, so no pause before b
    assert set(log.sent) == {"b"}


def test_rate_limit_propagates():
    client = StubClient(ids={"a": RateLimitedError("429")}, forms={})
    with pytest.raises(RateLimitedError):
        _run(client, ["a"])
