"""HTTP client for FetLife.

FetLife has no public API, so this client authenticates as a normal user and
requests the same server-rendered pages a browser would, then hands the HTML
to :mod:`fetlife.parsers`. Be a good citizen: keep the rate limit sane and use
this only for your own account and data you are permitted to access.
"""

from __future__ import annotations

import json
import random
import time
from urllib.parse import urljoin, quote_plus, urlsplit

from curl_cffi import requests as cffi_requests

from .config import Config
from .exceptions import (
    AuthenticationError,
    FetLifeError,
    NotFoundError,
    ParseError,
    RateLimitedError,
)
from .models import Event, Group, Member, Relationship
from . import parsers


class FetLifeClient:
    """A logged-in session against FetLife.

    Typical use::

        with FetLifeClient.from_env() as fl:
            fl.login()
            member = fl.get_member("JohnDoe")
    """

    def __init__(self, config: Config | None = None, session=None):
        self.config = config or Config.from_env()
        # curl_cffi impersonates a real browser's TLS/JA3 fingerprint, which is
        # what gets us past Cloudflare's bot check. A session may be injected
        # (e.g. a plain requests.Session in offline tests); the request/cookie
        # APIs we rely on are compatible across both.
        self.session = session or cffi_requests.Session(
            impersonate=self.config.impersonate
        )
        # Only override the User-Agent if one was explicitly configured — the
        # impersonation profile already sets a browser-consistent UA, and
        # replacing it with a mismatched string re-triggers Cloudflare.
        if self.config.user_agent:
            self.session.headers["User-Agent"] = self.config.user_agent
        self._last_request = 0.0
        # Count of HTTP requests actually sent (includes retries) — surfaced in
        # the discover progress display so the user can gauge crawl volume.
        self.requests = 0
        self._authenticated = False
        # Optional hook: on_retry(status, wait_seconds, attempt, max_attempts).
        self.on_retry = None
        self._load_cookies()

    # ------------------------------------------------------------------ #
    # Construction / lifecycle
    # ------------------------------------------------------------------ #
    @classmethod
    def from_env(cls, env_file: str | None = None) -> "FetLifeClient":
        return cls(Config.from_env(env_file))

    def __enter__(self) -> "FetLifeClient":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._save_cookies()
        self.session.close()

    # ------------------------------------------------------------------ #
    # Session persistence
    # ------------------------------------------------------------------ #
    def _cookie_domain(self) -> str:
        return urlsplit(self.config.base_url).hostname or "fetlife.com"

    def _clear_cookies(self) -> None:
        """Empty the session's cookie jar (used before a fresh login)."""
        try:
            self.session.cookies.clear()
        except (AttributeError, TypeError):
            # Fall back to deleting each known cookie individually.
            for name in [n for n, _ in self.session.cookies.items()]:
                try:
                    del self.session.cookies[name]
                except (KeyError, TypeError):
                    pass

    def _load_cookies(self) -> None:
        path = self.config.session_path
        if not path.exists():
            return
        try:
            data = json.loads(path.read_bytes().decode("utf-8"))
            domain = self._cookie_domain()
            for name, value in data.items():
                self.session.cookies.set(name, value, domain=domain)
        except (ValueError, OSError, TypeError, AttributeError):
            # A corrupt or legacy (pickle) cookie file should never be fatal —
            # just start fresh. ValueError covers JSON + unicode decode errors.
            pass

    def _save_cookies(self) -> None:
        path = self.config.session_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = {name: value for name, value in self.session.cookies.items()}
            path.write_text(json.dumps(data))
            path.chmod(0o600)
        except OSError:
            pass

    # ------------------------------------------------------------------ #
    # Low-level request helper
    # ------------------------------------------------------------------ #
    def _throttle(self) -> None:
        # Random interval in [min, max] since the last request — jitter is less
        # bot-like (and less throttle-prone) than a fixed cadence.
        target = random.uniform(self.config.rate_limit_min, self.config.rate_limit_max)
        elapsed = time.monotonic() - self._last_request
        wait = target - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def _url(self, path: str) -> str:
        return urljoin(self.config.base_url + "/", path.lstrip("/"))

    def _retry_wait(self, resp, attempt: int) -> float:
        """Seconds to wait before a retry: honor Retry-After, else exponential."""
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 300.0)
            except ValueError:
                pass
        return min(self.config.retry_backoff * (2 ** attempt), 120.0)

    def request(self, method: str, path: str, **kwargs) -> cffi_requests.Response:
        """Perform a rate-limited request, retrying 429/5xx with backoff."""
        url = self._url(path) if not path.startswith("http") else path
        attempts = max(1, self.config.max_retries + 1)
        for attempt in range(attempts):
            self._throttle()
            resp = self.session.request(method, url, allow_redirects=True, **kwargs)
            self.requests += 1
            status = resp.status_code

            if status == 429 or status >= 500:
                if attempt < attempts - 1:
                    wait = self._retry_wait(resp, attempt)
                    if self.on_retry:
                        self.on_retry(status, wait, attempt + 1, attempts - 1)
                    time.sleep(wait)
                    continue
                if status == 429:
                    raise RateLimitedError(
                        "FetLife returned HTTP 429 (Too Many Requests) after "
                        f"{attempts - 1} retries. Raise FETLIFE_RATE_LIMIT_MIN/"
                        "MAX and try again later (resume with `discover --resume`)."
                    )
                raise FetLifeError(f"Server error {status} for {url}")
            if status == 404:
                raise NotFoundError(f"Not found: {url}")
            break
        return resp

    def get(self, path: str, **kwargs) -> cffi_requests.Response:
        return self.request("GET", path, **kwargs)

    def get_json(self, path: str, **kwargs) -> dict:
        """GET *path* asking for JSON; return the parsed body.

        FetLife's SPA fetches profile/friends/etc. data from the same URLs the
        browser uses, but with ``Accept: application/json``. These are plain
        cookie-authenticated GETs — no CSRF token or request signing required.
        """
        headers = {"Accept": "application/json"}
        headers.update(kwargs.pop("headers", {}))
        resp = self.get(path, headers=headers, **kwargs)
        if "json" not in resp.headers.get("content-type", ""):
            raise ParseError(
                f"Expected JSON from {path} but got "
                f"{resp.headers.get('content-type', 'unknown')!r} — the endpoint "
                "may have changed."
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise ParseError(f"Invalid JSON from {path}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Authentication
    # ------------------------------------------------------------------ #
    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def login(self, force: bool = False) -> bool:
        """Authenticate with FetLife.

        Returns True on success. If a persisted session is still valid, this
        is a no-op unless *force* is set.
        """
        if not force and self._check_authenticated():
            self._authenticated = True
            return True

        username, password = self.config.require_credentials()

        # A stale, unauthenticated session cookie left over from a previous run
        # invalidates the fresh CSRF token the login page issues ("this page
        # expired"). Start the handshake from a clean jar.
        self._clear_cookies()

        # 1. Fetch the login page to obtain the CSRF (authenticity) token.
        login_page = self.get("/login")
        token = parsers.extract_csrf_token(login_page.text)
        if not token:
            raise AuthenticationError(
                "Could not find the CSRF token on the login page. "
                "FetLife's login markup may have changed."
            )

        # 2. Submit the credentials. FetLife sends browser form posts with these
        #    headers; omitting them can get the POST rejected outright.
        resp = self.request(
            "POST",
            "/login",
            headers={
                "Referer": self._url("/login"),
                "Origin": self.config.base_url,
            },
            data={
                "authenticity_token": token,
                "user[login]": username,
                "user[password]": password,
                "user[otp_attempt]": "",
            },
        )

        # 3. Verify against a fresh page rather than trusting the POST response
        #    (a successful login redirects; this is unambiguous either way).
        if not self._check_authenticated():
            reason = parsers.extract_login_error(resp.text)
            detail = f" FetLife said: “{reason}”" if reason else ""
            raise AuthenticationError(
                "Login failed." + detail + " Check FETLIFE_USERNAME / "
                "FETLIFE_PASSWORD in your .env (and complete any required 2FA "
                "or email confirmation in a browser first)."
            )

        self._authenticated = True
        self._save_cookies()
        return True

    def _check_authenticated(self, resp: cffi_requests.Response | None = None) -> bool:
        """Heuristically determine whether the current session is logged in."""
        try:
            page = resp or self.get("/home")
        except FetLifeError:
            return False
        return parsers.looks_authenticated(page.text, page.url)

    def _ensure_auth(self) -> None:
        if not self._authenticated and not self.login():
            raise AuthenticationError("Not authenticated.")

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def whoami(self) -> Member:
        """Return the currently logged-in user's own profile.

        Read from the ``window.FL.user`` blob FetLife embeds on every page —
        this is always fully populated, unlike other members' SPA profiles.
        """
        self._ensure_auth()
        resp = self.get("/home")
        user = parsers.extract_bootstrap_user(resp.text)
        if not user:
            raise parsers.ParseError(
                "Could not locate the logged-in user data (FL.user) on /home."
            )
        return parsers.member_from_bootstrap(user, base_url=self.config.base_url)

    def _profile_path(self, nickname_or_id: str) -> str:
        ident = str(nickname_or_id)
        return f"/users/{ident}" if ident.isdigit() else f"/{quote_plus(ident)}"

    def get_member(self, nickname_or_id: str) -> Member:
        """Fetch a member profile by nickname or numeric user id.

        Uses FetLife's JSON API (``GET /<nickname>`` with an application/json
        Accept header) — the SPA's own data source — so full data is returned
        for any member. Falls back to HTML parsing if the API shape changes.
        """
        self._ensure_auth()
        path = self._profile_path(nickname_or_id)
        try:
            data = self.get_json(path)
            return parsers.member_from_core(data, base_url=self.config.base_url)
        except (ParseError, KeyError):
            # Fallback: server-rendered HTML (full data only for your own page).
            resp = self.get(path)
            return parsers.parse_member(
                resp.text, url=resp.url, requested=str(nickname_or_id)
            )

    def _member_list(self, nickname_or_id: str, section: str, page: int) -> list[Member]:
        """Fetch a friends/followers/following list (all share one JSON shape)."""
        self._ensure_auth()
        path = self._profile_path(nickname_or_id) + f"/{section}"
        data = self.get_json(path, params={"page": page})
        return parsers.members_from_user_list(data, base_url=self.config.base_url)

    def get_friends(self, nickname_or_id: str, page: int = 1) -> list[Member]:
        """List a member's friends (entries include age/gender/role/location)."""
        return self._member_list(nickname_or_id, "friends", page)

    def get_followers(self, nickname_or_id: str, page: int = 1) -> list[Member]:
        """List the members following this member."""
        return self._member_list(nickname_or_id, "followers", page)

    def get_following(self, nickname_or_id: str, page: int = 1) -> list[Member]:
        """List the members this member is following."""
        return self._member_list(nickname_or_id, "following", page)

    def get_last_active(self, nickname_or_id: str):
        """Best-effort last-active time (UTC datetime) from the activity feed.

        Returns None if the member has no visible activity or the feed can't be
        read. See :func:`parsers.last_active_from_activity` for the caveat that
        this only reflects *public* activity.
        """
        self._ensure_auth()
        path = self._profile_path(nickname_or_id) + "/activity"
        try:
            data = self.get_json(path, params={"accurate_per_page": 5})
        except RateLimitedError:
            raise  # surface throttling so callers can back off / halt
        except FetLifeError:
            return None
        return parsers.last_active_from_activity(data)

    def get_relationships(self, nickname_or_id: str) -> list["Relationship"]:
        """List a member's relationships (vanilla and D/s).

        Sourced from the same profile JSON as :meth:`get_member`, so it's a
        single request.
        """
        self._ensure_auth()
        data = self.get_json(self._profile_path(nickname_or_id))
        return parsers.relationships_from_core(data, base_url=self.config.base_url)

    def search_members(self, query: str, page: int = 1) -> list[Member]:
        """Search members by keyword/nickname."""
        self._ensure_auth()
        resp = self.get(
            "/search/kinksters", params={"q": query, "page": page}
        )
        return parsers.parse_member_search(resp.text, base_url=self.config.base_url)

    def get_events(self, place_id: str | None = None, page: int = 1) -> list[Event]:
        """List upcoming events, optionally scoped to a place id."""
        self._ensure_auth()
        path = f"/places/{place_id}/events" if place_id else "/events"
        resp = self.get(path, params={"page": page})
        return parsers.parse_event_list(resp.text, base_url=self.config.base_url)

    def get_event(self, event_id: str) -> Event:
        """Fetch a single event by id."""
        self._ensure_auth()
        resp = self.get(f"/events/{event_id}")
        return parsers.parse_event(resp.text, url=resp.url)

    def get_group(self, group_id: str) -> Group:
        """Fetch a single group by id."""
        self._ensure_auth()
        resp = self.get(f"/groups/{group_id}")
        return parsers.parse_group(resp.text, url=resp.url)

    def fetch_raw(self, path: str) -> str:
        """Return the raw HTML for an arbitrary path (handy for debugging parsers)."""
        self._ensure_auth()
        return self.get(path).text
