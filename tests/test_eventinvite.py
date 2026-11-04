"""Tests for event invitations: the invite modal's parsers, the plan, and the
command, using a stub client (offline)."""

import json
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from fetlife import cli, eventinvite, parsers
from fetlife.client import FetLifeClient
from fetlife.config import Config
from fetlife.exceptions import FetLifeError, RateLimitedError
from fetlife.friending import FAILED, SENT, SKIPPED, WOULD_SEND
from fetlife.models import (
    Event, EventInviteForm, EventInviteRow, Group, GroupMembersPage, Member,
)

EVENT_URL = "https://fetlife.com/events/2026/11/21/a-party-abc123"


def _row_html(uid, nick, selectable, namespace="friends_and_followers", past=False, status=None):
    below = f'<span class="italic text-green-500">{status}</span>' if status else "Vineland, New Jersey"
    return f"""<label class="relative mb-3.5">
      <div class="truncate whitespace-nowrap font-bold leading-normal text-gray-300">
        <span class="text-base text-red-500">{nick}</span>
        <span class="relative top-px"><a title="FetLife Supporter"></a></span>
        <span class="text-sm text-gray-300">46M Dom</span>
      </div>
      <div class="truncate text-sm text-gray-400">{below}</div>
      <input type="checkbox" data-namespace="{namespace}" data-user-id="{uid}"
             data-selectable="{'true' if selectable else 'false'}"
             {'data-past-attendee="true"' if past else ''} {'' if selectable else 'disabled="disabled"'}/>
    </label>"""


MODAL = """
<div data-controller="invite-modal" data-invite-modal-limit-value="2">
  <form id="event-invite-modal-form" action="/events/77/invitations" method="post">
    <input type="hidden" name="authenticity_token" value="tok"/>
    <div data-invite-modal-target="store"></div>
    <div data-invite-modal-target="panel" data-tab-name="previously_rsvpd"></div>
  </form>
</div>"""


def test_invite_form_reads_token_limit_and_tabs():
    form = parsers.event_invite_form_from_html(MODAL)
    assert form == EventInviteForm("/events/77/invitations", "tok", 2, True)
    assert parsers.event_invite_form_from_html("<html><body>Nope</body></html>") is None


def test_invite_form_with_an_unreadable_limit_is_an_error():
    with pytest.raises(FetLifeError):
        parsers.event_invite_form_from_html(MODAL.replace('data-invite-modal-limit-value="2"',
                                                          'data-invite-modal-limit-value="lots"'))


def test_invite_rows_read_each_state_and_the_next_page():
    html = "".join([
        _row_html(1, "Open", True),
        _row_html(2, "OpenPast", True, past=True),
        _row_html(3, "Pending", False, status="Invite pending"),
        _row_html(4, "PastTab", True, namespace="previously_rsvpd"),
    ]) + '<turbo-frame id="invite-friends-page-2" src="/events/77/invitations/suggestions/friends?page=2" loading="lazy"></turbo-frame>'
    rows, following = parsers.event_invite_rows_from_html(html)
    assert rows == [
        EventInviteRow("1", "Open", True, False, None),
        EventInviteRow("2", "OpenPast", True, True, None),
        EventInviteRow("3", "Pending", False, False, "Invite pending"),
        EventInviteRow("4", "PastTab", True, True, None),
    ]
    assert following == ("invite-friends-page-2", "/events/77/invitations/suggestions/friends?page=2")
    # A frame already loaded (no src) isn't a next page.
    assert parsers.event_invite_rows_from_html('<turbo-frame id="invite-friends-page-2"></turbo-frame>') == ([], None)


def test_parse_event_takes_the_numeric_id_from_the_page_not_the_dated_url():
    html = '<h1>A Party</h1><turbo-frame id="event_rsvp_bar_77"></turbo-frame>'
    assert parsers.parse_event(html, url=EVENT_URL).id == "77"


def test_split_members_matches_nicknames_ignoring_case_and_drops_the_account():
    group = [Member(nickname="alice"), Member(nickname="Stranger"), Member(nickname="ME")]
    non, invitees = eventinvite.split_members({"alice": ["friends", "following"]}, group, "me")
    assert [m.nickname for m in non] == ["Stranger"]
    assert [(i.nickname, i.relation) for i in invitees] == [("alice", "friends, following")]


def test_plan_honors_the_limit_but_not_for_past_rsvps():
    invitees = [eventinvite.Invitee(n, "friends") for n in
                ("F1", "F2", "F3", "Past", "Pending", "FollowingOnly")]
    rows = {
        "f1": EventInviteRow("1", "F1", True),
        "f2": EventInviteRow("2", "F2", True),
        "f3": EventInviteRow("3", "F3", True),
        "past": EventInviteRow("4", "Past", True, past_attendee=True),
        "pending": EventInviteRow("5", "Pending", False, status="Invite pending"),
    }
    eventinvite.plan(invitees, rows, limit=2)
    assert [i.action for i in invitees] == [WOULD_SEND, WOULD_SEND, SKIPPED, WOULD_SEND, SKIPPED, SKIPPED]
    assert "limit (2 left)" in invitees[2].reason
    assert invitees[3].past_rsvp
    assert invitees[4].reason == "Invite pending"
    assert "doesn't list" in invitees[5].reason


class StubClient:
    """An account with two friends in the group, and modal rows to match."""

    class config:
        base_url = "https://fetlife.com"

    on_retry = None
    requests = 0

    def __init__(self, rows=None, form=True, send_error=None, rows_after=None, rows_after_error=None):
        self.form = EventInviteForm("/events/77/invitations", "tok", 1, False) if form else None
        self.rows = rows if rows is not None else [
            EventInviteRow("1", "Alice", True), EventInviteRow("2", "Bob", True),
            EventInviteRow("3", "Carol", True, past_attendee=True),
        ]
        self.send_error, self.rows_after, self.rows_after_error = send_error, rows_after, rows_after_error
        self.sent = []

    def __enter__(self): return self
    def __exit__(self, *exc): return False
    def whoami(self): return Member(nickname="Host", id="9")

    def get_event_by_url(self, url):
        return Event(id="77", name="A Party", url=url, start="2026-11-21")

    def get_event_invite_form(self, event_id): return self.form

    def iter_members(self, nickname, section):
        names = {"friends": ["Alice", "Bob"], "followers": ["Alice"], "following": ["Carol", "Dan"]}
        return (Member(nickname=n) for n in names[section])

    def iter_group_member_pages(self, group_id, start_page=1):
        members = [Member(nickname=n, location="NJ") for n in ("Alice", "Bob", "Carol", "Dan", "Eve", "Host")]
        yield 1, GroupMembersPage(group=Group(id=group_id, name="The Group"), members=members)

    def iter_event_invite_rows(self, event_id, tab):
        if tab != "friends":
            return iter(())
        if self.sent and self.rows_after_error:
            raise self.rows_after_error
        return iter(self.rows_after if self.sent and self.rows_after is not None else self.rows)

    def send_event_invites(self, form, past, friends):
        if self.send_error:
            raise self.send_error
        self.sent.append((past, friends))


def _run(monkeypatch, stub, *args, input=None):
    monkeypatch.setattr(cli, "_client", lambda ctx: stub)
    return CliRunner().invoke(cli.cli, ["--env-file", "/nonexistent.env", "invite-to-event",
                                        EVENT_URL, "https://fetlife.com/groups/317599", *args],
                              input=input)


AFTER = [EventInviteRow("1", "Alice", False, status="Invite pending"),
         EventInviteRow("2", "Bob", True),
         EventInviteRow("3", "Carol", False, status="Invite pending")]


@pytest.mark.parametrize("flag", ["--DryRun", "--dry-run"])
def test_dry_run_reports_everything_and_sends_nothing(monkeypatch, flag):
    stub = StubClient()
    result = _run(monkeypatch, stub, flag)
    assert result.exit_code == 0, result.output
    assert stub.sent == []
    for expected in ("A Party", EVENT_URL.split("/")[-1], "The Group", "NonMembers (1)", "Eve",
                     "Invitees (4)", "would send", "over", "DRY RUN"):
        assert expected in result.output, expected


def test_dry_run_beats_yes(monkeypatch):
    stub = StubClient()
    assert _run(monkeypatch, stub, "--DryRun", "--yes").exit_code == 0
    assert stub.sent == []


def test_declining_the_prompt_sends_nothing(monkeypatch):
    stub = StubClient()
    result = _run(monkeypatch, stub, input="n\n")
    assert result.exit_code == 1
    assert stub.sent == []


def test_yes_sends_once_past_rsvps_apart_and_verifies(monkeypatch):
    stub = StubClient(rows_after=AFTER)
    result = _run(monkeypatch, stub, "--yes", "--json")
    assert result.exit_code == 0, result.output
    # Alice takes the one invite left; Bob is over it; Carol is a free past RSVP; Dan isn't listed.
    assert stub.sent == [(["3"], ["1"])]
    # Rich may pretty-print across lines; the JSON is the whole of stdout minus status.
    payload = json.loads(result.stdout[result.stdout.index("{"):])
    actions = {i["nickname"]: i["action"] for i in payload["invitees"]}
    assert actions == {"Alice": SENT, "Bob": SKIPPED, "Carol": SENT, "Dan": SKIPPED}
    assert [n["nickname"] for n in payload["non_members"]] == ["Eve"]
    assert payload["event"]["name"] == "A Party"


def test_a_row_still_selectable_after_sending_is_reported_failed(monkeypatch):
    stub = StubClient(rows_after=[EventInviteRow("1", "Alice", True),
                                  EventInviteRow("3", "Carol", False, status="Invite pending")])
    report = eventinvite.prepare(stub, EVENT_URL, "317599")
    assert eventinvite.send(stub, report) is None
    actions = {i.nickname: i.action for i in report.invitees}
    assert actions["Alice"] == FAILED and actions["Carol"] == SENT


def test_a_failed_check_after_a_submit_still_counts_as_sent(monkeypatch):
    stub = StubClient(rows_after_error=RateLimitedError("slow down"))
    report = eventinvite.prepare(stub, EVENT_URL, "317599")
    assert eventinvite.send(stub, report) is None
    alice = next(i for i in report.invitees if i.nickname == "Alice")
    assert alice.action == SENT and "not confirmed" in alice.reason


def test_a_refused_submit_marks_invitees_failed_and_exits_nonzero(monkeypatch):
    stub = StubClient(send_error=FetLifeError("refused"))
    result = _run(monkeypatch, stub, "--yes")
    assert result.exit_code == 1
    assert "failed" in result.output and "refused" in result.output


def test_an_account_without_the_invite_form_stops_before_reading_lists(monkeypatch):
    stub = StubClient(form=False)
    stub.iter_members = lambda *a: pytest.fail("shouldn't read the lists")
    with pytest.raises(FetLifeError, match="Host can't send invitations"):
        eventinvite.prepare(stub, EVENT_URL, "317599")


def test_nothing_to_send_means_no_prompt_and_no_submit(monkeypatch):
    stub = StubClient(rows=[EventInviteRow("1", "Alice", False, status="Going")])
    result = _run(monkeypatch, stub)  # no --yes: a prompt here would abort on empty input
    assert result.exit_code == 0, result.output
    assert stub.sent == []


class _Resp(SimpleNamespace):
    pass


@pytest.fixture
def client(tmp_path, monkeypatch):
    config = Config(session_path=tmp_path / "cookies", throttle_state_path=tmp_path / "throttle")
    fl = FetLifeClient(config, session=SimpleNamespace(cookies={}, headers={}))
    monkeypatch.setattr(fl, "_ensure_auth", lambda: None)
    return fl


def test_client_posts_the_site_namespaces_without_retrying_server_errors(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "request", lambda method, path, **kw: (
        calls.append((method, path, kw)) or _Resp(status_code=200, text="", url="")))
    client.send_event_invites(EventInviteForm("/events/77/invitations", "tok", 1, True), ["3"], ["1"])
    method, path, kw = calls[0]
    assert (method, path) == ("POST", "/events/77/invitations")
    assert kw["retry_server_errors"] is False
    assert kw["data"] == [("authenticity_token", "tok"),
                          ("previously_rsvpd[all_selected]", "false"),
                          ("previously_rsvpd[checked_users][]", "3"),
                          ("friends_and_followers[checked_users][]", "1")]


def test_client_leaves_out_past_rsvp_params_when_the_modal_has_no_such_tab(client, monkeypatch):
    calls = []
    monkeypatch.setattr(client, "request", lambda method, path, **kw: (
        calls.append(kw) or _Resp(status_code=200, text="", url="")))
    client.send_event_invites(EventInviteForm("/events/77/invitations", "tok", 1, False), [], ["1"])
    assert calls[0]["data"] == [("authenticity_token", "tok"),
                                ("friends_and_followers[checked_users][]", "1")]


def test_client_refuses_to_exceed_the_limit_and_reports_a_refusal(client, monkeypatch):
    form = EventInviteForm("/events/77/invitations", "tok", 1, False)
    monkeypatch.setattr(client, "request", lambda *a, **k: pytest.fail("shouldn't post"))
    with pytest.raises(FetLifeError, match="over FetLife's limit"):
        client.send_event_invites(form, [], ["1", "2"])
    monkeypatch.setattr(client, "request", lambda *a, **k: _Resp(status_code=422, text="", url=""))
    with pytest.raises(FetLifeError, match="HTTP 422"):
        client.send_event_invites(form, [], ["1"])


def test_client_treats_a_401_invite_page_as_no_form(client, monkeypatch):
    monkeypatch.setattr(client, "get", lambda *a, **k: _Resp(status_code=401, text="", url=""))
    assert client.get_event_invite_form("77") is None


def test_client_walks_invite_pages_and_stops_on_a_repeat(client, monkeypatch):
    page2 = "/events/77/invitations/suggestions/friends?page=2"
    frame = f'<turbo-frame id="invite-friends-page-2" src="{page2}"></turbo-frame>'
    pages = {"/events/77/invitations/suggestions/friends?page=1": _row_html(1, "A", True) + frame,
             page2: _row_html(2, "B", True) + frame}  # page 2 points at itself
    fetched = []

    def fake_get(path, headers=None):
        fetched.append((path, headers["Turbo-Frame"]))
        return _Resp(status_code=200, text=pages[path], url=path)

    monkeypatch.setattr(client, "get", fake_get)
    assert [r.nickname for r in client.iter_event_invite_rows("77", "friends")] == ["A", "B"]
    assert fetched == [("/events/77/invitations/suggestions/friends?page=1", "invite-friends-list"),
                       (page2, "invite-friends-page-2")]


@pytest.mark.parametrize("given,path", [
    (EVENT_URL, "/events/2026/11/21/a-party-abc123"),
    (EVENT_URL + "/discussions", "/events/2026/11/21/a-party-abc123"),
    ("77", "/events/77"),
])
def test_client_resolves_an_event_from_its_url_or_id(client, monkeypatch, given, path):
    fetched = []
    monkeypatch.setattr(client, "get", lambda p: (
        fetched.append(p) or _Resp(status_code=200, text="<h1>A Party</h1>", url="https://fetlife.com" + p)))
    assert client.get_event_by_url(given).name == "A Party"
    assert fetched == [path]


def test_client_rejects_what_isnt_an_event_url(client):
    with pytest.raises(FetLifeError, match="isn't a FetLife event URL"):
        client.get_event_by_url("https://fetlife.com/groups/317599")
