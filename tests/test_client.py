"""Client tests that mock HTTP with the ``responses`` library (offline)."""

import json
import time

import pytest
import requests
import responses
from responses.matchers import query_param_matcher

from fetlife import client
from fetlife.client import FetLifeClient
from fetlife.config import Config
from fetlife.exceptions import RateLimitedError


def _client(tmp_path, **overrides):
    defaults = dict(
        username="tester",
        password="secret",
        base_url="https://fetlife.com",
        rate_limit_min=0.0,
        rate_limit_max=0.0,
        session_path=tmp_path / "session.cookies",
        # Keep the throttle memory in the tmp dir — the default is under ~/,
        # and tests must never read or write the real user's state.
        throttle_state_path=tmp_path / "throttle.json",
        retry_backoff=0.0,  # no real sleeping in tests
    )
    cfg = Config(**{**defaults, **overrides})
    # Inject a plain requests.Session so the `responses` mock can intercept;
    # production uses curl_cffi's browser-impersonating session instead.
    return FetLifeClient(cfg, session=requests.Session())


@responses.activate
def test_login_flow(tmp_path):
    responses.add(
        responses.GET,
        "https://fetlife.com/login",
        body='<meta name="csrf-token" content="tok">',
        status=200,
    )
    responses.add(
        responses.POST,
        "https://fetlife.com/login",
        body="",
        status=200,
    )
    responses.add(
        responses.GET,
        "https://fetlife.com/home",
        body='<a href="/logout">out</a>',
        status=200,
    )
    fl = _client(tmp_path)
    assert fl.login(force=True) is True
    assert fl.authenticated is True


@responses.activate
def test_rate_limit_raises_after_retries(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/home", status=429)
    fl = _client(tmp_path, max_retries=2)  # retry_backoff=0 -> no real sleep
    fl._authenticated = True  # skip login for this unit

    with pytest.raises(RateLimitedError):
        fl.get("/home")
    # 1 initial attempt + 2 retries = 3 requests
    assert len(responses.calls) == 3


@responses.activate
def test_retries_then_succeeds(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/home", status=429)
    responses.add(responses.GET, "https://fetlife.com/home", status=503)
    responses.add(responses.GET, "https://fetlife.com/home",
                  body="ok", status=200)
    fl = _client(tmp_path, max_retries=3)
    fl._authenticated = True

    resp = fl.get("/home")
    assert resp.status_code == 200
    assert len(responses.calls) == 3  # 429, 503, then 200


@responses.activate
def test_throttle_factor_persists_after_a_retried_429(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/home", status=429)
    responses.add(responses.GET, "https://fetlife.com/home", body="ok", status=200)
    fl = _client(tmp_path, max_retries=3)
    fl._authenticated = True
    slowdowns = []
    fl.on_slowdown = lambda factor, lo, hi: slowdowns.append(factor)

    assert fl.get("/home").status_code == 200
    # The retry succeeded, but the cadence must stay slower: FetLife's limit is
    # a rolling window, so resuming at full speed just re-trips it.
    assert fl._throttle_factor == client.THROTTLE_FACTOR_STEP
    assert slowdowns == [client.THROTTLE_FACTOR_STEP]


@responses.activate
def test_throttle_factor_decays_only_after_a_clean_streak(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/home", status=429)
    responses.add(responses.GET, "https://fetlife.com/home", body="ok", status=200)
    fl = _client(tmp_path, max_retries=3)
    fl._authenticated = True

    fl.get("/home")  # trips the 429, then succeeds
    assert fl._throttle_factor == 2.0
    for _ in range(client.THROTTLE_DECAY_AFTER - 2):
        fl.get("/home")
    assert fl._throttle_factor == 2.0  # still slow: streak not long enough
    fl.get("/home")
    assert fl._throttle_factor == 2.0 - client.THROTTLE_DECAY_STEP


def test_throttle_factor_is_capped(tmp_path):
    fl = _client(tmp_path)
    for _ in range(20):
        fl._register_throttled()
    assert fl._throttle_factor == client.THROTTLE_FACTOR_MAX


def test_delay_range_scales_with_throttle_factor(tmp_path):
    fl = _client(tmp_path, rate_limit_min=2.0, rate_limit_max=5.0)
    assert fl.delay_range == (2.0, 5.0)
    fl._register_throttled()
    assert fl.delay_range == (4.0, 10.0)


def test_throttle_state_survives_a_new_client(tmp_path):
    fl = _client(tmp_path)
    fl._register_throttled()
    fl._register_throttled()
    assert fl.throttle_factor == 4.0

    # A new process re-reads it and starts cautious — at half the learned
    # factor, so it speeds up if the window cleared and re-escalates if not.
    revived = _client(tmp_path)
    assert revived.throttle_factor == 2.0
    assert revived.seconds_since_throttled is not None
    assert revived.seconds_since_throttled < 60


def test_stale_throttle_state_is_discarded(tmp_path):
    fl = _client(tmp_path)
    fl._register_throttled()
    stale = time.time() - (client.THROTTLE_STATE_TTL_HOURS + 1) * 3600
    (tmp_path / "throttle.json").write_text(
        json.dumps({"factor": 8.0, "last_429": stale})
    )

    revived = _client(tmp_path)
    assert revived.throttle_factor == 1.0  # window long since cleared
    assert revived.seconds_since_throttled is None


def test_missing_or_corrupt_throttle_state_is_not_fatal(tmp_path):
    assert _client(tmp_path).throttle_factor == 1.0  # no file yet
    (tmp_path / "throttle.json").write_text("{not json")
    assert _client(tmp_path).throttle_factor == 1.0


def test_cooldown_remaining(tmp_path):
    fl = _client(tmp_path)
    assert fl.cooldown_remaining(3.0) == 0.0  # never throttled -> clear

    fl._register_throttled()
    remaining = fl.cooldown_remaining(3.0)
    assert 3 * 3600 - 60 < remaining <= 3 * 3600
    assert fl.cooldown_remaining(0) == 0.0  # 0 disables the check


def test_throttle_draws_from_min_max_range(tmp_path, monkeypatch):
    # Via the helper, so throttle state stays in tmp_path: reading the real
    # ~/.fetlife/throttle.json would scale the range and break this assertion.
    fl = _client(tmp_path, rate_limit_min=1.5, rate_limit_max=4.0)
    drawn = []
    monkeypatch.setattr("fetlife.client.random.uniform",
                        lambda a, b: drawn.append((a, b)) or 0.0)
    monkeypatch.setattr("fetlife.client.time.sleep", lambda s: None)

    fl._throttle()
    assert drawn == [(1.5, 4.0)]


@responses.activate
def test_on_retry_hook_called(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/home", status=429)
    responses.add(responses.GET, "https://fetlife.com/home", body="ok", status=200)
    fl = _client(tmp_path, max_retries=3)
    fl._authenticated = True
    seen = []
    fl.on_retry = lambda status, wait, attempt, mx: seen.append((status, attempt))

    fl.get("/home")
    assert seen == [(429, 1)]


# --------------------------------------------------------------------------- #
# Paged feeds (engagement): every loop below must terminate on FetLife's own
# end-of-list signals, not on a guessed page count.
# --------------------------------------------------------------------------- #
def _authed(tmp_path):
    fl = _client(tmp_path)
    fl._authenticated = True  # skip the login handshake
    return fl


def _relation(uid, nick):
    return (f'<div id="relation_user_{uid}"><div><a class="font-bold" href="/{nick}">{nick}</a>'
            f'<span class="font-bold">30M</span></div></div>')


@responses.activate
def test_iter_members_walks_pages_until_empty(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/X/friends",
                  body=_relation(1, "a") + _relation(2, "b"), match=[query_param_matcher({'page': '1'})])
    responses.add(responses.GET, "https://fetlife.com/X/friends",
                  body=_relation(3, "c"), match=[query_param_matcher({'page': '2'})])
    responses.add(responses.GET, "https://fetlife.com/X/friends",
                  body="<div></div>", match=[query_param_matcher({'page': '3'})])
    fl = _authed(tmp_path)
    assert [m.nickname for m in fl.iter_members("X", "friends")] == ["a", "b", "c"]
    assert len(responses.calls) == 3


@responses.activate
def test_iter_members_stops_when_a_page_only_repeats(tmp_path):
    # A list that ignores ?page= would serve page 1 forever.
    responses.add(responses.GET, "https://fetlife.com/X/followers",
                  body=_relation(1, "a"))
    fl = _authed(tmp_path)
    assert [m.nickname for m in fl.iter_members("X", "followers")] == ["a"]
    assert len(responses.calls) == 2


def _story(uid, when, loves=1):
    return (f'<article id="story_{uid}" data-story-type="picture_created"><header>'
            f'<a href="/X/pictures/{uid}"><time datetime="{when}">t</time></a></header>'
            f'<span data-controller="story-love-button" '
            f'data-story-love-button-content-id-value="u{uid}" '
            f'data-story-love-button-loves-count-value="{loves}"></span></article>')


def _loader(marker):
    return (f'<turbo-frame id="activity-stories-pagination-loader" '
            f'src="/X/activity/all-posts.turbo_stream?marker={marker}"></turbo-frame>')


@responses.activate
def test_iter_posts_follows_markers_and_stops_at_since(tmp_path):
    from datetime import datetime, timezone

    responses.add(responses.GET, "https://fetlife.com/X/activity/all-posts",
                  body=_story(1, "2026-09-18T00:00:00Z") + _loader("m2"))
    responses.add(responses.GET, "https://fetlife.com/X/activity/all-posts.turbo_stream",
                  body=_story(2, "2026-09-10T00:00:00Z") + _story(3, "2026-08-01T00:00:00Z")
                  + _loader("m3"), match=[query_param_matcher({'marker': 'm2', 'story_size': 'large'})])
    fl = _authed(tmp_path)
    since = datetime(2026, 9, 1, tzinfo=timezone.utc)
    got = [s.uid for s in fl.iter_posts("X", since=since)]
    assert got == ["u1", "u2"]           # u3 is older than `since`; m3 never fetched
    assert len(responses.calls) == 2


@responses.activate
def test_iter_posts_ends_without_a_marker(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/X/activity/all-posts",
                  body=_story(1, "2026-09-18T00:00:00Z"))
    fl = _authed(tmp_path)
    assert [s.uid for s in fl.iter_posts("X")] == ["u1"]


def _comment(nick, uid):
    return (f'<div data-comment-item-author-nickname-value="{nick}" '
            f'data-comment-item-author-id-value="{uid}"></div>')


@responses.activate
def test_iter_story_commenters_stops_on_the_empty_trailing_page(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/comments.turbo_stream",
                  body=_comment("a", 1) + _comment("b", 2)
                  + '<turbo-frame id="comments_page_c2" src="/comments.turbo_stream?cursor=c2&story_uid=abc">',
                  match=[query_param_matcher({"order": "oldest", "story_uid": "abc"})])
    responses.add(responses.GET, "https://fetlife.com/comments.turbo_stream",
                  body='<turbo-frame id="comments_page_c3" src="/comments.turbo_stream?cursor=c3&story_uid=abc">',
                  match=[query_param_matcher({"order": "oldest", "story_uid": "abc", "cursor": "c2"})])
    fl = _authed(tmp_path)
    assert [(m.nickname, m.id) for m in fl.iter_story_commenters("abc")] == [("a", "1"), ("b", "2")]
    assert len(responses.calls) == 2


@responses.activate
def test_get_story_lovers(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/loves/story/abc",
                  body='<div data-lover-nickname="zed"><a href="/zed"></a></div>',
                  match=[query_param_matcher({"content_type": "Story"})])
    fl = _authed(tmp_path)
    assert [m.nickname for m in fl.get_story_lovers("abc")] == ["zed"]


@responses.activate
def test_get_last_active_reads_the_html_feed(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/X/activity",
                  body=_story(1, "2026-09-18T00:00:00Z") + _story(2, "2026-09-01T00:00:00Z"))
    fl = _authed(tmp_path)
    assert fl.get_last_active("X").isoformat() == "2026-09-18T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# Direct messages
# --------------------------------------------------------------------------- #
COMPOSE = ('<form action="/conversations" method="post">'
           '<input type="hidden" name="authenticity_token" value="tok">'
           '<input type="hidden" name="source" value="profile">'
           '<input type="hidden" name="with[]" value="">'
           '<input type="hidden" name="with[]" value="42"></form>')


FLASH_OK = ('<div data-flash-toast data-controller="flash-toast" data-type="success">'
            'Your message has been successfully sent to Pal</div>')


@responses.activate
def test_send_message_posts_the_form_and_reads_the_success_flash(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/conversations/new", body=COMPOSE,
                  match=[query_param_matcher({"source": "profile", "with": "42"})])
    # Success is a redirect back to the member's profile carrying a flash toast.
    responses.add(responses.POST, "https://fetlife.com/conversations", status=302,
                  headers={"Location": "https://fetlife.com/Pal"})
    responses.add(responses.GET, "https://fetlife.com/Pal", body="<p>profile</p>" + FLASH_OK)
    fl = _authed(tmp_path)
    assert fl.send_message("42", "Hi there", "Body text") == \
        "Your message has been successfully sent to Pal"
    post = next(c.request for c in responses.calls if c.request.method == "POST")
    assert post.body == ("authenticity_token=tok&source=profile&with%5B%5D=42"
                         "&subject=Hi+there&body=Body+text")
    assert post.headers["Origin"] == "https://fetlife.com"


@responses.activate
def test_send_message_without_a_confirmation_is_an_error(tmp_path):
    from fetlife.exceptions import FetLifeError

    responses.add(responses.GET, "https://fetlife.com/conversations/new", body=COMPOSE,
                  match=[query_param_matcher({"source": "profile", "with": "42"})])
    responses.add(responses.POST, "https://fetlife.com/conversations", status=302,
                  headers={"Location": "https://fetlife.com/Pal"})
    responses.add(responses.GET, "https://fetlife.com/Pal", body="<p>profile, no flash</p>")
    fl = _authed(tmp_path)
    with pytest.raises(FetLifeError, match="did not confirm"):
        fl.send_message("42", "s", "b")


@responses.activate
def test_send_message_refuses_when_member_cannot_be_messaged(tmp_path):
    from fetlife.exceptions import FetLifeError

    # FetLife bounces the compose page back to the profile.
    responses.add(responses.GET, "https://fetlife.com/conversations/new", status=302,
                  headers={"Location": "https://fetlife.com/Closed"},
                  match=[query_param_matcher({"source": "profile", "with": "7"})])
    responses.add(responses.GET, "https://fetlife.com/Closed", body="<p>profile</p>")
    fl = _authed(tmp_path)
    assert fl.get_message_form("7") is None
    with pytest.raises(FetLifeError, match="doesn't accept messages"):
        fl.send_message("7", "s", "b")
    assert not [c for c in responses.calls if c.request.method == "POST"]


@responses.activate
def test_send_message_surfaces_a_rejected_post(tmp_path):
    from fetlife.exceptions import FetLifeError

    responses.add(responses.GET, "https://fetlife.com/conversations/new", body=COMPOSE,
                  match=[query_param_matcher({"source": "profile", "with": "42"})])
    responses.add(responses.POST, "https://fetlife.com/conversations", status=200,
                  body='<div id="flash" class="bg-red-500">Subject is too long</div>' + COMPOSE)
    fl = _authed(tmp_path)
    with pytest.raises(FetLifeError, match="Subject is too long"):
        fl.send_message("42", "s", "b")


def _group_page(nicks, next_page=None):
    entries = "".join(
        f'<div><div><a class="font-bold" href="/{n}">{n}</a>'
        f'<span class="font-bold">30M</span></div></div>' for n in nicks
    )
    nav = (f'<div class="pagination"><a class="next_page" rel="next" '
           f'href="/groups/9/members?page={next_page}">Next</a></div>' if next_page else "")
    return f'<h1>G</h1><div id="group_members_list">{entries}</div>{nav}'


@responses.activate
def test_iter_group_member_pages_follows_next_links(tmp_path):
    responses.add(responses.GET, "https://fetlife.com/groups/9/members",
                  body=_group_page(["a", "b"], next_page=2),
                  match=[query_param_matcher({'page': '1'})])
    responses.add(responses.GET, "https://fetlife.com/groups/9/members",
                  body=_group_page(["c"]),  # last page: no Next link
                  match=[query_param_matcher({'page': '2'})])
    fl = _authed(tmp_path)
    pages = list(fl.iter_group_member_pages("9"))
    assert [(p, [m.nickname for m in found.members]) for p, found in pages] == [
        (1, ["a", "b"]), (2, ["c"]),
    ]
    assert len(responses.calls) == 2  # stops on the missing Next link, no extra fetch


@responses.activate
def test_iter_group_member_pages_starts_where_told_and_stops_on_repeats(tmp_path):
    # A list that ignores ?page= would serve the same page forever.
    responses.add(responses.GET, "https://fetlife.com/groups/9/members",
                  body=_group_page(["a"], next_page=4))
    fl = _authed(tmp_path)
    assert [p for p, _ in fl.iter_group_member_pages("9", start_page=3)] == [3]
    assert [c.request.params["page"] for c in responses.calls] == ["3", "4"]
