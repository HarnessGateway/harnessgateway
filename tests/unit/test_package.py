import io
import json
import tarfile
from pathlib import Path

import pytest

from harnessgateway.domain import package as pkg
from harnessgateway.errors import Invalid

ROOT = Path(__file__).resolve().parents[2]


def write_package(folder: Path, manifest: dict | None = None, request: str = "echo hi\n") -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "harness.json").write_text(json.dumps(manifest or {"contract": 1, "name": "demo", "version": "1"}))
    (folder / "install.sh").write_text("#!/bin/bash\nmkdir -p \"$HG_TOOLS/bin\"\n")
    (folder / "request.sh").write_text("#!/bin/bash\n" + request)
    return folder


def test_builtin_packages_are_valid():
    names = set()
    for folder in sorted(p for p in (ROOT / "packages").iterdir() if (p / "harness.json").is_file()):
        manifest = pkg.validate(folder)
        assert manifest.name == folder.name
        names.add(manifest.name)
    assert "shell" in names


def test_manifest_is_strict():
    with pytest.raises(Invalid, match="(?i)extra"):
        pkg.Manifest.parse(json.dumps({"contract": 1, "name": "x", "version": "1", "surprise": True}))
    with pytest.raises(Invalid, match="contract"):
        pkg.Manifest.parse(json.dumps({"contract": 2, "name": "x", "version": "1"}))
    with pytest.raises(Invalid):
        pkg.Manifest.parse(json.dumps({"contract": 1, "name": "Bad Name", "version": "1"}))
    with pytest.raises(Invalid, match="egress"):
        pkg.Manifest.parse(json.dumps({"contract": 1, "name": "x", "version": "1", "run": {"egress": ["https://a.b"]}}))
    m = pkg.Manifest.parse(json.dumps({"contract": 1, "name": "x", "version": "1",
                                       "run": {"egress": ["API.Example.com", "a.example.com:8443"]}}))
    assert m.run.egress == ["a.example.com:8443", "api.example.com"]


def test_settings_are_checked():
    m = pkg.Manifest.parse(json.dumps({"contract": 1, "name": "x", "version": "1",
                                       "settings": {"TOKEN": {"secret": True, "required": True}}}))
    with pytest.raises(Invalid, match="not declared"):
        m.check_settings({"OTHER": "1"})
    assert m.missing_settings({}) == ["TOKEN"]


def test_digest_is_stable_and_pack_is_deterministic(tmp_path):
    a = write_package(tmp_path / "a")
    b = write_package(tmp_path / "b")
    assert pkg.digest(a) == pkg.digest(b)
    assert pkg.pack(a) == pkg.pack(b)
    (b / "request.sh").write_text("#!/bin/bash\necho other\n")
    assert pkg.digest(a) != pkg.digest(b)


def test_unpack_round_trip_and_strip_top_dir(tmp_path):
    src = write_package(tmp_path / "src")
    out = tmp_path / "out"
    pkg.unpack(pkg.pack(src), out)
    assert pkg.digest(out) == pkg.digest(src)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        tar.add(src, arcname="wrapped")
    out2 = tmp_path / "out2"
    pkg.unpack(raw.getvalue(), out2)
    assert (out2 / "harness.json").is_file()


def test_unpack_keeps_dotfiles(tmp_path):
    src = write_package(tmp_path / "src")
    (src / "seeds" / "workspace").mkdir(parents=True)
    (src / "seeds" / "workspace" / ".gitignore").write_text("x\n")
    out = tmp_path / "out"
    pkg.unpack(pkg.pack(src), out)
    assert (out / "seeds" / "workspace" / ".gitignore").read_text() == "x\n"


@pytest.mark.parametrize("name,kind", [("../evil", tarfile.REGTYPE), ("/abs", tarfile.REGTYPE),
                                       ("link", tarfile.SYMTYPE), ("dev", tarfile.CHRTYPE)])
def test_unpack_refuses_escapes_and_specials(tmp_path, name, kind):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        if kind == tarfile.SYMTYPE:
            info.linkname = "/etc/passwd"
        tar.addfile(info, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
    with pytest.raises(Invalid):
        pkg.unpack(raw.getvalue(), tmp_path / "x")


def test_validate_refuses_bad_bash_and_missing_files(tmp_path):
    bad = write_package(tmp_path / "bad", request="if then fi (\n")
    with pytest.raises(Invalid, match="not valid bash"):
        pkg.validate(bad)
    missing = write_package(tmp_path / "missing")
    (missing / "install.sh").unlink()
    with pytest.raises(Invalid, match="install.sh is missing"):
        pkg.validate(missing)
