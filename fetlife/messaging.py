"""Send one message to each member in a list, carefully.

Same shape as :mod:`fetlife.friending`: every recipient is checked first (does
FetLife let this account message them?), a log means a rerun over the same
file never messages anyone twice, and the caller caps how many go out per run
and how far apart. The subject and body may use ``{nickname}``, filled in per
recipient.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from typing import Callable, Iterable, Iterator

from .exceptions import FetLifeError, NotFoundError, RateLimitedError
from .friending import FAILED, SENT, SKIPPED, WOULD_SEND, Outcome, RequestLog

DEFAULT_LOG_PATH = os.path.expanduser("~/.fetlife/messages.jsonl")
DEFAULT_LIMIT = 10
DEFAULT_PAUSE = 30.0


def render(template: str, nickname: str) -> str:
    """Fill ``{nickname}`` in; any other braces are left as written."""
    return template.replace("{nickname}", nickname)


def run(
    client,
    nicknames: Iterable[str],
    log: RequestLog,
    subject: str,
    body: str,
    limit: int = DEFAULT_LIMIT,
    pause: float = DEFAULT_PAUSE,
    dry_run: bool = False,
    resend: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> Iterator[Outcome]:
    """Yield an :class:`Outcome` per nickname, sending at most *limit* messages.

    Each member is resolved and their compose form fetched; a message goes
    out only when FetLife offers one (members who don't accept messages from
    this account are skipped). *pause* seconds separate consecutive sends.
    With *dry_run* nothing is sent and eligible members are reported as
    "would send" — they still count against *limit*, so a dry run previews
    exactly the run that would follow. Members in *log* are skipped unless
    *resend*. Throttling (HTTP 429) is raised to the caller.
    """
    sent = 0
    for nickname in nicknames:
        if sent >= limit:
            yield Outcome(nickname, SKIPPED, f"over --limit {limit}")
            continue
        when = log.sent.get(nickname.lower())
        if when and not resend:
            yield Outcome(nickname, SKIPPED, f"already messaged {when[:10]}")
            continue

        try:
            if nickname.isdigit():
                user_id = nickname
            else:
                relation, _ = client.get_profile_relation(nickname)
                user_id = relation.user_id
            form = client.get_message_form(user_id)
        except RateLimitedError:
            raise
        except NotFoundError:
            yield Outcome(nickname, SKIPPED, "profile not found")
            continue
        except FetLifeError as exc:
            yield Outcome(nickname, FAILED, f"could not read profile: {exc}")
            continue

        url = f"{client.config.base_url}/{nickname}"
        if form is None:
            yield Outcome(nickname, SKIPPED, "doesn't accept messages from this account",
                          user_id, url)
            continue

        if dry_run:
            sent += 1
            yield Outcome(nickname, WOULD_SEND, "", user_id, url)
            continue

        if sent and pause > 0:
            sleep(pause)
        try:
            confirmation = client.send_message(
                user_id, render(subject, nickname), render(body, nickname), form=form
            )
        except RateLimitedError:
            raise
        except FetLifeError as exc:
            outcome = Outcome(nickname, FAILED, str(exc), user_id, url, now().isoformat())
        else:
            sent += 1
            outcome = Outcome(nickname, SENT, confirmation, user_id, url, now().isoformat())
        log.append(outcome)
        yield outcome
