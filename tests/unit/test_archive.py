"""Spec §8.1: a package is a gzip-compressed tar archive of a folder, with a digest that depends only on its files."""

import gzip
import io
import json
import tarfile
from typing import Any

import pytest

from harnessgateway.domain.archive import FILES_MAX, PACKED_MAX, UNPACKED_MAX, Package, read_archive
from harnessgateway.errors import Invalid, TooLarge

MANIFEST = {"contract": 1, "name": "demo", "version": "1.0.0-1", "apis": []}
RUN = b"#!/bin/sh\necho run\n"


def archive(files: dict[str, Any], *, order: list[str] | None = None, mtime: int = 0, prefix: str = "",
            compresslevel: int = 6) -> bytes:
    """A tar.gz of files. A value is bytes, (bytes, mode), or a ready TarInfo for an unusual member."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in order or list(files):
            value = files[name]
            if isinstance(value, tarfile.TarInfo):
                tar.addfile(value)
                continue
            content, mode = value if isinstance(value, tuple) else (value, 0o644)
            info = tarfile.TarInfo(prefix + name)
            info.size, info.mode, info.mtime = len(content), mode, mtime
            tar.addfile(info, io.BytesIO(content))
    return gzip.compress(raw.getvalue(), compresslevel=compresslevel, mtime=mtime)


def demo(**more: Any) -> dict[str, Any]:
    return {"harness.json": json.dumps(MANIFEST).encode(), "install": (b"#!/bin/sh\n", 0o755), "run": (RUN, 0o755),
            **more}


def special(name: str, kind: bytes, linkname: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, linkname
    return info


def test_a_package_is_read_into_its_manifest_and_files() -> None:
    package = read_archive(archive(demo(**{"README.md": b"# Demo\n", "seeds/mind/.demo/settings.json": b"{}"})))
    assert isinstance(package, Package)
    assert (package.manifest.name, package.manifest.version) == ("demo", "1.0.0-1")
    assert [(f.path, f.executable) for f in package.files] == [
        ("README.md", False), ("harness.json", False), ("install", True), ("run", True),
        ("seeds/mind/.demo/settings.json", False)]
    assert package.files[3].content == RUN
    assert len(package.digest) == 64


def test_the_digest_depends_only_on_paths_executable_bits_and_content() -> None:
    files = demo(**{"README.md": b"# Demo\n"})
    one = read_archive(archive(files)).digest
    assert read_archive(archive(files, order=sorted(files, reverse=True), mtime=1_700_000_000)).digest == one
    assert read_archive(archive(files, prefix="./", compresslevel=1)).digest == one
    assert read_archive(archive({**files, "run": (RUN, 0o700)})).digest == one              # still executable

    assert read_archive(archive({**files, "run": (RUN + b"#", 0o755)})).digest != one
    assert read_archive(archive({**files, "README.md": (b"# Demo\n", 0o755)})).digest != one
    assert read_archive(archive({**files, "NOTES.md": b""})).digest != one


def test_moving_bytes_between_a_path_and_its_content_changes_the_digest() -> None:
    one = read_archive(archive(demo(**{"ab": b"c"}))).digest
    other = read_archive(archive(demo(**{"a": b"bc"}))).digest
    assert one != other


def test_directories_in_the_archive_are_fine_and_not_files() -> None:
    package = read_archive(archive(demo(**{"seeds": special("seeds", tarfile.DIRTYPE), "seeds/a": b"a"})))
    assert "seeds" not in [f.path for f in package.files]
    assert "seeds/a" in [f.path for f in package.files]


@pytest.mark.parametrize(("files", "message"), [
    ({"install": (b"", 0o755), "run": (RUN, 0o755)}, "harness.json"),
    ({"harness.json": b"{not json", "install": (b"", 0o755), "run": (RUN, 0o755)}, "harness.json is not JSON"),
    ({"harness.json": json.dumps({**MANIFEST, "name": "Bad Name"}).encode(), "install": (b"", 0o755),
      "run": (RUN, 0o755)}, "name"),
    ({"harness.json": json.dumps(MANIFEST).encode(), "run": (RUN, 0o755)}, "install"),
    ({"harness.json": json.dumps(MANIFEST).encode(), "install": (b"", 0o755)}, "run"),
    ({"harness.json": json.dumps(MANIFEST).encode(), "install": (b"", 0o755), "run": (RUN, 0o644)},
     "run is not executable"),
])
def test_a_package_without_its_three_parts_is_refused(files: dict[str, Any], message: str) -> None:
    with pytest.raises(Invalid, match=message):
        read_archive(archive(files))


@pytest.mark.parametrize("name", ["/etc/passwd", "../outside", "a/../../b", "a//b", "a/./b"])
def test_a_path_that_leaves_the_package_is_refused(name: str) -> None:
    with pytest.raises(Invalid, match="path"):
        read_archive(archive(demo(**{name: b"x"})))


@pytest.mark.parametrize("member", [
    special("link", tarfile.SYMTYPE, "/etc/passwd"),
    special("inside-link", tarfile.SYMTYPE, "run"),
    special("hard", tarfile.LNKTYPE, "run"),
    special("device", tarfile.CHRTYPE),
    special("pipe", tarfile.FIFOTYPE),
])
def test_links_and_special_files_are_refused(member: tarfile.TarInfo) -> None:
    with pytest.raises(Invalid, match="plain files and folders"):
        read_archive(archive(demo(**{member.name: member})))


def test_the_same_path_twice_is_refused() -> None:
    with pytest.raises(Invalid, match="twice"):
        read_archive(archive(demo(**{"./run": (RUN, 0o755)})))


def test_something_that_is_not_an_archive_is_refused() -> None:
    with pytest.raises(Invalid, match="gzip-compressed tar"):
        read_archive(b"this is not an archive")
    with pytest.raises(Invalid, match="gzip-compressed tar"):
        read_archive(gzip.compress(b"compressed, but not a tar"))


def test_the_limits_are_stated_when_passed() -> None:
    with pytest.raises(TooLarge, match="64 MiB"):
        read_archive(b"\x1f\x8b" + b"\x00" * PACKED_MAX)
    with pytest.raises(TooLarge, match="5000 files"):
        read_archive(archive(demo(**{f"f/{n}": b"" for n in range(FILES_MAX)})))
    big = b"\x00" * (UNPACKED_MAX // 4)
    with pytest.raises(TooLarge, match="256 MiB"):
        read_archive(archive(demo(**{f"big-{n}": big for n in range(5)}), compresslevel=1))


def test_the_kit_s_place_in_an_install_cannot_be_taken_by_a_package() -> None:
    with pytest.raises(Invalid, match="kit/"):
        read_archive(archive(demo(**{"kit/hgkit.py": b"print('not the kit')"})))
    assert read_archive(archive(demo(**{"toolkit/a.py": b""})))
