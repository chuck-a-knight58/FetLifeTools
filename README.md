# FetLifeTools

Command-line tools for querying [FetLife](https://fetlife.com) from Python.

FetLife does not publish an official API, so these tools authenticate as a normal user (with **your own** credentials) and parse the same server-rendered pages a browser would. Treat this as a personal automation helper.

FetLife sits behind Cloudflare, which blocks plain HTTP clients. To get through, the client uses [`curl_cffi`](https://github.com/yifeikong/curl_cffi) to impersonate a real browser's TLS fingerprint (`FETLIFE_IMPERSONATE`, default `chrome`). If logins start failing with a Cloudflare block page, try a different impersonation profile.

> **Use responsibly.** Only access data your account is permitted to see, keep the rate limit polite, and review FetLife's Terms of Use before automating against the site. This project stores no credentials in code and ships no account.

## Features

- Persistent, rate-limited authenticated session (login handled once, cookies cached).
- `whoami` — your own profile, fully populated (id, age, gender, role, orientation, avatar).
- `profile` — look up any member by nickname or id (full data via the JSON API).
- `friends` — list a member's friends, with age/gender/role/location.
- `relationships` — a member's vanilla and D/s relationships (and who they're with).
- `followers` / `following` — who follows a member, and who they follow.
- `discover` — crawl the friends/followers graph to find members near a location (D/s flag + activity filter).
- `engagement` — who loves/comments on a member's posts without being a friend, follower or followed.
- `search` — keyword member search _(experimental — see notes)_.
- `events` / `event` — list events or fetch one by id _(experimental — partial data)_.
- `group` — fetch a group by id (name + member count).
- `raw` — dump raw HTML of any path (for inspecting/updating parsers).
- JSON output on every command (`--json`) for piping into `jq` or scripts.

## Install

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"      # or: pip install -r requirements.txt
```

## Configure

```bash
cp .env.example .env
# edit .env with your FETLIFE_USERNAME and FETLIFE_PASSWORD
```

Configuration is read from environment variables (a `.env` file is loaded automatically). `.env` is git-ignored.

| Variable | Default | Purpose |
|---|---|---|
| `FETLIFE_USERNAME` | — | Your nickname or email |
| `FETLIFE_PASSWORD` | — | Your password |
| `FETLIFE_RATE_LIMIT_MIN` | `2.0` | Lower bound (seconds) for the random per-request delay |
| `FETLIFE_RATE_LIMIT_MAX` | `5.0` | Upper bound; each request waits a random interval in `[min, max]` |
| `FETLIFE_RATE_LIMIT` | _(unset)_ | Legacy single value — used as both bounds if the min/max aren't set |
| `FETLIFE_MAX_RETRIES` | `4` | Retries on HTTP 429/5xx before failing |
| `FETLIFE_RETRY_BACKOFF` | `5.0` | Base seconds for exponential backoff (honors `Retry-After`) |
| `FETLIFE_SESSION_PATH` | `~/.fetlife/session.cookies` | Cached session cookies |
| `FETLIFE_THROTTLE_STATE_PATH` | `~/.fetlife/throttle.json` | Remembers the delay learned from past 429s (see [Rate limiting](#rate-limiting)) |
| `FETLIFE_IMPERSONATE` | `chrome` | Browser profile curl_cffi impersonates (Cloudflare) |
| `FETLIFE_USER_AGENT` | _(unset)_ | Override the UA — leave unset; a custom UA re-triggers Cloudflare |
| `FETLIFE_GEOCODE_CACHE_PATH` | `~/.fetlife/geocode.json` | Cache for `discover`'s geocoding |
| `FETLIFE_GEOCODER_USER_AGENT` | project default | UA sent to Nominatim (`discover`) |

## Usage

```bash
fetlife login                       # verify credentials, cache a session
fetlife whoami                      # your own profile, fully populated
fetlife profile JohnDoe             # any member, by nickname
fetlife profile 12345               # by numeric id
fetlife friends JohnDoe             # a member's friends list
fetlife relationships JohnDoe       # vanilla + D/s relationships
fetlife followers JohnDoe           # who follows them
fetlife following JohnDoe           # who they follow
fetlife discover --seed JohnDoe --center "Washington, NJ" --radius 50 --ds-only
fetlife discover --seed JohnDoe --active-within "2 weeks"   # activity filter (default 1 month)
fetlife engagement JohnDoe          # engagers on their posts since the last scan who aren't connected
fetlife engagement JohnDoe --since "2 weeks" --all   # every engager, with how they're connected
fetlife search "rope portland"
fetlife events --place 123
fetlife event 5551234
fetlife group 88 --json | jq .
fetlife raw /home > home.html       # inspect live markup
```

Also runnable as a module: `python -m fetlife --help`.

## Command reference

Global options (before the subcommand):

| Option | Purpose |
|---|---|
| `--json` | Emit JSON instead of a table (also available per-command as `-j`). |
| `--env-file PATH` | Load credentials/settings from a specific `.env` file. |
| `--version` | Print the version. |
| `-h`, `--help` | Help for the CLI or any subcommand (`fetlife <cmd> --help`). |

Every command accepts `--json` (or `-j` after the subcommand). Table output uses [rich](https://github.com/Textualize/rich); JSON output is a list of objects (or a single object for one-item results) suitable for `jq`. Outputs below are **illustrative** (nicknames/values are examples).

> **Command status.** `whoami`, `profile`, `friends`, `relationships`, `followers`, `following`, `discover`, `engagement`, `group`, `login`, and `raw` use FetLife's JSON API (or stable server-rendered fields) and return full data. `search`, `events`, and `event` are **experimental** — see notes on each; they were scaffolded against older markup and are awaiting the JSON endpoints the SPA now uses.

### `login`

Authenticate and cache a session cookie so later commands don't re-login.

```bash
fetlife login
```
```
Login successful. Session cached.
```

Runs the CSRF-token login flow and stores cookies at `FETLIFE_SESSION_PATH`. On bad credentials it surfaces FetLife's own message, e.g. `Error: Login failed. FetLife said: "…Nickname, Email or Password is incorrect…"`.

### `whoami`

Your own profile (read from the `window.FL.user` blob FetLife embeds on every page).

```bash
fetlife whoami
```
```
                              You
┏━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━┓
┃ nickname  ┃ id       ┃ age ┃ gender ┃ role     ┃ url                   ┃
┡━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━┩
│ YourNick  │ 15758532 │ 48  │ Male   │ Dominant │ https://fetlife.com/… │
└───────────┴──────────┴─────┴────────┴──────────┴───────────────────────┘
```

`whoami --json` includes orientation, verification, supporter status, and avatar URL.

### `profile`

Look up any member by nickname or numeric id (full data via the JSON API).

```bash
fetlife profile JohnDoe          # by nickname
fetlife profile 12345            # by numeric id
fetlife profile JohnDoe --json
```
```
                                   Profile
┏━━━━━━━━━━┳━━━━━━━━━━┳━━━━━┳━━━━━━━━┳━━━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━┓
┃ nickname ┃ id       ┃ age ┃ gender ┃ role       ┃ orientation ┃ location       ┃ … ┃
┡━━━━━━━━━━╇━━━━━━━━━━╇━━━━━╇━━━━━━━━╇━━━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━┩
│ JohnDoe  │ 12345    │ 42  │ Male   │ Dominant   │ Straight    │ Trenton, NJ,   │ … │
│          │          │     │        │            │             │ United States  │   │
└──────────┴──────────┴─────┴────────┴────────────┴─────────────┴────────────────┴───┘
```

`--json` adds `about`, `joined`, `verified`, and the full `relationships` list:

```jsonc
{
  "id": "12345", "nickname": "JohnDoe", "age": 42, "gender": "Male",
  "role": "Dominant", "orientation": "Straight",
  "location": "Trenton, NJ, United States",
  "url": "https://fetlife.com/JohnDoe",
  "about": "Longtime rope top…", "joined": "2019-03-01T12:00:00.000Z",
  "verified": true,
  "relationships": [
    {"kind": "D/s", "status_with_connector": "owner of", "with_nickname": "RopeBunny",
     "with_url": "https://fetlife.com/RopeBunny", "pending": false}
  ]
}
```

### `friends`

List a member's friends. Entries include age/gender/role/location inline.

```bash
fetlife friends JohnDoe
fetlife friends JohnDoe --page 2      # paginate
```
```
                          Friends of JohnDoe
┏━━━━━━━━━━━━━┳━━━━━┳━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ nickname    ┃ age ┃ gender ┃ role     ┃ location       ┃ url                 ┃
┡━━━━━━━━━━━━━╇━━━━━╇━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ RopeBunny   │ 33  │ Woman  │ submiss… │ Newark, NJ     │ https://fetlife.co… │
│ SwitchKate  │ 29  │ Woman  │ Switch   │ Philadelphia   │ https://fetlife.co… │
└─────────────┴─────┴────────┴──────────┴────────────────┴─────────────────────┘
```

### `relationships`

A member's vanilla and D/s relationships, and who each is with.

```bash
fetlife relationships JohnDoe
```
```
                     Relationships of JohnDoe
┏━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━┓
┃ kind         ┃ status_with_connec… ┃ with_nickname ┃ with_url            ┃
┡━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━┩
│ relationship │ Monogamish with     │ SwitchKate    │ https://fetlife.co… │
│ D/s          │ owner of            │ RopeBunny     │ https://fetlife.co… │
└──────────────┴─────────────────────┴───────────────┴─────────────────────┘
```

`kind` is `relationship` (vanilla) or `D/s` (power-exchange). These are also embedded in
`profile --json` under `relationships`.

### `followers` / `following`

Who follows a member, and who they follow. Same columns and `--page` as `friends`.

```bash
fetlife followers JohnDoe
fetlife following JohnDoe --json
```
```
                        Followers of JohnDoe
┏━━━━━━━━━━━━━┳━━━━━┳━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ nickname    ┃ age ┃ gender ┃ role          ┃ location      ┃ url          ┃
┡━━━━━━━━━━━━━╇━━━━━╇━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ boondock42  │ 42  │ Male   │ Switch        │ Fairfax, VA   │ https://fet… │
└─────────────┴─────┴────────┴───────────────┴───────────────┴──────────────┘
```

### `discover`

Find members near a location by crawling the friends/followers graph, with distance, D/s, and activity filters. **See the dedicated [`discover` section](#discover--find-members-near-a-location) below for full details.**

```bash
fetlife discover --seed JohnDoe --center "Washington, NJ" --radius 50 --ds-only
```

### `engagement`

Who engages with a member's posts without being connected to them. **See the dedicated [`engagement` section](#engagement--who-engages-without-being-connected) below.**

```bash
fetlife engagement JohnDoe --since "2 weeks"
```

### `group`

Fetch a group by numeric id (name + member count).

```bash
fetlife group 88
```
```
                         Group
┏━━━━━━━━━━┳━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ name     ┃ member_count ┃ url                           ┃
┡━━━━━━━━━━╇━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Profania │ 112          │ https://fetlife.com/groups/88 │
└──────────┴──────────────┴───────────────────────────────┘
```

### `search` (experimental)

Keyword member search.

```bash
fetlife search "rope"
```

> **Status:** currently unreliable — the HTML search parser returns page navigation links (Home, Places, …) rather than members. FetLife's search is now SPA-rendered; this command needs its JSON endpoint wired in (the same treatment `profile`/`friends` already got).

### `events` / `event` (experimental)

List upcoming events, or fetch one by id.

```bash
fetlife events
fetlife events --place 123      # scope to a place id
fetlife event 5551234
```
```
                                Events
┏━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ id   ┃ name                     ┃ start ┃ location ┃ url                     ┃
┡━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ 2026 │ Munch Night — Trenton    │       │          │ https://fetlife.com/ev… │
└──────┴──────────────────────────┴───────┴──────────┴─────────────────────────┘
```

> **Status:** event **names and URLs** come through, but `start`/`end`/`location`/`address` are blank and the list `id` is unreliable (it picks the year out of the date-based URL), because event pages are SPA-rendered. Use the `url` to open an event; treat the other fields as best-effort until the JSON endpoints are wired in.

### `raw`

Dump the raw HTML (or JSON) FetLife returns for any path — the tool for discovering/updating parsers and endpoints.

```bash
fetlife raw /home > home.html
fetlife raw /JohnDoe/friends > friends.html
```

Prints the response body to stdout. Combine with `Accept: application/json` discovery in `client.py` to find new endpoints (see [How profiles are fetched](#how-profiles-are-fetched-the-json-api)).

## How it works

```
fetlife/
  cli.py        Click CLI — thin command layer, JSON/table rendering
  client.py     FetLifeClient — session, login (CSRF), rate limiting, queries
  parsers.py    All HTML/JSON parsing (the part that changes when FetLife does)
  crawl.py      Geo-bounded BFS over friends/followers (the `discover` command)
  engagement.py Connections vs. post engagers (the `engagement` command)
  geo.py        Haversine + Nominatim geocoder (cached)
  models.py     Member / Relationship / Event / Group dataclasses
  config.py     Env/.env credential + settings loading
  exceptions.py Typed error hierarchy
```

Because most pages require login, the parsers can't be validated against live HTML without an account. They're written defensively (structured data first, heuristic fallbacks) and isolated so that **if a command returns empty or partial results, `fetlife raw <path>` shows you the current markup** and you adjust the relevant function in `parsers.py`.

### How profiles are fetched (the JSON API)

FetLife's profiles are a **client-side-rendered single-page app**: the server returns an HTML shell and the data is loaded afterward by JavaScript. Rather than run a headless browser, this toolkit calls **the same JSON endpoints the SPA itself uses** — the canonical page URL with an `Accept: application/json`
header:

| Data | Endpoint | Client method |
|---|---|---|
| Profile card | `GET /<nickname>` → `{core, currentUserRelation}` | `get_member` / `profile` |
| Relationships | `GET /<nickname>` → `core.relationships` + `core.dsRelationships` | `get_relationships` / `relationships` |
| Friends | `GET /<nickname>/friends?page=N` **(HTML)**, 20 per page | `get_friends` / `friends`; `iter_members` walks every page |
| Followers / following | `GET /<nickname>/{followers,following}?page=N` **(HTML)** | `get_followers` `get_following` / `followers` `following` |
| Activity feed | `GET /<nickname>/activity[/<tab>]` **(HTML)**, then `…/activity[/<tab>].turbo_stream?marker=M` for each later page | `get_activity` / `iter_posts` (the `all-posts` tab) |
| Last-active | newest `<time datetime>` on `GET /<nickname>/activity` | `get_last_active` |
| Loves on a post | `GET /loves/story/<uid>?content_type=Story` | `get_story_lovers` |
| Comments on a post | `GET /comments.turbo_stream?order=oldest&story_uid=<uid>[&cursor=C]` | `iter_story_commenters` |
| Pictures | `GET /<nickname>/pictures` | *(easy to add)* |

The relation lists and the activity feed are the exceptions: FetLife answers the JSON variant of those with a 404/406 (for any member, your own profile included), so they are read from the server-rendered page — `members_from_relations_html` and `stories_from_activity_html`. The markup carries the same data the JSON did, at the same one request per page. The feed, loves and comments are all [Hotwire](https://hotwired.dev) pages: the first page is plain HTML and each later page is the Turbo Stream that the page's lazy pagination `<turbo-frame>` would load, addressed by the `marker`/`cursor` in that frame's `src`. Every post in the feed carries a **story uid** (on its love button) that keys the loves and comments endpoints.

These are **plain cookie-authenticated GETs** — no CSRF token, no request signing — so they replay directly from our `curl_cffi` session (which also clears Cloudflare). This is why full data is available for *any* member, not just the logged-in viewer. See `get_json` in `client.py`; adding the remaining endpoints is a few lines each following `get_friends`.

`whoami` is the one thing that still reads the server-embedded
`window.FL.user` blob, since it needs no nickname to identify "you".

## `discover` — find members near a location

FetLife has no radius/location search, so `discover` builds one by **breadth-first crawling the friends/followers graph** and filtering the people it finds by distance, relationship type, and recency of activity.

```bash
fetlife discover [OPTIONS]
```

### How it works

Starting from a **seed** member (default: you), the crawl repeatedly:

1. Pulls the seed's **friends + followers** — each list entry already includes a location *string* (e.g. `"Carlisle, Pennsylvania"`).
2. **Geocodes** that string and measures the distance from `--center`.
3. If the member is **inside `--radius`**, it fetches their full profile (for the D/s relationship data), prints a row, and **expands** them — adding *their* friends and followers to the queue. Members **outside** the radius are dropped and never expanded.
4. Repeats until the queue is empty or `--max-visits` is reached.

Because only in-area members are expanded, the crawl stays within the local cluster reachable from your seed. **Pick a `--seed` who is actually connected to the target area** — seeding from someone with no ties there yields nothing. (Trade-off: an in-area member only reachable through an out-of-area "bridge" person can be missed.)

Distance is checked on the cheap list *string* first, so a full profile is fetched only for candidates that are actually nearby.

### Options

| Option | Default | Description |
|---|---|---|
| `--center TEXT` | `"Washington, NJ"` | Circle center. A place name (geocoded) **or** a `"lat,lng"` literal (used as-is, no geocoding). |
| `--radius FLOAT` | `50` | Circle radius, in `--units`. |
| `--units [mi\|km]` | `mi` | Distance units for `--radius`. |
| `--seed TEXT` | you (`whoami`) | Member whose friends/followers seed the crawl. |
| `--ds-only / --all` | `--all` | `--ds-only` shows only members with a D/s relationship. Default `--all` shows everyone in-area (the `ds` column marks who is D/s). Never changes what gets expanded. |
| `--active-within TEXT` | `"1 month"` | Hide members whose most recent activity is older than this. Accepts `"1 month"`, `"2 weeks"`, `"90 days"`, `30d`, `6m`, `1y`, `48h`, …; `any`/`none`/`0`/`off` disables the filter. |
| `--max-visits INTEGER` | `300` | Hard cap on profiles fetched **for the whole search**, including profiles visited by earlier `--resume` runs. |
| `--max-pages INTEGER` | `1` | Pages of each friends/followers list to expand (each page = 20 members). Each page costs one request *per list*, so raising this multiplies request volume — and your odds of being rate-limited. |
| `--max-results INTEGER` | `0` | Stop after N displayed rows (`0` = unlimited). |
| `--state PATH` | `~/.fetlife/discover_state.json` | Where the resumable frontier + visited set is stored. |
| `--resume / --fresh` | `--fresh` | `--resume` continues a suspended crawl from `--state`; `--fresh` (default) starts over. |
| `--cooldown FLOAT` | `3.0` | Refuse to start within this many hours of the last HTTP 429. `0` disables the check. |
| `-j, --json` | off | Stream JSON Lines (one object per line) instead of a text table. |

### Streaming & resume

Results are **streamed as they're found** — each match is printed and flushed immediately (a text row, or a JSON-Lines object with `-j`), rather than buffered into a table at the end. So `discover` is safe to pipe or tee for long runs:

```bash
fetlife discover --seed JohnDoe --json | tee found.jsonl | jq -c '{fet_name, gps}'
```

The crawl's **frontier and visited set are persisted** to `--state` (`~/.fetlife/discover_state.json` by default) after every visit. This gives two things:

- **Cycle safety** — a member is never processed twice, even across runs.
- **Resume** — a crawl stopped by `Ctrl-C`, `--max-visits`, or a crash can be continued:

  ```bash
  fetlife discover --seed JohnDoe --radius 40 --max-visits 50      # first 50 profiles
  fetlife discover --resume --max-visits 150                       # 50 more, and so on
  ```

  `--max-visits` is a budget for the **entire search**, not per run — the count is stored
  in the state file, so each `--resume` must raise it to buy more visits. (The frontier
  grows far faster than it drains: every in-area member adds dozens of candidates and
  consumes one. A per-run cap would make total request volume unbounded across resumes,
  which is exactly what trips FetLife's rate limit.)

  On resume the original geometry (`--center`/`--radius`/`--units`) is read back from the
  state file. When the frontier is exhausted the state file is removed (search complete);
  otherwise it's kept so you can `--resume`.

### Output columns

| Column | Meaning |
|---|---|
| `fet_name` | Member nickname. |
| `age` | Age. |
| `gender` | Gender (FetLife's label, e.g. `Woman`, `Male`). |
| `role` | Role(s), e.g. `Dominant`, `submissive`, `Switch`. |
| `location` | The member's location string. |
| `ds` | **Boolean** — `True` if they have any D/s relationship, else `False`. |
| `url` | Full profile URL. |
| `gps` | `"lat, lng"`. Doubles as the location-confidence flag (see below). |
| `last_active` | ISO timestamp of their most recent public activity, or blank if unknown. |

**Location confidence** is surfaced in the `gps` column (and as a `location_flag` field in `--json`):

- `40.20, -77.19` — geocoded to a city/town; distance is reliable.
- `≈ 41.20, -77.19 (state-only)` — only a state/region resolved, so the distance is a
  centroid **approximation** (included if the centroid is within radius).
- `not found` — could not be geocoded. Shown flagged (never silently dropped), but **not**
  distance-filtered and **not** expanded.

### Geocoding

Location strings are resolved with OpenStreetMap **Nominatim** and cached to `~/.fetlife/geocode.json` (`FETLIFE_GEOCODE_CACHE_PATH`), so repeated runs don't re-query the same places. Only generic place-name strings are sent — never member identities. Nominatim asks for a descriptive User-Agent; set `FETLIFE_GEOCODER_USER_AGENT` to your own.

### The activity filter

"Last active" is inferred from the newest story timestamp on `GET /<nickname>/activity` —
FetLife exposes no explicit "last seen" field. Consequently a member who only **lurks** (logs in but never posts or reacts publicly) has no stories and reads as *inactive*, so they're hidden by default. Members with unknown activity are also hidden. Use
`--active-within any` to switch the filter off entirely.

The filter only affects **display** — 
Hidden members are still expanded, so filtering never shrinks the crawl's reach. It adds one activity request per displayed candidate, so `--active-within any` also makes large crawls cheaper.

### Performance & cost

At the default 2s rate limit, a crawl of a few hundred members takes **tens of minutes**.
To keep runs bounded and fast:

- Lower the per-request delay for a sweep: `FETLIFE_RATE_LIMIT_MIN=0.5 FETLIFE_RATE_LIMIT_MAX=1.5 fetlife discover …`.
- Cap the work with `--max-visits` and `--max-pages`.
- Use a tight `--radius` and a well-connected `--seed`.

### Rate limiting

A single in-area member costs **up to 4 requests** at `--max-pages 1` (profile, activity
feed, friends page, followers page) and 2 more for every extra page of each list.
Out-of-area members cost nothing — they're rejected on the list *string* alone.

If FetLife throttles you (HTTP 429), three things happen:

1. The request **retries with exponential backoff** (`FETLIFE_MAX_RETRIES` /
   `FETLIFE_RETRY_BACKOFF`, honoring `Retry-After`).
2. The client then **stays slower for the rest of the run** — every 429 doubles the
   inter-request delay (up to 8×), and it only eases back down after 25 consecutive
   clean requests. A short backoff alone isn't enough: the limit is a *rolling window*,
   so resuming at the old cadence just re-trips it a few requests later.
3. If it's *still* throttling after the retries, the crawl **stops without losing
   progress** — the member being fetched stays on the frontier — so wait and `--resume`.

**Widening `FETLIFE_RATE_LIMIT_MIN`/`MAX` is usually the wrong lever.** It changes the
spacing between requests, not how many you make; if the cap is per hour or per day,
a slower crawl reaches it just as surely, only later. Reach for `--max-pages 1`, a tighter
`--radius`, `--active-within any` (drops one request per displayed member), and a lower
`--max-visits` instead — those reduce the actual request count.

#### Throttle memory

The learned delay outlives the process, in `~/.fetlife/throttle.json`
(`FETLIFE_THROTTLE_STATE_PATH`), because FetLife's limit outlives it too — otherwise
every run rediscovers the wall by walking into it. On start-up:

- A record older than 24h is discarded — one bad afternoon shouldn't slow you forever.
- Otherwise the client resumes at **half** the remembered factor: fast enough to benefit
  if the window has cleared, cautious enough not to re-trip it immediately (and if it is
  still blocked, the first 429 puts the factor straight back).
- `discover` refuses to start within `--cooldown` hours (default 3) of the last 429,
  before sending a single request. `--cooldown 0` overrides it.

#### Exit codes

| Code | Meaning |
|---|---|
| `0` | Ran to completion (frontier exhausted, or stopped cleanly at `--max-results`). |
| `75` | Rate-limited or inside `--cooldown` — **retryable**; state is intact, resume later. |
| `1` | A real error (bad credentials, unparseable page, bad arguments). |
| `130` | Interrupted with `Ctrl-C`. |

`75` is `EX_TEMPFAIL` from `sysexits(3)`, so a wrapper can tell a temporary block apart
from a genuine failure. [`go.sh`](go.sh) does exactly that — it resumes the crawl, sleeps
`RETRY_HOURS` (default 4) whenever it sees `75`, and gives up on anything else:

```bash
./go.sh                                    # up to 6 attempts, 4h apart
RETRY_HOURS=8 MAX_ATTEMPTS=3 ./go.sh       # more patient, fewer tries
```

Keep `RETRY_HOURS` at or above `--cooldown`, or the next attempt is refused by the
cooldown guard before it reaches the network.

Live progress (`visited / queued / found`) streams to **stderr**; the results stream to
**stdout**, so you can redirect just the data cleanly: `fetlife discover … --json > out.jsonl`
(progress still shows on the terminal).

### Examples

```bash
# Default: within 50 mi of Washington, NJ, seeded from you, active in the last month
fetlife discover

# D/s members within 40 mi of a seed's area, active in the last two weeks
fetlife discover --seed VirginiaSunshine --center "Lancaster, PA" \
  --radius 40 --ds-only --active-within "2 weeks"

# Chunked crawl you can stop and continue: 50 profiles at a time
# (--max-visits is the running total, so raise it on each resume)
fetlife discover --seed JohnDoe --radius 40 --max-visits 50
fetlife discover --resume --max-visits 100

# Everyone within 30 km of explicit coordinates, ignore activity, JSONL to a file
fetlife discover --center "40.759,-74.979" --units km --radius 30 \
  --active-within any --json > nearby.jsonl

# Fast, tightly-bounded sweep
FETLIFE_RATE_LIMIT_MIN=0.5 FETLIFE_RATE_LIMIT_MAX=1.5 \
  fetlife discover --seed JohnDoe --radius 25 --max-visits 100 --max-pages 1

# My example
fetlife discover --seed Knight_of_Xanadu --center "Washington, NJ" --radius 100 --ds-only --active-within any
center 'Washington, NJ' -> 40.758,-74.979 | radius 1000.0mi | streaming results; ~2s/request

ds   fet_name            age  gender   role              location                gps                       last_active  url
---------------------------------------------------------------------------------------------------------------------------
Y    Sestra_Chaos        55   Woman    Clown, Goddess,…  New Jersey, United St…  ≈ 40.0757, -74.4042 (st…  2026-07-2…  https://fetlife.com/Sestra_Chaos
Y    curious_kitti       45   Female   Lioness, Dom-le…  Ringwood, New Jersey,…  41.1134, -74.2454         2026-07-2…  https://fetlife.com/curious_kitti
Y    FreeSpiritNJ42      103  Male     submissive        Summit, New Jersey, U…  40.7182, -74.3592         2026-07-1…  https://fetlife.com/FreeSpiritNJ42
Y    MsDenisse           45   Female   Mistress, Ma'am   Bridgewater Township,…  40.5931, -74.6050         2026-07-2…  https://fetlife.com/MsDenisse
Y    Omaira              51   Female   property, slave…  Baltimore, Maryland, …  39.2909, -76.6108         2026-07-1…  https://fetlife.com/Omaira
Y    ManicMethod         44   Male     Master, Exhibit…  Ellicott City, Maryla…  39.2757, -76.8317         2026-07-0…  https://fetlife.com/ManicMethod
Y    LadyElainaNY        106  Female   Master            Beverly, New Jersey, …  40.0654, -74.9191         2026-07-2…  https://fetlife.com/LadyElainaNY
visited 7/300 · queued 345 · found 7

```

`--json` streams **JSON Lines** — one object per line with all columns plus `location_flag` and the raw `coord` array. Pipe into `jq` (each line is a standalone object):

```bash
fetlife discover --seed JohnDoe --ds-only --json | jq -c '{fet_name, gps, last_active}'
```

## `engagement` — who engages without being connected

Given a member, `engagement` answers: *who loves or comments on their posts but isn't a friend, a follower, or someone they follow?*

```bash
fetlife engagement [OPTIONS] NICKNAME_OR_ID
```

### How it works

1. **Connections.** Pulls the member's complete **friends**, **followers** and **following** lists (every page).
2. **Posts.** Walks the member's *All Posts* feed — pictures, writings, statuses, videos — newest first, stopping at the first post older than `--since`. Posts by other members that show up in the feed (shares, tags) are ignored.
3. **Engagers.** For each post, fetches who **loved** it and who **commented** on it. Posts the feed reports with zero loves (or zero comments) skip that fetch.
4. **Diff.** Everyone from step 3 who appears in none of the step-1 lists is listed, most engaged first. The member's own loves and comments on their posts are ignored.

Members are matched by nickname (case-insensitive) — the loves grid exposes nothing else.

### Options

| Option | Default | Description |
|---|---|---|
| `--since TEXT` | last scan, else 30 days | Only posts created after this. An ISO date/time (`2026-09-01`, `2026-09-01T12:00Z`) or a duration back from now (`"2 weeks"`, `30d`, `6m`). |
| `--all` / `--strangers` | `--strangers` | `--all` lists every engager with a `relation` column (`friend`, `follower`, `following`, or combinations); `--strangers` lists only those not connected. |
| `--state-dir PATH` | `~/.fetlife/engagement` | Where each member's last-scan date is kept (`<nickname>.json`). |
| `--no-save` | off | Don't record this run as the member's last scan. |
| `--csv` | off | Write the rows to stdout as CSV (header + one row per engager) instead of a table. |
| `-j, --json` | off | Emit the full report as JSON. |

### The last-scan date

Each successful run records its **start time** as the member's last scan, keyed by the canonical nickname (so `johndoe`, `JohnDoe` and the numeric id share one record). The next run without `--since` scans only the posts made after that, so a periodic run sees each post once. `--since` overrides the stored date for that run (the record is still updated afterwards unless `--no-save`). A run that fails — including one stopped by rate limiting, exit code 75 — leaves the record untouched, so rerunning it covers the same window.

Note the window is on the **post's creation date**: a love or comment left *today* on a post from *before* the window is not seen. Widen `--since` when you want to catch up on older posts.

### Output

```
              Not connected to JohnDoe, but engaging
┏━━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃ nickname     ┃ loves ┃ comments ┃ posts ┃ url                         ┃
┡━━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ RopeCurious  │ 4     │ 1        │ 4     │ https://fetlife.com/RopeCu… │
│ QuietFan_22  │ 2     │ 0        │ 2     │ https://fetlife.com/QuietF… │
└──────────────┴───────┴──────────┴───────┴─────────────────────────────┘
```

| Column | Meaning |
|---|---|
| `loves` / `comments` | How many of the member's posts they loved, and how many comments they left (an author who comments twice on one post counts twice). |
| `posts` | Distinct posts they engaged with. |
| `relation` | (`--all` only) which of the member's lists they're in. |

A summary line goes to stderr: list sizes, posts scanned, engagers found, how many are not connected, and any posts whose loves/comments couldn't be read.

`--csv` writes the same rows the table would show (`--strangers` or `--all`) as CSV to stdout, with the columns `nickname,loves,comments,posts,connected,relation,url,post_urls` — `post_urls` is the space-separated list of posts engaged with. The summary line still goes to stderr, so redirecting stdout gives a clean file:

```bash
fetlife engagement JohnDoe --all --csv > johndoe-engagers.csv
```

`--json` emits one object: `target`, `since`, `scanned_at`, the three list sizes, `posts`, `skipped`, `engagers` (everyone, each with `loves`, `comments`, the `posts` URLs, `connected`, `relation`) and `strangers` (the not-connected subset):

```bash
fetlife engagement JohnDoe --json | jq -r '.strangers[] | "\(.nickname)\t\(.loves)\t\(.comments)"'
```

### Cost

One request per 20 connections, one per 20 posts, and per post one for the loves plus one per page of comments (pages are large; a post with a handful of comments is one request, plus one empty trailing page FetLife always serves). A member with ~900 connections and a dozen posts is about 70 requests — a few minutes at the default delay. The connection lists are re-fetched on every run, so they dominate for a member with many connections and few posts. Throttling stops the run with exit code 75 like `discover`; wait a few hours and rerun.

## Development

```bash
pytest            # fully offline: parser + client tests use fixtures/mocks
```

## Disclaimer

Not affiliated with or endorsed by FetLife / BitLove Inc. Provided as-is for personal, authorized use.
