"""Spec §12.2: the snapshot manifest."""

import json

import pytest

from harnessgateway.domain.snapshot import CHUNK, Entry, ManifestError, SnapshotManifest

H1, H2 = "a" * 64, "b" * 64


def file(path: str, size: int = 10, hashes: tuple[str, ...] = (H1,), mtime: int = 1, mode: int = 0o644) -> Entry:
    return Entry(path=path, kind="f", mode=mode, size=size, mtime=mtime, hashes=hashes)


def test_a_manifest_round_trips_through_its_text_form() -> None:
    manifest = SnapshotManifest([
        file("work/src/main.py"),
        Entry(path="work/src", kind="d", mode=0o755),
        Entry(path="mind/.claude", kind="d", mode=0o755),
        Entry(path="work/latest", kind="l", target="build/2026"),
        file("work/empty", size=0, hashes=()),
    ])
    text = manifest.dumps()
    assert SnapshotManifest.loads(text) == manifest
    assert [json.loads(line)["p"] for line in text.splitlines()] == sorted(e.path for e in manifest.entries.values())


def test_the_text_form_uses_the_keys_of_the_specification() -> None:
    line = json.loads(SnapshotManifest([file("work/a", size=10, mtime=7, mode=0o644)]).dumps())
    assert line == {"p": "work/a", "t": "f", "m": 0o644, "s": 10, "mt": 7, "h": [H1]}
    assert json.loads(SnapshotManifest([Entry(path="mind/d", kind="d", mode=0o755)]).dumps()) == {
        "p": "mind/d", "t": "d", "m": 0o755}
    assert json.loads(SnapshotManifest([Entry(path="work/l", kind="l", target="x")]).dumps()) == {
        "p": "work/l", "t": "l", "to": "x"}


def test_size_files_and_hashes_are_summed_over_files() -> None:
    manifest = SnapshotManifest([
        file("work/a", size=10, hashes=(H1,)),
        file("work/b", size=CHUNK + 1, hashes=(H1, H2)),
        Entry(path="work/d", kind="d", mode=0o755),
    ])
    assert (manifest.size(), manifest.files(), manifest.hashes()) == (CHUNK + 11, 2, {H1, H2})


def test_an_empty_manifest_is_valid() -> None:
    assert SnapshotManifest.loads("") == SnapshotManifest([])
    assert SnapshotManifest([]).dumps() == ""


@pytest.mark.parametrize("path", ["/work/a", "work/../etc", "work//a", "work/", "etc/passwd", "work", "mind", "",
                                  "work/a\x00b", "work/./a", "work/" + "x" * 5000])
def test_a_path_outside_the_two_roots_or_not_normal_is_refused(path: str) -> None:
    with pytest.raises(ManifestError):
        SnapshotManifest([file(path)])


@pytest.mark.parametrize("line", [
    "not json",
    "[]",
    '{"p": "work/a", "t": "x"}',
    '{"p": "work/a", "t": "f", "m": 420, "s": 10, "mt": 1, "h": []}',                  # 10 bytes need one hash
    '{"p": "work/a", "t": "f", "m": 420, "s": 0, "mt": 1, "h": ["' + H1 + '"]}',       # none for an empty file
    '{"p": "work/a", "t": "f", "m": 420, "s": 10, "mt": 1, "h": ["zz"]}',
    '{"p": "work/a", "t": "f", "m": 420, "s": -1, "mt": 1, "h": []}',
    '{"p": "work/a", "t": "f", "m": 99999, "s": 0, "mt": 1, "h": []}',
    '{"p": "work/a", "t": "l"}',
    '{"p": "work/a", "t": "d"}',
    '{"p": "work/a", "t": "d", "m": 493, "extra": 1}',
])
def test_a_malformed_line_is_refused(line: str) -> None:
    with pytest.raises(ManifestError):
        SnapshotManifest.loads(line)


def test_a_path_listed_twice_is_refused() -> None:
    with pytest.raises(ManifestError, match="twice"):
        SnapshotManifest([file("work/a"), file("work/a")])


def test_a_file_is_unchanged_when_size_time_and_mode_match() -> None:
    before = file("work/a", size=10, mtime=5, mode=0o644)
    assert before.unchanged(size=10, mtime=5, mode=0o644)
    assert not before.unchanged(size=11, mtime=5, mode=0o644)
    assert not before.unchanged(size=10, mtime=6, mode=0o644)
    assert not before.unchanged(size=10, mtime=5, mode=0o600)
    assert not Entry(path="work/a", kind="d", mode=0o644).unchanged(size=0, mtime=0, mode=0o644)


def test_the_work_listing_is_files_only_with_paths_relative_to_work() -> None:
    manifest = SnapshotManifest([
        file("work/out/report.md", size=120, mtime=3_000_000_000),
        file("mind/.claude/session.jsonl"),
        Entry(path="mind/.claude", kind="d", mode=0o755),
        Entry(path="work/out", kind="d", mode=0o755),
    ])
    assert manifest.listing("work") == [{"path": "out/report.md", "bytes": 120, "modified_at": 3}]


def test_an_entry_whose_parent_is_not_a_listed_directory_is_refused() -> None:
    """A restore writes entries where they say; nothing may sit under a link, a file or an unlisted folder."""
    with pytest.raises(ManifestError, match="work/src"):
        SnapshotManifest([file("work/src/main.py")])
    with pytest.raises(ManifestError, match="work/link"):
        SnapshotManifest([Entry(path="work/link", kind="l", target="/etc"), file("work/link/passwd")])
    with pytest.raises(ManifestError, match="work/a"):
        SnapshotManifest([file("work/a"), file("work/a/b")])
    SnapshotManifest([file("work/src/main.py"), Entry(path="work/src", kind="d", mode=0o755)])
