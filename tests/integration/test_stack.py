"""Against a live stack (an API with a node): the contract end to end, on the `shell` package.

    HG_TEST_STACK=1 HG_TEST_URL=http://localhost:8000 HG_TEST_KEY=hg_... .venv/bin/pytest -q tests/integration

These tests install `shell` from `packages/shell` (upgrading it if installed), install and remove a harness
`hg-test-seeds`, and set the organization's egress (to `*.npmjs.org`, `!evil.npmjs.org`, `mcp.hg-test.dev`)."""
import io
import json
import os
import tarfile
import time
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("HG_TEST_STACK"), reason="needs a live stack (HG_TEST_STACK=1)")
PLACEHOLDER_LEN = len("hgs_") + 32


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=os.environ.get("HG_TEST_URL", "http://localhost:8000"),
                      headers={"authorization": f"Bearer {os.environ['HG_TEST_KEY']}"}, timeout=600) as c:
        r = c.put("/v1/egress", json={"egress": ["*.npmjs.org", "!evil.npmjs.org", "mcp.hg-test.dev"]})
        r.raise_for_status()
        r = c.post("/v1/harnesses", files={"package": ("shell.tgz", pack(SHELL))})
        assert r.status_code == 200, r.text
        yield c


SHELL = Path(__file__).resolve().parents[2] / "packages" / "shell"


def pack(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for p in sorted(folder.rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                tar.add(p, arcname=p.relative_to(folder).as_posix())
    return buf.getvalue()


def turn(api: httpx.Client, body: dict) -> dict:
    r = api.post("/v1/responses", json={"model": "shell", **body})
    assert r.status_code == 200, r.text
    return r.json()


def text(response: dict) -> str:
    return "".join(c["text"] for o in response["output"] if o["type"] == "message" for c in o["content"])


def test_a_turn_runs_in_a_sandbox_and_the_session_continues(api):
    r1 = turn(api, {"input": "echo one > a.txt; id -u; pwd"})
    assert r1["status"] == "completed" and text(r1) == "10001\n/workspace\n"
    assert r1["output"][0]["type"] == "shell_call" and r1["metadata"]["session_id"].startswith("cntr_")
    r2 = turn(api, {"input": "cat a.txt; echo $HG_TURN", "previous_response_id": r1["id"]})
    assert r2["status"] == "completed" and text(r2) == "one\n2\n"
    assert r2["metadata"]["session_id"] == r1["metadata"]["session_id"]
    files = api.get(f"/v1/containers/{r1['metadata']['session_id']}/files").json()["data"]
    assert "/workspace/a.txt" in {f["path"] for f in files}
    got = api.get(f"/v1/responses/{r2['id']}").json()
    assert got["output"] == r2["output"]
    sid = r1["metadata"]["session_id"]
    assert api.get(f"/v1/containers/{sid}").json()["metadata"]["created_by"].startswith("key:key_")
    listed = api.get(f"/v1/containers/{sid}/responses").json()
    assert [x["id"] for x in listed["data"]] == [r1["id"], r2["id"]] and listed["data"][1]["output"] == r2["output"]
    assert api.get(f"/v1/containers/{sid}/responses?limit=1&after={r1['id']}").json()["data"][0]["id"] == r2["id"]
    for _ in range(50):                                   # the harness record follows the node's finish report
        traces = api.get(f"/v1/containers/{sid}/traces").json()
        if sum(rec["kind"] == "harness" for rec in traces["data"]) == 2:
            break
        time.sleep(0.2)
    assert [rec["response_id"] for rec in traces["data"] if rec["kind"] == "harness"] == [r1["id"], r2["id"]]
    assert all("stdout_key" not in rec["data"] for rec in traces["data"])


def test_an_earlier_response_forks_from_its_checkpoint(api):
    r1 = turn(api, {"input": "echo one > a.txt; ls *.txt"})
    r2 = turn(api, {"input": "echo two > b.txt; ls *.txt", "previous_response_id": r1["id"]})
    assert text(r2) == "a.txt\nb.txt\n" and r2["metadata"]["session_id"] == r1["metadata"]["session_id"]
    for _ in range(100):                                  # the fork needs r1's checkpoint, recorded once its save landed
        f = api.post("/v1/responses", json={"model": "shell", "input": "ls *.txt; cat a.txt", "previous_response_id": r1["id"]})
        if f.status_code != 409:
            break
        time.sleep(0.3)
    assert f.status_code == 200, f.text
    fork = f.json()
    assert fork["status"] == "completed" and text(fork) == "a.txt\none\n"          # the state after r1: no b.txt
    sid, parent = fork["metadata"]["session_id"], r1["metadata"]["session_id"]
    assert sid != parent
    assert api.get(f"/v1/containers/{sid}").json()["metadata"]["forked_from"] == r1["id"]
    assert "forked_from" not in api.get(f"/v1/containers/{parent}").json()["metadata"]
    r3 = turn(api, {"input": "ls *.txt", "previous_response_id": r2["id"]})               # the parent kept its own state
    assert text(r3) == "a.txt\nb.txt\n" and r3["metadata"]["session_id"] == parent
    r4 = turn(api, {"input": "ls *.txt", "previous_response_id": fork["id"]})            # and the fork continues as a session
    assert text(r4) == "a.txt\n" and r4["metadata"]["session_id"] == sid
    assert api.delete(f"/v1/containers/{parent}").json()["deleted"]                # the fork outlives its parent
    r5 = turn(api, {"input": "cat a.txt", "previous_response_id": r4["id"]})
    assert r5["status"] == "completed" and text(r5) == "one\n"


def test_the_stream_is_the_responses_event_sequence(api):
    with api.stream("POST", "/v1/responses", json={"model": "shell", "input": "echo streamed", "stream": True}) as r:
        kinds = [line[len("event: "):] for line in r.iter_lines() if line.startswith("event: ")]
    assert kinds[:2] == ["response.created", "response.in_progress"] and kinds[-1] == "response.completed"
    assert "response.output_text.delta" in kinds


def test_egress_follows_the_organization_and_deny_entries_win(api):
    cmd = ("for h in registry.npmjs.org evil.npmjs.org example.com; do "
           "printf '%s %s\\n' $h $(curl -s -o /dev/null -w '%{http_connect}' https://$h/); done")
    r = turn(api, {"input": cmd})
    assert text(r).split("\n")[:3] == ["registry.npmjs.org 200", "evil.npmjs.org 403", "example.com 403"]
    for _ in range(50):                                   # the door's record follows the node's finish report
        trace = api.get(f"/v1/traces/{r['id']}").json()
        if door := next((rec for rec in trace["records"] if rec["kind"] == "door"), None):
            break
        time.sleep(0.2)
    assert door, trace
    verdicts = {c["host"]: c["verdict"] for c in door["data"]["connections"]}
    assert verdicts == {"registry.npmjs.org": "allowed", "evil.npmjs.org": "denied", "example.com": "denied"}


def test_a_caller_mcp_token_never_enters_the_sandbox(api):
    tools = [{"type": "mcp", "server_label": "t", "server_url": "https://mcp.hg-test.dev/mcp",
              "authorization": "caller-secret-token"}]
    r = turn(api, {"input": "jq -r '.[0].headers.Authorization' $HG_MCP; grep -c caller-secret-tok''en "
                            "/run/hg/request.json || true", "tools": tools})         # the value, not this command
    seen = text(r).split("\n")
    assert seen[0].startswith("Bearer hgs_") and len(seen[0]) == len("Bearer ") + PLACEHOLDER_LEN and seen[1] == "0"
    echoed = api.get(f"/v1/responses/{r['id']}").json()["tools"][0]["authorization"]
    assert echoed.startswith("hgs_") and "caller-secret-token" not in json.dumps(r)
    denied = api.post("/v1/responses", json={"model": "shell", "input": "true", "tools": [
        {"type": "mcp", "server_label": "x", "server_url": "https://example.org/mcp"}]})
    assert denied.status_code == 400 and denied.json()["error"]["code"] == "mcp_server_not_allowed"


def test_cancel_stops_a_running_turn(api):
    r = api.post("/v1/responses", json={"model": "shell", "input": "sleep 60; echo late", "background": True})
    rid = r.json()["id"]
    for _ in range(100):
        if api.get(f"/v1/responses/{rid}").json()["status"] == "in_progress":
            break
        time.sleep(0.2)
    api.post(f"/v1/responses/{rid}/cancel")
    for _ in range(200):
        got = api.get(f"/v1/responses/{rid}").json()
        if got["status"] in ("cancelled", "failed", "completed", "incomplete"):
            break
        time.sleep(0.2)
    assert got["status"] == "cancelled" and got["metadata"]["hg_reason"] == "cancelled"


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


def test_a_package_adds_the_organizations_seeds_settings_secrets_and_mcp(api):
    r = api.post("/v1/harnesses", files={"package": ("seeds.tgz", seeded_archive())})
    assert r.status_code == 200, r.text
    assert r.json()["digest"] and r.json()["name"] == "hg-test-seeds"
    try:
        r = api.patch("/v1/harnesses/hg-test-seeds", json={"secrets": {"T": "test-token-123"}, "settings": {"GREETING": "hi"}})
        assert r.status_code == 200 and r.json()["secrets"]["T"]["set"] is True and "test-token-123" not in r.text
        cmd = ("cat $HOME/AGENTS.md hello.txt; echo $GREETING; echo ${#T}; jq -r '.[0].headers.Authorization' $HG_MCP;"
               " curl -s https://mcp.hg-test.dev/mcp -H \"Authorization: $(jq -r '.[0].headers.Authorization' $HG_MCP)\"")
        got = api.post("/v1/responses", json={"model": "hg-test-seeds", "input": cmd}).json()
        assert got["status"] == "completed", got
        lines = text(got).split("\n")
        assert lines[:4] == ["be kind", "from the seeds", "hi", str(PLACEHOLDER_LEN)]
        assert lines[4].startswith("Bearer hgs_") and "test-token-123" not in json.dumps(got)
        if os.environ.get("HG_TEST_MCP_SERVER"):             # a test server answering mcp.hg-test.dev, checking the token
            assert json.loads(lines[5]) == {"authorized": True}
    finally:
        api.delete("/v1/harnesses/hg-test-seeds")
