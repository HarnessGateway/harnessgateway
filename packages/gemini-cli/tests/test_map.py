"""Offline: recorded `gemini -o stream-json` streams fed through map.py's Mapper; every printed line must pass the
platform's own reader (contract.check_output). The `mock_*` streams are the real CLI (0.65.0, in a throwaway
sandbox container) talking to a scripted Gemini API server and, for MCP, a minimal MCP server; `platform_*` is a real
platform turn against Google with an invalid key."""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from hgkit import Emitter

from harnessgateway.domain.contract import check_output

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
REPO = PACKAGE.parents[1]

_spec = importlib.util.spec_from_file_location("gemini_map", PACKAGE / "map.py")
gmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gmap)


def replay(lines: list[str], exit_code: int = 0, stderr: str = "", labels: list[str] | None = None):
    buf = io.StringIO()
    mapper = gmap.Mapper(Emitter(stream=buf), mcp_labels=labels)
    for line in lines:
        mapper.line(line)
    mapper.finish(exit_code, stderr)
    events, o = check_output(buf.getvalue())            # raises OutputError on anything invalid
    return events, (o.status, o.output, o.usage, o.error, o.incomplete, o.metadata)


def run(fixture: str, exit_code: int = 0, stderr: str = "", labels: list[str] | None = None):
    return replay((HERE / "fixtures" / f"{fixture}.jsonl").read_text().splitlines(), exit_code, stderr, labels)


def types(output: list[dict]) -> list[str]:
    return [i["type"] for i in output]


def text(output: list[dict]) -> str:
    return "".join(c["text"] for i in output if i["type"] == "message" for c in i["content"])


def test_write_shell_text_usage():
    events, (status, output, usage, error, _, meta) = run("mock_hello")
    assert status == "completed" and error is None
    assert types(output) == ["message", "apply_patch_call", "apply_patch_call_output", "shell_call",
                             "shell_call_output", "message"]
    assert output[1]["operation"] == {"type": "create_file", "path": "hello.py", "diff": "+print('hello')"}
    assert output[3]["action"]["commands"] == ["python3 hello.py"] and output[3]["call_id"] == output[4]["call_id"]
    assert output[4]["output"][0] == {"stdout": "hello", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}
    assert text(output[5:]) == "It printed `hello`."
    assert sum(e["type"] == "response.output_text.delta" for e in events) == 4
    # stats: 3 calls of 1000/1000/1200 prompt tokens (800/800/0 cached), 63 output and 24 thinking tokens
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (3200, 87, 3287)
    assert usage["input_tokens_details"]["cached_tokens"] == 1600
    assert usage["output_tokens_details"]["reasoning_tokens"] == 24
    assert meta == {"gemini_session": "8e3b03b4-1610-4bec-b088-cd93f9cec022", "gemini_model": "gemini-2.5-flash"}


def test_resumed_turn():
    _, (status, output, usage, *_rest) = run("mock_recall")
    assert status == "completed" and types(output) == ["message"] and usage["input_tokens"] == 1200


def test_replace_is_an_update_patch():
    _, (status, output, *_rest) = run("mock_edit_exit3")
    assert status == "completed"
    assert output[0]["operation"] == {"type": "update_file", "path": "style.txt", "diff": "@@\n-red\n+blue"}
    shell = next(i for i in output if i["type"] == "shell_call_output")["output"][0]
    # the stream carries the command's output, not its exit code (here 3): a run the CLI reports as a success is 0
    assert shell == {"stdout": "color: blue\noops", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}


def test_mcp_call():
    _, (status, output, *_rest) = run("mock_mcp_call", labels=["docs"])
    assert status == "completed" and types(output) == ["mcp_call", "message"]
    call = output[0]
    assert (call["server_label"], call["name"], call["status"], call["output"]) == ("docs", "search", "completed",
                                                                                   "3 hits for x")
    assert json.loads(call["arguments"]) == {"q": "x"}


def test_mcp_label_with_underscores_maps_back():
    m = gmap.Mapper(Emitter(stream=io.StringIO()), mcp_labels=["my_docs"])
    assert gmap.mcp_name("my_docs") == "my-docs"
    assert m._mcp("mcp_my-docs_search_all") == ("my_docs", "search_all") and m._mcp("read_file") is None


def test_provider_error_on_the_platform_is_unwrapped():
    _, (status, output, usage, error, *_rest) = run("platform_api_key_invalid")
    assert status == "failed" and error["code"] == "provider_error" and output == [] and usage is None
    assert error["message"] == "[API Error: 400 API key not valid. Please pass a valid API key.]"


def test_provider_error_from_the_api():
    _, (status, _, _, error, *_rest) = run("mock_api_key_invalid", exit_code=144)
    assert status == "failed" and error["message"].startswith("[API Error: 400 API key not valid")


def test_turn_limit_is_incomplete():
    _, (status, output, usage, _, incomplete, meta) = run("mock_max_session_turns", exit_code=53)
    assert status == "incomplete" and incomplete == {"reason": "max_messages"}
    assert "max session turns" in meta["gemini_stop"] and types(output)[-1] == "apply_patch_call_output"
    assert usage["input_tokens"] == 1000


def test_a_stream_cut_off_mid_tool_still_ends_with_one_terminal_event():
    _, (status, output, _, error, *_rest) = run("mock_sleep_cancelled", exit_code=-15)
    assert status == "failed" and error["code"] == "cli_error" and types(output) == ["shell_call"]


def test_failed_tool_results():
    use = {"type": "tool_use", "tool_name": "run_shell_command", "tool_id": "c1", "parameters": {"command": "false"}}
    res = {"type": "tool_result", "tool_id": "c1", "status": "error", "output": "",
           "error": {"type": "TOOL_EXECUTION_ERROR", "message": "exited 1"}}
    use2 = {"type": "tool_use", "tool_name": "read_file", "tool_id": "c2", "parameters": {"file_path": "x"}}
    res2 = {"type": "tool_result", "tool_id": "c2", "status": "error", "error": {"message": "File not found"}}
    done = {"type": "result", "status": "success", "stats": {"total_tokens": 0}}
    _, (status, output, usage, *_rest) = replay([json.dumps(x) for x in (use, res, use2, res2, done)])
    assert status == "completed" and usage is None
    assert output[1]["output"][0] == {"stdout": "", "stderr": "exited 1", "outcome": {"type": "exit", "exit_code": 1}}
    assert output[3] == {**output[3], "status": "incomplete", "output": "File not found"}


@pytest.mark.parametrize("effort,model,config", [
    ("low", "gemini-2.5-flash", {"thinkingBudget": 2048}),
    ("none", "gemini-2.5-pro", {"thinkingBudget": 0}),
    ("high", "gemini-3.8-flash", {"thinkingLevel": "HIGH"}),
])
def test_settings_from_the_request(effort, model, config):
    req = gmap.Request({"input": "x", "reasoning": {"effort": effort}, "max_output_tokens": 500, "max_tool_calls": 7})
    seed = {"mcpServers": {"org": {"httpUrl": "https://org.example/mcp"}}, "ui": {"theme": "x"}}
    doc = gmap.gemini_settings(req, model, MCP_JSON[:1], seed)
    assert doc["security"]["auth"]["selectedType"] == "gemini-api-key" and doc["ui"] == {"theme": "x"}
    assert doc["privacy"]["usageStatisticsEnabled"] is False and doc["telemetry"]["enabled"] is False
    assert doc["context"]["fileName"][-1] == gmap.INSTRUCTIONS_FILE
    aliases = doc["modelConfigs"]["customAliases"]
    assert {a["modelConfig"]["model"] for a in aliases.values()} == {model}
    assert aliases["classifier"]["extends"] == "base" and "extends" not in aliases["chat-compression-default"]
    override = doc["modelConfigs"]["customOverrides"][0]
    assert override == {"match": {"model": model},
                        "modelConfig": {"generateContentConfig": {"thinkingConfig": config, "maxOutputTokens": 500}}}
    assert doc["model"]["maxSessionTurns"] == 7
    assert set(doc["mcpServers"]) == {"org", "docs-site"}
    assert "mcpServers" in seed and "modelConfigs" not in seed            # the seed itself is left as it was


MCP_JSON = [  # /run/hg/mcp.json as the platform writes it: header values are placeholders
    {"label": "docs.site", "url": "https://x.example/mcp", "transport": "http",
     "headers": {"Authorization": "Bearer hgs_0123456789abcdef0123456789abcdef"}, "allowed_tools": ["search"]},
    {"label": "wiki", "url": "https://wiki.example/sse", "transport": "sse", "headers": {}, "allowed_tools": None},
    {"label": "none", "url": "https://none.example/mcp", "transport": "http", "headers": {}, "allowed_tools": []},
]


def test_mcp_servers_come_from_hg_mcp(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps(MCP_JSON))
    monkeypatch.setenv("HG_MCP", str(path))
    assert gmap.mcp_settings(gmap.Request.mcp_servers()) == {
        "docs-site": {"httpUrl": "https://x.example/mcp", "trust": True, "includeTools": ["search"],
                      "headers": {"Authorization": "Bearer hgs_0123456789abcdef0123456789abcdef"}},
        "wiki": {"url": "https://wiki.example/sse", "trust": True},
    }                                                  # an empty allowed_tools offers nothing: the server is left out


def test_ignored_fields_are_only_those_present():
    assert gmap.ignored(gmap.Request({"input": "x", "max_output_tokens": 9, "tools": [{"type": "mcp"}]})) == []
    req = gmap.Request({"input": "x", "temperature": 0.2, "tool_choice": "none", "tools": [{"type": "web_search"}],
                        "reasoning": {"effort": "low", "summary": "auto"}, "text": {"verbosity": "low"}})
    assert gmap.ignored(req) == ["tool_choice", "temperature", "tools", "reasoning.summary", "text.verbosity"]


def test_system_and_developer_messages_are_instructions_not_prompt():
    req = gmap.Request({"instructions": "Be French.", "input": [
        {"role": "system", "content": "Be brief."},
        {"role": "developer", "content": [{"type": "input_text", "text": "No emoji."}]},
        {"role": "user", "content": [{"type": "input_text", "text": "Describe."}]}]})
    assert gmap.instructions_for(req) == "Be French.\n\nBe brief.\n\nNo emoji."
    assert gmap.prompt_for(req, []) == "Describe."


def test_environment_command_and_instructions(monkeypatch, tmp_path):
    monkeypatch.setenv("HG_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-door:8080")
    monkeypatch.setenv("HG_PROVIDER_TOKEN", "tok")
    monkeypatch.setenv("GOOGLE_API_KEY", "leaked")
    monkeypatch.setenv("NO_PROXY", "hg-door,localhost")
    env = gmap.environment()
    assert (env["GOOGLE_GEMINI_BASE_URL"], env["GEMINI_API_KEY"]) == ("http://hg-door:8080", "tok")
    assert "GOOGLE_API_KEY" not in env and env["NO_PROXY"] == "hg-door,localhost"     # the platform's, as given
    assert gmap.command("abc") == ["gemini", "-o", "stream-json", "-m", "gemini-2.5-flash", "--approval-mode", "yolo",
                                   "--skip-trust", "-r", "abc"]
    req = gmap.Request({"input": "Describe.", "instructions": "Be French.",
                        "text": {"format": {"type": "json_schema", "name": "o", "schema": {"type": "object"}}}})
    ins = gmap.instructions_for(req)
    assert ins.startswith("Be French.") and '{"type": "object"}' in ins
    assert gmap.prompt_for(req, ["a b.png"]).endswith("@a\\ b.png")


def test_resume_only_when_the_transcript_survived(tmp_path):
    state = gmap.State(str(tmp_path / "state"))
    state.set("gemini_session", "8e3b03b4-1610-4bec-b088-cd93f9cec022")
    assert gmap.saved_session(state, tmp_path) == ""
    chats = tmp_path / ".gemini/tmp/workspace/chats"
    chats.mkdir(parents=True)
    (chats / "session-t01-38-8e3b03b4.jsonl").write_text("{}\n")
    assert gmap.saved_session(state, tmp_path) == "8e3b03b4-1610-4bec-b088-cd93f9cec022"


# ── end to end, offline: map.py run as run.sh runs it, against a fake `gemini` that replays a recorded stream ──
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": {k: os.environ.get(k, "") for k in (
        "GOOGLE_GEMINI_BASE_URL", "GEMINI_API_KEY", "GOOGLE_API_KEY")}}) + "\n")
if "-r" in sys.argv:                  # as 0.65.0 answers for a session with no model reply in it
    sys.stderr.write("Error resuming session: No previous sessions found for this project.\n")
    sys.exit(42)
sys.stdout.write(open(os.environ["FAKE_STREAM"]).read())
'''


def test_run_end_to_end_with_an_unresumable_session(tmp_path):
    bin_dir, ws, home, state = (tmp_path / d for d in ("bin", "workspace", "home", "state"))
    for d in (bin_dir, ws, home, state):
        d.mkdir()
    (state / "gemini_session").write_text("21846f07-1d46-44ee-98d0-20b80d40b81c")    # a turn 1 that failed
    chats = home / ".gemini/tmp/workspace/chats"
    chats.mkdir(parents=True)
    (chats / "session-t20-56-21846f07.jsonl").write_text("{}\n")
    cli = bin_dir / "gemini"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "mcp.json").write_text(json.dumps(MCP_JSON[:1]))
    (tmp_path / "request.json").write_text(json.dumps({
        "model": "gemini-cli", "temperature": 0.5, "input": [
            {"role": "developer", "content": "Be brief."},
            {"role": "user", "content": [{"type": "input_text", "text": "Run hello.py."},
                                         {"type": "input_file", "filename": "data.csv",
                                          "file_data": "data:text/csv;base64,YWxwaGEsMQo="}]}]}))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HG_REQUEST": str(tmp_path / "request.json"),
           "HG_MCP": str(tmp_path / "mcp.json"), "HG_FILES": str(tmp_path / "files"), "HG_WORKSPACE": str(ws),
           "HG_STATE": str(state), "HOME": str(home), "HG_PACKAGE_DIR": str(PACKAGE), "HG_MODEL": "gemini-2.5-flash",
           "HG_PROVIDER_API": "google", "HG_PROVIDER_URL": "http://hg-door:8080", "HG_PROVIDER_TOKEN": "turn-token",
           "GOOGLE_API_KEY": "leaked", "PYTHONPATH": str(REPO / "sandbox"), "FAKE_LOG": str(tmp_path / "call.json"),
           "FAKE_STREAM": str(HERE / "fixtures" / "mock_hello.jsonl")}
    proc = subprocess.run([sys.executable, str(PACKAGE / "map.py")], env=env, cwd=ws, capture_output=True, text=True,
                          timeout=60)
    _, o = check_output(proc.stdout)
    assert o.status == "completed" and o.metadata["hg_ignored_fields"] == "temperature"
    calls = [json.loads(x) for x in (tmp_path / "call.json").read_text().splitlines()]
    assert calls[0]["argv"][-2:] == ["-r", "21846f07-1d46-44ee-98d0-20b80d40b81c"]   # refused: a new session
    call = calls[1]
    assert call["argv"][:4] == ["-o", "stream-json", "-m", "gemini-2.5-flash"] and "-r" not in call["argv"]
    assert call["stdin"] == "Run hello.py.\n\nAttached files (in the working directory): @data.csv"
    assert call["env"] == {"GOOGLE_GEMINI_BASE_URL": "http://hg-door:8080", "GEMINI_API_KEY": "turn-token",
                           "GOOGLE_API_KEY": ""}
    assert (ws / "data.csv").read_text() == "alpha,1\n"
    assert (home / ".gemini" / gmap.INSTRUCTIONS_FILE).read_text() == "Be brief.\n"
    settings = json.loads((home / ".gemini" / "settings.json").read_text())
    assert settings["mcpServers"]["docs-site"]["headers"] == MCP_JSON[0]["headers"]
    assert (state / "gemini_session").read_text() == "8e3b03b4-1610-4bec-b088-cd93f9cec022"
