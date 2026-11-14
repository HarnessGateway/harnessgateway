"""Spec §18.3: the installer, run with stand-ins for `docker` and `curl`. What it writes is what is checked."""

import os
import re
import subprocess
from pathlib import Path

import harnessgateway

INSTALLER = Path(__file__).parents[2] / "deploy" / "install.sh"
DOCKER = """#!/bin/sh
echo "docker $*" >> "$CALLS"
case "$*" in *Runtimes*) echo '{"runc":{}}' ;; esac
"""
CURL = """#!/bin/sh
echo "curl $*" >> "$CALLS"
"""


def install(tmp_path: Path, *options: str) -> dict[str, str]:
    """Run the installer into tmp_path/hg and return its .env as a mapping."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for name, script in (("docker", DOCKER), ("curl", CURL)):
        (tools / name).write_text(script)
        (tools / name).chmod(0o755)
    env = {"PATH": f"{tools}:{os.environ['PATH']}", "HOME": str(tmp_path), "CALLS": str(tmp_path / "calls")}
    subprocess.run(["bash", str(INSTALLER), "--dir", str(tmp_path / "hg"), *options], env=env, check=True,
                   capture_output=True, text=True)
    lines = (tmp_path / "hg" / ".env").read_text().splitlines()
    assert len(lines) == len({line.split("=", 1)[0] for line in lines})             # each setting is written once
    return dict(line.split("=", 1) for line in lines)


def test_a_first_run_writes_this_version_s_images_and_new_secrets(tmp_path: Path) -> None:
    written = install(tmp_path, "--port", "8100")
    version = harnessgateway.__version__
    assert written["HG_PLATFORM_IMAGE"] == f"ghcr.io/harnessgateway/harnessgateway:{version}"
    assert written["HG_SANDBOX_IMAGE"] == f"ghcr.io/harnessgateway/sandbox:{version}"
    assert (written["HG_PORT"], written["HG_BIND"]) == ("8100", "127.0.0.1")
    assert all(len(written[name]) >= 24 for name in ("HG_SECRET_KEY", "HG_PROXY_KEY", "HG_DB_PASSWORD"))
    assert (tmp_path / "hg" / ".env").stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "hg" / "compose.yaml").exists()


def test_run_again_it_moves_to_this_version_and_keeps_everything_else(tmp_path: Path) -> None:
    first = install(tmp_path, "--port", "8100")
    env = tmp_path / "hg" / ".env"
    earlier = re.sub(r"(_IMAGE=.*):.*", r"\1:3.9.9", env.read_text()) + "HG_RUNTIME=runc\n"   # as an older one left it
    env.write_text(earlier)

    again = install(tmp_path)
    version = harnessgateway.__version__
    assert again["HG_PLATFORM_IMAGE"] == f"ghcr.io/harnessgateway/harnessgateway:{version}"
    assert again["HG_SANDBOX_IMAGE"] == f"ghcr.io/harnessgateway/sandbox:{version}"
    kept = [name for name in again if not name.endswith("_IMAGE")]
    assert {name: again[name] for name in kept} == {**{name: first[name] for name in first if name in kept},
                                                    "HG_RUNTIME": "runc"}
    assert env.stat().st_mode & 0o777 == 0o600
    calls = (tmp_path / "calls").read_text()
    assert "docker compose pull" in calls
    assert "docker compose up --detach serve proxy" in calls
    assert calls.count("http://127.0.0.1:8100/readyz") == 2                         # it waits where it listens
    assert "8000" not in calls


def test_a_port_or_an_address_given_again_takes_the_place_of_the_one_kept(tmp_path: Path) -> None:
    first = install(tmp_path)
    assert (first["HG_PORT"], first["HG_BIND"]) == ("8000", "127.0.0.1")
    again = install(tmp_path, "--port", "9000", "--bind", "0.0.0.0")
    assert (again["HG_PORT"], again["HG_BIND"]) == ("9000", "0.0.0.0")
    assert again["HG_SECRET_KEY"] == first["HG_SECRET_KEY"]
    assert "http://127.0.0.1:9000/readyz" in (tmp_path / "calls").read_text()
