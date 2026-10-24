"""The shipped packages: every one validates on contract 7, says what it is, and the template runs
through the whole platform (install.sh on the fake node, request.sh reading request.json, map.py's
events becoming the response), including a continuation."""
import asyncio
import io
import json
import tarfile
from pathlib import Path

import httpx

from harnessgateway.contract import package as harness
from harnessgateway.domain.catalog import Catalog
from test_api import owner_client, platform, with_worker  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def folder_archive(folder: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(folder.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts:
                tar.add(f, arcname=f.relative_to(folder).as_posix())
    return buf.getvalue()


NAMES = {"template", "shell", "claude-code", "codex", "hermes", "pi", "cline", "kilo", "omp", "openclaw", "dsh", "command-code"}
NO_PROVIDER = {"template", "shell"}                  # the stand-in CLI, and any command line


def test_every_package_validates():
    catalog = Catalog()
    assert set(catalog.names()) == NAMES
    for name in catalog.names():
        config = harness.validate(catalog.path(name))
        info = catalog.info(name)
        assert config.cli and config.version, name
        assert info["intro"] and info["docs"] and "checks" in info["verified"], name
        assert (catalog.path(name) / "map.py").is_file() or (catalog.path(name) / "response.sh").is_file(), name
        if name not in NO_PROVIDER:
            assert config.provider and config.model and config.needs_provider, name
            assert "input" in config.accepts and "previous_response_id" in config.accepts, name
    files = {f["path"] for f in catalog.files("template")}
    assert {"harness.json", "install.sh", "request.sh", "map.py", "instructions.py", "seeds/workspace/AGENTS.md"} <= files


def test_template_runs_end_to_end(tmp_path, vault):
    from harnessgateway.api.app import Service, create_app
    from harnessgateway.config import Settings
    from harnessgateway.services.worker import Worker
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


def test_the_shell_package_runs_any_command_without_a_provider(platform):  # noqa: F811
    """No provider in the organization at all: the package declares no provider_env, so the turn gets no
    token and no gateway, and the sandbox's only reach is the (empty) egress list."""
    app, transport, worker, node = platform

    async def go():
        client, key, org = await owner_client(transport)
        pushed = await client.put("/v1/harnesses/sh", content=folder_archive(Catalog(ROOT / "catalog").path("shell")))
        assert pushed.status_code == 200, pushed.text
        r = (await client.post("/v1/responses", json={"model": "sh", "input": "echo hello > a.txt && wc -c < a.txt", "instructions": "export GREETING=hi"})).json()
        assert r["status"] == "completed" and r["output_text"] == "6\n" if "output_text" in r else True
        items = r["output"]
        assert [i["type"] for i in items] == ["shell_call", "shell_call_output", "message"]
        assert items[1]["output"][0]["outcome"]["exit_code"] == 0 and items[2]["content"][0]["text"] == "6\n"
        assert r["model"] == "sh" and r["metadata"]["turn"] == "1"
        d = (await client.get(f"/v1/responses/{r['id']}/detail")).json()
        assert "running the instructions as setup" in d["log"]
        # the workspace persists: a continuation reads the file; a failing command is still a completed turn with its exit code
        r2 = (await client.post("/v1/responses", json={"model": "sh", "input": "cat a.txt; exit 3", "previous_response_id": r["id"]})).json()
        assert r2["status"] == "completed" and r2["output"][2]["content"][0]["text"] == "hello\n" and r2["output"][1]["output"][0]["outcome"]["exit_code"] == 3
        captures = (await client.get(f"/v1/responses/{r['id']}/captures")).json()["data"]
        assert not any(c.startswith("provider/") for c in captures)                                # nothing went to any provider
        # streamed
        async with client.stream("POST", "/v1/responses", json={"model": "sh", "input": "printf streamed", "stream": True}) as s:
            events = [json.loads(line[6:]) for line in (await s.aread()).decode().splitlines() if line.startswith("data: ")]
        assert any(e["type"] == "response.output_text.delta" and e["delta"] == "streamed" for e in events) and events[-1]["type"] == "response.completed"
    asyncio.run(with_worker(worker, go()))
