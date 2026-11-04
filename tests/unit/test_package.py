"""A package folder is three files: harness.json beside two executables, `install` and `run`, in any language."""
import json
from pathlib import Path

import pytest

from harnessgateway.domain import package as pkg
from harnessgateway.errors import Invalid

MANIFEST = json.dumps({"contract": 1, "name": "p", "version": "1-1"})


def write(folder: Path, files: dict[str, str], executable: bool = True) -> Path:
    folder.mkdir(exist_ok=True)
    for name, text in files.items():
        p = folder / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if executable and name in ("install", "run"):
            p.chmod(0o755)
    return folder


def test_run_may_be_any_executable_and_is_marked_executable(tmp_path):
    folder = write(tmp_path / "p", {"harness.json": MANIFEST, "install": "#!/usr/bin/env bash\ntrue\n",
                                     "run": "#!/usr/bin/env node\nconsole.log('{}')\n"}, executable=False)
    assert pkg.validate(folder).name == "p"
    assert (folder / "run").stat().st_mode & 0o111 and (folder / "install").stat().st_mode & 0o111
    digest = pkg.digest(folder)
    (folder / "run").chmod(0o644)
    assert pkg.digest(folder) != digest                     # the executable bit is part of the digest


def test_a_binary_is_an_executable_too(tmp_path):
    folder = write(tmp_path / "p", {"harness.json": MANIFEST, "install": "#!/bin/sh\n", "run": "#!/bin/sh\n"})
    (folder / "run").write_bytes(b"\x7fELF\x02\x01\x01\x00")
    assert pkg.validate(folder).name == "p"


def test_an_executable_without_a_shebang_or_a_missing_one_is_refused(tmp_path):
    with pytest.raises(Invalid, match="interpreter"):
        pkg.validate(write(tmp_path / "a", {"harness.json": MANIFEST, "install": "#!/bin/sh\n", "run": "print(1)\n"}))
    with pytest.raises(Invalid, match="needs `run`"):
        pkg.validate(write(tmp_path / "b", {"harness.json": MANIFEST, "install": "#!/bin/sh\n"}))
    with pytest.raises(Invalid, match="needs `install`"):                       # run.sh is not run
        pkg.validate(write(tmp_path / "c", {"harness.json": MANIFEST, "install.sh": "true\n", "run": "#!/bin/sh\n"}))


def test_an_archive_round_trips_with_its_executable_bits(tmp_path):
    folder = write(tmp_path / "p", {"harness.json": MANIFEST, "install": "#!/bin/sh\n", "run": "#!/bin/sh\n",
                                     "seeds/home/.agent/config.toml": "x = 1\n"})
    pkg.validate(folder)
    pkg.unpack(pkg.pack(folder), tmp_path / "out")
    assert pkg.digest(tmp_path / "out") == pkg.digest(folder)
    assert (tmp_path / "out" / "run").stat().st_mode & 0o111
    assert not (tmp_path / "out" / "seeds/home/.agent/config.toml").stat().st_mode & 0o111
