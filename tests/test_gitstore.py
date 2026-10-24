"""Layer 2: a package as a bare git repository driven through the plumbing."""
import io
import tarfile

import pytest

from harnessgateway.errors import Invalid, NotFound
from harnessgateway.backends.repos.git import Repo, checked_path, split_author


def test_commits_tree_read_log_export(tmp_path):
    r = Repo.init(tmp_path / "a.git")
    assert r.head() == "" and r.log() == []
    s1 = r.commit({"harness.json": b"{}", "request.sh": b"#!/bin/bash\necho hi\n"}, "first", "Chase <c@x.test>")
    s2 = r.commit({"README.md": b"hello", "deep/dir/file.py": b"print(1)"}, "more", "dev")
    assert r.head() == s2 and [(e.path, e.executable) for e in r.tree()] == [("README.md", False), ("deep/dir/file.py", False), ("harness.json", False), ("request.sh", True)]
    assert r.read("README.md") == b"hello" and r.read("request.sh", s1).startswith(b"#!/bin/bash")
    assert [(c.message, c.author) for c in r.log()] == [("more", "dev"), ("first", "Chase")]
    with pytest.raises(NotFound):
        r.read("README.md", s1)
    with tarfile.open(fileobj=io.BytesIO(r.export(s1)), mode="r:gz") as tar:
        assert sorted(tar.getnames()) == ["harness.json", "request.sh"]
    r.commit({"README.md": None}, "rm", "dev")
    assert "README.md" not in {e.path for e in r.tree()} and len(r.log()) == 3


def test_stale_parent_and_bad_paths(tmp_path):
    r = Repo.init(tmp_path / "a.git")
    s1 = r.commit({"a": b"1"}, "one", "x")
    r.commit({"b": b"2"}, "two", "x")
    with pytest.raises(Invalid):
        r.commit({"c": b"3"}, "stale", "x", parent=s1)
    for bad in ("../x", "/abs", ".git/config", ""):
        with pytest.raises(Invalid):
            checked_path(bad)
    with pytest.raises(Invalid):
        r.commit({}, "nothing", "x")


def test_fork_bundle_import_tree(tmp_path):
    r = Repo.init(tmp_path / "a.git")
    r.commit({"a": b"1", "b": b"2"}, "one", "x")
    f = r.fork(tmp_path / "f.git")
    assert f.head() == r.head() and len(f.log()) == 1
    f.commit({"c": b"3"}, "fork only", "y")
    assert len(r.log()) == 1 and len(f.log()) == 2
    b = Repo.from_bundle(tmp_path / "b.git", f.bundle())
    assert b.head() == f.head() and {e.path for e in b.tree()} == {"a", "b", "c"}
    folder = tmp_path / "folder"
    (folder / "sub").mkdir(parents=True)
    (folder / "a").write_bytes(b"1")
    (folder / "sub" / "n").write_bytes(b"new")
    sha = b.import_tree(folder, "import", "z")
    assert {e.path for e in b.tree()} == {"a", "sub/n"} and sha == b.head()
    assert b.import_tree(folder, "again", "z") == sha                 # nothing changed: the head stands
    with pytest.raises(Invalid):
        Repo.from_bundle(tmp_path / "x.git", b"not a bundle")


def test_split_author():
    assert split_author("Chase <c@x.test>") == ("Chase", "c@x.test")
    assert split_author("dev")[0] == "dev" and split_author("dev")[1].endswith("@harnessgateway.local")
    assert split_author("")[0] == "member"
