"""Command-line entry point for FetLifeTools.

Run ``fetlife --help`` for the full command list.
"""

from __future__ import annotations

import dataclasses
import json
import sys

import click
from rich.console import Console
from rich.table import Table

from . import __version__, crawl, geo
from .client import FetLifeClient
from .config import Config
from .exceptions import FetLifeError, RateLimitedError

console = Console()
err_console = Console(stderr=True, style="bold red")
status_console = Console(stderr=True)


def json_option(fn):
    """Add a per-command --json/-j flag (works after the subcommand too)."""
    return click.option(
        "--json", "-j", "json_local", is_flag=True, default=False,
        help="Output raw JSON.",
    )(fn)


def _as_json(ctx: click.Context, json_local: bool) -> bool:
    """Effective JSON flag: set either globally (before the subcommand) or locally."""
    return bool(json_local or ctx.obj.get("as_json"))


def _emit(objs, as_json: bool, columns: list[str], title: str) -> None:
    """Render a list of dataclass instances as JSON or a table."""
    items = objs if isinstance(objs, list) else [objs]
    if as_json:
        payload = [dataclasses.asdict(o) for o in items]
        console.print_json(json.dumps(payload if isinstance(objs, list) else payload[0]))
        return

    if not items:
        console.print("[yellow]No results.[/yellow]")
        return

    table = Table(title=title, header_style="bold magenta")
    for col in columns:
        table.add_column(col)
    for obj in items:
        data = dataclasses.asdict(obj)
        # `is None` (not truthiness) so booleans/zero render, e.g. ds=False.
        table.add_row(*[
            "" if data.get(c) is None else str(data.get(c)) for c in columns
        ])
    console.print(table)

    # Surface any advisory note attached to a result (e.g. SPA limitation).
    for obj in items:
        note = getattr(obj, "meta", {}).get("note")
        if note:
            console.print(f"[dim]note: {note}[/dim]")


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="fetlife")
@click.option("--json", "as_json", is_flag=True, help="Output raw JSON.")
@click.option("--env-file", type=click.Path(), default=None, help="Path to a .env file.")
@click.pass_context
def cli(ctx: click.Context, as_json: bool, env_file: str | None) -> None:
    """Command-line tools for querying the FetLife website.

    Requires your own FetLife credentials via FETLIFE_USERNAME /
    FETLIFE_PASSWORD (see .env.example). Use responsibly and only for data you
    are permitted to access.
    """
    ctx.ensure_object(dict)
    ctx.obj["as_json"] = as_json
    ctx.obj["config"] = Config.from_env(env_file)


def _client(ctx: click.Context) -> FetLifeClient:
    return FetLifeClient(ctx.obj["config"])


@cli.command()
@click.pass_context
def login(ctx: click.Context) -> None:
    """Verify credentials and cache a session."""
    with _client(ctx) as fl:
        fl.login(force=True)
        console.print("[green]Login successful.[/green] Session cached.")


@cli.command()
@json_option
@click.pass_context
def whoami(ctx: click.Context, json_local: bool) -> None:
    """Show the currently logged-in user's own profile (fully populated)."""
    with _client(ctx) as fl:
        member = fl.whoami()
    _emit(
        member,
        _as_json(ctx, json_local),
        ["nickname", "id", "age", "gender", "role", "url"],
        "You",
    )


@cli.command()
@click.argument("nickname_or_id")
@json_option
@click.pass_context
def profile(ctx: click.Context, nickname_or_id: str, json_local: bool) -> None:
    """Look up a member profile by NICKNAME_OR_ID (nickname or numeric id)."""
    with _client(ctx) as fl:
        member = fl.get_member(nickname_or_id)
    _emit(
        member,
        _as_json(ctx, json_local),
        ["nickname", "id", "age", "gender", "role", "orientation", "location", "url"],
        "Profile",
    )


_MEMBER_LIST_COLUMNS = ["nickname", "age", "gender", "role", "location", "url"]


def _emit_member_list(ctx, json_local, members, title) -> None:
    _emit(members, _as_json(ctx, json_local), _MEMBER_LIST_COLUMNS, title)


@cli.command()
@click.argument("nickname_or_id")
@click.option("--page", default=1, show_default=True, help="Results page.")
@json_option
@click.pass_context
def friends(ctx: click.Context, nickname_or_id: str, page: int, json_local: bool) -> None:
    """List a member's friends (with age/gender/role/location)."""
    with _client(ctx) as fl:
        found = fl.get_friends(nickname_or_id, page=page)
    _emit_member_list(ctx, json_local, found, f"Friends of {nickname_or_id}")


@cli.command()
@click.argument("nickname_or_id")
@click.option("--page", default=1, show_default=True, help="Results page.")
@json_option
@click.pass_context
def followers(ctx: click.Context, nickname_or_id: str, page: int, json_local: bool) -> None:
    """List the members following NICKNAME_OR_ID."""
    with _client(ctx) as fl:
        found = fl.get_followers(nickname_or_id, page=page)
    _emit_member_list(ctx, json_local, found, f"Followers of {nickname_or_id}")


@cli.command()
@click.argument("nickname_or_id")
@click.option("--page", default=1, show_default=True, help="Results page.")
@json_option
@click.pass_context
def following(ctx: click.Context, nickname_or_id: str, page: int, json_local: bool) -> None:
    """List the members NICKNAME_OR_ID is following."""
    with _client(ctx) as fl:
        found = fl.get_following(nickname_or_id, page=page)
    _emit_member_list(ctx, json_local, found, f"{nickname_or_id} is following")


# (column, width) for the streamed discover table; url is appended untruncated.
_DISCOVER_STREAM_COLS = [
    ("ds", 3), ("fet_name", 18), ("age", 3), ("gender", 7), ("role", 16),
    ("location", 22), ("gps", 24), ("last_active", 10),
]


def _cell(value, width: int) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "Y" if value else "N"
    else:
        text = str(value)
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


def _stream_write(line: str) -> None:
    """Write a result line to stdout and flush immediately (as created)."""
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _discover_header() -> None:
    header = "  ".join(name.ljust(w) for name, w in _DISCOVER_STREAM_COLS) + "  url"
    _stream_write(header)
    _stream_write("-" * len(header))


def _discover_row_text(disc) -> str:
    data = dataclasses.asdict(disc)
    cells = [_cell(data.get(name), w) for name, w in _DISCOVER_STREAM_COLS]
    return "  ".join(cells) + "  " + (disc.url or "")


@cli.command()
@click.option("--center", default="Washington, NJ", show_default=True,
              help='Circle center; a place name or "lat,lng".')
@click.option("--radius", default=50.0, show_default=True, help="Circle radius.")
@click.option("--units", type=click.Choice(["mi", "km"]), default="mi", show_default=True)
@click.option("--seed", default=None,
              help="Member whose friends/followers seed the crawl (default: you).")
@click.option("--ds-only/--all", default=False, show_default=True,
              help="Restrict rows to members in a D/s relationship.")
@click.option("--active-within", default="1 month", show_default=True,
              help="Only show members active within this window "
                   "(e.g. '1 month', '2 weeks', '90d'); 'any' disables the filter.")
@click.option("--max-visits", default=300, show_default=True,
              help="Cap on profiles fetched (the crawl is slow; ~seconds/request).")
@click.option("--max-pages", default=3, show_default=True,
              help="Pages of each friends/followers list to expand.")
@click.option("--max-results", default=0, show_default=True,
              help="Stop after N displayed rows (0 = unlimited).")
@click.option("--state", "state_path", default=crawl.DEFAULT_STATE_PATH, show_default=True,
              help="File holding the resumable frontier + visited set.")
@click.option("--resume/--fresh", default=False, show_default=True,
              help="Resume a previously suspended crawl from --state (else start fresh).")
@json_option
@click.pass_context
def discover(ctx, center, radius, units, seed, ds_only, active_within, max_visits,
             max_pages, max_results, state_path, resume, json_local):
    """Find members near a location by crawling the friends/followers graph.

    Walks outward from SEED (default: you), keeping members within --radius of
    --center, and prints each match as it's found (with a `ds` Y/N column).
    Only in-area members are expanded, so pick a --seed connected to the area.
    By default only members active within the last month are shown; set
    --active-within any to include everyone (activity = public activity feed).

    Progress is persisted to --state, so a crawl stopped by Ctrl-C or --max-visits
    can be continued later with --resume (this also prevents revisiting members).
    """
    config = ctx.obj["config"]
    try:
        active_delta = crawl.parse_duration(active_within)
    except ValueError as exc:
        raise FetLifeError(str(exc))
    geocoder = geo.Geocoder(
        cache_path=config.geocode_cache_path,
        user_agent=config.geocoder_user_agent,
    )
    as_json = _as_json(ctx, json_local)

    state = crawl.CrawlState.resume_or_new(state_path, resume)
    if resume and state.is_fresh:
        raise FetLifeError(f"No resumable crawl state at {state_path!r}.")

    if state.is_fresh:
        # New search: resolve the center and stamp the crawl-defining params.
        center_coord, _ = geocoder.locate(center)
        if center_coord is None:
            raise FetLifeError(f"Could not geocode center location: {center!r}")
        state.params = {
            "center": list(center_coord), "radius": radius, "units": units,
            "seed": seed, "center_label": center,
        }
    else:
        # Resuming: continue with the original crawl geometry.
        p = state.params
        center_coord = tuple(p["center"])
        radius, units, center = p["radius"], p["units"], p.get("center_label", center)
        status_console.print(f"[dim]resuming crawl from {state_path} "
                             f"({len(state.visited)} visited, {len(state.queue)} queued)[/dim]")

    status_console.print(
        f"[dim]center {center!r} -> {center_coord[0]:.3f},{center_coord[1]:.3f} "
        f"| radius {radius}{units} | streaming results; "
        f"{config.rate_limit_min:g}-{config.rate_limit_max:g}s/request[/dim]"
    )

    # Leading "\n" ends any pending progress line (printed with end="\r") on its
    # own row, so a mid-crawl status message doesn't overwrite / mangle it.
    def on_retry(status, wait, attempt, max_attempts):
        status_console.print(
            f"\n[yellow]HTTP {status}; backing off {wait:.0f}s "
            f"(retry {attempt}/{max_attempts})…[/yellow]"
        )

    if not as_json:
        _discover_header()

    found = 0
    stopped_reason = None  # None -> ran to completion
    try:
        with _client(ctx) as fl:
            def on_progress(p: crawl.Progress) -> None:
                status_console.print(
                    f"[dim]visited {p.visited}/{max_visits} · queued {p.queued} · "
                    f"found {p.found} · requests {fl.requests}[/dim]",
                    end="\r",
                )

            fl.on_retry = on_retry
            for disc in crawl.discover(
                fl, geocoder, center_coord, radius, units=units, seed=seed,
                ds_only=ds_only, active_within=active_delta, max_visits=max_visits,
                max_pages=max_pages, on_progress=on_progress, state=state,
            ):
                if as_json:
                    _stream_write(json.dumps(dataclasses.asdict(disc)))
                else:
                    _stream_write(_discover_row_text(disc))
                found += 1
                if max_results and found >= max_results:
                    break
    except KeyboardInterrupt:
        stopped_reason = "interrupted"
    except RateLimitedError as exc:
        stopped_reason = "rate-limited"
        status_console.print(f"\n[red]{exc}[/red]")

    status_console.print()  # end the progress line
    if stopped_reason or state.queue:
        # More frontier remains -> keep the state file so it can be resumed.
        why = stopped_reason or "stopped"
        status_console.print(
            f"[dim]{why}; {len(state.queue)} still queued. Resume with: "
            f"fetlife discover --resume --state {state_path}[/dim]"
        )
    else:
        state.remove()  # frontier exhausted -> search complete
        status_console.print(f"[dim]crawl complete; {found} shown.[/dim]")


@cli.command()
@click.argument("nickname_or_id")
@json_option
@click.pass_context
def relationships(ctx: click.Context, nickname_or_id: str, json_local: bool) -> None:
    """List a member's relationships (vanilla and D/s)."""
    with _client(ctx) as fl:
        found = fl.get_relationships(nickname_or_id)
    _emit(
        found,
        _as_json(ctx, json_local),
        ["kind", "status_with_connector", "with_nickname", "with_url"],
        f"Relationships of {nickname_or_id}",
    )


@cli.command()
@click.argument("query")
@click.option("--page", default=1, show_default=True, help="Results page.")
@json_option
@click.pass_context
def search(ctx: click.Context, query: str, page: int, json_local: bool) -> None:
    """Search members matching QUERY."""
    with _client(ctx) as fl:
        members = fl.search_members(query, page=page)
    _emit(members, _as_json(ctx, json_local), ["nickname", "url"],
          f"Members matching '{query}'")


@cli.command()
@click.option("--place", "place_id", default=None, help="Scope to a place id.")
@click.option("--page", default=1, show_default=True, help="Results page.")
@json_option
@click.pass_context
def events(ctx: click.Context, place_id: str | None, page: int, json_local: bool) -> None:
    """List upcoming events."""
    with _client(ctx) as fl:
        found = fl.get_events(place_id=place_id, page=page)
    _emit(found, _as_json(ctx, json_local),
          ["id", "name", "start", "location", "url"], "Events")


@cli.command()
@click.argument("event_id")
@json_option
@click.pass_context
def event(ctx: click.Context, event_id: str, json_local: bool) -> None:
    """Show a single event by EVENT_ID."""
    with _client(ctx) as fl:
        found = fl.get_event(event_id)
    _emit(
        found,
        _as_json(ctx, json_local),
        ["name", "start", "end", "location", "address", "url"],
        "Event",
    )


@cli.command()
@click.argument("group_id")
@json_option
@click.pass_context
def group(ctx: click.Context, group_id: str, json_local: bool) -> None:
    """Show a single group by GROUP_ID."""
    with _client(ctx) as fl:
        found = fl.get_group(group_id)
    _emit(found, _as_json(ctx, json_local), ["name", "member_count", "url"], "Group")


@cli.command()
@click.argument("path")
@click.pass_context
def raw(ctx: click.Context, path: str) -> None:
    """Dump the raw HTML for an arbitrary PATH (debug parsers)."""
    with _client(ctx) as fl:
        sys.stdout.write(fl.fetch_raw(path))


def main() -> None:
    try:
        cli(obj={})
    except FetLifeError as exc:
        err_console.print(f"Error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
