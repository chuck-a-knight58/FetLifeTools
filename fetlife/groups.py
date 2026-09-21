"""Every member of a group, as a CSV the friending and messaging commands read.

Pure orchestration over a :class:`~fetlife.client.FetLifeClient`, like
:mod:`fetlife.engagement`: the client walks the pages, this module collects
the members and writes them out.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from .exceptions import RateLimitedError
from .models import Group, Member

# ``nickname`` first, so `friend-requests` and `message --from-csv` read the
# file as-is; the rest is what the member list shows. FetLife gives no user id
# on group member lists, so that column is not offered.
MEMBERS_CSV_COLUMNS = ("nickname", "age", "gender", "role", "location", "joined", "url")


@dataclass
class Progress:
    """Passed to the ``on_progress`` callback after every page."""

    page: int
    members: int


@dataclass
class Result:
    group: Group
    members: list[Member] = field(default_factory=list)
    pages: int = 0
    # Set when the walk stopped early on FetLife's rate limit; the page to
    # rerun with ``--start-page`` to pick up where it left off.
    resume_page: int | None = None
    # The next page after the last one fetched when --max-pages cut the walk
    # short (None when the list was read to its end).
    next_page: int | None = None


def gather_members(
    fl,
    group_id: str,
    start_page: int = 1,
    max_pages: int | None = None,
    on_page: Optional[Callable[[list[Member]], None]] = None,
    on_progress: Optional[Callable[[Progress], None]] = None,
) -> Result:
    """Collect the members of *group_id*, page by page, from *start_page*.

    At most *max_pages* pages are read (all of them when ``None``). Each
    page's members go to *on_page* as soon as they arrive (so a caller can
    write them out before the next request is made), then to *on_progress*.
    A :class:`RateLimitedError` ends the walk instead of propagating: the
    members read so far are returned with ``resume_page`` set, so the caller
    can rerun later from that page.
    """
    result = Result(group=Group(id=str(group_id)))
    try:
        for page, found in fl.iter_group_member_pages(group_id, start_page):
            if not result.pages:
                result.group = found.group
            result.members.extend(found.members)
            result.pages += 1
            result.next_page = found.next_page
            if on_page:
                on_page(found.members)
            if on_progress:
                on_progress(Progress(page=page, members=len(result.members)))
            if max_pages is not None and result.pages >= max_pages:
                break
    except RateLimitedError:
        # The failed request was for start_page, or for the page after the
        # last one that came back.
        result.resume_page = result.next_page if result.pages else start_page
    return result


class MembersCsvWriter:
    """Streams members to a CSV (see MEMBERS_CSV_COLUMNS) as they arrive.

    The header goes out on construction and every :meth:`write` is committed
    to disk (flushed and fsynced) before it returns, so the file is complete
    up to the last page whatever stops the run — a rate limit, Ctrl-C, or a
    crash — and nothing already fetched has to be fetched again.
    """

    def __init__(self, fh):
        self.fh = fh
        self._writer = csv.writer(fh, lineterminator="\n")
        self._writer.writerow(MEMBERS_CSV_COLUMNS)
        self._commit()

    def write(self, members: list[Member]) -> None:
        for m in members:
            self._writer.writerow([
                m.nickname, "" if m.age is None else m.age, m.gender or "", m.role or "",
                m.location or "", m.joined or "", m.url or "",
            ])
        self._commit()

    def _commit(self) -> None:
        self.fh.flush()
        try:
            os.fsync(self.fh.fileno())
        except (OSError, ValueError, AttributeError):
            pass  # a pipe, stdout or an in-memory buffer: flushed is as far as it goes


def write_members_csv(members: list[Member], fh) -> None:
    """Write *members* as one CSV (see MEMBERS_CSV_COLUMNS), in list order."""
    MembersCsvWriter(fh).write(members)
