"""Content-addressed snapshots (SPEC §9.5): walk, diff, chunk, round-trip, restore, and what the format refuses."""
import hashlib
import os
import time

import pytest
from hgsnapshot import CHUNK, Entry, Manifest, ManifestError, included_roots, restore, walk


def make_tree(root):
    (root / "workspace" / "src").mkdir(parents=True)
    (root / "workspace" / "src" / "a.py").write_text("print(1)\n")
    (root / "workspace" / "run.sh").write_text("#!/bin/sh\n")
    (root / "workspace" / "run.sh").chmod(0o755)
    (root / "workspace" / "empty").mkdir()
    os.symlink("src/a.py", root / "workspace" / "link")
    (root / "state").mkdir()
    (root / "state" / "session").write_text("abc")
    (root / "home" / "hg" / ".cli").mkdir(parents=True)
    (root / "home" / "hg" / ".cli" / "config").write_text("x=1\n")
    (root / "home" / "hg" / "cache").mkdir()
    (root / "home" / "hg" / "cache" / "junk").write_text("junk")


def store_of(root, manifest):
    """Every blob the manifest names, read from the tree: a fake object store."""
    blobs = {}
    for e in manifest.entries.values():
        if e.kind == "f":
            with (root / e.path).open("rb") as f:
                for h in e.hashes:
                    blobs[h] = f.read(CHUNK)
    return blobs


def test_included_roots_and_the_whole_home():
    assert included_roots([".cli", "x/y"]) == ["workspace", "state", "home/hg/.cli", "home/hg/x/y"]
    assert included_roots(["."]) == ["workspace", "state", "home/hg"]


def test_walk_lists_files_dirs_and_links_under_the_roots_only(tmp_path):
    make_tree(tmp_path)
    m = walk(tmp_path, included_roots([".cli"]))
    paths = sorted(m.entries)
    assert "workspace/src/a.py" in paths and "home/hg/.cli/config" in paths and "state/session" in paths
    assert "home/hg/cache/junk" not in paths                     # not persisted: the manifest names .cli only
    assert m.entries["workspace/link"].kind == "l" and m.entries["workspace/link"].target == "src/a.py"
    assert m.entries["workspace/empty"].kind == "d"
    assert m.entries["workspace/run.sh"].mode & 0o111
    assert m.entries["workspace/src/a.py"].hashes == (hashlib.sha256(b"print(1)\n").hexdigest(),)
    assert m.workspace_files() == [{"path": "run.sh", "bytes": 10, "mtime": m.entries["workspace/run.sh"].mtime // 10**9},
                                   {"path": "src/a.py", "bytes": 9, "mtime": m.entries["workspace/src/a.py"].mtime // 10**9}]


def test_a_second_walk_hashes_only_what_changed(tmp_path, monkeypatch):
    make_tree(tmp_path)
    first = walk(tmp_path, included_roots(["."]))
    hashed = []
    import hgsnapshot
    real = hgsnapshot.hash_file
    monkeypatch.setattr(hgsnapshot, "hash_file", lambda p: (hashed.append(p.name), real(p))[1])
    time.sleep(0.01)
    (tmp_path / "workspace" / "src" / "a.py").write_text("print(2)\n")
    second = walk(tmp_path, included_roots(["."]), first)
    assert hashed == ["a.py"]
    assert second.hashes() - first.hashes() == {hashlib.sha256(b"print(2)\n").hexdigest()}
    assert walk(tmp_path, included_roots(["."]), second) == second          # unchanged: equal, nothing hashed twice


def test_large_files_are_chunked_and_an_append_reuses_the_prefix(tmp_path):
    (tmp_path / "workspace").mkdir()
    big = tmp_path / "workspace" / "big.bin"
    big.write_bytes(b"a" * CHUNK + b"b" * 10)
    m = walk(tmp_path, ["workspace"])
    e = m.entries["workspace/big.bin"]
    assert e.size == CHUNK + 10 and len(e.hashes) == 2 and e.hashes[0] == hashlib.sha256(b"a" * CHUNK).hexdigest()
    with big.open("ab") as f:
        f.write(b"c" * 5)
    m2 = walk(tmp_path, ["workspace"], m)
    assert m2.entries["workspace/big.bin"].hashes[0] == e.hashes[0]       # the first chunk's blob is shared


def test_round_trip_and_restore_reproduce_the_tree(tmp_path):
    src, dst = tmp_path / "src", tmp_path / "dst"
    src.mkdir()
    make_tree(src)
    m = walk(src, included_roots(["."]))
    text = m.dumps()
    loaded = Manifest.loads(text)
    assert loaded == m and loaded.dumps() == text and loaded.size() == m.size() and loaded.files() == 5
    blobs = store_of(src, m)
    dst.mkdir()
    written = restore(dst, loaded, blobs.__getitem__)
    assert written == m.size()
    again = walk(dst, included_roots(["."]), None)
    assert again == m                                              # modes, mtimes, sizes and content all match
    assert os.readlink(dst / "workspace" / "link") == "src/a.py"
    with pytest.raises(ManifestError, match="does not match"):
        restore(tmp_path / "bad", loaded, lambda h: b"wrong")


def test_under_keeps_one_root(tmp_path):
    make_tree(tmp_path)
    m = walk(tmp_path, included_roots(["."]))
    ws = m.under("workspace")
    assert all(p.startswith("workspace") for p in ws.entries) and len(ws) < len(m) and ws.hashes() <= m.hashes()


@pytest.mark.parametrize("line, match", [
    ("nope", "not JSON"),
    ('{"p": "/etc/passwd", "t": "f", "m": 420, "mt": 0, "s": 0, "h": []}', "relative"),
    ('{"p": "workspace/../x", "t": "f", "m": 420, "mt": 0, "s": 0, "h": []}', "normalized"),
    ('{"p": "opt/x", "t": "f", "m": 420, "mt": 0, "s": 0, "h": []}', "under workspace"),
    ('{"p": "home/other/x", "t": "f", "m": 420, "mt": 0, "s": 0, "h": []}', "home/hg"),
    ('{"p": "workspace/x", "t": "s", "m": 420}', "entry type"),
    ('{"p": "workspace/x", "t": "f", "m": 420, "mt": 0, "s": 5, "h": []}', "chunks"),
    ('{"p": "workspace/x", "t": "f", "m": 420, "mt": 0, "s": 1, "h": ["zz"]}', "64 hex"),
    ('{"p": "workspace/x", "t": "l", "m": 420}', "a link has a target"),
])
def test_the_format_refuses_what_it_does_not_allow(line, match):
    with pytest.raises(ManifestError, match=match):
        Manifest.loads(line)


def test_entries_listed_twice_are_refused():
    e = Entry("workspace/x", "d").to_line()
    with pytest.raises(ManifestError, match="twice"):
        Manifest.loads(e + "\n" + e)
