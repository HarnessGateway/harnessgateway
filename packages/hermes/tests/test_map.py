"""Offline tests for the hermes mapper: recorded Hermes stream-json (`hermes chat --format stream-json`, from real
turns) fed through map.Mapper; every printed line must pass the platform's own reader (contract.check_output) and
the outcome must be the one the turn had.

Run from the repo root: .venv/bin/pytest -q packages/hermes
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from hgkit import Emitter, Request, State

from harnessgateway.domain.contract import check_output

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
REPO = PACKAGE.parents[1]

_spec = importlib.util.spec_from_file_location("hermes_map", PACKAGE / "map.py")
hmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hmap)


def run_fixture(name: str, tmp_path: Path, labels: list[str] | None = None, exit_code: int = 0):
    buf = io.StringIO()
    state = State(str(tmp_path / "state"))
    mapper = hmap.Mapper(Emitter(buf), state, labels)
    for line in (HERE / "fixtures" / name).read_text().splitlines():
        mapper.line(line)
    mapper.finish(exit_code, "")
    events, o = check_output(buf.getvalue())       # raises OutputError on anything the platform refuses
    assert buf.getvalue().splitlines()[-1].startswith('{"type":"response.'), "the terminal event is last"
    return {"status": o.status, "output": o.output, "usage": o.usage, "error": o.error, "incomplete": o.incomplete,
            "meta": o.metadata, "events": events, "state": state}


def types(output):
    return [i["type"] for i in output]


def text_of(output):
    return "".join(c.get("text", "") for i in output if i["type"] == "message" for c in i["content"])


@pytest.mark.parametrize("name", ["turn1_write_run.jsonl", "openai_turn1.jsonl"])
def test_turn1_write_and_run(name, tmp_path):
    r = run_fixture(name, tmp_path)
    assert r["status"] == "completed"
    assert types(r["output"]) == ["apply_patch_call", "apply_patch_call_output", "shell_call", "shell_call_output",
                                  "message"]
    patch = r["output"][0]
    assert patch["operation"]["type"] == "create_file" and patch["operation"]["path"] == "hello.py"
    assert patch["operation"]["diff"].startswith("+print(") and "hello from hermes" in patch["operation"]["diff"]
    assert r["output"][1]["call_id"] == patch["call_id"] and r["output"][1]["status"] == "completed"
    shell, shell_out = r["output"][2], r["output"][3]
    assert "hello.py" in shell["action"]["commands"][0]
    assert shell_out["call_id"] == shell["call_id"]
    assert shell_out["output"][0]["stdout"] == "hello from hermes"
    assert shell_out["output"][0]["outcome"] == {"type": "exit", "exit_code": 0}
    assert "hello from hermes" in text_of(r["output"])
    assert len([e for e in r["events"] if e["type"] == "response.output_text.delta"]) > 10   # token by token
    u = r["usage"]
    assert u["input_tokens"] > u["input_tokens_details"]["cached_tokens"] > 0 and u["output_tokens"] > 0
    assert u["total_tokens"] == u["input_tokens"] + u["output_tokens"]
    sid = r["meta"]["hermes_session_id"]
    assert sid.startswith("local-") and r["state"].get("hermes_session") == sid
    assert r["meta"]["hermes_version"] == hmap.CLI_VERSION


def test_dropped_paragraph_break_is_restored(tmp_path):
    """The live stream lost the blank line before "Exit code 0."; the message ends with Hermes's stored reply."""
    r = run_fixture("openai_turn1.jsonl", tmp_path)
    deltas = "".join(e["delta"] for e in r["events"] if e["type"] == "response.output_text.delta")
    assert "hermesExit code" in deltas
    assert text_of(r["output"]).endswith("hello from hermes\n\nExit code 0.")


def test_turn2_recall_has_no_tools(tmp_path):
    r = run_fixture("turn2_recall.jsonl", tmp_path)
    assert r["status"] == "completed" and types(r["output"]) == ["message"]
    assert "hello.py" in text_of(r["output"]) and "hello from hermes" in text_of(r["output"])


def test_patch_is_an_update_with_its_diff(tmp_path):
    r = run_fixture("patch_edit.jsonl", tmp_path)
    assert r["status"] == "completed"
    patch = next(i for i in r["output"] if i["type"] == "apply_patch_call")
    assert patch["operation"] == {"type": "update_file", "path": "hello.py",
                                  "diff": "@@ -1 +1 @@\n-print('hello from hermes')\n+print('hello again from hermes')\n"}
    assert any(i["type"] == "shell_call_output" and i["output"][0]["stdout"] == "hello again from hermes"
               for i in r["output"])


def test_parallel_calls_keep_their_outputs(tmp_path):
    r = run_fixture("input_file_parallel.jsonl", tmp_path)
    assert r["status"] == "completed"
    read = next(i for i in r["output"] if i["type"] == "function_call")
    assert read["name"] == "read_file" and json.loads(read["arguments"])["path"].endswith("data.csv")
    read_out = next(i for i in r["output"] if i["type"] == "function_call_output")
    assert read_out["call_id"] == read["call_id"] and "alpha,1" in read_out["output"]
    calls = {i["call_id"] for i in r["output"] if i["type"] in ("shell_call", "function_call")}
    outputs = {i["call_id"] for i in r["output"] if i["type"] in ("shell_call_output", "function_call_output")}
    assert calls == outputs
    assert types(r["output"])[0] == "message" and types(r["output"])[-1] == "message"   # text before and after tools
    assert "6" in text_of(r["output"])


def test_instructions_turn(tmp_path):
    r = run_fixture("instructions_french.jsonl", tmp_path)
    assert r["status"] == "completed" and text_of(r["output"]).startswith("Bonjour")


def test_max_tool_calls_is_incomplete_with_the_reply(tmp_path):
    r = run_fixture("max_tool_calls.jsonl", tmp_path)
    assert r["status"] == "incomplete" and r["incomplete"] == {"reason": "max_messages"}
    assert types(r["output"]) == ["shell_call", "shell_call_output", "message"]   # the reply was not streamed
    assert text_of(r["output"]).startswith("Only the first command ran")


def test_provider_error_is_a_clean_failure(tmp_path):
    r = run_fixture("provider_error.jsonl", tmp_path)
    assert r["status"] == "failed" and r["output"] == []           # Hermes's explanation is not model output
    assert r["error"]["code"] == "provider_error"
    assert r["error"]["message"].startswith("HTTP 400") and "no-such-model" in r["error"]["message"]
    assert r["usage"] is None


def test_interrupted_command(tmp_path):
    r = run_fixture("interrupted.jsonl", tmp_path)
    assert r["status"] == "failed" and r["error"]["code"] == "cancelled"
    shell, out = r["output"]
    assert shell["action"]["commands"] == ["sleep 300"]
    assert out["output"][0]["outcome"] == {"type": "exit", "exit_code": 130}


def test_mcp_call(tmp_path):
    r = run_fixture("mcp_call.jsonl", tmp_path, labels=["deepwiki"])
    assert r["status"] == "completed"
    mcp = [i for i in r["output"] if i["type"] == "mcp_call"]
    assert len(mcp) == 1
    assert mcp[0]["server_label"] == "deepwiki" and mcp[0]["name"] == "read_wiki_structure"
    assert json.loads(mcp[0]["arguments"]) == {"repoName": "anthropics/claude-code"}
    assert mcp[0]["status"] == "completed" and mcp[0]["output"].startswith("Available pages for anthropics/claude-code")
    added = [e for e in r["events"] if e["type"] == "response.output_item.added" and e["item"]["type"] == "mcp_call"]
    assert added[0]["item"]["status"] == "in_progress"         # added when called, done when the result came


@pytest.mark.parametrize("name", sorted(p.name for p in (HERE / "fixtures").glob("*.jsonl")))
def test_every_fixture_validates(name, tmp_path):
    r = run_fixture(name, tmp_path, labels=["deepwiki"])
    assert r["status"] in ("completed", "failed", "incomplete")


def test_unfinished_calls_and_no_result(tmp_path):
    buf = io.StringIO()
    mapper = hmap.Mapper(Emitter(buf), State(str(tmp_path)), ["docs"])
    mapper.line('{"type": "tool_use", "name": "terminal", "tool_call_id": "c1", "input": {"command": "sleep 9"}}')
    mapper.line('{"type": "tool_use", "name": "mcp__docs__search", "tool_call_id": "c2", "input": {"q": "x"}}')
    mapper.finish(1, "boom")
    _, o = check_output(buf.getvalue())
    assert o.status == "failed" and "boom" in o.error["message"]
    assert [i["type"] for i in o.output] == ["shell_call", "shell_call_output", "mcp_call"]
    assert o.output[1]["status"] == "incomplete" and o.output[2]["status"] == "failed"


MCP_JSON = [  # /run/hg/mcp.json as the platform writes it: header values are placeholders
    {"label": "docs", "url": "https://mcp.example.com/mcp", "transport": "http",
     "headers": {"Authorization": "Bearer hgs_0123456789abcdef0123456789abcdef"}, "allowed_tools": ["search"]},
    {"label": "wiki.site", "url": "https://wiki.example.com/sse", "transport": "sse", "headers": {},
     "allowed_tools": None},
    {"label": "none", "url": "https://none.example.com/mcp", "transport": "http", "headers": {}, "allowed_tools": []},
]


def test_mcp_servers_come_from_hg_mcp(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(MCP_JSON))
    monkeypatch.setenv("HG_MCP", str(path))
    assert hmap.mcp_config(Request.mcp_servers()) == {
        "docs": {"url": "https://mcp.example.com/mcp",
                 "headers": {"Authorization": "Bearer hgs_0123456789abcdef0123456789abcdef"},
                 "tools": {"include": ["search"]}},
        "wiki.site": {"url": "https://wiki.example.com/sse", "headers": {}, "transport": "sse"},
    }                                                  # an empty allowed_tools offers nothing: the server is left out
    m = hmap.Mapper(Emitter(io.StringIO()), None, ["wiki.site"])
    assert m._mcp_server("mcp__wiki_site__ask") == "wiki_site" and m.labels["wiki_site"] == "wiki.site"


def test_config_and_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HG_MODEL", "deepseek-flash")
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-door:8080")
    req = Request({"input": "hi", "reasoning": {"effort": "low"}, "max_tool_calls": 7})
    for api, url, transport in (("anthropic", "http://hg-door:8080", "anthropic_messages"),
                                ("openai", "http://hg-door:8080/v1", "chat_completions")):
        monkeypatch.setenv("HG_PROVIDER_API", api)
        cfg = hmap.config(req, MCP_JSON[:1])
        provider = cfg["providers"][cfg["model"]["provider"]]
        assert provider["base_url"] == url and provider["transport"] == transport
        assert provider["key_env"] == "HG_PROVIDER_TOKEN" and cfg["model"]["default"] == "deepseek-flash"
    assert cfg["agent"]["reasoning_effort"] == "low" and cfg["agent"]["max_turns"] == 7
    assert cfg["approvals"]["mode"] == "off" and cfg["security"]["allow_lazy_installs"] is False
    assert list(cfg["mcp_servers"]) == ["docs"]
    venv = tmp_path / "home" / ".hermes" / "installs" / "k" / "environments" / "g" / "venv"
    facts = tmp_path / "home" / ".hermes" / "installs" / "k" / "facts.json"
    facts.parent.mkdir(parents=True)
    facts.write_text(json.dumps({"packages": {"venv": {"environment": str(venv)}}}))
    monkeypatch.setattr(hmap, "INSTALL_ROOT", str(tmp_path / "home" / ".hermes"))
    argv = hmap.command("/tmp/q.md", resume="local-1")
    assert argv[0] == str(venv / "bin" / "hermes")
    assert argv[1:] == ["chat", "--query-file", "/tmp/q.md", "--format", "stream-json", "--yolo", "--accept-hooks",
                        "--resume", "local-1"]


def test_environment(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/opt/hg/lib")
    monkeypatch.setenv("NO_PROXY", "hg-door,localhost")
    env = hmap.environment(Request({"input": "hi", "instructions": "Answer in French."}))
    assert "PYTHONPATH" not in env and env["HERMES_HOME"] == hmap.HERMES_HOME
    assert env["NO_PROXY"] == "hg-door,localhost"                       # the platform's, as given
    assert env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] == "Answer in French."
    assert "HERMES_EPHEMERAL_SYSTEM_PROMPT" not in hmap.environment()      # the gateway's own environment


def test_system_and_developer_messages_join_the_instructions():
    req = Request({"instructions": "Answer in French.", "input": [
        {"role": "system", "content": "Be brief."},
        {"role": "developer", "content": [{"type": "input_text", "text": "No emoji."}]},
        {"role": "user", "content": [{"type": "input_text", "text": "hi"}]}]})
    env = hmap.environment(req)
    assert env["HERMES_EPHEMERAL_SYSTEM_PROMPT"] == "Answer in French.\n\nBe brief.\n\nNo emoji."
    assert hmap.prompt(req, []) == "hi"                                   # the query is the user's text only


def test_ignored_fields_are_only_those_present():
    assert hmap.ignored(Request({"input": "hi", "tools": [{"type": "mcp", "server_label": "docs"}]})) == []
    req = Request({"input": "hi", "temperature": 0.2, "max_output_tokens": 100, "parallel_tool_calls": False,
                   "reasoning": {"effort": "low", "summary": "auto"}, "text": {"verbosity": "low"},
                   "tools": [{"type": "web_search"}, {"type": "mcp", "server_label": "docs"}]})
    assert hmap.ignored(req) == ["parallel_tool_calls", "temperature", "max_output_tokens", "tools",
                                 "reasoning.summary", "text.verbosity"]


def test_prompt_names_files_and_format():
    req = Request({"input": "sum it", "text": {"format": {"type": "json_schema", "name": "x",
                                                          "schema": {"type": "object"}}}})
    text = hmap.prompt(req, ["data.csv"])
    assert text.startswith("sum it\n\nAttached files (saved in the working directory): data.csv")
    assert text.endswith('JSON Schema:\n{"type": "object"}')


# ── end to end, offline: map.py run as run.sh runs it, against a fake `hermes` that replays a recorded stream ──
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, signal, sys, time
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "env": {k: os.environ.get(k, "") for k in (
        "HERMES_HOME", "HERMES_EPHEMERAL_SYSTEM_PROMPT", "HG_PROVIDER_TOKEN")}}) + "\n")
if sys.argv[1] == "gateway":                             # runs until it is stopped, then leaves a mark
    def stop(*_):
        open(os.environ["FAKE_LOG"] + ".stopped", "w").close()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    while True:
        time.sleep(1)
if os.environ.get("FAKE_HANG"):
    time.sleep(300)
if "--resume" in sys.argv:
    sid = sys.argv[sys.argv.index("--resume") + 1]
    print(json.dumps({"type": "result", "session_id": "", "exit_code": 1, "text": "", "tokens": {},
                      "error": f"No session found matching '{sid}'."}))
    sys.exit(1)
sys.stdout.write(open(os.environ["FAKE_STREAM"]).read())
'''


def _sandbox(tmp_path: Path, **extra: str) -> dict[str, str]:
    venv = tmp_path / "tools" / "home" / ".hermes" / "installs" / "k" / "environments" / "g" / "venv"
    (venv / "bin").mkdir(parents=True)
    cli = venv / "bin" / "hermes"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    (venv.parents[2] / "facts.json").write_text(json.dumps({"packages": {"venv": {"environment": str(venv)}}}))
    for d in ("workspace", "state", "home"):
        (tmp_path / d).mkdir()
    (tmp_path / "state" / "hermes_session").write_text("local-old")
    data = "data:text/csv;base64,YWxwaGEsMQo="                           # an inline file: "alpha,1"
    (tmp_path / "request.json").write_text(json.dumps({"model": "hermes", "instructions": "Be brief.", "temperature": 0.5,
                                                       "input": [{"role": "user", "content": [
                                                           {"type": "input_text", "text": "make hello.py"},
                                                           {"type": "input_file", "filename": "data.csv",
                                                            "file_data": data}]}]}))
    (tmp_path / "mcp.json").write_text(json.dumps(MCP_JSON[:1]))
    return {**os.environ, "HG_REQUEST": str(tmp_path / "request.json"), "HG_STATE": str(tmp_path / "state"),
            "HG_WORKSPACE": str(tmp_path / "workspace"), "HG_FILES": str(tmp_path / "files"),
            "HG_TOOLS": str(tmp_path / "tools"), "HOME": str(tmp_path / "home"), "HG_MODEL": "deepseek-flash",
            "HG_PROVIDER_API": "anthropic", "HG_PROVIDER_URL": "http://hg-door:8080", "HG_PROVIDER_TOKEN": "turn-token",
            "HG_MCP": str(tmp_path / "mcp.json"),
            "TMPDIR": str(tmp_path), "PYTHONPATH": str(REPO / "sandbox"), "FAKE_LOG": str(tmp_path / "calls.jsonl"),
            "FAKE_STREAM": str(HERE / "fixtures" / "turn1_write_run.jsonl"), **extra}


def test_run_end_to_end_with_lost_session(tmp_path):
    env = _sandbox(tmp_path)
    ws, st, home = tmp_path / "workspace", tmp_path / "state", tmp_path / "home"
    proc = subprocess.run([sys.executable, str(PACKAGE / "map.py")], env=env, cwd=ws, capture_output=True, text=True,
                          timeout=60)
    _, o = check_output(proc.stdout)
    assert o.status == "completed" and "shell_call" in types(o.output) and o.usage["output_tokens"] == 131
    assert o.metadata["hg_ignored_fields"] == "temperature"
    calls = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    gateway = [c for c in calls if c["argv"][0] == "gateway"]
    chats = [c for c in calls if c["argv"][0] == "chat"]
    assert [c["argv"] for c in gateway] == [["gateway", "run"]]
    assert gateway[0]["env"]["HERMES_EPHEMERAL_SYSTEM_PROMPT"] == ""   # instructions are the client's, per session
    assert chats[0]["argv"][-2:] == ["--resume", "local-old"]           # the session was gone: a new one
    assert len(chats) == 2 and "--resume" not in chats[1]["argv"]
    assert chats[1]["env"] == {"HERMES_HOME": str(home / ".hermes"), "HERMES_EPHEMERAL_SYSTEM_PROMPT": "Be brief.",
                               "HG_PROVIDER_TOKEN": "turn-token"}
    query = Path(chats[1]["argv"][chats[1]["argv"].index("--query-file") + 1]).read_text()
    assert query == "make hello.py\n\nAttached files (saved in the working directory): data.csv"
    assert (ws / "data.csv").read_text() == "alpha,1\n"
    cfg = json.loads((home / ".hermes" / "config.yaml").read_text())
    assert cfg["model"]["default"] == "deepseek-flash" and list(cfg["mcp_servers"]) == ["docs"]
    assert (st / "hermes_session").read_text().startswith("local-")
    assert (tmp_path / "calls.jsonl.stopped").exists()           # the gateway was stopped once the turn ended
    assert any(line.startswith("hermes-stream {") for line in proc.stdout.splitlines())


def test_sigterm_stops_the_gateway_and_the_client(tmp_path):
    env = _sandbox(tmp_path, FAKE_HANG="1")
    proc = subprocess.Popen([sys.executable, str(PACKAGE / "map.py")], env=env, cwd=tmp_path / "workspace",
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True, text=True)
    log = tmp_path / "calls.jsonl"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (not log.exists() or log.read_text().count("\n") < 2):
        time.sleep(0.2)
    os.killpg(proc.pid, signal.SIGTERM)                          # what the node does to the turn's process group
    assert proc.wait(15) == 128 + signal.SIGTERM
    assert (tmp_path / "calls.jsonl.stopped").exists()           # the gateway got its SIGTERM (and saved)
    assert '"type":"response.' not in proc.stdout.read()          # a cancelled turn needs no terminal event
