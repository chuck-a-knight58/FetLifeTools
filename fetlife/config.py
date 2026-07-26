"""Configuration and credential loading.

Credentials are read from environment variables (optionally populated from a
`.env` file). They are never written to disk by this project.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import AuthenticationError

DEFAULT_BASE_URL = "https://fetlife.com"
# Each request waits a *random* interval in [min, max] seconds since the last
# one — jitter looks less bot-like than a fixed cadence and eases throttling.
DEFAULT_RATE_LIMIT_MIN = 2.0
DEFAULT_RATE_LIMIT_MAX = 5.0
# On HTTP 429/5xx, retry with exponential backoff instead of failing — important
# for long crawls, which will otherwise trip FetLife's throttling eventually.
DEFAULT_MAX_RETRIES = 4
DEFAULT_RETRY_BACKOFF = 5.0
DEFAULT_SESSION_PATH = "~/.fetlife/session.cookies"
# Where the client remembers being throttled. FetLife's rate limit outlives the
# process that tripped it, so the learned delay has to outlive it too — without
# this every run rediscovers the wall by walking into it.
DEFAULT_THROTTLE_STATE_PATH = "~/.fetlife/throttle.json"
# Browser profile curl_cffi impersonates to clear Cloudflare's TLS fingerprint
# check. See curl_cffi docs for the full list (chrome, safari, edge, ...).
DEFAULT_IMPERSONATE = "chrome"
# By default we send NO custom User-Agent: curl_cffi's impersonation installs a
# matching browser UA, and overriding it with a bot string re-triggers
# Cloudflare's block. Set FETLIFE_USER_AGENT only if you know what you're doing.
DEFAULT_USER_AGENT: str | None = None
DEFAULT_GEOCODE_CACHE_PATH = "~/.fetlife/geocode.json"
# Nominatim's usage policy requires a descriptive User-Agent identifying the app.
DEFAULT_GEOCODER_USER_AGENT = "FetLifeTools/0.1 (+https://github.com/chuck/FetLifeTools)"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Runtime configuration for a :class:`~fetlife.client.FetLifeClient`."""

    username: str | None = None
    password: str | None = None
    base_url: str = DEFAULT_BASE_URL
    rate_limit_min: float = DEFAULT_RATE_LIMIT_MIN
    rate_limit_max: float = DEFAULT_RATE_LIMIT_MAX
    session_path: Path = Path(DEFAULT_SESSION_PATH).expanduser()
    throttle_state_path: Path = Path(DEFAULT_THROTTLE_STATE_PATH).expanduser()
    user_agent: str | None = DEFAULT_USER_AGENT
    impersonate: str = DEFAULT_IMPERSONATE
    geocode_cache_path: Path = Path(DEFAULT_GEOCODE_CACHE_PATH).expanduser()
    geocoder_user_agent: str = DEFAULT_GEOCODER_USER_AGENT
    max_retries: int = DEFAULT_MAX_RETRIES
    retry_backoff: float = DEFAULT_RETRY_BACKOFF

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = None) -> "Config":
        """Build a config from environment variables (and an optional .env)."""
        # `load_dotenv` is a no-op if the file is missing; existing env wins.
        load_dotenv(dotenv_path=env_file, override=False)

        session_path = os.getenv("FETLIFE_SESSION_PATH", DEFAULT_SESSION_PATH)
        # Legacy FETLIFE_RATE_LIMIT still works as the fallback for both bounds.
        legacy = _env_float("FETLIFE_RATE_LIMIT", None) if os.getenv("FETLIFE_RATE_LIMIT") else None
        rate_min = _env_float("FETLIFE_RATE_LIMIT_MIN",
                              legacy if legacy is not None else DEFAULT_RATE_LIMIT_MIN)
        rate_max = _env_float("FETLIFE_RATE_LIMIT_MAX",
                              legacy if legacy is not None else DEFAULT_RATE_LIMIT_MAX)
        rate_min = max(0.0, rate_min)
        rate_max = max(rate_min, rate_max)  # ensure max >= min

        return cls(
            username=os.getenv("FETLIFE_USERNAME"),
            password=os.getenv("FETLIFE_PASSWORD"),
            base_url=os.getenv("FETLIFE_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            rate_limit_min=rate_min,
            rate_limit_max=rate_max,
            session_path=Path(session_path).expanduser(),
            throttle_state_path=Path(
                os.getenv("FETLIFE_THROTTLE_STATE_PATH", DEFAULT_THROTTLE_STATE_PATH)
            ).expanduser(),
            user_agent=os.getenv("FETLIFE_USER_AGENT") or DEFAULT_USER_AGENT,
            impersonate=os.getenv("FETLIFE_IMPERSONATE", DEFAULT_IMPERSONATE),
            geocode_cache_path=Path(
                os.getenv("FETLIFE_GEOCODE_CACHE_PATH", DEFAULT_GEOCODE_CACHE_PATH)
            ).expanduser(),
            geocoder_user_agent=os.getenv(
                "FETLIFE_GEOCODER_USER_AGENT", DEFAULT_GEOCODER_USER_AGENT
            ),
            max_retries=int(_env_float("FETLIFE_MAX_RETRIES", DEFAULT_MAX_RETRIES)),
            retry_backoff=_env_float("FETLIFE_RETRY_BACKOFF", DEFAULT_RETRY_BACKOFF),
        )

    def require_credentials(self) -> tuple[str, str]:
        """Return (username, password) or raise if either is missing."""
        if not self.username or not self.password:
            raise AuthenticationError(
                "Missing credentials. Set FETLIFE_USERNAME and "
                "FETLIFE_PASSWORD (e.g. in a .env file — see .env.example)."
            )
        return self.username, self.password
