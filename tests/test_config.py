"""Config env-parsing tests, focused on the min/max rate-limit resolution."""

import pytest

from fetlife.config import (
    Config,
    DEFAULT_RATE_LIMIT_MIN,
    DEFAULT_RATE_LIMIT_MAX,
)

_RATE_VARS = [
    "FETLIFE_RATE_LIMIT", "FETLIFE_RATE_LIMIT_MIN", "FETLIFE_RATE_LIMIT_MAX",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    # Don't let the developer's real .env bleed into these tests.
    monkeypatch.setattr("fetlife.config.load_dotenv", lambda *a, **k: None)
    for var in _RATE_VARS:
        monkeypatch.delenv(var, raising=False)


def test_defaults():
    cfg = Config.from_env()
    assert cfg.rate_limit_min == DEFAULT_RATE_LIMIT_MIN
    assert cfg.rate_limit_max == DEFAULT_RATE_LIMIT_MAX


def test_explicit_min_max(monkeypatch):
    monkeypatch.setenv("FETLIFE_RATE_LIMIT_MIN", "1.5")
    monkeypatch.setenv("FETLIFE_RATE_LIMIT_MAX", "6")
    cfg = Config.from_env()
    assert (cfg.rate_limit_min, cfg.rate_limit_max) == (1.5, 6.0)


def test_legacy_rate_limit_is_fallback_for_both(monkeypatch):
    monkeypatch.setenv("FETLIFE_RATE_LIMIT", "3")
    cfg = Config.from_env()
    assert (cfg.rate_limit_min, cfg.rate_limit_max) == (3.0, 3.0)


def test_min_max_override_legacy(monkeypatch):
    monkeypatch.setenv("FETLIFE_RATE_LIMIT", "3")
    monkeypatch.setenv("FETLIFE_RATE_LIMIT_MAX", "8")
    cfg = Config.from_env()
    # min falls back to legacy 3, max is the explicit 8
    assert (cfg.rate_limit_min, cfg.rate_limit_max) == (3.0, 8.0)


def test_max_clamped_to_at_least_min(monkeypatch):
    monkeypatch.setenv("FETLIFE_RATE_LIMIT_MIN", "5")
    monkeypatch.setenv("FETLIFE_RATE_LIMIT_MAX", "2")  # invalid: max < min
    cfg = Config.from_env()
    assert cfg.rate_limit_min == 5.0
    assert cfg.rate_limit_max == 5.0  # bumped up to min


def test_invalid_values_fall_back_to_default(monkeypatch):
    monkeypatch.setenv("FETLIFE_RATE_LIMIT_MIN", "abc")
    cfg = Config.from_env()
    assert cfg.rate_limit_min == DEFAULT_RATE_LIMIT_MIN
