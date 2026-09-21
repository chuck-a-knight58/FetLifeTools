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


# A profile activity feed, as the HTML tab renders it: one <article> per story.
# The member's own posts carry a love button (with the story uid used by the
# loves/comments endpoints) and a comment CTA; other stories (loves, follows)
# carry neither.
ACTIVITY_HTML = """
<div id="activity-stories-list">
<article id="story_11790045501" data-story-type="picture_created" data-story-actor-id="21621264">
  <header>
    <a href="/Xanadu_Kink">Xanadu_Kink</a>
    <span><a href="/Xanadu_Kink/pictures/225264245">
      <time datetime="2026-09-18T15:19:15Z">38s</time></a></span>
  </header>
  <footer>
    <span data-controller="story-love-button"
          data-story-love-button-content-id-value="fd87pc9ga4"
          data-story-love-button-loves-count-value="3"></span>
    <a data-controller="comment-cta" data-comment-cta-target-class-value="Picture"
       data-comment-cta-target-id-value="225264245"
       href="/Xanadu_Kink/pictures/225264245#comment">
      <span data-comment-cta-count="">2</span><span data-comment-cta-count="">2</span>
    </a>
  </footer>
</article>
<article id="story_11789813485" data-story-type="loved_picture" data-story-actor-id="21621264">
  <header><time datetime="2026-09-18T14:37:01Z">1h</time></header>
</article>
<article id="story_11760041052" data-story-type="status_created" data-story-actor-id="21621264">
  <header><span><a href="/Xanadu_Kink/s/tmmzbkb3d8">
    <time datetime="2026-09-14T02:03:29Z">4d</time></a></span></header>
  <footer>
    <span data-controller="story-love-button"
          data-story-love-button-content-id-value="tmmzbkb3d8"
          data-story-love-button-loves-count-value="0"></span>
    <a data-controller="comment-cta" data-comment-cta-target-class-value="Status"
       data-comment-cta-target-id-value="65607586" href="/Xanadu_Kink/s/tmmzbkb3d8#comment">
      <span data-comment-cta-count="">0</span>
    </a>
  </footer>
</article>
</div>
<div id="activity-stories-pagination">
  <turbo-frame id="activity-stories-pagination-loader" loading="lazy"
    src="/Xanadu_Kink/activity.turbo_stream?marker=1789504280755269&amp;story_size=large">
  </turbo-frame>
</div>
"""

# The continuation pages are Turbo Streams: the same articles inside
# <turbo-stream><template>, and a loader frame for the page after.
ACTIVITY_STREAM = """
<turbo-stream action="append" target="activity-stories-list"><template>
<article id="story_11684468864" data-story-type="post_created" data-story-actor-id="21621264">
  <header><span><a href="/Xanadu_Kink/posts/14430217">
    <time datetime="2026-09-02T17:04:00Z">16d</time></a></span></header>
  <footer>
    <span data-controller="story-love-button"
          data-story-love-button-content-id-value="sl0bl4pk35"
          data-story-love-button-loves-count-value="7"></span>
    <a data-controller="comment-cta" data-comment-cta-target-class-value="Post"
       data-comment-cta-target-id-value="14430217" href="/Xanadu_Kink/posts/14430217#comment">
      <span data-comment-cta-count>4</span>
    </a>
  </footer>
</article>
</template></turbo-stream>
<turbo-stream action="update" target="activity-stories-pagination"><template>
  <turbo-frame id="activity-stories-pagination-loader" loading="lazy"
    src="/Xanadu_Kink/activity/all-posts.turbo_stream?marker=1784165667699051&amp;story_size=large">
  </turbo-frame>
</template></turbo-stream>
"""

LOVES_HTML = """
<turbo-frame id="story-loves-grid">
  <div data-lover-nickname="Hammerbacher"><a href="/Hammerbacher" title="Hammerbacher"><img></a></div>
  <div data-lover-nickname="Lulu-1313"><a href="/Lulu-1313" title="Lulu-1313"><img></a></div>
</turbo-frame>
"""

COMMENTS_STREAM = """
<turbo-stream action="append" target="comments_wrapper"><template>
  <div data-comment-id="1" data-controller="comment-item"
       data-comment-item-author-id-value="15758532"
       data-comment-item-author-nickname-value="Knight_of_Xanadu"><p>First</p></div>
  <div data-comment-id="2" data-controller="comment-item"
       data-comment-item-author-id-value="4063157"
       data-comment-item-author-nickname-value="JadedViper"><p>Second</p></div>
</template></turbo-stream>
<turbo-stream action="update" target="comments_pagination"><template>
  <turbo-frame id="comments_page_MTc4OTcz" loading="lazy"
    src="/comments.turbo_stream?cursor=MTc4OTcz&amp;order=oldest&amp;story_uid=vpu26glhhc">
  </turbo-frame>
</template></turbo-stream>
"""

COMMENTS_STREAM_LAST = """
<turbo-stream action="append" target="comments_wrapper"><template></template></turbo-stream>
"""


def test_stories_from_activity_html():
    stories, marker = parsers.stories_from_activity_html(
        ACTIVITY_HTML, base_url="https://fetlife.com"
    )
    assert marker == "1789504280755269"
    assert [s.type for s in stories] == ["picture_created", "loved_picture", "status_created"]

    pic = stories[0]
    assert pic.id == "11790045501"
    assert pic.uid == "fd87pc9ga4"
    assert pic.kind == "Picture" and pic.content_id == "225264245"
    assert pic.url == "https://fetlife.com/Xanadu_Kink/pictures/225264245"
    assert pic.created_at == "2026-09-18T15:19:15Z"
    assert pic.created().tzinfo is not None
    assert pic.loves == 3 and pic.comments == 2

    loved = stories[1]  # not a post: no uid, no counts, but still timestamped
    assert loved.uid is None and loved.loves is None and loved.comments is None
    assert loved.created_at == "2026-09-18T14:37:01Z"

    status = stories[2]
    assert status.kind == "Status" and status.loves == 0 and status.comments == 0


def test_stories_from_turbo_stream_reads_counts_inside_template():
    stories, marker = parsers.stories_from_activity_html(
        ACTIVITY_STREAM, base_url="https://fetlife.com"
    )
    assert marker == "1784165667699051"
    assert len(stories) == 1
    assert stories[0].uid == "sl0bl4pk35"
    assert stories[0].url == "https://fetlife.com/Xanadu_Kink/posts/14430217"
    # Text inside <template> is invisible to get_text() unless unwrapped.
    assert stories[0].loves == 7 and stories[0].comments == 4


def test_stories_last_page_has_no_marker():
    html = ACTIVITY_HTML.split('<div id="activity-stories-pagination">')[0]
    stories, marker = parsers.stories_from_activity_html(html)
    assert len(stories) == 3 and marker is None


def test_last_active_from_activity_picks_newest():
    dt = parsers.last_active_from_activity(ACTIVITY_HTML)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 9, 18, 15)
    assert dt.tzinfo is not None  # UTC-aware


def test_last_active_from_activity_empty():
    assert parsers.last_active_from_activity("<html><body></body></html>") is None
    assert parsers.last_active_from_activity("") is None


def test_lovers_from_loves_html():
    lovers = parsers.lovers_from_loves_html(LOVES_HTML, base_url="https://fetlife.com")
    assert [m.nickname for m in lovers] == ["Hammerbacher", "Lulu-1313"]
    assert lovers[1].url == "https://fetlife.com/Lulu-1313"
    assert parsers.lovers_from_loves_html("<div></div>") == []


def test_comments_from_stream():
    authors, cursor = parsers.comments_from_stream(
        COMMENTS_STREAM, base_url="https://fetlife.com"
    )
    assert [(a.nickname, a.id) for a in authors] == [
        ("Knight_of_Xanadu", "15758532"), ("JadedViper", "4063157"),
    ]
    assert authors[0].url == "https://fetlife.com/Knight_of_Xanadu"
    assert cursor == "MTc4OTcz"


def test_comments_from_stream_last_page():
    authors, cursor = parsers.comments_from_stream(COMMENTS_STREAM_LAST)
    assert authors == [] and cursor is None


RELATIONS_HTML = """
<div id="relations_items">
  <div class="min-w-0" id="relation_user_7878605">
    <div onclick="openLink(event, '/Miss__Lynne')">
      <a href="/Miss__Lynne" title="Miss__Lynne"><img src="https://x/big.jpg" alt=""></a>
      <div class="relative flex-auto">
        <div class="leading-normal truncate">
          <a href="/Miss__Lynne" class="link text-base font-bold text-red-500">Miss__Lynne</a>
          <span class="relative top-px"><a href="/support" title="FetLife Supporter"></a></span>
          <span class="text-sm font-bold text-gray-300">51W Switch</span>
        </div>
        <div class="text-sm truncate">Denver, Colorado</div>
        <div class="text-sm text-gray-500"><a href="/Miss__Lynne/pictures">32 pics</a></div>
      </div>
    </div>
  </div>
  <div class="min-w-0" id="relation_user_16294">
    <div>
      <div class="relative flex-auto">
        <div class="leading-normal truncate">
          <a href="/no_age" class="link text-base font-bold text-red-500">no_age</a>
        </div>
        <div class="text-sm truncate">New Jersey</div>
      </div>
    </div>
  </div>
</div>
"""


def test_members_from_relations_html():
    friends = parsers.members_from_relations_html(
        RELATIONS_HTML, base_url="https://fetlife.com"
    )
    assert len(friends) == 2
    f = friends[0]
    assert f.id == "7878605"
    # The supporter badge sits between the nickname and the age/gender line.
    assert f.nickname == "Miss__Lynne" and f.age == 51 and f.gender == "W"
    assert f.role == "Switch"
    assert f.location == "Denver, Colorado"  # not the "32 pics" stats line
    assert f.url == "https://fetlife.com/Miss__Lynne"
    assert f.avatar_url == "https://x/big.jpg"


def test_members_from_relations_html_without_age():
    """A hidden age hides the gender with it — they share one glued token."""
    f = parsers.members_from_relations_html(RELATIONS_HTML)[1]
    assert f.nickname == "no_age" and f.age is None and f.gender is None
    assert f.location == "New Jersey"


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


PAGE_DATA_HTML = """
<html><head>
<script id="page-data" data-request-id="x">(function refreshPageData(data) {
  mergePageData(window.FL ||= {}, data)
})
({"user":{"id":1065304737,"gender":"Male","role":"Mad Scientist","age":27,
  "nickname":"Hammerbacher","profileUrl":"/Hammerbacher","avatarUrls":{"c50":"https://x/c50.jpg"}},
  "firebase":{"api_key":"k"}});</script>
</head><body></body></html>
"""

# The relation button on a profile the viewer can friend-request (and follow).
RELATION_OPEN_HTML = """
<turbo-frame id="profile_relation_button_2678009_mobile">
  <form method="post" action="/Errrp/follow?source=profile"><button><span>Follow</span></button></form>
  <div data-dropdown-target="menu">
    <a href="/requests?source=profile&amp;user_id=2678009" data-controller="dropdown-menu-entry"
       data-dropdown-menu-entry-method-value="POST"
       data-dropdown-menu-entry-href-value="/requests?source=profile&amp;user_id=2678009">
      <span>Add as Friend</span></a>
  </div>
</turbo-frame>
<turbo-frame id="profile_relation_button_2678009_aside"></turbo-frame>
"""

# Already following; the request is still offered, plus an unfollow entry.
RELATION_FOLLOWING_HTML = """
<turbo-frame id="profile_relation_button_21621264_aside">
  <button><span>Following</span></button>
  <a data-dropdown-menu-entry-method-value="POST"
     data-dropdown-menu-entry-href-value="/favorite_members?target_user_id=21621264"><span>Add to Favorites</span></a>
  <a data-dropdown-menu-entry-method-value="POST"
     data-dropdown-menu-entry-href-value="/requests?source=profile&amp;user_id=21621264"><span>Add as Friend</span></a>
  <a data-dropdown-menu-entry-method-value="DELETE"
     data-dropdown-menu-entry-href-value="/Xanadu_Kink/follow?source=profile"><span>Unfollow</span></a>
</turbo-frame>
"""

# No request offered (already friends, or one is pending).
RELATION_CLOSED_HTML = """
<turbo-frame id="profile_relation_button_555_aside">
  <button><span>Friends</span></button>
  <a data-dropdown-menu-entry-method-value="DELETE"
     data-dropdown-menu-entry-href-value="/Pal/follow"><span>Unfollow</span></a>
</turbo-frame>
"""


def test_extract_bootstrap_user_from_page_data_script():
    user = parsers.extract_bootstrap_user(PAGE_DATA_HTML)
    assert user["nickname"] == "Hammerbacher" and user["id"] == 1065304737
    m = parsers.member_from_bootstrap(user, base_url="https://fetlife.com")
    assert m.url == "https://fetlife.com/Hammerbacher" and m.age == 27


def test_extract_bootstrap_user_legacy_assignment_still_works():
    html = '<script>window.FL.user = {"id": 1, "nickname": "Old"};</script>'
    assert parsers.extract_bootstrap_user(html)["nickname"] == "Old"


def test_profile_relation_open():
    rel = parsers.profile_relation_from_html(RELATION_OPEN_HTML)
    assert rel.user_id == "2678009"
    assert rel.can_friend_request is True
    assert rel.request_path == "/requests?source=profile&user_id=2678009"
    assert rel.following is False
    assert rel.labels == ["Follow", "Add as Friend"]


def test_profile_relation_following_but_not_friends():
    rel = parsers.profile_relation_from_html(RELATION_FOLLOWING_HTML)
    assert rel.user_id == "21621264"
    assert rel.can_friend_request and rel.following
    assert "Unfollow" in rel.labels


def test_profile_relation_closed():
    rel = parsers.profile_relation_from_html(RELATION_CLOSED_HTML)
    assert rel.user_id == "555"
    assert rel.can_friend_request is False and rel.request_path is None
    assert rel.labels == ["Friends", "Unfollow"]


def test_profile_relation_missing_raises():
    with pytest.raises(ParseError):
        parsers.profile_relation_from_html("<html><body>nothing</body></html>")


COMPOSE_HTML = """
<form action="/search" method="get"><input name="q"></form>
<form data-controller="chat-draft" action="/conversations" accept-charset="UTF-8" method="post">
  <input type="hidden" name="authenticity_token" value="tok123" />
  <input type="hidden" name="source" id="source" value="profile" />
  <input type="hidden" name="with[]" value="">
  <input type="hidden" name="with[]" value="2678009">
  <input type="hidden" name="with[]" value="2678009">
  <input type="text" name="subject" maxlength="255">
  <textarea name="body"></textarea>
  <button type="submit">Send</button>
</form>
"""


def test_message_form_from_html():
    form = parsers.message_form_from_html(COMPOSE_HTML)
    assert form == {"authenticity_token": "tok123", "source": "profile", "with[]": ["2678009"]}
    assert parsers.message_form_from_html("<html><body>profile</body></html>") is None


def test_conversation_error_from_html():
    html = '<div class="flash bg-red-500">Body can\'t be blank</div>'
    assert parsers.conversation_error_from_html(html) == "Body can't be blank"
    assert parsers.conversation_error_from_html("<p>ok</p>") is None
