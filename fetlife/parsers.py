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
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from .exceptions import ParseError
from .models import Event, Group, Member, ProfileRelation, Relationship, Story

_PARSER = "lxml"


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, _PARSER)


# Turbo Stream responses wrap their markup in <turbo-stream><template>. Text
# inside <template> is invisible to BeautifulSoup's get_text() (it becomes a
# TemplateString, which get_text skips), so the wrapper is dropped before
# parsing. Plain pages are left alone: their <template>s hold inert modals.
_TEMPLATE_TAG_RE = re.compile(r"</?template(?:\s[^>]*)?>", re.I)


def _page_soup(html: str) -> BeautifulSoup:
    if "<turbo-stream" in html:
        html = _TEMPLATE_TAG_RE.sub("", html)
    return _soup(html)


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
# Every page embeds the *current logged-in user* server-side. Older pages
# assigned it directly (`window.FL.user = {...}`); current ones pass a
# `{"user": {...}}` blob to a merge function in `<script id="page-data">`.
_FL_USER_RE = re.compile(r"FL\.user\s*=\s*(\{.*?\})\s*;", re.DOTALL)
_PAGE_DATA_RE = re.compile(
    r'<script[^>]*id="page-data"[^>]*>.*?\(\s*(\{"user":.*?\})\s*\)\s*;?\s*</script>',
    re.DOTALL,
)


def extract_bootstrap_user(html: str) -> dict | None:
    """Return the logged-in viewer's bootstrap object, if the page has one."""
    for pattern in (_FL_USER_RE, _PAGE_DATA_RE):
        m = pattern.search(html)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        user = data.get("user") if pattern is _PAGE_DATA_RE else data
        if isinstance(user, dict):
            return user
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


_RELATION_ID_RE = re.compile(r"^relation_user_(\d+)$")
# A profile href, as opposed to a sub-page like /<nick>/pictures.
_PROFILE_HREF_RE = re.compile(r"^/[^/?#]+$")
# The bold line under a nickname: age glued to a gender abbreviation, then an
# optional role — "49M", "56W Villain", "38F Submissive".
_AGE_GENDER_RE = re.compile(r"^(\d+)([A-Za-z]*)$")


def _age_gender_role(text: str | None) -> tuple[int | None, str | None, str | None]:
    """Split a list entry's "49M Sadist" line into age, gender, role."""
    if not text:
        return None, None, None
    first, _, rest = text.partition(" ")
    m = _AGE_GENDER_RE.match(first)
    if not m:
        # No age shown: the whole line is the role (gender is glued to the age,
        # so without one there is nothing to read a gender from).
        return None, None, _clean(text)
    return int(m.group(1)), (m.group(2) or None), _clean(rest)


def members_from_relations_html(html: str, base_url: str = "") -> list[Member]:
    """Parse a server-rendered friends/followers/following page into Members.

    FetLife serves these lists as HTML only — the JSON variant these endpoints
    used to answer (``Accept: application/json``) now 404s. Each entry is a
    ``<div id="relation_user_ID">`` carrying nickname, age/gender/role and the
    location string, which is everything the crawl filters on, so no per-member
    fetch is needed here either.
    """
    out: list[Member] = []
    for block in _soup(html).select("div[id^='relation_user_']"):
        m = _RELATION_ID_RE.match(block.get("id", ""))
        link = block.select_one("a.font-bold[href]") or block.find(
            "a", href=_PROFILE_HREF_RE
        )
        if link is None:
            continue
        href = link.get("href", "")
        nickname = _clean(link.get_text()) or href.lstrip("/")
        name_line = link.find_parent("div")
        # The bold span, specifically: a supporter/verified badge can sit in an
        # unstyled span between the nickname and the age/gender line.
        stats = name_line.select_one("span.font-bold") if name_line else None
        age, gender, role = _age_gender_role(_clean(stats.get_text()) if stats else None)
        img = block.find("img")
        out.append(
            Member(
                id=m.group(1) if m else None,
                nickname=nickname,
                age=age,
                gender=gender,
                role=role,
                location=_relation_location(name_line),
                url=urljoin(base_url + "/", href.lstrip("/")) if href else None,
                avatar_url=img.get("src") if img else None,
            )
        )
    return out


def _relation_location(name_line) -> str | None:
    """The location line of a list entry: the first text-only div after the name.

    The divs that follow the name are location, then a stats row of links ("32
    pics", "1 writing"), so "has no link in it" is what distinguishes them.
    """
    if name_line is None:
        return None
    for sib in name_line.find_next_siblings("div"):
        if sib.find("a"):
            continue
        text = _clean(sib.get_text(" "))
        if text:
            return text
    return None


def _parse_iso(text) -> datetime | None:
    """Parse an ISO-8601 timestamp like '2026-07-07T19:11:32.432Z' as UTC-aware."""
    if not isinstance(text, str) or not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# Activity feed, loves and comments
# --------------------------------------------------------------------------- #
# The activity feed is server-rendered: the first page is a normal HTML page and
# every later page is a Turbo Stream fetched through the lazy
# ``activity-stories-pagination-loader`` frame, whose ``src`` carries the
# ``marker`` cursor. Both formats wrap each entry in <article id="story_N">, so
# one parser reads them all. (The JSON variant these feeds used to answer now
# returns 406.)
_STORY_ID_RE = re.compile(r"^story_(\d+)$")


def _query_param(url: str | None, name: str) -> str | None:
    if not url:
        return None
    values = parse_qs(urlsplit(url).query).get(name)
    return values[0] if values else None


def _loader_param(soup: BeautifulSoup, frame_id: str, name: str) -> str | None:
    """The cursor a lazy pagination frame would fetch the next page with."""
    frame = soup.find("turbo-frame", id=frame_id)
    return _query_param(frame.get("src"), name) if frame else None


def _story_from_article(art, base_url: str) -> Story:
    m = _STORY_ID_RE.match(art.get("id", ""))
    header = art.find("header") or art
    when = header.find("time") or art.find("time")
    permalink = when.find_parent("a") if when else None
    love = art.select_one("[data-controller='story-love-button']")
    cta = art.select_one("[data-controller='comment-cta']")
    href = (permalink.get("href") if permalink else None) or (
        cta.get("href", "").split("#")[0] if cta else None
    )
    count = None
    for span in art.select("[data-comment-cta-count]"):
        count = _int(span.get_text())
        if count is not None:
            break
    return Story(
        id=m.group(1) if m else None,
        type=art.get("data-story-type"),
        uid=love.get("data-story-love-button-content-id-value") if love else None,
        kind=cta.get("data-comment-cta-target-class-value") if cta else None,
        content_id=cta.get("data-comment-cta-target-id-value") if cta else None,
        actor_id=art.get("data-story-actor-id"),
        url=urljoin(base_url + "/", href.lstrip("/")) if href else None,
        created_at=when.get("datetime") if when else None,
        loves=_int(love.get("data-story-love-button-loves-count-value")) if love else None,
        comments=count,
    )


def stories_from_activity_html(html: str, base_url: str = "") -> tuple[list[Story], str | None]:
    """Parse one page of an activity feed into ``(stories, next_marker)``.

    Works on the HTML first page and the Turbo Stream continuation pages alike.
    ``next_marker`` is None on the last page.
    """
    soup = _page_soup(html)
    stories = [
        _story_from_article(art, base_url)
        for art in soup.find_all("article", id=_STORY_ID_RE)
    ]
    return stories, _loader_param(soup, "activity-stories-pagination-loader", "marker")


def last_active_from_activity(html: str) -> datetime | None:
    """Return the newest story timestamp on an activity page, or None.

    FetLife has no explicit "last seen" field, so a member's most recent public
    activity (loves, comments, follows and posts in ``GET /<nickname>/activity``)
    is the best available signal. Members who only lurk produce no stories and
    will read as inactive.
    """
    stories, _ = stories_from_activity_html(html)
    times = [s.created() for s in stories]
    times = [t for t in times if t]
    return max(times) if times else None


def lovers_from_loves_html(html: str, base_url: str = "") -> list[Member]:
    """Members who loved a story (``GET /loves/story/<uid>?content_type=Story``).

    The grid is a list of avatars, each tagged with ``data-lover-nickname``;
    no age/location is shown, so only nickname and URL are filled in.
    """
    out: list[Member] = []
    for block in _soup(html).select("[data-lover-nickname]"):
        nickname = _clean(block.get("data-lover-nickname"))
        if not nickname:
            continue
        link = block.find("a", href=_PROFILE_HREF_RE)
        href = link.get("href") if link else f"/{nickname}"
        out.append(Member(nickname=nickname, url=urljoin(base_url + "/", href.lstrip("/"))))
    return out


def comments_from_stream(html: str, base_url: str = "") -> tuple[list[Member], str | None]:
    """Parse a page of ``GET /comments.turbo_stream?story_uid=…`` into authors.

    Returns ``(authors, next_cursor)``; one entry per comment, in feed order,
    so an author who commented twice appears twice. FetLife emits a pagination
    frame even after the last comment, so callers should stop on an empty page
    rather than on a missing cursor.
    """
    soup = _page_soup(html)
    out: list[Member] = []
    for item in soup.select("[data-comment-item-author-nickname-value]"):
        nickname = _clean(item.get("data-comment-item-author-nickname-value"))
        if not nickname:
            continue
        out.append(Member(
            id=item.get("data-comment-item-author-id-value") or None,
            nickname=nickname,
            url=urljoin(base_url + "/", nickname),
        ))
    frame = soup.find("turbo-frame", id=re.compile(r"^comments_page_"))
    cursor = _query_param(frame.get("src"), "cursor") if frame else None
    return out, cursor


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


_RELATION_FRAME_RE = re.compile(r"^profile_relation_button_(\d+)")
_FRIEND_REQUEST_HREF_RE = re.compile(r"^/requests\b")


def profile_relation_from_html(html: str) -> ProfileRelation:
    """How the viewer stands to the profile on this page, from its relation button.

    The profile header carries a ``profile_relation_button_<id>`` frame (one per
    layout) whose dropdown offers the actions still open to the viewer. An
    "Add as Friend" entry (``POST /requests?user_id=<id>``) means a request can
    be sent; its absence means they are already friends, a request is pending,
    or the profile is otherwise closed to one. The visible labels are kept so a
    skip can say why.
    """
    soup = _soup(html)
    frame = soup.find("turbo-frame", id=_RELATION_FRAME_RE)
    if frame is None:
        raise ParseError(
            "No relation button on the profile page — the profile parser in "
            "fetlife/parsers.py likely needs updating."
        )
    user_id = _RELATION_FRAME_RE.match(frame["id"]).group(1)
    request = None
    for entry in frame.select("a[data-dropdown-menu-entry-href-value]"):
        href = entry.get("data-dropdown-menu-entry-href-value", "")
        method = (entry.get("data-dropdown-menu-entry-method-value") or "").upper()
        if _FRIEND_REQUEST_HREF_RE.match(href) and method == "POST":
            request = href
            break
    labels: list[str] = []
    for node in frame.select("a, button"):
        text = _clean(node.get_text(" "))
        if text and len(text) <= 40 and text not in labels:
            labels.append(text)
    following = any(a.get("data-dropdown-menu-entry-method-value", "").upper() == "DELETE"
                    and "/follow" in a.get("data-dropdown-menu-entry-href-value", "")
                    for a in frame.select("a[data-dropdown-menu-entry-href-value]"))
    return ProfileRelation(
        user_id=user_id,
        can_friend_request=request is not None,
        request_path=request,
        following=following,
        labels=labels,
    )


def message_form_from_html(html: str) -> dict | None:
    """The hidden fields of the new-conversation form (``POST /conversations``).

    Returns None when the page carries no such form — FetLife redirects the
    compose page away when the viewer isn't allowed to message that member.
    """
    soup = _soup(html)
    form = soup.find("form", action="/conversations")
    if form is None:
        return None
    fields: dict = {}
    for tag in form.find_all("input", attrs={"type": "hidden"}):
        name, value = tag.get("name"), tag.get("value")
        if not name or not value:
            continue
        if name.endswith("[]"):
            fields.setdefault(name, [])
            if value not in fields[name]:
                fields[name].append(value)
        else:
            fields[name] = value
    return fields


def flash_from_html(html: str) -> str | None:
    """The flash notice a page shows (Rails' one-shot message), if any.

    FetLife answers a successful write with a redirect and puts the outcome
    ("Your message has been successfully sent to X") in a flash toast on the
    landing page, so this is how a caller learns what happened.
    """
    soup = _soup(html)
    for el in soup.select("[data-controller~='flash-toast'], [class~='flash'], [id^='flash']"):
        text = _clean(el.get_text(" "))
        if text and 3 < len(text) < 300:
            return text
    return None


def _base_from_url(url: str | None) -> str:
    if not url:
        return ""
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
