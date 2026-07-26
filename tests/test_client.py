"""Client tests that mock HTTP with the ``responses`` library (offline)."""

import json
import time

import pytest
import requests
import responses

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
