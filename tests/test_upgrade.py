"""A 7.1.0 data directory boots on this version: its organization, members, key, provider, harness
repositories, sessions, responses, traces, usage and rodeo read as they were, the sealed provider
key still opens under the same master key, and new work goes on in the same organization. The
fixture is tests/fixtures/data-7.1.0.tgz, made by a 7.1.0 platform."""
import asyncio
import json
import tarfile
from pathlib import Path

import httpx
import pytest

from harnessgateway.api.app import Service, create_app
from harnessgateway.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "data-7.1.0.tgz"


@pytest.fixture
def old(tmp_path, monkeypatch):
    with tarfile.open(FIXTURE) as tar:
        tar.extractall(tmp_path, filter="data")
    facts = json.loads((tmp_path / "facts.json").read_text())
    monkeypatch.setenv("HG_MASTER_KEY_FILE", str(tmp_path / "master.key"))
    monkeypatch.delenv("HG_MASTER_KEY", raising=False)
    settings = Settings(data_dir=tmp_path / "data", public_url="http://api.test", deployer_key=facts["deployer_key"], sandbox="process")
    return Service(settings), facts


def test_a_7_1_data_directory_reads_as_it_was(old):
    service, facts = old
    org = service.orgs.get(facts["org"])
    assert org.plan == "free" and not org.trace_sharing and org.trace_sharing_since == 0 and org.ceilings.max_runs == 4   # its own limits, kept
    assert org.ceilings.requests_per_minute == 120                                        # a limit 7.1 did not have takes the default
    assert {m.name: m.role for m in service.members.list(org.name)} == {"chase": "owner", "dev": "member"}
    assert [t.name for t in service.teams.list(org.name)] == ["platform"]
    who = service.keys.resolve(facts["key"])
    assert who.org == org.name and who.name == "chase" and who.owner
    assert service.principals.is_deployer(facts["deployer_key"])
    p = service.providers.get(org.name, "prov")
    assert service.providers.open_key(p) == "sk-fixture"                                    # sealed by 7.1, opened by 8
    assert {h.name: h.team for h in service.harnesses.list(org.name)} == {"echo": "", "echo-platform": "platform"}
    assert service.harnesses.config_at(org.name, "echo", service.harnesses.get(org.name, "echo").head).cli == "echo-cli"
    sessions = service.sessions.list(who)
    assert facts["session"] in {s.id for s in sessions}
    for rid in facts["responses"]:
        r = service.sessions.turn(who, rid)
        body = service.sessions.response_object(r)
        assert r.final and body["id"] == rid and body["status"] in ("completed", "failed", "cancelled", "incomplete")
    trace = service.sessions.trace(who, facts["session"])
    assert [t["kind"] for t in trace][:2] == ["request", "response"]
    assert service.sessions.events.read(org.name, facts["responses"][0])                  # the chunks are readable
    usage = service.metering.summary(org.name, days=0)
    assert usage["totals"]["turns"] == 3 and usage["totals"]["input_tokens"] > 0 and usage["totals"]["cpu_seconds"] == 0   # not metered then
    assert service.rodeos.get(who, facts["rodeo"])["id"] == facts["rodeo"]
    assert service.dataset.summary()["orgs"] == 0                                           # nothing shares until an owner says so


def test_work_goes_on_in_the_old_organization(old):
    service, facts = old
    app = create_app(service)

    async def go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://api.test",
                                     headers={"Authorization": f"Bearer {facts['key']}"}) as c:
            me = (await c.get("/v1/me")).json()
            assert me["org"] == facts["org"] and me["plan"] == "free" and me["teams"] == []
            assert (await c.get("/v1/usage?days=0")).json()["totals"]["turns"] == 3
            r = await c.post("/v1/responses", json={"model": "echo", "input": "again", "background": True})
            assert r.status_code == 200 and r.json()["status"] == "in_progress"           # queued in the old organization
            got = (await c.get(f"/v1/sessions/{facts['session']}")).json()
            assert got["responses"][:2] == facts["responses"][:2]
            assert (await c.post("/v1/responses", json={"model": "echo", "input": "more", "previous_response_id": facts["responses"][0], "background": True})).status_code in (200, 409)
    asyncio.run(go())
