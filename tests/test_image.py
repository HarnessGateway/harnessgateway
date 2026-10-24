"""The platform image, run the way an operator runs it: one container, the Docker socket, a named
volume at /data, one published port. Nothing else. An organization, a provider, a package and a turn go
through it, so the sandboxes the containerized platform starts can reach their mounts and the
gateway. Marked docker: it needs a daemon, and it builds the image unless HG_TEST_IMAGE names one."""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from fakes import archive

pytestmark = pytest.mark.docker
ROOT = Path(__file__).resolve().parents[1]
NAME, VOLUME, PORT = "hg-test-platform", "hg-test-data", 18000
REQUEST = """#!/usr/bin/env bash
set -u
prompt=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["input"])' "$HG_REQUEST")
gateway=$(python3 -c 'import urllib.request,os; r=urllib.request.urlopen(urllib.request.Request(os.environ["HG_PROVIDER_URL"]+"/v1/models", headers={"Authorization": "Bearer "+open(os.environ["HG_PROVIDER_KEY_FILE"]).read()}), timeout=20); print(r.status)' 2>&1 | tail -1)
echo made > made.txt
python3 -c 'import json,sys; print(json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": f"turn={sys.argv[1]} prompt={sys.argv[2]} gateway={sys.argv[3]}", "annotations": []}]}}))' "$HG_TURN" "$prompt" "$gateway"
"""


def sh(*args: str, check: bool = True) -> str:
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"{' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r.stdout


def logs() -> str:
    """The container's log, both streams: the first boot prints to stderr."""
    return subprocess.run(["docker", "logs", NAME], capture_output=True, text=True).stderr + sh("docker", "logs", NAME, check=False)


def up() -> None:
    for _ in range(60):
        try:
            if urllib.request.urlopen(f"http://127.0.0.1:{PORT}/healthz", timeout=2).status == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    raise AssertionError("the platform did not come up:\n" + logs())


def call(method: str, path: str, body: bytes | dict | None = None, key: str = "", content_type: str = "application/json") -> dict:
    data = json.dumps(body).encode() if isinstance(body, dict) else body
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, method=method,
                                 headers={"Authorization": f"Bearer {key}", "content-type": content_type} if key else {"content-type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise AssertionError(f"{method} {path}: {e.code} {e.read().decode()}") from None


@pytest.fixture(scope="module")
def platform():
    image = os.environ.get("HG_TEST_IMAGE") or "hg-test-platform:local"
    if not os.environ.get("HG_TEST_IMAGE"):
        sh("docker", "build", "-q", "-f", str(ROOT / "deploy" / "Dockerfile"), "-t", image, str(ROOT))
    sh("docker", "rm", "-f", NAME, check=False)
    sh("docker", "volume", "rm", "-f", VOLUME, check=False)
    sh("docker", "run", "-d", "--name", NAME, "-p", f"127.0.0.1:{PORT}:8000", "-v", "/var/run/docker.sock:/var/run/docker.sock",
       "-v", f"{VOLUME}:/data", "-e", "HG_BASE_IMAGE=python:3.12-slim", "-e", "HG_WORKER_SLOTS=1", "-e", "HG_ORG=acme", "-e", "HG_OWNER=ops",
       *(["-e", f"HG_CA_FILE={os.environ['HG_CA_FILE']}", "-v", f"{os.environ['HG_CA_FILE']}:{os.environ['HG_CA_FILE']}:ro"] if os.environ.get("HG_CA_FILE") else []),
       image)
    try:
        up()
        yield image
    finally:
        if os.environ.get("HG_TEST_KEEP"):
            return
        sh("docker", "rm", "-f", NAME, check=False)
        sh("docker", "volume", "rm", "-f", VOLUME, check=False)


def test_a_turn_through_the_containerized_platform(platform):
    log = logs()
    assert "deployer key: hgd_" in log and "org acme created with owner ops" in log, log
    key = next(line.split()[-1] for line in log.splitlines() if "API key:" in line)
    assert call("PUT", "/v1/providers/none", {"url": "https://example.com", "api": "none", "key": ""}, key)["api"] == "none"
    assert call("PUT", "/v1/harnesses/echo", archive({"request.sh": REQUEST}), key, "application/gzip")["name"] == "echo"
    r = call("POST", "/v1/responses", {"model": "echo/none/m", "input": "hello from outside"}, key)
    text = r["output"][0]["content"][0]["text"]
    assert r["status"] == "completed", (r, sh("docker", "logs", "--tail", "40", NAME, check=False))
    assert "turn=1 prompt=hello from outside" in text
    assert "gateway=" in text and "gateway=Traceback" not in text and "Connection refused" not in text, text
    detail = call("GET", f"/v1/responses/{r['id']}/detail", key=key)
    assert detail["worker"]
    sid = r["metadata"]["session_id"]
    r2 = call("POST", "/v1/responses", {"model": "echo/none/m", "input": "again", "previous_response_id": r["id"]}, key)
    assert r2["status"] == "completed" and "turn=2" in r2["output"][0]["content"][0]["text"]
    caps = call("GET", f"/v1/responses/{r['id']}/captures", key=key)["data"]
    assert "door.log" in caps, caps
    assert sid == r2["metadata"]["session_id"]
    sh("docker", "restart", NAME)
    up()
    assert logs().count("deployer key:") == 1, "the second boot made nothing"
    assert call("GET", "/v1/me", key=key)["name"] == "ops"
