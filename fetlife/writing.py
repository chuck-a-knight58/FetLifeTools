"""Turn the pieces of a writing into what FetLife's New Writing form posts.

FetLife's editor (Lexxy, Rails' rich-text editor) submits the body as
ActionText HTML in ``post[rich_body]``. A picture is uploaded first, on its
own, and then appears in that HTML as an ``<action-text-attachment>``
pointing back at the upload (its ``attachable_sgid``). This module builds
that HTML from plain text and checks the other fields the way the site's
form does; :class:`~fetlife.client.FetLifeClient` does the requests.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from .exceptions import FetLifeError

CATEGORIES = ("Journal Entry", "Erotica", "Note")
# The privacy menu's values, and the labels the site shows for them.
PRIVACY_LEVELS = {
    "public": "FetLifers",
    "friends_and_followers": "Friends & Followers",
    "friends": "Friends",
}
MAX_TAGS = 5
TAG_BLOCKED_CHARS = "@#"

# The picture types FetLife's upload accepts (its picture_mime_types.js).
IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".heic": "image/heic",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}

# What the site has the poster tick before a writing with a picture can be
# published (the form's content-consent checkbox), verbatim.
IMAGE_CONSENT = (
    "I certify that everyone in the picture consented to have it uploaded to "
    "FetLife, that everyone was 18 or older when it was taken, and that I have "
    "documentation to support this."
)


def image_type(path: str | Path) -> str:
    """The MIME type to upload *path* as; FetLifeError if FetLife won't take it."""
    mime = IMAGE_TYPES.get(Path(path).suffix.lower())
    if mime is None:
        raise FetLifeError(
            f"{Path(path).name}: FetLife accepts only "
            + ", ".join(sorted({ext.lstrip('.') for ext in IMAGE_TYPES})) + " pictures."
        )
    return mime


def clean_tags(tags) -> list[str]:
    """The tag names to send: split on commas, trimmed, without repeats.

    The site's tag box allows at most five and refuses ``@`` and ``#``.
    """
    names: list[str] = []
    for raw in tags:
        for name in str(raw).split(","):
            name = " ".join(name.split())
            if not name:
                continue
            bad = [c for c in TAG_BLOCKED_CHARS if c in name]
            if bad:
                raise FetLifeError(f"Tag {name!r} can't contain {' or '.join(bad)}.")
            if name.lower() not in {n.lower() for n in names}:
                names.append(name)
    if len(names) > MAX_TAGS:
        raise FetLifeError(f"FetLife allows at most {MAX_TAGS} tags; got {len(names)}: {', '.join(names)}.")
    return names


def text_html(text: str) -> str:
    """Plain text as editor HTML: a paragraph per blank-line-separated block,
    single line breaks kept, everything else escaped."""
    blocks = [b.strip("\n") for b in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip())]
    return "".join(
        "<p>" + "<br>".join(html.escape(line, quote=False) for line in block.split("\n")) + "</p>"
        for block in blocks if block.strip()
    )


def picture_html(picture: dict, content_type: str, size: int) -> str:
    """The editor's node for an uploaded picture (from ``/attachments_pictures``).

    Shaped like the site's own: the picture's ``attachable_sgid`` is what the
    server resolves; the rest only drives the preview. The filename is left
    blank, as the site does, so a file's name never shows up in the post.
    """
    attrs = {
        "sgid": picture["attachable_sgid"],
        "content-type": content_type,
        "url": picture.get("src_2x") or picture.get("src_1x") or "",
        "filename": "",
        "filesize": str(size),
        "width": str(picture.get("width") or ""),
        "height": str(picture.get("height") or ""),
        "previewable": "true",
        "presentation": "gallery",
    }
    return ("<action-text-attachment "
            + " ".join(f'{k}="{html.escape(v)}"' for k, v in attrs.items() if v != "" or k == "filename")
            + "></action-text-attachment>")
