"""Lightweight data models returned by the client.

These are plain dataclasses so results are easy to serialize (``asdict``) for
JSON output and easy to assert against in tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Relationship:
    """A relationship between a member and another person.

    ``kind`` is "relationship" for vanilla ties (e.g. "Monogamish with") or
    "D/s" for power-exchange ties (e.g. "owned by"). ``status_with_connector``
    is the human-readable phrasing that reads naturally before ``with_nickname``
    (e.g. "babygirl of" + "Sir2BabyGirl").
    """

    kind: str = "relationship"
    status: str | None = None
    status_with_connector: str | None = None
    with_nickname: str = ""
    with_id: str | None = None
    with_url: str | None = None
    pending: bool | None = None


@dataclass
class Member:
    """A FetLife member / profile."""

    id: str | None = None
    nickname: str = ""
    age: int | None = None
    gender: str | None = None
    role: str | None = None
    orientation: str | None = None
    location: str | None = None
    url: str | None = None
    avatar_url: str | None = None
    about: str | None = None
    joined: str | None = None
    verified: bool | None = None
    relationships: list[Relationship] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class Story:
    """One entry in a member's activity feed.

    ``type`` is FetLife's story type (``picture_created``, ``post_created``,
    ``status_created``, ``loved_picture``, ``follow_created``...). Only the
    member's own posts carry a ``uid``, which is the handle the loves and
    comments endpoints take; ``kind``/``content_id`` identify the underlying
    Picture / Post / Status / Video.
    """

    id: str | None = None
    type: str | None = None
    uid: str | None = None
    kind: str | None = None
    content_id: str | None = None
    actor_id: str | None = None
    url: str | None = None
    created_at: str | None = None
    loves: int | None = None
    comments: int | None = None

    def created(self) -> "datetime | None":
        """``created_at`` as an aware UTC datetime (None if absent/unparseable)."""
        if not self.created_at:
            return None
        try:
            dt = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@dataclass
class ProfileRelation:
    """The viewer's standing with a profile, as its relation button shows it."""

    user_id: str
    can_friend_request: bool = False
    request_path: str | None = None
    following: bool = False
    # Visible button/menu labels ("Follow", "Add as Friend", "Message"...).
    labels: list[str] = field(default_factory=list)


@dataclass
class Event:
    """A FetLife event listing."""

    id: str | None = None
    name: str = ""
    start: str | None = None
    end: str | None = None
    location: str | None = None
    address: str | None = None
    cost: str | None = None
    url: str | None = None
    going_count: int | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Group:
    """A FetLife group."""

    id: str | None = None
    name: str = ""
    member_count: int | None = None
    category: str | None = None
    url: str | None = None
    meta: dict = field(default_factory=dict)
