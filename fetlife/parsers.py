"""HTML parsing for FetLife pages.

All markup-specific knowledge lives here so that when FetLife changes its HTML
you only have to update this one module. Every parser is defensive: it prefers
structured signals (meta tags, ``data-*`` attributes, JSON blobs) and falls
back to looser heuristics, returning partially-populated models rather than
crashing.

Because FetLife requires a login to view most pages, the exact selectors below
should be treated as a starting point and verified against live HTML using::

    fetlife raw /<path>            # dump HTML to inspect current structure
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .exceptions import ParseError
from .models import Event, Group, Member, Relationship

_PARSER = "lxml"


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, _PARSER)


def _int(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"\d[\d,]*", text)
    return int(m.group().replace(",", "")) if m else None


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = " ".join(text.split()).strip()
    return cleaned or None


# --------------------------------------------------------------------------- #
# Authentication helpers
# --------------------------------------------------------------------------- #
def extract_csrf_token(html: str) -> str | None:
    """Pull Rails' authenticity token from a page.

    FetLife is a Rails app; the token is exposed either as
    ``<meta name="csrf-token">`` or a hidden ``authenticity_token`` input.
    """
    soup = _soup(html)
    meta = soup.find("meta", attrs={"name": "csrf-token"})
    if meta and meta.get("content"):
        return meta["content"]
    hidden = soup.find("input", attrs={"name": "authenticity_token"})
    if hidden and hidden.get("value"):
        return hidden["value"]
    return None


def extract_login_error(html: str) -> str | None:
    """Return FetLife's login flash/error message, if the page shows one.

    FetLife renders failures like "…Nickname, Email or Password is incorrect…"
    in a red flash element. We surface whatever short error text we can find so
    the CLI can report the real reason rather than a generic guess.
    """
    soup = _soup(html)
    error_re = re.compile(
        r"incorrect|invalid|wasn't found|not found|locked|confirm|"
        r"try again|password|blocked|suspend",
        re.I,
    )
    candidates = soup.select(
        "[class*='bg-red'], [id*='flash'], [class*='flash'], [role='alert']"
    )
    texts = [
        t for t in (_clean(el.get_text()) for el in candidates)
        if t and 3 < len(t) < 200
    ]
    # Prefer a candidate that actually reads like an error message; only fall
    # back to the first flash-ish element (or a loose page search) otherwise.
    for text in texts:
        if error_re.search(text):
            return text
    if texts:
        return texts[0]
    node = soup.find(string=error_re)
    return _clean(str(node)) if node else None


def looks_authenticated(html: str, url: str | None = None) -> bool:
    """Heuristically decide whether a page was served to a logged-in user."""
    if url and "/login" in url:
        return False
    soup = _soup(html)
    # A logged-in FetLife page exposes the current user via a logout link,
    # a "new post" affordance, or a body/data attribute for the current user.
    if soup.find("a", href=re.compile(r"/logout|/session")):
        return True
    if soup.find(attrs={"data-current-user-id": True}):
        return True
    if soup.find("meta", attrs={"name": "current-user"}):
        return True
    # Fallback: the login form is absent on authenticated pages.
    return soup.find("input", attrs={"name": "user[password]"}) is None


# --------------------------------------------------------------------------- #
# Structured-data extraction
# --------------------------------------------------------------------------- #
def _json_ld(soup: BeautifulSoup) -> list[dict]:
    """Return any schema.org JSON-LD objects embedded in the page."""
    out: list[dict] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, list):
            out.extend(d for d in data if isinstance(d, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def _id_from_url(url: str | None, keyword: str) -> str | None:
    if not url:
        return None
    m = re.search(rf"/{keyword}/(\d+)", url)
    return m.group(1) if m else None


# --------------------------------------------------------------------------- #
# Member parsing
# --------------------------------------------------------------------------- #
# FetLife's profile UI is a client-rendered SPA, but every page embeds the
# *current logged-in user* server-side as `window.FL.user = {...}`. That's the
# one profile we can read reliably over plain HTTP.
_FL_USER_RE = re.compile(r"FL\.user\s*=\s*(\{.*?\})\s*;", re.DOTALL)


def extract_bootstrap_user(html: str) -> dict | None:
    """Return the ``window.FL.user`` object (the logged-in viewer), if present."""
    m = _FL_USER_RE.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def _avatar_from_bootstrap(user: dict) -> str | None:
    urls = user.get("avatarUrls")
    if isinstance(urls, dict) and urls:
        # Prefer the largest available crop; keys look like c50, c120, ...
        def _size(key: str) -> int:
            digits = re.search(r"\d+", key)
            return int(digits.group()) if digits else 0

        return urls[max(urls, key=_size)]
    return None


def member_from_bootstrap(user: dict, base_url: str = "") -> Member:
    """Build a :class:`Member` from a ``window.FL.user`` dict (the viewer)."""
    profile_url = user.get("profileUrl")
    return Member(
        id=str(user["id"]) if user.get("id") is not None else None,
        nickname=user.get("nickname", ""),
        age=user.get("age"),
        gender=user.get("gender"),
        role=user.get("role"),
        orientation=user.get("orientation"),
        url=urljoin(base_url + "/", profile_url.lstrip("/")) if profile_url else None,
        avatar_url=_avatar_from_bootstrap(user),
        verified=user.get("isProfileVerified"),
        meta={"supporter": user.get("isSupporter")},
    )


def _names(items) -> str | None:
    """Join the display names of a list of {key,name} objects (roles, genders...)."""
    if not isinstance(items, list):
        return None
    names = [i.get("name") or i.get("key") for i in items if isinstance(i, dict)]
    names = [n for n in names if n]
    return ", ".join(names) or None


def _location_parts(value) -> list[str]:
    """Return the ordered place names (city, region, country) from a location."""
    if isinstance(value, list):
        return [p.get("name") for p in value if isinstance(p, dict) and p.get("name")]
    if isinstance(value, dict) and value.get("name"):
        return [value["name"]]
    if isinstance(value, str) and _clean(value):
        return [p.strip() for p in value.split(",") if p.strip()]
    return []


def _location_names(value) -> str | None:
    """FetLife returns location as a list of place dicts (city, region, country)."""
    parts = _location_parts(value)
    return ", ".join(parts) or None


def _html_to_text(html: str | None) -> str | None:
    if not html:
        return None
    return _clean(_soup(html).get_text(" "))


def _relationship_from_obj(obj: dict, kind: str, base_url: str = "") -> Relationship:
    url = obj.get("withUrl")
    return Relationship(
        kind=kind,
        status=obj.get("status"),
        status_with_connector=obj.get("statusWithConnector"),
        with_nickname=obj.get("withNickname", ""),
        with_id=str(obj["withId"]) if obj.get("withId") is not None else None,
        with_url=urljoin(base_url + "/", url.lstrip("/")) if url else None,
        pending=obj.get("pending"),
    )


def relationships_from_core(payload: dict, base_url: str = "") -> list[Relationship]:
    """Extract vanilla + D/s relationships from the profile ``core`` JSON."""
    core = payload.get("core") or {}
    out: list[Relationship] = []
    for obj in core.get("relationships") or []:
        if isinstance(obj, dict):
            out.append(_relationship_from_obj(obj, "relationship", base_url))
    for obj in core.get("dsRelationships") or []:
        if isinstance(obj, dict):
            out.append(_relationship_from_obj(obj, "D/s", base_url))
    return out


def member_from_core(payload: dict, base_url: str = "") -> Member:
    """Build a :class:`Member` from the ``GET /<nickname>`` JSON API response.

    This is the SPA's own data source, so it returns full profile data for any
    member — not just the logged-in viewer.
    """
    core = payload.get("core") or {}
    relation = payload.get("currentUserRelation") or {}

    # There's no standalone age field; it's embedded in `identity` ("60F sub").
    identity = core.get("identity") or ""
    age_match = re.match(r"\s*(\d{1,3})", identity)

    url = core.get("url")
    return Member(
        id=str(core["userId"]) if core.get("userId") is not None else None,
        nickname=core.get("nickname", ""),
        age=int(age_match.group(1)) if age_match else None,
        gender=_names(core.get("genders")),
        role=_names(core.get("roles")),
        orientation=_names(core.get("orientations")),
        location=_location_names(relation.get("location")),
        url=urljoin(base_url + "/", url.lstrip("/")) if url else None,
        avatar_url=core.get("avatarUrl") or core.get("smallAvatarUrl"),
        about=_html_to_text(core.get("aboutHtml")),
        joined=core.get("joinDate"),
        verified=core.get("isProfileVerified"),
        relationships=relationships_from_core(payload, base_url),
        meta={
            "identity": identity or None,
            "looking_for": core.get("isLookingFor"),
            "not_looking_for": core.get("isNotLookingFor"),
            "supporter": core.get("isSupporter"),
            # Ordered place names (city, region, country) for precise geocoding.
            "location_parts": _location_parts(relation.get("location")),
        },
    )


def members_from_user_list(payload: dict, base_url: str = "") -> list[Member]:
    """Parse a ``{users, page, no_more}`` list into Members.

    Shared by friends / followers / following — all three endpoints return the
    same shape, with age/gender/role inline, so no per-member fetch is needed.
    """
    out: list[Member] = []
    for u in payload.get("users", []):
        if not isinstance(u, dict):
            continue
        url = u.get("url")
        out.append(
            Member(
                id=str(u["id"]) if u.get("id") is not None else None,
                nickname=u.get("nickname", ""),
                age=u.get("age"),
                gender=u.get("gender"),
                role=u.get("role"),
                location=_location_names(u.get("location")),
                url=urljoin(base_url + "/", url.lstrip("/")) if url else None,
                avatar_url=u.get("large_avatar_url") or u.get("avatar_url"),
            )
        )
    return out


def _parse_iso(text) -> datetime | None:
    """Parse an ISO-8601 timestamp like '2026-07-07T19:11:32.432Z' as UTC-aware."""
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def last_active_from_activity(payload: dict) -> datetime | None:
    """Return the newest story ``created_at`` in an activity feed, or None.

    FetLife has no explicit "last seen" field, so a member's most recent public
    activity (reactions/posts in ``GET /<nickname>/activity``) is the best
    available signal of recent activity. Members who only lurk produce no
    stories and will read as inactive.
    """
    newest: datetime | None = None
    for group in payload.get("story_groups") or []:
        for story in group.get("stories") or []:
            dt = _parse_iso(story.get("created_at"))
            if dt and (newest is None or dt > newest):
                newest = dt
    return newest


def _nickname_from_title(soup: BeautifulSoup) -> str:
    title = soup.title.string if soup.title else None
    if not title:
        og = soup.find("meta", property="og:title")
        title = og["content"] if og and og.get("content") else ""
    # "Knight_of_Xanadu - Kinksters | FetLife" -> "Knight_of_Xanadu"
    return _clean(title.split(" - ")[0].split(" | ")[0]) or ""


def _matches(user: dict, requested: str | None) -> bool:
    if requested is None:  # caller asked for "self"
        return True
    req = str(requested).lstrip("/").lower()
    return req in {
        str(user.get("id", "")).lower(),
        str(user.get("nickname", "")).lower(),
        str(user.get("profileUrl", "")).lstrip("/").lower(),
    }


def parse_member(
    html: str, url: str | None = None, requested: str | None = None
) -> Member:
    """Parse a profile page.

    If the page's embedded ``FL.user`` is the profile that was requested (i.e.
    you're viewing your own profile), we return its full, structured data.
    Otherwise only the nickname is available over plain HTTP — the rest of the
    profile is rendered client-side — so we return a partial Member annotated
    with that limitation rather than pretending the fields are empty.
    """
    soup = _soup(html)
    user = extract_bootstrap_user(html)
    if user and _matches(user, requested):
        member = member_from_bootstrap(user, base_url=_base_from_url(url))
        if url:
            member.url = url
        return member

    nickname = _nickname_from_title(soup)
    if not nickname:
        raise ParseError(
            "Could not parse a nickname from the profile page. "
            "The member parser in fetlife/parsers.py likely needs updating."
        )
    og_image = soup.find("meta", property="og:image")
    return Member(
        id=_id_from_url(url, "users"),
        nickname=nickname,
        url=url,
        avatar_url=og_image["content"] if og_image and og_image.get("content") else None,
        meta={
            "note": (
                "Only the nickname is available over plain HTTP; FetLife renders "
                "the rest of this profile client-side. See README (SPA profiles)."
            )
        },
    )


def _base_from_url(url: str | None) -> str:
    if not url:
        return ""
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme else ""


def parse_member_search(html: str, base_url: str = "") -> list[Member]:
    soup = _soup(html)
    results: list[Member] = []
    # Search results are a list of profile links; each user link points to
    # /<nickname> or /users/<id>. We de-duplicate by URL.
    seen: set[str] = set()
    for a in soup.select("a[href^='/users/'], a[href]"):
        href = a.get("href", "")
        if not re.match(r"^/(users/\d+|[A-Za-z0-9_%-]+)$", href):
            continue
        if href in seen or href.startswith(("/login", "/logout", "/search")):
            continue
        text = _clean(a.get_text())
        if not text:
            continue
        seen.add(href)
        results.append(
            Member(
                nickname=text,
                url=urljoin(base_url + "/", href.lstrip("/")),
                id=_id_from_url(href, "users"),
            )
        )
    return results


# --------------------------------------------------------------------------- #
# Event parsing
# --------------------------------------------------------------------------- #
def parse_event(html: str, url: str | None = None) -> Event:
    soup = _soup(html)
    event = Event(url=url, id=_id_from_url(url, "events"))

    for obj in _json_ld(soup):
        if obj.get("@type") == "Event":
            event.name = _clean(obj.get("name")) or event.name
            event.start = obj.get("startDate") or event.start
            event.end = obj.get("endDate") or event.end
            location = obj.get("location") or {}
            if isinstance(location, dict):
                event.location = _clean(location.get("name")) or event.location
                addr = location.get("address")
                if isinstance(addr, dict):
                    parts = [
                        addr.get("streetAddress"),
                        addr.get("addressLocality"),
                        addr.get("addressRegion"),
                    ]
                    event.address = _clean(", ".join(p for p in parts if p))
                elif isinstance(addr, str):
                    event.address = _clean(addr)
            break

    if not event.name:
        h1 = soup.find("h1")
        event.name = _clean(h1.get_text()) if h1 else ""

    if not event.name:
        raise ParseError(
            "Could not parse an event name. The event parser in "
            "fetlife/parsers.py likely needs updating."
        )
    return event


def parse_event_list(html: str, base_url: str = "") -> list[Event]:
    soup = _soup(html)
    events: list[Event] = []
    seen: set[str] = set()
    for a in soup.select("a[href^='/events/']"):
        href = a.get("href", "")
        m = re.match(r"^/events/(\d+)", href)
        if not m or href in seen:
            continue
        name = _clean(a.get_text())
        if not name:
            continue
        seen.add(href)
        events.append(
            Event(
                id=m.group(1),
                name=name,
                url=urljoin(base_url + "/", href.lstrip("/")),
            )
        )
    return events


# --------------------------------------------------------------------------- #
# Group parsing
# --------------------------------------------------------------------------- #
def parse_group(html: str, url: str | None = None) -> Group:
    soup = _soup(html)
    group = Group(url=url, id=_id_from_url(url, "groups"))

    h1 = soup.find("h1")
    group.name = _clean(h1.get_text()) if h1 else ""

    member_hint = soup.find(string=re.compile(r"member", re.I))
    if member_hint:
        group.member_count = _int(str(member_hint))

    if not group.name:
        raise ParseError(
            "Could not parse a group name. The group parser in "
            "fetlife/parsers.py likely needs updating."
        )
    return group
