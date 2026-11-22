"""A link to a session's files: what it names, how long it lasts, and what a browser is told."""

import pytest

from harnessgateway.domain import view

KEY, SESSION = bytes(range(32)), "sess_" + "0" * 26


def test_a_token_names_one_session_until_its_time_and_only_as_it_was_signed():
    token = view.sign(KEY, "org_a", SESSION, expires_at=1000)
    assert view.read(KEY, token, now=999) == ("org_a", SESSION)
    assert view.read(KEY, token, now=1001) is None                                   # past its time
    assert view.read(bytes(32), token, now=999) is None                              # not made here
    encoded, signature = token.split(".")
    assert view.read(KEY, f"{encoded}.{signature[:-1]}0", now=999) is None           # changed
    other = view.sign(KEY, "org_b", SESSION, expires_at=1000).split(".")[0]
    assert view.read(KEY, f"{other}.{signature}", now=999) is None                   # another organization's, re-signed by nobody
    for junk in ("", ".", "x.y", "%%%.abc"):
        assert view.read(KEY, junk, now=0) is None
    assert view.read(KEY, view.sign(KEY, "org_a", "not-a-session", expires_at=1000), now=0) is None


@pytest.mark.parametrize(("header", "size", "span"), [
    (None, 10, None), ("bytes=0-3", 10, (0, 3)), ("bytes=4-", 10, (4, 9)), ("bytes=-3", 10, (7, 9)), ("bytes=2-99", 10, (2, 9)),
    ("bytes=0-1,4-5", 10, None), ("items=0-1", 10, None), ("bytes=-", 10, None), ("bytes=0-3", 0, None)])
def test_a_range_is_one_span_inside_the_file(header: str | None, size: int, span: tuple[int, int] | None):
    assert view.byte_range(header, size) == span


@pytest.mark.parametrize("header", ["bytes=10-", "bytes=5-2", "bytes=-0"])
def test_a_range_outside_the_file_is_refused(header: str):
    with pytest.raises(ValueError):
        view.byte_range(header, 10)


def test_a_file_is_what_its_name_says_and_source_is_shown_not_run():
    assert view.content_type("site/index.html") == "text/html; charset=utf-8"
    assert view.content_type("a.PNG") == "image/png" and view.content_type("clip.mov") == "video/quicktime"
    assert view.content_type("notes.md") == "text/markdown; charset=utf-8" and view.content_type("main.py") == "text/plain; charset=utf-8"
    assert view.content_type("data.ipynb") == "application/json; charset=utf-8" and view.content_type("blob") == "application/octet-stream"
    assert view.POLICY.startswith("sandbox ") and "allow-same-origin" not in view.POLICY
