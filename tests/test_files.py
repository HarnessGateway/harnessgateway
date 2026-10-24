"""Layer 0: the server's reads and writes under a sandbox's trees never follow what the sandbox planted."""
import os

import pytest

from harnessgateway.kernel import files
from harnessgateway.errors import Invalid


def test_write_and_read_plain_files(tmp_path):
    files.write(tmp_path, "a/b/c.txt", b"hello")
    assert (tmp_path / "a" / "b" / "c.txt").read_bytes() == b"hello"
    assert files.read(tmp_path, "a/b/c.txt") == b"hello" and files.read(tmp_path, "a/b/c.txt", limit=2) == b"he"
    assert files.exists(tmp_path, "a/b/c.txt") and not files.exists(tmp_path, "a/b/none")
    files.write(tmp_path, "a/b/c.txt", b"again")
    assert files.read(tmp_path, "a/b/c.txt") == b"again"
    files.unlink(tmp_path, "a/b/c.txt")
    files.unlink(tmp_path, "a/b/c.txt")                 # gone already: nothing
    assert not (tmp_path / "a" / "b" / "c.txt").exists()


def test_refuses_links_at_every_component(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "target").write_text("untouched")
    tree = tmp_path / "tree"
    tree.mkdir()
    os.symlink(outside, tree / "dirlink")
    os.symlink(outside / "target", tree / "filelink")
    with pytest.raises(Invalid, match="not a plain"):
        files.write(tree, "dirlink/x", b"evil")
    with pytest.raises(Invalid, match="not a plain"):
        files.write(tree, "filelink", b"evil")
    with pytest.raises(Invalid, match="not a plain"):
        files.read(tree, "filelink")
    assert (outside / "target").read_text() == "untouched" and not (outside / "x").exists()
    files.unlink(tree, "filelink")                      # removes the link, not its target
    assert not (tree / "filelink").is_symlink() and (outside / "target").exists()


def test_refuses_fifos_and_escapes(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(Invalid, match="not a plain"):
        files.read(tmp_path, "pipe")
    for bad in ("../x", "/etc/passwd", "", "a/../../x"):
        with pytest.raises(Invalid):
            files.write(tmp_path, bad, b"")
    (tmp_path / "d").mkdir()
    with pytest.raises(Invalid):
        files.read(tmp_path, "d")
