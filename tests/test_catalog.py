"""The shipped packages: every one validates on contract 7, says what it is, and the template runs
through the whole platform (install.sh on the fake node, request.sh reading request.json, map.py's
events becoming the response), including a continuation."""
import asyncio
import base64

import httpx
import pytest

from harnessgateway import harness
from harnessgateway.catalog import Catalog
from test_api import owner_client, with_worker

NAMES = {"template", "claude-code", "codex", "hermes", "pi", "cline", "kilo", "omp", "openclaw", "dsh", "command-code"}


def test_every_package_validates():
    catalog = Catalog()
    assert set(catalog.names()) == NAMES
    for name in catalog.names():
        config = harness.validate(catalog.path(name))
        info = catalog.info(name)
        assert config.cli and config.version, name
        assert info["intro"] and info["docs"] and "checks" in info["verified"], name
        assert (catalog.path(name) / "map.py").is_file() or (catalog.path(name) / "response.sh").is_file(), name
        if name != "template":
            assert config.provider and config.model, name
            assert "input" in config.accepts and "previous_response_id" in config.accepts, name
    files = {f["path"] for f in catalog.files("template")}
    assert {"harness.json", "install.sh", "request.sh", "map.py", "instructions.py", "seeds/workspace/AGENTS.md"} <= files


def test_template_runs_end_to_end(tmp_path, vault):
    from harnessgateway.api.app import Service, create_app
    from harnessgateway.api.config import Settings
    from harnessgateway.worker import Worker
    from fakes import FakeNode
    settings = Settings(data_dir=tmp_path / "data", public_url="http://api.test", signup="open", join_token="jt", deployer_key="hgd_x")
    app = create_app(Service(settings, vault))
    transport = httpx.ASGITransport(app=app)
    worker = Worker("http://api.test", FakeNode(), tmp_path / "cache", name="w1", slots=1, version="t", join_token="jt", transport=transport)

    async def go():
        client, key, org = await owner_client(transport)
        await client.put("/v1/providers/prov", json={"url": "https://api.example.com/v1", "api": "openai-chat", "key": "sk-test"})
        h = (await client.post("/v1/harnesses/tpl", json={"catalog": "template"})).json()
        assert h["origin"] == "catalog/template"
        r = (await client.post("/v1/responses", json={"model": "tpl/prov/m1", "input": "Say hello to the team", "instructions": "Answer in French."})).json()
        assert r["status"] == "completed", r
        assert [i["type"] for i in r["output"]] == ["shell_call", "shell_call_output", "apply_patch_call", "apply_patch_call_output", "message"]
        assert r["output"][-1]["content"][0]["text"] == "You said: Say hello to the team" and r["usage"]["total_tokens"] == 19
        sid = r["metadata"]["session_id"]
        files = {f["path"] for f in (await client.get(f"/v1/sessions/{sid}/files")).json()["data"]}
        assert files == {"AGENTS.md", "NOTES.txt"}
        agents = (await client.get(f"/v1/sessions/{sid}/files/AGENTS.md")).text
        assert "Working agreement" in agents and "Answer in French." in agents
        r2 = (await client.post("/v1/responses", json={"model": "tpl", "input": "again", "previous_response_id": r["id"]})).json()
        assert r2["status"] == "completed" and r2["metadata"]["turn"] == "2"
        detail = (await client.get(f"/v1/responses/{r2['id']}/detail")).json()
        assert "turn=2" in detail["log"] and "resume=sess_" in detail["log"]
        assert "Answer in French." not in (await client.get(f"/v1/sessions/{sid}/files/AGENTS.md")).text   # the section follows the request
    asyncio.run(with_worker(worker, go()))
