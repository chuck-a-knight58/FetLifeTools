"""Client tests that mock HTTP with the ``responses`` library (offline)."""

import pytest
import requests
import responses

from fetlife.client import FetLifeClient
from fetlife.config import Config
from fetlife.exceptions import RateLimitedError


def _client(tmp_path, **overrides):
    cfg = Config(
        username="tester",
        password="secret",
        base_url="https://fetlife.com",
        rate_limit_min=0.0,
        rate_limit_max=0.0,
        session_path=tmp_path / "session.cookies",
        retry_backoff=0.0,  # no real sleeping in tests
        **overrides,
    )
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


def test_throttle_draws_from_min_max_range(tmp_path, monkeypatch):
    cfg = Config(
        username="t", password="p", base_url="https://fetlife.com",
        rate_limit_min=1.5, rate_limit_max=4.0, retry_backoff=0.0,
        session_path=tmp_path / "session.cookies",
    )
    fl = FetLifeClient(cfg, session=requests.Session())
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
