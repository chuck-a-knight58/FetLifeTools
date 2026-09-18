"""Who engages with a member's posts without being connected to them.

Given a member, gather their friends, followers and following; then walk the
posts they made since the last scan and collect everyone who loved or
commented on them. The people in the second set who are not in the first are
the interesting ones — an audience the member has no tie to.

Like :mod:`fetlife.crawl`, this is pure orchestration over a
:class:`~fetlife.client.FetLifeClient`, so it can be unit-tested with a stub.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

from . import crawl
from .exceptions import FetLifeError, RateLimitedError

# One small file per scanned member, next to the other ~/.fetlife state, so a
# scan of A doesn't reset the "last scan" of B.
DEFAULT_STATE_DIR = os.path.expanduser("~/.fetlife/engagement")
# Window for a member's very first scan, when there is no last-scan date yet.
DEFAULT_LOOKBACK = timedelta(days=30)

RELATION_SECTIONS = ("friends", "followers", "following")
# How each list reads from the scanned member's point of view.
_RELATION_LABELS = {"friends": "friend", "followers": "follower", "following": "following"}


@dataclass
class Engager:
    """One member who loved or commented on the scanned member's posts."""

    nickname: str
    url: Optional[str]
    id: Optional[str]
    loves: int = 0
    comments: int = 0
    # Post URLs engaged with, in feed order (newest first), each once.
    posts: list[str] = field(default_factory=list)
    connected: bool = False
    # "friend", "follower", "following" — every list they appear in, joined
    # with ", "; None when not connected.
    relation: Optional[str] = None

    @property
    def total(self) -> int:
        return self.loves + self.comments


# One row per engager. `posts` is the count; the URLs follow, space-separated,
# so a spreadsheet can still get to them without a second file.
CSV_COLUMNS = ["nickname", "loves", "comments", "posts", "connected", "relation",
               "url", "post_urls"]


def write_csv(engagers: list[Engager], fh) -> None:
    """Write *engagers* to *fh* as CSV with a header row (see CSV_COLUMNS)."""
    writer = csv.writer(fh, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for e in engagers:
        writer.writerow([
            e.nickname, e.loves, e.comments, len(e.posts),
            "yes" if e.connected else "no", e.relation or "",
            e.url or "", " ".join(e.posts),
        ])


@dataclass
class Report:
    """Everything one scan found, with the connection sets summarized."""

    target: str
    target_url: Optional[str]
    since: str
    scanned_at: str
    friends: int = 0
    followers: int = 0
    following: int = 0
    posts: int = 0
    # Posts whose loves/comments couldn't be read (deleted, restricted...).
    skipped: int = 0
    engagers: list[Engager] = field(default_factory=list)

    @property
    def strangers(self) -> list[Engager]:
        """Engagers who are neither friends, followers nor followed."""
        return [e for e in self.engagers if not e.connected]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["strangers"] = [asdict(e) for e in self.strangers]
        return data


@dataclass
class Progress:
    """Live counters passed to the optional progress callback."""

    phase: str = ""
    connections: int = 0
    posts: int = 0
    engagers: int = 0


class ScanState:
    """Remembers when a member was last scanned (``<state_dir>/<nickname>.json``)."""

    def __init__(self, state_dir: str | os.PathLike | None, nickname: str):
        self.path = (
            Path(state_dir) / f"{nickname.lower()}.json" if state_dir else None
        )
        self.last_scan: Optional[datetime] = None
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.last_scan = _parse_datetime(data.get("last_scan"))
        except (OSError, ValueError, AttributeError):
            self.last_scan = None  # unreadable -> treat as never scanned

    def record(self, report: Report) -> None:
        """Persist *report*'s scan time as the member's last scan."""
        if not self.path:
            return
        data = {
            "nickname": report.target,
            "last_scan": report.scanned_at,
            "posts": report.posts,
            "engagers": len(report.engagers),
            "strangers": len(report.strangers),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass


def _parse_datetime(text) -> Optional[datetime]:
    if not isinstance(text, str) or not text.strip():
        return None
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_since(text: str, now: Optional[datetime] = None) -> datetime:
    """Turn a ``--since`` value into an aware UTC datetime.

    Accepts an ISO date or datetime (``2026-09-01``, ``2026-09-01T12:00Z``) or
    a duration back from now (``"2 weeks"``, ``30d`` — see
    :func:`crawl.parse_duration`).
    """
    now = now or datetime.now(timezone.utc)
    try:
        dt = _parse_datetime(text)
    except ValueError:
        dt = None
    if dt is not None:
        return dt
    delta = crawl.parse_duration(text)  # raises ValueError on junk
    if delta is None:
        raise ValueError(
            f"--since needs a date or a duration, not {text!r} (try 2026-09-01 or '2 weeks')."
        )
    return now - delta


def _owner_of(url: Optional[str]) -> Optional[str]:
    """The nickname a post URL belongs to (``/<nick>/pictures/123`` -> nick)."""
    if not url:
        return None
    parts = urlsplit(url).path.strip("/").split("/")
    return parts[0].lower() if parts and parts[0] else None


def _key(member) -> str:
    return (member.nickname or "").lower()


def scan(
    client,
    target,
    since: datetime,
    on_progress: Optional[Callable[[Progress], None]] = None,
    now: Optional[datetime] = None,
) -> Report:
    """Scan one member: connections first, then the engagers on posts since *since*.

    *target* is the :class:`~fetlife.models.Member` to scan (resolve it with
    ``client.get_member`` first, so the caller has the canonical nickname for
    the state file). Members are matched by nickname (case-insensitive) because the loves grid
    exposes nothing else. The scanned member's own loves and comments on their
    posts are ignored. A post whose loves or comments can't be read is counted
    in ``Report.skipped`` and otherwise ignored; throttling (HTTP 429) is
    raised so the caller can stop and retry later.
    """
    scanned_at = now or datetime.now(timezone.utc)
    progress = Progress()

    def _emit(phase: str) -> None:
        progress.phase = phase
        if on_progress:
            on_progress(progress)

    report = Report(
        target=target.nickname,
        target_url=target.url,
        since=since.isoformat(),
        scanned_at=scanned_at.isoformat(),
    )
    target_key = _key(target)

    # 1. Connections: nickname -> the lists it appears in.
    relations: dict[str, list[str]] = {}
    for section in RELATION_SECTIONS:
        _emit(section)
        count = 0
        for member in client.iter_members(target.nickname, section):
            relations.setdefault(_key(member), []).append(_RELATION_LABELS[section])
            count += 1
            progress.connections = len(relations)
            _emit(section)
        setattr(report, section, count)

    # 2. Posts since the cutoff, and who loved / commented on each.
    engagers: dict[str, Engager] = {}

    def _note(member, post_url: str, loved: bool) -> None:
        key = _key(member)
        if not key or key == target_key:
            return
        eng = engagers.get(key)
        if eng is None:
            labels = relations.get(key)
            eng = engagers[key] = Engager(
                nickname=member.nickname, url=member.url, id=member.id,
                connected=labels is not None,
                relation=", ".join(labels) if labels else None,
            )
        if member.id and not eng.id:
            eng.id = member.id  # comments carry ids, loves don't
        if loved:
            eng.loves += 1
        else:
            eng.comments += 1
        if post_url not in eng.posts:
            eng.posts.append(post_url)

    _emit("posts")
    for story in client.iter_posts(target.nickname, since=since):
        # The feed can carry posts by others that the member shared or was
        # tagged in; only the member's own posts are theirs to be engaged with.
        if not story.uid or _owner_of(story.url) != target_key:
            continue
        report.posts += 1
        progress.posts = report.posts
        post_url = story.url or story.uid
        try:
            # Counts of 0 come straight from the feed, so those fetches are
            # skipped; an unknown count (None) is fetched to be safe.
            lovers = client.get_story_lovers(story.uid) if story.loves != 0 else []
            commenters = (
                list(client.iter_story_commenters(story.uid))
                if story.comments != 0 else []
            )
        except RateLimitedError:
            raise  # halt so the run can be retried after the window clears
        except FetLifeError:
            report.skipped += 1
            continue
        for member in lovers:
            _note(member, post_url, loved=True)
        for member in commenters:
            _note(member, post_url, loved=False)
        progress.engagers = len(engagers)
        _emit("posts")

    report.engagers = sorted(
        engagers.values(), key=lambda e: (-e.total, e.nickname.lower())
    )
    _emit("done")
    return report
