"""Invite the logged-in account's connections who are in a group to an event.

UserMembers are the account's friends, followers and following; GroupMembers
are the group's members. GroupMembers outside UserMembers are the NonMembers
(reported), and those inside are the Invitees. Group member lists carry no user
ids, so the two are matched on nickname.

FetLife decides who can be invited. Its invite modal lists Friends, Followers
and past RSVPs (there is no Following tab) and says, row by row, whether it
offers an invite, so that page is what's checked before sending and what's
re-read afterwards; a rerun skips whoever has already been invited. Picks from
Friends or Followers count against the limit the modal shows. Past RSVPs don't,
so they are sent under that namespace.

Orchestration over a :class:`~fetlife.client.FetLifeClient`, like
:mod:`fetlife.friending`, and testable with a stub.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .exceptions import FetLifeError
from .friending import FAILED, SENT, SKIPPED, WOULD_SEND
from .models import Event, EventInviteForm, EventInviteRow, Group, Member

RELATIONS = ("friends", "followers", "following")


@dataclass
class Invitee:
    """A UserMember in the group, and what happened with their invitation."""

    nickname: str
    relation: str  # "friends, followers"
    action: str = ""
    reason: str = ""
    user_id: str | None = None
    url: str | None = None
    past_rsvp: bool = False


@dataclass
class Report:
    account: str
    event: Event
    form: EventInviteForm
    group: Group
    user_member_count: int = 0
    group_member_count: int = 0
    non_members: list[Member] = field(default_factory=list)
    invitees: list[Invitee] = field(default_factory=list)


def _tabs(form: EventInviteForm) -> tuple[str, ...]:
    return ("friends", "followers") + (("previously_rsvpd",) if form.has_past_rsvps else ())


def find_rows(fl, event_id: str, form: EventInviteForm, nicknames: set[str]) -> dict[str, EventInviteRow]:
    """The invite modal's rows for *nicknames* (lowercase), keyed the same way.

    Stops reading as soon as all are found, so someone the modal doesn't list
    is what costs a walk of every tab.
    """
    found: dict[str, EventInviteRow] = {}
    for tab in _tabs(form):
        for row in fl.iter_event_invite_rows(event_id, tab):
            key = row.nickname.lower()
            if key in nicknames and key not in found:
                found[key] = row
                if len(found) == len(nicknames):
                    return found
    return found


def split_members(user_members: dict[str, list[str]], group_members: list[Member],
                  account: str) -> tuple[list[Member], list[Invitee]]:
    """``(NonMembers, Invitees)``, both in group order and without *account*.

    *user_members* maps a lowercase nickname to the lists it's on.
    """
    non_members: list[Member] = []
    invitees: list[Invitee] = []
    for member in group_members:
        key = member.nickname.lower()
        if key == account.lower():
            continue
        if key in user_members:
            invitees.append(Invitee(member.nickname, ", ".join(user_members[key]), url=member.url))
        else:
            non_members.append(member)
    return non_members, invitees


def plan(invitees: list[Invitee], rows: dict[str, EventInviteRow], limit: int) -> None:
    """Set each invitee's action from what the modal offers; nothing is sent."""
    # Past RSVPs are free, so they mustn't lose out to the limit by list order.
    left = limit
    for invitee in invitees:
        row = rows.get(invitee.nickname.lower())
        if row is None:
            invitee.action = SKIPPED
            invitee.reason = "FetLife doesn't list them for invites (not a friend, follower or past RSVP)"
            continue
        invitee.user_id = row.user_id
        if not row.selectable:
            invitee.action = SKIPPED
            invitee.reason = row.status or "FetLife offers no invite"
        elif row.past_attendee:
            invitee.action, invitee.past_rsvp = WOULD_SEND, True
            invitee.reason = "past RSVP; not counted against the limit"
        elif left > 0:
            left -= 1
            invitee.action = WOULD_SEND
        else:
            invitee.action = SKIPPED
            invitee.reason = f"over FetLife's invite limit ({limit} left)"


def prepare(fl, event_url: str, group_id: str,
            on_status: Callable[[str], None] = lambda _: None) -> Report:
    """Read everything and plan the invitations. Sends nothing.

    Throttling is raised to the caller, not swallowed: partial lists would put
    connections among the NonMembers and leave invitees out.
    """
    me = fl.whoami()
    event = fl.get_event_by_url(event_url)
    form = fl.get_event_invite_form(event.id)
    if form is None:
        raise FetLifeError(f"{me.nickname} can't send invitations to “{event.name}”: FetLife "
                           "offers that account no invite form. Use --env-file to pick the "
                           "account that can.")

    user_members: dict[str, list[str]] = {}
    for relation in RELATIONS:
        count = 0
        for member in fl.iter_members(me.nickname, relation):
            user_members.setdefault(member.nickname.lower(), []).append(relation)
            count += 1
        on_status(f"{relation}: {count}")

    group = Group(id=str(group_id))
    group_members: list[Member] = []
    for page, found in fl.iter_group_member_pages(group_id):
        if page == 1:
            group = found.group
        group_members.extend(found.members)
    if not group_members:
        raise FetLifeError(f"Read no members from group {group_id}; "
                           f"can {me.nickname} see its member list?")
    on_status(f"group members: {len(group_members)}")

    non_members, invitees = split_members(user_members, group_members, me.nickname or "")
    # Read last: the walks above take minutes, and the limit can change meanwhile.
    form = fl.get_event_invite_form(event.id) or form
    rows = find_rows(fl, event.id, form, {i.nickname.lower() for i in invitees})
    plan(invitees, rows, form.limit)
    return Report(me.nickname, event, form, group, len(user_members), len(group_members),
                  non_members, invitees)


def send(fl, report: Report) -> FetLifeError | None:
    """Send the planned invitations in one submit, then confirm against the modal.

    Returns the error that stopped the submit (each invitee is marked failed),
    or None. A failed check afterwards doesn't undo a submit FetLife accepted,
    so it only leaves those invitees marked sent but unconfirmed.
    """
    chosen = [i for i in report.invitees if i.action == WOULD_SEND]
    if not chosen:
        return None
    try:
        fl.send_event_invites(report.form,
                              [i.user_id for i in chosen if i.past_rsvp],
                              [i.user_id for i in chosen if not i.past_rsvp])
    except FetLifeError as exc:
        for invitee in chosen:
            invitee.action, invitee.reason = FAILED, str(exc)
        return exc

    unchecked = None
    try:
        rows = find_rows(fl, report.event.id, report.form, {i.nickname.lower() for i in chosen})
    except FetLifeError as exc:
        rows, unchecked = {}, exc
    for invitee in chosen:
        row = rows.get(invitee.nickname.lower())
        if unchecked is not None:
            invitee.action, invitee.reason = SENT, f"accepted, but not confirmed: {unchecked}"
        elif row is not None and row.selectable:
            invitee.action, invitee.reason = FAILED, "FetLife still offers an invite after the submit"
        else:
            invitee.action, invitee.reason = SENT, (row.status if row and row.status else "")
    return None
