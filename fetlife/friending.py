"""Send friend requests to a list of members, carefully.

This is the one place the toolkit *writes* to FetLife, so it is built to be
hard to misuse: every member's profile is checked first and only those the
site itself offers an "Add as Friend" action for are requested; a log of every
request sent means a rerun over the same file never asks anyone twice; and
the caller caps how many go out per run and how far apart.

FetLife's own confirm on that action reads "If you are not friends with X we
recommend you first send them a message" — bulk requests to people who don't
know the account are exactly what gets it flagged, so keep runs small.

Like :mod:`fetlife.engagement`, this is orchestration over a
:class:`~fetlife.client.FetLifeClient` and testable with a stub.
"""

from __future__ import annotations

import csv
import json
import os
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional

from .exceptions import FetLifeError, NotFoundError, RateLimitedError

# Every request ever sent by this tool, one JSON object per line, so a rerun
# over the same list skips the people already asked.
DEFAULT_LOG_PATH = os.path.expanduser("~/.fetlife/friend_requests.jsonl")
DEFAULT_LIMIT = 10
DEFAULT_PAUSE = 30.0

SENT = "sent"
WOULD_SEND = "would send"
SKIPPED = "skipped"
FAILED = "failed"


@dataclass
class Outcome:
    """What happened for one nickname."""

    nickname: str
    action: str
    reason: str = ""
    user_id: Optional[str] = None
    url: Optional[str] = None
    at: Optional[str] = None


def read_nicknames(fh) -> list[str]:
    """Nicknames from a CSV: its ``nickname`` column, else its first column.

    Blank rows and repeats (case-insensitively) are dropped; order is kept.
    """
    rows = list(csv.reader(fh))
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    if "nickname" in header:
        col, body = header.index("nickname"), rows[1:]
    else:
        col, body = 0, rows  # headerless list of names
    out: list[str] = []
    seen: set[str] = set()
    for row in body:
        name = row[col].strip() if len(row) > col else ""
        if name and name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out


class RequestLog:
    """Append-only record of sent requests (``DEFAULT_LOG_PATH``)."""

    def __init__(self, path: str | os.PathLike | None):
        self.path = Path(path) if path else None
        self.sent: dict[str, str] = {}  # lowercase nickname -> when
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("action") == SENT and row.get("nickname"):
                    self.sent[row["nickname"].lower()] = row.get("at") or ""
        except OSError:
            pass

    def append(self, outcome: Outcome) -> None:
        if outcome.action == SENT:
            self.sent[outcome.nickname.lower()] = outcome.at or ""
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(outcome)) + "\n")
        except OSError:
            pass


def run(
    client,
    nicknames: Iterable[str],
    log: RequestLog,
    limit: int = DEFAULT_LIMIT,
    pause: float = DEFAULT_PAUSE,
    dry_run: bool = False,
    resend: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Iterator[Outcome]:
    """Yield an :class:`Outcome` per nickname, sending at most *limit* requests.

    Each profile is fetched and checked; a request goes out only when the page
    offers "Add as Friend". *pause* seconds separate consecutive sends (checks
    and skips don't wait). With *dry_run* nothing is sent and eligible members
    are reported as "would send" — those still count against *limit*, so a dry
    run previews exactly the run that would follow. Members in *log* are
    skipped unless *resend*. Throttling (HTTP 429) is raised to the caller.
    """
    sent = 0
    for nickname in nicknames:
        if sent >= limit:
            yield Outcome(nickname, SKIPPED, f"over --limit {limit}")
            continue
        when = log.sent.get(nickname.lower())
        if when and not resend:
            yield Outcome(nickname, SKIPPED, f"already requested {when[:10]}")
            continue

        try:
            relation, token = client.get_profile_relation(nickname)
        except RateLimitedError:
            raise
        except NotFoundError:
            yield Outcome(nickname, SKIPPED, "profile not found")
            continue
        except FetLifeError as exc:
            yield Outcome(nickname, FAILED, f"could not read profile: {exc}")
            continue

        url = f"{client.config.base_url}/{nickname}"
        if not relation.can_friend_request:
            state = ", ".join(relation.labels) or "no actions offered"
            yield Outcome(nickname, SKIPPED, f"no 'Add as Friend' ({state})",
                          relation.user_id, url)
            continue

        if dry_run:
            sent += 1
            yield Outcome(nickname, WOULD_SEND, "", relation.user_id, url)
            continue

        if sent and pause > 0:
            sleep(pause)
        try:
            after = client.send_friend_request(relation, token)
        except RateLimitedError:
            raise
        except FetLifeError as exc:
            yield Outcome(nickname, FAILED, str(exc), relation.user_id, url)
            continue
        if after is not None and after.can_friend_request:
            # The site answered but still offers the request: it didn't take.
            outcome = Outcome(nickname, FAILED, "still offers 'Add as Friend' after posting",
                              relation.user_id, url, now().isoformat())
        else:
            sent += 1
            outcome = Outcome(nickname, SENT, "", relation.user_id, url, now().isoformat())
        log.append(outcome)
        yield outcome
