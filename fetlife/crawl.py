"""Geo-bounded breadth-first crawl of the FetLife social graph.

Starting from a seed member, walk their friends/followers, keep only members
inside a radius of a center point, expand those, and yield each in-area member
(optionally only those in a D/s relationship). Distance is filtered on the
cheap list-item location *string* first, so a full profile is fetched only for
candidates that are actually nearby.

The crawl is pure orchestration over a :class:`~fetlife.client.FetLifeClient`
and a :class:`~fetlife.geo.Geocoder`, so it can be unit-tested with stubs.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator, Optional

from . import geo
from .exceptions import FetLifeError, RateLimitedError
from .geo import Coord

# Default location for the resumable crawl state (a temp file).
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "fetlife_discover_state.json")

# Duration units, in days, for --active-within ("1 month", "30d", "2 weeks"...).
_DURATION_UNITS = {
    "h": 1 / 24, "hour": 1 / 24, "hours": 1 / 24,
    "d": 1, "day": 1, "days": 1,
    "w": 7, "week": 7, "weeks": 7,
    "m": 30, "mo": 30, "month": 30, "months": 30,
    "y": 365, "year": 365, "years": 365,
}
_DISABLED_DURATIONS = {"any", "all", "none", "off", "0"}


def parse_duration(text: str) -> Optional[timedelta]:
    """Parse a human duration into a timedelta.

    Accepts e.g. ``"1 month"``, ``"30d"``, ``"2 weeks"``, ``"90 days"``, ``"6m"``.
    Returns None for ``any``/``all``/``none``/``off``/``0`` (filter disabled).
    """
    if text is None:
        return None
    t = text.strip().lower()
    if t in _DISABLED_DURATIONS:
        return None
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([a-z]*)", t)
    if not m:
        raise ValueError(f"Unrecognized duration: {text!r} (try '1 month', '30d', '2 weeks').")
    value, unit = float(m.group(1)), (m.group(2) or "d")
    if unit not in _DURATION_UNITS:
        raise ValueError(f"Unknown duration unit {unit!r} in {text!r}.")
    return timedelta(days=value * _DURATION_UNITS[unit])


@dataclass
class Candidate:
    """A frontier entry discovered from a friends/followers list."""

    key: str
    nickname: str
    id: Optional[str]
    url: Optional[str]
    location_str: Optional[str]


@dataclass
class Discovery:
    """One output row: a member found within the search area."""

    fet_name: str
    age: Optional[int]
    gender: Optional[str]
    role: Optional[str]
    location: Optional[str]
    ds: bool
    url: Optional[str]
    gps: Optional[str]
    location_flag: str
    last_active: Optional[str] = field(default=None)
    coord: Optional[Coord] = field(default=None)


@dataclass
class Progress:
    """Live counters passed to the optional progress callback."""

    visited: int = 0
    queued: int = 0
    found: int = 0


class CrawlState:
    """The crawl's frontier + visited set, optionally persisted to disk.

    Persisting after each visit gives two things: **cycle safety** (a member is
    never processed twice, across runs) and **resumability** (a search that is
    interrupted — Ctrl-C, `--max-visits`, a crash — can be continued later with
    ``--resume``). With ``path=None`` it lives purely in memory (used by tests).
    """

    def __init__(self, path: str | os.PathLike | None = None, params: dict | None = None):
        self.path = str(path) if path else None
        self.params = params or {}
        self.queue: deque[Candidate] = deque()
        self.visited: set[str] = set()
        self._queued: set[str] = set()  # keys currently in queue (dedup)
        # Profiles actually crawled, excluding the seed. Persisted so --max-visits
        # caps the whole search rather than resetting to 0 on every --resume.
        self.visits = 0

    @property
    def is_fresh(self) -> bool:
        return not self.visited and not self.queue

    def enqueue(self, cand: Candidate) -> None:
        """Add a candidate unless already visited or already queued (no cycles)."""
        if cand.key and cand.key not in self.visited and cand.key not in self._queued:
            self.queue.append(cand)
            self._queued.add(cand.key)

    def pop(self) -> Candidate:
        cand = self.queue.popleft()
        self._queued.discard(cand.key)
        return cand

    def mark_visited(self, key: str) -> None:
        self.visited.add(key)

    def save(self) -> None:
        """Atomically persist state (temp file + rename) so it's crash-safe."""
        if not self.path:
            return
        data = {
            "params": self.params,
            "visits": self.visits,
            "visited": sorted(self.visited),
            "queue": [asdict(c) for c in self.queue],
        }
        tmp = self.path + ".tmp"
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def remove(self) -> None:
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass

    @classmethod
    def load(cls, path: str | os.PathLike) -> "CrawlState":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        state = cls(path, data.get("params") or {})
        state.visited = set(data.get("visited") or [])
        # State written before `visits` existed: the visited set (minus the
        # seed) is the same number, so use it rather than restarting the budget.
        state.visits = data.get("visits", max(0, len(state.visited) - 1))
        for c in data.get("queue") or []:
            cand = Candidate(**c)
            state.queue.append(cand)
            state._queued.add(cand.key)
        return state

    @classmethod
    def resume_or_new(cls, path: str | os.PathLike | None, resume: bool) -> "CrawlState":
        """Load existing state when *resume* and a file exists; else start fresh."""
        if resume and path and os.path.exists(path):
            return cls.load(path)
        return cls(path)


def _candidate_from_member(m) -> Candidate:
    key = (m.id or m.nickname or "").lower()
    return Candidate(key=key, nickname=m.nickname, id=m.id, url=m.url, location_str=m.location)


def _is_ds(member) -> bool:
    return any(r.kind == "D/s" for r in getattr(member, "relationships", []))


def _format_gps(coord: Optional[Coord], flag: str) -> Optional[str]:
    if coord is None:
        return "not found" if flag == geo.NOT_FOUND else None
    text = f"{coord[0]:.4f}, {coord[1]:.4f}"
    return f"≈ {text} (state-only)" if flag == geo.STATE_ONLY else text


def _discovery(member, ds, coord, flag, last_active=None) -> Discovery:
    return Discovery(
        fet_name=member.nickname,
        age=member.age,
        gender=member.gender,
        role=member.role,
        location=member.location,
        ds=ds,
        url=member.url,
        gps=_format_gps(coord, flag),
        location_flag=flag,
        last_active=last_active.isoformat() if last_active else None,
        coord=coord,
    )


def _connections(client, ident: str, max_pages: int) -> list[Candidate]:
    """Combined friends + followers for a member, deduped, up to max_pages each."""
    seen: dict[str, Candidate] = {}
    for fetch in (client.get_friends, client.get_followers):
        for page in range(1, max_pages + 1):
            try:
                members = fetch(ident, page=page)
            except RateLimitedError:
                # These list fetches are most of the crawl's traffic, so a 429
                # here must halt it. Treating one as "end of list" (which the
                # FetLifeError branch below would do) silently truncates the
                # frontier AND keeps hammering a site that just asked us to stop.
                raise
            except FetLifeError:
                break
            if not members:
                break
            for m in members:
                cand = _candidate_from_member(m)
                if cand.key and cand.key not in seen:
                    seen[cand.key] = cand
    return list(seen.values())


def discover(
    client,
    geocoder,
    center: Coord,
    radius: float,
    units: str = "mi",
    seed: Optional[str] = None,
    ds_only: bool = False,
    active_within: Optional[timedelta] = None,
    max_visits: int = 300,
    max_pages: int = 1,
    on_progress: Optional[Callable[[Progress], None]] = None,
    now: Optional[datetime] = None,
    state: Optional[CrawlState] = None,
) -> Iterator[Discovery]:
    """Yield members within *radius* of *center*, reached by crawling the graph.

    *seed* defaults to the logged-in user. *center* is a ``(lat, lng)`` coord
    (resolve strings with the geocoder before calling). When *ds_only* is set,
    only members in a D/s relationship are yielded, but the crawl still expands
    through every in-area member. When *active_within* is set, members whose most
    recent public activity is older than that (or unknown) are not displayed —
    but are still expanded. Filtering never affects the crawl's reach.

    *state* holds the frontier + visited set; pass a persisted
    :class:`CrawlState` to make the crawl cycle-safe across runs and resumable.
    A fresh (empty) state is seeded from *seed*'s connections; a resumed one
    continues where it left off without re-seeding.

    *max_visits* is a budget for the whole search, not for one run: a resumed
    crawl counts the profiles earlier runs already visited. The frontier grows
    far faster than it drains (each in-area member adds dozens of candidates and
    consumes one), so a per-run cap would make total request volume unbounded
    across resumes — which is exactly what trips FetLife's rolling-window limit.
    """
    if state is None:
        state = CrawlState(path=None)
    cutoff = ((now or datetime.now(timezone.utc)) - active_within) if active_within else None
    progress = Progress()

    if state.is_fresh:
        seed_ident = seed or client.whoami().nickname
        state.mark_visited((seed_ident or "").lower())
        for cand in _connections(client, seed_ident, max_pages):
            state.enqueue(cand)
        state.save()
    progress.visited = state.visits  # what earlier runs already spent
    progress.queued = len(state.queue)

    def _emit_progress() -> None:
        if on_progress:
            on_progress(progress)

    _emit_progress()

    while state.queue and progress.visited < max_visits:
        cand = state.pop()
        if not cand.key or cand.key in state.visited:
            continue
        state.mark_visited(cand.key)
        state.visits += 1
        progress.visited += 1

        ident = cand.id or cand.nickname
        coord, quality = geocoder.locate(cand.location_str)
        member = None
        result: Optional[Discovery] = None

        if quality == geo.NOT_FOUND:
            # Can't place from the list string; refine using the full profile.
            member = _safe_member(client, ident)
            if member is not None:
                coord, quality = geocoder.locate_member(member)

        if quality == geo.NOT_FOUND:
            # Undisplaceable: surface it (flagged) but never expand it.
            if member is None:
                member = _safe_member(client, ident)
            if member is not None and (_is_ds(member) or not ds_only):
                ok, last = _last_active_ok(client, ident, cutoff)
                if ok:
                    result = _discovery(member, _is_ds(member), None, geo.NOT_FOUND, last)
        elif coord is not None and geo.haversine(center, coord, units) <= radius:
            # In area: display (subject to filters) and expand.
            if member is None:
                member = _safe_member(client, ident)
            if member is not None:
                ds = _is_ds(member)
                if ds or not ds_only:
                    ok, last = _last_active_ok(client, ident, cutoff)
                    if ok:
                        result = _discovery(member, ds, coord, quality, last)
            for nxt in _connections(client, ident, max_pages):
                state.enqueue(nxt)
        # else: out of area -> drop, do not expand.

        progress.queued = len(state.queue)
        state.save()  # persist AFTER expansion so a resume is consistent
        if result is not None:
            progress.found += 1
            yield result
        _emit_progress()


def _safe_member(client, ident: str):
    try:
        return client.get_member(ident)
    except RateLimitedError:
        # Don't swallow throttling — let it halt the crawl so the current
        # candidate stays on the (unsaved) frontier and is retried on resume.
        raise
    except FetLifeError:
        return None  # e.g. a deleted/unreadable profile — skip just this member


def _last_active_ok(client, ident: str, cutoff: Optional[datetime]):
    """Return ``(passes, last_active)``. Unknown activity ⇒ treated as inactive.

    Only performs the extra activity fetch when a cutoff is in effect.
    """
    if cutoff is None:
        return True, None
    try:
        last = client.get_last_active(ident)
    except RateLimitedError:
        raise  # halt for resume rather than mislabel the member inactive
    except FetLifeError:
        last = None
    if last is None:
        return False, None
    return last >= cutoff, last
