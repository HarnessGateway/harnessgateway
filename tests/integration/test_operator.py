"""The operator's view (needs HG_TEST_OPERATOR_KEY besides HG_TEST_STACK and HG_TEST_KEY): health, boxes, the
organizations and their limits, usage across them, configuration, versions, stopping a box."""
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


def test_health_config_versions(ops):
    h = ops.get("/internal/health").json()
    assert h["database"]["ok"] and h["redis"]["ok"] and h["objects"]["ok"] and "turns" in h and "boxes" in h
    c = ops.get("/internal/config").json()
    assert "secret_key" not in c["settings"] and "database_url" not in c["settings"] and c["defaults"]["vcpus"] > 0
    v = ops.get("/internal/versions").json()
    assert v["platform"] and v["migrations"]["harnessgateway.migrations"] >= 1
    assert ops.get("/internal/metrics").text.startswith("hg_up")


def test_an_org_is_configured_and_usage_summed(ops, api):
    org = ops.get("/internal/orgs").json()["data"][0]
    before = org["limits"]["standby_s"]
    r = ops.patch(f"/internal/orgs/{org['id']}", json={"limits": {"standby_s": before + 1}})
    assert r.status_code == 200 and r.json()["limits"]["standby_s"] == before + 1
    ops.patch(f"/internal/orgs/{org['id']}", json={"limits": {"standby_s": before}})
    assert ops.patch(f"/internal/orgs/{org['id']}", json={"plan": "x"}).status_code == 400
    assert ops.get("/internal/usage?group_by=org").json()["group_by"] == "org"
    assert api.get("/v1/platform").json()["object"] == "platform"
    assert api.get("/internal/health").status_code == 403                     # an admin key is not the operator


def test_a_standing_box_is_listed_and_stopped(ops, api):
    r = api.post("/v1/responses", json={"model": "shell", "input": "echo warm"}).json()
    sid = r["metadata"]["session_id"]
    box = next((b for b in ops.get("/internal/boxes").json()["data"] if b["session_id"] == sid), None)
    assert box and box["kind"] == "session" and box["status"] in ("running", "standby")
    for _ in range(100):                                  # standing by once the turn's save has landed
        box = next((b for b in ops.get("/internal/boxes").json()["data"] if b["session_id"] == sid), None)
        if not box or box["status"] == "standby":
            break
        time.sleep(0.3)
    if box:
        assert ops.post(f"/internal/boxes/{box['id']}/stop").json()["status"] == "gone"
    assert ops.post("/internal/boxes/box_00000000000000000000000000000000/stop").status_code in (200, 404)
    again = api.post("/v1/responses", json={"model": "shell", "input": "echo again", "previous_response_id": r["id"]}).json()
    assert again["status"] == "completed"                 # a new box served the session
