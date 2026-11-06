"""The live stack every integration test runs against (HG_TEST_STACK=1): an admin key, with `shell` installed from
`packages/shell` (its install run by a worker) and the organization's egress set."""
import io
import json
import os
import tarfile
import time
from pathlib import Path

import httpx
import pytest

SHELL = Path(__file__).resolve().parents[2] / "packages" / "shell"
INSTALL_WAIT_S = 600


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


def seeded_archive() -> bytes:
    """The shell package under another name, with seeds, a setting, a secret and an MCP server: what an organization's
    own package adds to a CLI."""
    manifest = {"contract": 1, "name": "hg-test-seeds", "version": "test",
                "settings": {"GREETING": {"description": "shown", "default": "hello"}},
                "secrets": {"T": {"description": "an mcp token", "hosts": ["mcp.hg-test.dev"]}},
                "mcp": {"t": {"url": "https://mcp.hg-test.dev/mcp", "headers": {"Authorization": "Bearer ${T}"}}}}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        files = [("harness.json", json.dumps(manifest).encode()), ("seeds/home/AGENTS.md", b"be kind\n"),
                 ("seeds/workspace/hello.txt", b"from the seeds\n")]
        files += [(n, (SHELL / n).read_bytes()) for n in ("install", "run")]
        for path, data in files:
            info = tarfile.TarInfo(path)
            info.size = len(data)
            info.mode = 0o755 if path in ("install", "run") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def installed(c: httpx.Client, name: str) -> None:
    """Wait until the harness's install is ready (a worker runs it in an install box); fail on a failed install."""
    deadline = time.monotonic() + INSTALL_WAIT_S
    while time.monotonic() < deadline:
        install = c.get(f"/v1/harnesses/{name}/install").json()
        if install["status"] == "ready":
            return
        assert install["status"] != "failed", install["log_tail"]
        time.sleep(1)
    raise AssertionError(f"the install of {name} did not finish in {INSTALL_WAIT_S}s")


@pytest.fixture(scope="session")
def api():
    with httpx.Client(base_url=os.environ.get("HG_TEST_URL", "http://localhost:8000"), timeout=600,
                      headers={"authorization": f"Bearer {os.environ['HG_TEST_KEY']}"}) as c:
        r = c.put("/v1/egress", json={"egress": ["*.npmjs.org", "!evil.npmjs.org", "mcp.hg-test.dev"]})
        r.raise_for_status()
        r = c.post("/v1/harnesses", files={"package": ("shell.tgz", pack(SHELL))})
        assert r.status_code == 200, r.text
        installed(c, "shell")
        yield c
