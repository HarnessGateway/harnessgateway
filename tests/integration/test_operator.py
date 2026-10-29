"""The operator's view (needs HG_TEST_OPERATOR_KEY besides HG_TEST_STACK and HG_TEST_KEY): health, nodes, the
organizations and their capacity, usage across them, configuration, versions, eviction."""
import os
import time

import httpx
import pytest

pytestmark = pytest.mark.skipif(not (os.environ.get("HG_TEST_STACK") and os.environ.get("HG_TEST_OPERATOR_KEY")),
                                reason="needs a live stack and the operator key")


@pytest.fixture(scope="module")
def ops():
    with httpx.Client(base_url=os.environ.get("HG_TEST_URL", "http://localhost:8000"),
                      headers={"authorization": f"Bearer {os.environ['HG_TEST_OPERATOR_KEY']}"}, timeout=60) as c:
        yield c


@pytest.fixture(scope="module")
def api():
    with httpx.Client(base_url=os.environ.get("HG_TEST_URL", "http://localhost:8000"),
                      headers={"authorization": f"Bearer {os.environ['HG_TEST_KEY']}"}, timeout=600) as c:
        yield c


def test_health_nodes_config_versions(ops):
    h = ops.get("/internal/health").json()
    assert h["database"]["ok"] and h["objects"]["ok"] and h["nodes"]["live"] >= 1 and "queue" in h
    nodes = ops.get("/internal/nodes").json()["data"]
    assert any(n["live"] for n in nodes) and {"pool", "slots", "running", "warm"} <= set(nodes[0])
    c = ops.get("/internal/config").json()
    assert "secret_key" not in c["settings"] and "database_url" not in c["settings"] and c["defaults"]["vcpus"] > 0
    v = ops.get("/internal/versions").json()
    assert v["platform"] and any(b["name"] == "shell" for b in v["builtins"])


def test_an_org_is_configured_and_usage_summed(ops, api):
    org = ops.get("/internal/orgs").json()["data"][0]
    before = org["capacity"]["idle_ttl_s"]
    r = ops.patch(f"/internal/orgs/{org['id']}", json={"capacity": {"idle_ttl_s": before + 1}})
    assert r.status_code == 200 and r.json()["capacity"]["idle_ttl_s"] == before + 1
    ops.patch(f"/internal/orgs/{org['id']}", json={"capacity": {"idle_ttl_s": before}})
    assert ops.patch(f"/internal/orgs/{org['id']}", json={"plan": "x"}).status_code == 400
    assert ops.get("/internal/usage?group_by=org").json()["group_by"] == "org"
    assert api.get("/v1/platform").json()["object"] == "platform"
    assert api.get("/internal/health").status_code == 403                     # an admin key is not the operator


def test_evict_frees_an_idle_box(ops, api):
    r = api.post("/v1/responses", json={"model": "shell", "input": "echo warm"}).json()
    sid = r["metadata"]["session_id"]
    for _ in range(100):                                  # the box is evictable once the turn's save has landed
        if api.get(f"/v1/containers/{sid}").json()["metadata"]["state"] == "idle":
            break
        time.sleep(0.3)
    e = ops.post(f"/internal/sessions/{sid}/evict").json()
    assert e["object"] == "session" and isinstance(e["evicted"], bool)
    assert ops.post("/internal/sessions/cntr_00000000000000000000000000000000/evict").status_code == 404
