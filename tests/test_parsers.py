"""Unit tests for the HTML parsers using synthetic fixtures.

These run fully offline — no FetLife account or network is required.
"""

import pytest

from fetlife import parsers
from fetlife.exceptions import ParseError

LOGIN_HTML = """
<html><head>
<meta name="csrf-token" content="abc123token">
</head><body>
<form action="/login" method="post">
<input type="hidden" name="authenticity_token" value="formtoken456">
<input name="user[login]"><input name="user[password]" type="password">
</form>
</body></html>
"""

AUTHED_HTML = """
<html><head><meta name="current-user" content="42"></head>
<body><a href="/logout">Log out</a></body></html>
"""

SEARCH_HTML = """
<html><body>
<a href="/users/1001">AliceExample</a>
<a href="/BobExample">BobExample</a>
<a href="/login">Log in</a>
<a href="/search/kinksters">Search</a>
</body></html>
"""

EVENT_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"Event","name":"Munch Night","startDate":"2026-08-01T19:00",
 "endDate":"2026-08-01T22:00",
 "location":{"name":"The Cafe","address":{"streetAddress":"1 Main St",
 "addressLocality":"Portland","addressRegion":"OR"}}}
</script></head><body><h1>Munch Night</h1></body></html>
"""

EVENT_LIST_HTML = """
<html><body>
<a href="/events/555">Rope Social</a>
<a href="/events/555">Rope Social (dup)</a>
<a href="/events/777">Play Party</a>
<a href="/users/9">someone</a>
</body></html>
"""

GROUP_HTML = "<html><body><h1>Rope Enthusiasts</h1><p>1,234 members</p></body></html>"


def test_extract_csrf_prefers_meta():
    assert parsers.extract_csrf_token(LOGIN_HTML) == "abc123token"


def test_extract_csrf_falls_back_to_hidden_input():
    html = LOGIN_HTML.replace(
        '<meta name="csrf-token" content="abc123token">', ""
    )
    assert parsers.extract_csrf_token(html) == "formtoken456"


def test_login_page_not_authenticated():
    assert parsers.looks_authenticated(LOGIN_HTML) is False


def test_authed_page_detected():
    assert parsers.looks_authenticated(AUTHED_HTML) is True


def test_authed_false_when_url_is_login():
    assert parsers.looks_authenticated(AUTHED_HTML, "https://fetlife.com/login") is False


BOOTSTRAP_HTML = """
<html><head><title>Knight_of_Xanadu - Kinksters | FetLife</title></head>
<body><script>
window.FL={};FL.user={"id":15758532,"gender":"Male","role":"Dominant",
"orientation":"Straight","age":68,"nickname":"Knight_of_Xanadu",
"isProfileVerified":true,"isSupporter":true,
"avatarUrls":{"c50":"https://x/c50.jpg","c120":"https://x/c120.jpg"},
"profileUrl":"/Knight_of_Xanadu"};
</script></body></html>
"""


def test_parse_member_from_bootstrap_when_self():
    m = parsers.parse_member(
        BOOTSTRAP_HTML, url="https://fetlife.com/Knight_of_Xanadu",
        requested="Knight_of_Xanadu",
    )
    assert m.nickname == "Knight_of_Xanadu"
    assert m.id == "15758532"
    assert m.age == 68
    assert m.gender == "Male"
    assert m.role == "Dominant"
    assert m.avatar_url == "https://x/c120.jpg"  # largest crop chosen
    assert m.orientation == "Straight"
    assert m.verified is True


def test_parse_member_other_profile_is_partial_with_note():
    # FL.user (the viewer) does NOT match the requested profile → partial result.
    m = parsers.parse_member(
        BOOTSTRAP_HTML, url="https://fetlife.com/SomeoneElse",
        requested="SomeoneElse",
    )
    assert m.nickname == "Knight_of_Xanadu"  # from <title> of that page
    assert m.age is None and m.gender is None
    assert "client-side" in m.meta["note"]


def test_member_from_bootstrap_helper():
    m = parsers.member_from_bootstrap(
        {"id": 5, "nickname": "X", "age": 30, "gender": "F", "role": "Switch",
         "profileUrl": "/X", "avatarUrls": {}},
        base_url="https://fetlife.com",
    )
    assert m.url == "https://fetlife.com/X"
    assert m.avatar_url is None


CORE_PAYLOAD = {
    "core": {
        "userId": 21482572,
        "nickname": "VirginiaSunshine",
        "identity": "60F sub",
        "aboutHtml": "<p>Hello <b>world</b></p>\n<p>Second line</p>",
        "joinDate": "2025-04-01T12:00:00.000Z",
        "isLookingFor": ["friendship"],
        "isProfileVerified": True,
        "isSupporter": True,
        "url": "/VirginiaSunshine",
        "avatarUrl": "https://x/u500.jpg",
        "roles": [{"key": "submissive", "name": "submissive"}],
        "orientations": [{"key": "bisexual", "name": "Bisexual"}],
        "genders": [{"key": "F", "name": "Female"}],
        "relationships": [
            {"status": "Monogamish", "statusWithConnector": "Monogamish with",
             "withId": 5777959, "withNickname": "Sir2BabyGirl", "withUrl": "/Sir2BabyGirl"},
        ],
        "dsRelationships": [
            {"status": "owned", "statusWithConnector": "owned by",
             "withId": 5777959, "withNickname": "Sir2BabyGirl", "withUrl": "/Sir2BabyGirl"},
        ],
    },
    "currentUserRelation": {
        "location": [
            {"name": "Peach Bottom"},
            {"name": "Pennsylvania"},
            {"name": "United States"},
        ]
    },
}


def test_member_from_core():
    m = parsers.member_from_core(CORE_PAYLOAD, base_url="https://fetlife.com")
    assert m.id == "21482572"
    assert m.nickname == "VirginiaSunshine"
    assert m.age == 60                       # parsed from identity "60F sub"
    assert m.gender == "Female"
    assert m.role == "submissive"
    assert m.orientation == "Bisexual"
    assert m.location == "Peach Bottom, Pennsylvania, United States"
    assert m.url == "https://fetlife.com/VirginiaSunshine"
    assert m.about == "Hello world Second line"   # HTML stripped
    assert m.joined.startswith("2025-04-01")
    assert m.verified is True


def test_relationships_from_core():
    rels = parsers.relationships_from_core(CORE_PAYLOAD, base_url="https://fetlife.com")
    assert len(rels) == 2
    vanilla = next(r for r in rels if r.kind == "relationship")
    ds = next(r for r in rels if r.kind == "D/s")
    assert vanilla.status_with_connector == "Monogamish with"
    assert vanilla.with_nickname == "Sir2BabyGirl"
    assert vanilla.with_url == "https://fetlife.com/Sir2BabyGirl"
    assert ds.status == "owned" and ds.status_with_connector == "owned by"
    # And they're attached to the parsed Member too.
    m = parsers.member_from_core(CORE_PAYLOAD)
    assert len(m.relationships) == 2


def test_member_from_core_handles_missing_fields():
    m = parsers.member_from_core({"core": {"nickname": "X"}})
    assert m.nickname == "X"
    assert m.age is None and m.gender is None and m.location is None


def test_last_active_from_activity_picks_newest():
    payload = {
        "story_groups": [
            {"stories": [
                {"created_at": "2026-07-07T19:11:32.432Z"},
                {"created_at": "2026-07-05T00:00:00Z"},
            ]},
            {"stories": [{"created_at": "2026-06-01T00:00:00Z"}]},
        ]
    }
    dt = parsers.last_active_from_activity(payload)
    assert dt is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 7
    assert dt.tzinfo is not None  # UTC-aware


def test_last_active_from_activity_empty():
    assert parsers.last_active_from_activity({"story_groups": []}) is None
    assert parsers.last_active_from_activity({}) is None


def test_members_from_user_list():
    payload = {
        "users": [
            {"id": 7878605, "nickname": "Miss__Lynne", "url": "/Miss__Lynne",
             "age": 51, "gender": "W", "role": "Switch",
             "location": [{"name": "Denver"}, {"name": "Colorado"}],
             "large_avatar_url": "https://x/big.jpg"},
        ],
        "page": 1, "no_more": True,
    }
    friends = parsers.members_from_user_list(payload, base_url="https://fetlife.com")
    assert len(friends) == 1
    f = friends[0]
    assert f.nickname == "Miss__Lynne" and f.age == 51 and f.role == "Switch"
    assert f.location == "Denver, Colorado"
    assert f.url == "https://fetlife.com/Miss__Lynne"
    assert f.avatar_url == "https://x/big.jpg"


def test_parse_member_requires_nickname():
    with pytest.raises(ParseError):
        parsers.parse_member("<html><body></body></html>")


def test_parse_member_search_dedups_and_filters():
    results = parsers.parse_member_search(SEARCH_HTML, base_url="https://fetlife.com")
    nicks = {r.nickname for r in results}
    assert "AliceExample" in nicks
    assert "BobExample" in nicks
    assert "Log in" not in nicks
    alice = next(r for r in results if r.nickname == "AliceExample")
    assert alice.id == "1001"
    assert alice.url == "https://fetlife.com/users/1001"


def test_parse_event_from_json_ld():
    e = parsers.parse_event(EVENT_HTML, url="https://fetlife.com/events/555")
    assert e.name == "Munch Night"
    assert e.id == "555"
    assert e.start == "2026-08-01T19:00"
    assert e.location == "The Cafe"
    assert "Portland" in e.address


def test_parse_event_list():
    events = parsers.parse_event_list(EVENT_LIST_HTML, base_url="https://fetlife.com")
    ids = [e.id for e in events]
    assert ids == ["555", "777"]  # deduped, users link ignored


def test_extract_login_error():
    html = (
        '<div class="bg-red-600">Looks like your Nickname, Email or Password '
        'is incorrect, please try again!</div>'
    )
    msg = parsers.extract_login_error(html)
    assert msg is not None and "incorrect" in msg.lower()


def test_extract_login_error_none_on_clean_page():
    assert parsers.extract_login_error("<html><body><h1>Home</h1></body></html>") is None


def test_parse_group():
    g = parsers.parse_group(GROUP_HTML, url="https://fetlife.com/groups/88")
    assert g.name == "Rope Enthusiasts"
    assert g.id == "88"
    assert g.member_count == 1234
