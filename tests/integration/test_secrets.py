"""The secrets invariant on a live stack (SPEC §8): a provider key, a harness secret and a caller's MCP credential go
in, and come out nowhere: not in the sandbox (its environment, its request, its MCP file), not in any response, event
stream, trace, listing or audit row the API returns."""
import json
import os
import time

import pytest

from .conftest import seeded_archive

pytestmark = pytest.mark.skipif(not os.environ.get("HG_TEST_STACK"), reason="needs a live stack (HG_TEST_STACK=1)")

PROVIDER_KEY = "sk-provider-key-9f3e1c7ab2d4"
HARNESS_SECRET = "harness-secret-value-5a6b7c8d"
CALLER_TOKEN = "caller-mcp-token-1e2f3a4b"
SECRETS = (PROVIDER_KEY, HARNESS_SECRET, CALLER_TOKEN)
# everything in the sandbox a secret could have reached: its environment, the request, the MCP file, the inputs folder
LOOK = "env; cat $HG_REQUEST $HG_MCP; ls -R /run/hg; cat $HG_HISTORY"


def leaked(text: str) -> list[str]:
    return [s for s in SECRETS if s in text]


def test_no_secret_comes_back_out(api):
    api.put("/v1/providers/hg-test-secret", json={"api": "openai", "base_url": "https://api.openai.com", "key": PROVIDER_KEY}
            ).raise_for_status()
    assert api.post("/v1/harnesses", files={"package": ("seeds.tgz", seeded_archive())}).status_code == 200
    try:
        api.patch("/v1/harnesses/hg-test-seeds", json={"secrets": {"T": HARNESS_SECRET}}).raise_for_status()
        tools = [{"type": "mcp", "server_label": "caller", "server_url": "https://mcp.hg-test.dev/mcp",
                  "authorization": CALLER_TOKEN, "headers": {"X-Key": CALLER_TOKEN}}]
        r = api.post("/v1/responses", json={"model": "hg-test-seeds", "input": LOOK, "tools": tools})
        assert r.status_code == 200, r.text
        response = r.json()
        assert response["status"] == "completed", response
        rid, sid = response["id"], response["metadata"]["session_id"]
        seen = {"response": r.text, "get": api.get(f"/v1/responses/{rid}").text,
                "stream": api.get(f"/v1/responses/{rid}?stream=true&starting_after=-1").text,
                "input_items": api.get(f"/v1/responses/{rid}/input_items").text,
                "container": api.get(f"/v1/containers/{sid}").text,
                "container_responses": api.get(f"/v1/containers/{sid}/responses").text,
                "harness": api.get("/v1/harnesses/hg-test-seeds").text,
                "providers": api.get("/v1/providers").text, "provider": api.get("/v1/providers/hg-test-secret").text,
                "audit": api.get("/v1/audit?limit=100").text}
        for _ in range(50):                                   # the harness and door records follow the finish report
            trace = api.get(f"/v1/traces/{rid}").json()
            if {rec["kind"] for rec in trace["records"]} >= {"harness", "door"}:
                break
            time.sleep(0.2)
        seen["trace"] = json.dumps(trace)
        seen["session_traces"] = api.get(f"/v1/containers/{sid}/traces").text
        assert {where: leaked(text) for where, text in seen.items() if leaked(text)} == {}
        # what the sandbox saw stands in for the placeholders, never the values
        sandbox = "".join(c["text"] for o in response["output"] if o["type"] == "message" for c in o["content"])
        assert "T=hgs_" in sandbox and "Bearer hgs_" in sandbox and sandbox.count("hgs_") >= 3
    finally:
        api.delete("/v1/harnesses/hg-test-seeds")
        api.delete("/v1/providers/hg-test-secret")
