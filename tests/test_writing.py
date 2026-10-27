"""Tests for post-writing: fetlife.writing, its parsers and client calls (offline)."""

from urllib.parse import parse_qsl

import pytest
import requests
import responses

from fetlife import parsers, writing
from fetlife.client import FetLifeClient
from fetlife.config import Config
from fetlife.exceptions import FetLifeError


def _authed(tmp_path):
    cfg = Config(username="tester", password="secret", base_url="https://fetlife.com",
                 rate_limit_min=0.0, rate_limit_max=0.0, session_path=tmp_path / "s.cookies",
                 throttle_state_path=tmp_path / "throttle.json", retry_backoff=0.0)
    fl = FetLifeClient(cfg, session=requests.Session())
    fl._authenticated = True
    return fl


# The New Writing page, trimmed to what's parsed.
NEW_WRITING = """<html><head><meta name="csrf-token" content="page-tok"></head><body>
<form action="/search" method="get"><input name="q"></form>
<form action="/posts" method="post" data-controller="posts--form posts--draft-autosave">
  <input type="hidden" name="authenticity_token" value="form-tok">
  <input type="text" name="post[title]" id="title">
  <select name="post[category]" id="post_category">
    <option value="">Select Category</option><option value="Journal Entry">Journal Entry</option>
    <option value="Erotica">Erotica</option><option value="Note">Note</option>
  </select>
  <div data-controller="form-typeahead-multi-select">
    <input id="post_collections" type="text">
    <div data-form-typeahead-multi-select-option data-label="Monday" data-value="mon123"></div>
    <div data-form-typeahead-multi-select-option data-label="The Xanadu Word" data-value="xan456"></div>
  </div>
  <input type="hidden" name="post[privacy_level]" id="post_privacy_level" value="public">
  <a href="#" data-dropdown-value-param="public" data-dropdown-label-param="FetLifers">FetLifers</a>
  <a href="#" data-dropdown-value-param="friends_and_followers">Friends &amp; Followers</a>
  <a href="#" data-dropdown-value-param="friends">Friends</a>
</form></body></html>"""


def test_text_html_makes_paragraphs_and_escapes():
    text = "One <b> & two\n\nThree, line one\r\nline two\n"
    assert writing.text_html(text) == ("<p>One &lt;b&gt; &amp; two</p>"
                                       "<p>Three, line one<br>line two</p>")
    assert writing.text_html("  \n\n ") == ""


def test_text_html_adds_an_empty_line_for_each_extra_blank_line():
    text = "\n\nFirst\n\n\nSecond\nstill second\n \n\n\nThird\n\n\n"
    assert writing.text_html(text) == ("<p>First</p><p><br></p>"
                                       "<p>Second<br>still second</p><p><br></p><p><br></p>"
                                       "<p>Third</p>")


def test_picture_html_points_at_the_upload_and_hides_the_filename():
    node = writing.picture_html({"attachable_sgid": "sg==--x", "src_2x": "https://cdn/p.jpg?a=1&b=2",
                                 "width": 400, "height": 300}, "image/png", 1234)
    assert node.startswith('<action-text-attachment sgid="sg==--x" content-type="image/png" ')
    assert 'url="https://cdn/p.jpg?a=1&amp;b=2"' in node and 'filename=""' in node
    assert 'width="400" height="300"' in node and node.endswith("></action-text-attachment>")


def test_clean_tags_splits_trims_dedupes_and_enforces_the_sites_rules():
    assert writing.clean_tags(["rope, first time", " Rope ", "shibari"]) == ["rope", "first time", "shibari"]
    with pytest.raises(FetLifeError, match="at most 5"):
        writing.clean_tags(["a,b,c", "d,e,f"])
    with pytest.raises(FetLifeError, match=r"can't contain #"):
        writing.clean_tags(["#rope"])


def test_image_type_accepts_only_what_fetlife_takes():
    assert writing.image_type("Photo.JPG") == "image/jpeg"
    assert writing.image_type("a.heic") == "image/heic"
    with pytest.raises(FetLifeError, match="accepts only"):
        writing.image_type("drawing.svg")


def test_writing_form_reads_both_tokens_and_the_live_choices():
    form = parsers.writing_form_from_html(NEW_WRITING)
    assert form == {"token": "form-tok", "csrf": "page-tok",
                    "categories": ["Journal Entry", "Erotica", "Note"],
                    "collections": [{"name": "Monday", "uid": "mon123"},
                                    {"name": "The Xanadu Word", "uid": "xan456"}],
                    "privacy_levels": ["public", "friends_and_followers", "friends"]}
    assert parsers.writing_form_from_html("<form action='/search'></form>") is None


@responses.activate
def test_create_writing_posts_the_form_and_returns_the_writing(tmp_path):
    responses.add(responses.POST, "https://fetlife.com/posts", status=302,
                  headers={"Location": "https://fetlife.com/Pal/posts/123"})
    responses.add(responses.GET, "https://fetlife.com/Pal/posts/123", body="<h1>My title</h1>")
    fl = _authed(tmp_path)
    url = fl.create_writing("form-tok", "My title", "<p>Hi</p>", "Note", ["rope", "first time"],
                            "friends", picture_ids=[77], collection_uids=["collection-77"])
    assert url == "https://fetlife.com/Pal/posts/123"
    post = next(c.request for c in responses.calls if c.request.method == "POST")
    assert parse_qsl(post.body, keep_blank_values=True) == [
        ("authenticity_token", "form-tok"), ("post[title]", "My title"),
        ("post[rich_body]", "<p>Hi</p>"), ("post[category]", "Note"),
        ("post[tag_names][]", ""), ("post[tag_names][]", "rope"), ("post[tag_names][]", "first time"),
        ("post[privacy_level]", "friends"), ("post[album_uids][]", ""),
        ("post[album_uids][]", "collection-77"),
        ("post[attachment_ids][]", "77"),
        ("post[audio][id]", ""), ("post[audio][consent]", "false"), ("post[audio]", ""),
        ("post[draft]", ""),
    ]


@responses.activate
def test_create_writing_as_a_draft_lands_on_the_drafts(tmp_path):
    responses.add(responses.POST, "https://fetlife.com/posts", status=302,
                  headers={"Location": "https://fetlife.com/Pal/posts/drafts"})
    responses.add(responses.GET, "https://fetlife.com/Pal/posts/drafts",
                  body='<div data-controller="flash-toast">Draft successfully saved!</div>')
    fl = _authed(tmp_path)
    assert fl.create_writing("t", "T", "<p>b</p>", "Note", [], "public", draft=True) == \
        "https://fetlife.com/Pal/posts/drafts"
    post = next(c.request for c in responses.calls if c.request.method == "POST")
    assert ("post[draft]", "1") in parse_qsl(post.body, keep_blank_values=True)


@responses.activate
def test_create_writing_reports_the_sites_reasons(tmp_path):
    responses.add(responses.POST, "https://fetlife.com/posts", status=422,
                  body='<div id="error_explanation"><ul><li>Category can\'t be blank</li></ul></div>')
    fl = _authed(tmp_path)
    with pytest.raises(FetLifeError, match="did not publish the writing: Category can't be blank"):
        fl.create_writing("t", "T", "<p>b</p>", "", [], "public")


@responses.activate
def test_create_writing_never_retries_a_server_error(tmp_path):
    responses.add(responses.POST, "https://fetlife.com/posts", status=502)
    fl = _authed(tmp_path)
    with pytest.raises(FetLifeError, match="Server error 502"):
        fl.create_writing("t", "T", "<p>b</p>", "Note", [], "public")
    assert len(responses.calls) == 1  # a retry could post it twice


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


def test_upload_writing_picture_sends_the_page_token_and_returns_the_picture(tmp_path, monkeypatch):
    fl = _authed(tmp_path)
    image = tmp_path / "p.png"
    image.write_bytes(b"\x89PNG fake")
    calls = []

    def fake_request(method, path, **kw):
        calls.append((method, path, kw["headers"]))
        return _Resp(201, {"attachment": {"id": 9, "attachable_sgid": "sg"}})

    monkeypatch.setattr(fl, "request", fake_request)
    assert fl.upload_writing_picture(image, "image/png", "page-tok") == {"id": 9, "attachable_sgid": "sg"}
    method, path, headers = calls[0]
    assert (method, path) == ("POST", "/attachments_pictures")
    assert headers["X-CSRF-Token"] == "page-tok" and headers["Accept"] == "application/json"

    monkeypatch.setattr(fl, "request", lambda *a, **k: _Resp(422, {"error": {"messages": ["Too small"]}}))
    with pytest.raises(FetLifeError, match="refused the picture p.png: Too small"):
        fl.upload_writing_picture(image, "image/png", "page-tok")


def test_cli_refuses_a_picture_without_the_consent_certification(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from fetlife import cli

    def no_network(*a, **k):
        raise AssertionError("must fail before contacting FetLife")

    monkeypatch.setattr(cli, "_client", no_network)
    image = tmp_path / "p.png"
    image.write_bytes(b"\x89PNG fake")
    result = CliRunner().invoke(cli.cli, ["--env-file", str(tmp_path / "none.env"), "post-writing",
                                          "-t", "T", "-b", "Body", "-c", "note", "--image", str(image)])
    assert result.exit_code != 0
    assert "--image-consent" in str(result.exception) + result.output


def test_cli_puts_the_text_before_the_picture(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from fetlife import cli
    from fetlife.models import Member

    posted = {}

    class FakeClient:
        on_retry = None
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def whoami(self): return Member(nickname="Pal", id="1")
        def get_writing_form(self):
            return {"token": "form-tok", "csrf": "page-tok", "categories": list(writing.CATEGORIES),
                    "privacy_levels": list(writing.PRIVACY_LEVELS),
                    "collections": [{"name": "Word of the Week", "uid": "word-uid"}]}
        def upload_writing_picture(self, path, mime, token):
            assert token == "page-tok"
            return {"id": 9, "attachable_sgid": "sg", "src_2x": "https://cdn/p.jpg", "width": 4, "height": 3}
        def create_writing(self, token, title, rich_body, category, tags, privacy, picture_ids,
                           draft, collection_uids):
            posted.update(token=token, rich_body=rich_body, picture_ids=picture_ids,
                          draft=draft, collection_uids=collection_uids)
            return "https://fetlife.com/Pal/posts/drafts"

    monkeypatch.setattr(cli, "_client", lambda ctx: FakeClient())
    image = tmp_path / "p.png"
    image.write_bytes(b"\x89PNG fake")
    result = CliRunner().invoke(cli.cli, ["--env-file", str(tmp_path / "none.env"), "post-writing",
                                          "-t", "T", "-b", "First.\n\nSecond.", "-c", "note",
                                          "--image", str(image), "--image-consent", "--draft",
                                          "--collections", "Word of the Week"])
    assert result.exit_code == 0, result.output
    body = posted["rich_body"]
    assert body.startswith("<p>First.</p><p>Second.</p><action-text-attachment sgid=\"sg\"")
    assert body.endswith("</action-text-attachment>")
    assert posted["picture_ids"] == [9] and posted["token"] == "form-tok" and posted["draft"]
    assert posted["collection_uids"] == ["word-uid"]
