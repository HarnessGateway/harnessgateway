"""Layer 2: a package is contract 8 (or 7, which still loads) or it is refused by name; an archive is unpacked safely; a digest
names a version; seeds copy through the safe writer."""
import io
import json
import os
import tarfile

import pytest

from fakes import HARNESS_JSON, archive
from harnessgateway.errors import Invalid
from harnessgateway.contract.package import Config, copy_seed, digest, load_config, pack, unpack, validate


def test_config_defaults_and_values():
    assert load_config(json.dumps({"contract": 8})) == Config() == load_config(json.dumps({"contract": 7, "interrupt": "SIGUSR1"}))
    c = load_config(json.dumps(HARNESS_JSON))
    assert (c.provider, c.model, c.key_env, c.url_env) == ("prov", "m1", "FAKE_KEY", "FAKE_URL")
    assert c.install_egress == ("registry.example",) and c.run_egress == ("git.example",) and c.timeout_s == 60
    assert c.egress == ("git.example", "registry.example")
    assert c.as_json()["run"]["egress"] == ["git.example"]


@pytest.mark.parametrize("bad, says", [
    ("not json", "not JSON"), ('{"contract": 3}', '"contract": 8'), ('{"contract": 8, "extra": 1}', "unknown keys"),
    ('{"contract": 8, "defaults": {"sandbox": "x"}}', "defaults is an object"),
    ('{"contract": 8, "interrupt": "SIGUSR1"}', "unknown keys"),
    ('{"contract": 8, "run": {"timeout_s": -1}}', "positive number"),
    ('{"contract": 8, "provider_env": {"key": "has space"}}', "environment variable names"),
    ('{"contract": 8, "run": {"egress": "host"}}', "list of hosts"),
])
def test_config_refusals(bad, says):
    with pytest.raises(Invalid, match=says):
        load_config(bad)


def test_unpack_validate_digest_pack(tmp_path):
    unpack(archive(), tmp_path / "a")
    config = validate(tmp_path / "a")
    assert config.model == "m1" and (tmp_path / "a" / "request.sh").stat().st_mode & 0o100
    unpack(archive(root="my-harness"), tmp_path / "b")
    assert digest(tmp_path / "a") == digest(tmp_path / "b")
    unpack(archive(description="other"), tmp_path / "c")
    assert digest(tmp_path / "c") != digest(tmp_path / "a")
    unpack(pack(tmp_path / "a"), tmp_path / "d")       # the canonical archive round-trips
    assert digest(tmp_path / "d") == digest(tmp_path / "a")


@pytest.mark.parametrize("files, says", [
    ({"request.sh": None}, "request.sh is missing"), ({"install.sh": None}, "install.sh is missing"),
    ({"request.sh": "if then fi"}, "not valid bash"), ({"response.sh": "if then fi"}, "not valid bash"),
    ({"seeds/global/x": "y"}, "seeds holds only"),
])
def test_validate_refusals(tmp_path, files, says):
    unpack(archive(files), tmp_path / "a")
    with pytest.raises(Invalid, match=says):
        validate(tmp_path / "a")


def test_unpack_refuses_escapes_and_links(tmp_path):
    with pytest.raises(Invalid, match="tar archive"):
        unpack(b"not a tar", tmp_path / "x")
    for name, kind in (("../escape", tarfile.REGTYPE), ("link", tarfile.SYMTYPE)):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.SYMTYPE:
                info.linkname = "/etc/passwd"
            tar.addfile(info, io.BytesIO(b"") if kind == tarfile.REGTYPE else None)
        with pytest.raises(Invalid):
            unpack(buf.getvalue(), tmp_path / name.strip("./"))


def test_copy_seed_is_safe(tmp_path):
    src = tmp_path / "src"
    (src / "d").mkdir(parents=True)
    (src / "d" / "f").write_text("new")
    dest = tmp_path / "dest"
    (dest / "d").mkdir(parents=True)
    (dest / "d" / "f").write_text("old")
    copy_seed(src, dest, overwrite=False)
    assert (dest / "d" / "f").read_text() == "old"
    copy_seed(src, dest, overwrite=True)
    assert (dest / "d" / "f").read_text() == "new"
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    os.unlink(dest / "d" / "f")
    os.symlink(outside, dest / "d" / "f")
    with pytest.raises(Invalid):
        copy_seed(src, dest, overwrite=True)
    assert outside.read_text() == "untouched"
