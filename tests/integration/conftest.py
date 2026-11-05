"""The live stack every integration test runs against (HG_TEST_STACK=1): an admin key, with `shell` installed from
`packages/shell` and the organization's egress set."""
import io
import os
import tarfile
from pathlib import Path

import httpx
import pytest

SHELL = Path(__file__).resolve().parents[2] / "packages" / "shell"


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


@pytest.fixture(scope="session")
def api():
    with httpx.Client(base_url=os.environ.get("HG_TEST_URL", "http://localhost:8000"), timeout=600,
                      headers={"authorization": f"Bearer {os.environ['HG_TEST_KEY']}"}) as c:
        r = c.put("/v1/egress", json={"egress": ["*.npmjs.org", "!evil.npmjs.org", "mcp.hg-test.dev"]})
        r.raise_for_status()
        r = c.post("/v1/harnesses", files={"package": ("shell.tgz", pack(SHELL))})
        assert r.status_code == 200, r.text
        yield c
