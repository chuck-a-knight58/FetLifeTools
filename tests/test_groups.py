"""Tests for fetlife.groups with a stub client (offline)."""

import io

from fetlife import friending, groups
from fetlife.exceptions import RateLimitedError
from fetlife.models import Group, GroupMembersPage, Member


class StubClient:
    """Serves pages from a dict; a page mapped to RateLimitedError raises it."""

    def __init__(self, pages):
        self.pages = pages
        self.fetched = []

    def iter_group_member_pages(self, group_id, start_page=1):
        page = start_page
        while page is not None:
            self.fetched.append(page)
            found = self.pages[page]
            if isinstance(found, Exception):
                raise found
            yield page, found
            page = found.next_page


def _page(nicks, next_page=None, count=None):
    return GroupMembersPage(
        group=Group(id="9", name="G", member_count=count),
        members=[Member(nickname=n, age=30, gender="M", role="Top",
                        location="Here", joined="2020-01-01T00:00:00Z",
                        url=f"https://fetlife.com/{n}") for n in nicks],
        next_page=next_page,
    )


def test_gather_members_reads_every_page():
    fl = StubClient({1: _page(["a", "b"], 2, count=3), 2: _page(["c"])})
    seen = []
    r = groups.gather_members(fl, "9", on_progress=seen.append)
    assert [m.nickname for m in r.members] == ["a", "b", "c"]
    assert r.group.name == "G" and r.group.member_count == 3
    assert r.pages == 2 and r.next_page is None and r.resume_page is None
    assert [(p.page, p.members) for p in seen] == [(1, 2), (2, 3)]


def test_gather_members_honors_max_pages():
    fl = StubClient({1: _page(["a"], 2), 2: _page(["b"], 3), 3: _page(["c"])})
    r = groups.gather_members(fl, "9", max_pages=2)
    assert [m.nickname for m in r.members] == ["a", "b"]
    assert r.next_page == 3 and r.resume_page is None
    assert fl.fetched == [1, 2]


def test_gather_members_keeps_what_it_has_when_rate_limited():
    fl = StubClient({1: _page(["a"], 2), 2: RateLimitedError("429")})
    r = groups.gather_members(fl, "9")
    assert [m.nickname for m in r.members] == ["a"]
    assert r.resume_page == 2


def test_gather_members_hands_each_page_over_as_it_arrives():
    """on_page sees a page's members before the next page is requested."""
    fl = StubClient({1: _page(["a", "b"], 2), 2: _page(["c"], 3), 3: RateLimitedError("429")})
    batches = []
    fl_fetched_at = []
    groups.gather_members(
        fl, "9",
        on_page=lambda ms: (batches.append([m.nickname for m in ms]),
                            fl_fetched_at.append(list(fl.fetched))),
    )
    assert batches == [["a", "b"], ["c"]]
    # When page 1 was handed over, page 2 had not been fetched yet (and so on).
    assert fl_fetched_at == [[1], [1, 2]]


class CommittingFile(io.StringIO):
    """A StringIO that records every flush and what had been written by then."""

    def __init__(self):
        super().__init__()
        self.commits = []

    def flush(self):
        super().flush()
        self.commits.append(self.getvalue())

    def fileno(self):
        raise io.UnsupportedOperation("fileno")  # like stdout under a test runner


def test_members_csv_writer_commits_the_header_and_every_page():
    fh = CommittingFile()
    w = groups.MembersCsvWriter(fh)
    assert fh.commits == ["nickname,age,gender,role,location,joined,url\n"]
    w.write(_page(["a"]).members)
    w.write(_page(["b"]).members)
    assert len(fh.commits) == 3
    assert fh.commits[1].splitlines()[1:] == ["a,30,M,Top,Here,2020-01-01T00:00:00Z,https://fetlife.com/a"]
    assert fh.commits[2].splitlines()[1:] == [
        "a,30,M,Top,Here,2020-01-01T00:00:00Z,https://fetlife.com/a",
        "b,30,M,Top,Here,2020-01-01T00:00:00Z,https://fetlife.com/b",
    ]


def test_members_csv_writer_fsyncs_real_files(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(groups.os, "fsync", synced.append)
    with open(tmp_path / "m.csv", "w", newline="") as fh:
        w = groups.MembersCsvWriter(fh)
        w.write(_page(["a"]).members)
        assert synced == [fh.fileno()] * 2  # header, then the page


def test_gather_members_rate_limited_on_the_first_page():
    fl = StubClient({5: RateLimitedError("429")})
    r = groups.gather_members(fl, "9", start_page=5)
    assert r.members == [] and r.resume_page == 5


def test_members_csv_feeds_the_friending_and_messaging_reader():
    members = _page(["a", "b"]).members
    members[1].age = members[1].role = members[1].location = None
    out = io.StringIO()
    groups.write_members_csv(members, out)
    lines = out.getvalue().splitlines()
    assert lines[0] == "nickname,age,gender,role,location,joined,url"
    assert lines[1] == "a,30,M,Top,Here,2020-01-01T00:00:00Z,https://fetlife.com/a"
    assert lines[2] == "b,,M,,,2020-01-01T00:00:00Z,https://fetlife.com/b"
    # The same file is what `friend-requests` / `message --from-csv` take.
    assert friending.read_nicknames(io.StringIO(out.getvalue())) == ["a", "b"]
