"""Offline tests for the claude-code mapper: recorded Claude Code stream-json (from real turns through the platform)
fed through map.Mapper; every printed line must pass the platform's own validator (wire.TurnOutput) and the
outcome must be the one the turn had.

Run from the repo root: .venv/bin/python -m pytest packages/claude-code/tests -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent
REPO = PACKAGE.parents[1]
sys.path.insert(0, str(REPO / "sandbox"))          # hgkit, as the sandbox image has it on PYTHONPATH
sys.path.insert(0, str(REPO))

from hgkit import Emitter, Request, State  # noqa: E402

from harnessgateway.domain.wire import TurnOutput  # noqa: E402

_spec = importlib.util.spec_from_file_location("claude_code_map", PACKAGE / "map.py")
cmap = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cmap)


def run_fixture(name: str, tmp_path: Path, labels: list[str] | None = None, exit_code: int = 0):
    buf = io.StringIO()
    state = State(str(tmp_path / "state"))
    mapper = cmap.Mapper(Emitter(buf), state, labels)
    for line in (HERE / "fixtures" / name).read_text().splitlines():
        mapper.line(line)
    mapper.finish(exit_code, "")
    wire = TurnOutput()
    events = []
    for line in buf.getvalue().splitlines():
        kind, value = wire.line(line)               # raises HarnessOutputError on anything the platform refuses
        if kind == "event":
            events.append(value)
    assert wire.terminal is not None, "no terminal event"
    assert buf.getvalue().splitlines()[-1].startswith('{"type":"response.'), "the terminal event is last"
    status, output, usage, error, incomplete, meta = wire.outcome()
    return {"status": status, "output": output, "usage": usage, "error": error, "incomplete": incomplete,
            "meta": meta, "events": events, "state": state, "mapper": mapper}


def types(output):
    return [i["type"] for i in output]


def text_of(output):
    return "".join(c.get("text", "") for i in output if i["type"] == "message" for c in i["content"])


def test_turn1_write_and_bash(tmp_path):
    r = run_fixture("turn1_write_bash.jsonl", tmp_path)
    assert r["status"] == "completed"
    assert types(r["output"]) == ["message", "apply_patch_call", "apply_patch_call_output", "shell_call",
                                  "shell_call_output", "message"]
    patch = r["output"][1]
    assert patch["operation"] == {"type": "create_file", "path": "hello.py", "diff": "+print('hello from claude-code')"}
    shell, shell_out = r["output"][3], r["output"][4]
    assert shell["action"]["commands"] == ["python3 hello.py"]
    assert shell_out["call_id"] == shell["call_id"]
    assert shell_out["output"][0]["stdout"] == "hello from claude-code"
    assert shell_out["output"][0]["outcome"] == {"type": "exit", "exit_code": 0}
    assert "hello from claude-code" in text_of(r["output"])
    # text arrived token by token, not as one block
    deltas = [e for e in r["events"] if e["type"] == "response.output_text.delta"]
    assert len(deltas) > 20
    # Anthropic's input_tokens leaves out cache reads; OpenAI's includes them
    assert r["usage"]["input_tokens"] == 11495 + 22016
    assert r["usage"]["input_tokens_details"] == {"cached_tokens": 22016, "cache_write_tokens": 0}
    assert r["usage"]["output_tokens"] == 181
    assert r["usage"]["total_tokens"] == 11495 + 22016 + 181
    sid = "62582618-2f40-4f27-8bb4-387392706e9d"
    assert r["state"].get("session") == sid
    assert r["meta"]["claude_session_id"] == sid


def test_no_raw_stream_in_events(tmp_path):
    r = run_fixture("turn1_write_bash.jsonl", tmp_path)
    for e in r["events"]:
        assert e["type"].startswith("response.")
        assert "stream_event" not in json.dumps(e)


def test_command_maps_request_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("HG_MODEL", "deepseek-flash")
    req = Request({"model": "claude-code/deepseek/deepseek-flash", "input": "hi", "instructions": "Answer in French.",
                   "reasoning": {"effort": "minimal"}, "max_tool_calls": 7,
                   "text": {"format": {"type": "json_schema", "name": "x", "schema": {"type": "object"}}},
                   "tools": [{"type": "mcp", "server_label": "docs", "server_url": "https://mcp.example.com/mcp",
                              "authorization": "tok", "require_approval": "never"}]})
    argv = cmap.command(req, resume="abc", tmp=str(tmp_path))
    joined = " ".join(argv)
    for flag in ("-p", "--output-format stream-json", "--verbose", "--include-partial-messages",
                 "--dangerously-skip-permissions", "--model deepseek-flash", "--resume abc", "--effort low",
                 "--max-turns 7", "--strict-mcp-config", '--json-schema {"type":"object"}'):
        assert flag in joined, flag
    assert (tmp_path / "instructions.md").read_text() == "Answer in French."
    mcp = json.loads((tmp_path / "mcp.json").read_text())
    assert mcp == {"mcpServers": {"docs": {"type": "http", "url": "https://mcp.example.com/mcp",
                                           "headers": {"Authorization": "Bearer tok"}}}}


def test_environment_pins_every_model(monkeypatch):
    monkeypatch.setenv("HG_MODEL", "deepseek-flash")
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-egress:8080")
    monkeypatch.setenv("HG_PROVIDER_TOKEN", "t0k")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "stray")
    env = cmap.environment(Request({"input": "hi", "reasoning": {"effort": "none"}}))
    assert env["ANTHROPIC_BASE_URL"] == "http://hg-egress:8080" and env["ANTHROPIC_API_KEY"] == "t0k"
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert all(env[k] == "deepseek-flash" for k in cmap.MODEL_ENV)
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == env["DISABLE_AUTOUPDATER"] == env["DISABLE_TELEMETRY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    assert "hg-egress" in env["NO_PROXY"]


def test_no_result_is_a_clean_failure(tmp_path):
    buf = io.StringIO()
    mapper = cmap.Mapper(Emitter(buf), State(str(tmp_path)))
    mapper.line('{"type":"system","subtype":"init","session_id":"s1"}')
    mapper.finish(1, "boom")
    wire = TurnOutput()
    for line in buf.getvalue().splitlines():
        wire.line(line)
    status, _, _, error, _, _ = wire.outcome()
    assert status == "failed" and "boom" in error["message"]


@pytest.mark.parametrize("name", sorted(p.name for p in (HERE / "fixtures").glob("*.jsonl")))
def test_every_fixture_validates(name, tmp_path):
    r = run_fixture(name, tmp_path, labels=["docs"])
    assert r["status"] in ("completed", "failed", "incomplete")


def test_thinking_streams_into_one_reasoning_item(tmp_path):
    r = run_fixture("thinking_streamed.jsonl", tmp_path)
    assert r["status"] == "completed"
    assert types(r["output"]) == ["reasoning", "message"]
    summary = r["output"][0]["summary"][0]["text"]
    assert summary.startswith("The user wants") and len(summary) > 200
    deltas = [e for e in r["events"] if e["type"] == "response.reasoning_summary_text.delta"]
    assert len(deltas) > 50
    assert "".join(d["delta"] for d in deltas) == summary
    assert "blue" in text_of(r["output"])


def test_provider_error_is_a_clean_failure(tmp_path):
    r = run_fixture("provider_error.jsonl", tmp_path)
    assert r["status"] == "failed"
    assert r["output"] == []                         # the CLI's synthetic "API Error" message is not model output
    assert r["error"]["code"] == "provider_error"
    assert "no-such-model" in r["error"]["message"] and r["error"]["message"].startswith("API Error: 400")
    assert r["usage"]["total_tokens"] == 0


def test_failed_tool_and_other_tools(tmp_path):
    r = run_fixture("blocked_bash_toolsearch.jsonl", tmp_path)
    assert r["status"] == "completed"
    t = types(r["output"])
    assert t.count("reasoning") == 3 and t[-1] == "message"
    shell_out = next(i for i in r["output"] if i["type"] == "shell_call_output")
    assert shell_out["output"][0]["outcome"] == {"type": "exit", "exit_code": 1}
    assert "Blocked: standalone sleep 300" in shell_out["output"][0]["stderr"]
    fc = next(i for i in r["output"] if i["type"] == "function_call")
    fco = next(i for i in r["output"] if i["type"] == "function_call_output")
    assert fc["name"] == "ToolSearch" and json.loads(fc["arguments"])["query"] == "select:Monitor"
    assert fco["call_id"] == fc["call_id"] and fco["output"] == "[tool loaded: Monitor]"


def test_edit_becomes_update_patch(tmp_path):
    r = run_fixture("pro_write_edit_bash.jsonl", tmp_path)
    assert r["status"] == "completed"
    patches = [i for i in r["output"] if i["type"] == "apply_patch_call"]
    assert patches[0]["operation"] == {"type": "create_file", "path": "notes.txt", "diff": "+alpha"}
    assert patches[1]["operation"] == {"type": "update_file", "path": "notes.txt", "diff": "@@ -1,1 +1,1 @@\n-alpha\n+beta"}
    outs = [i for i in r["output"] if i["type"] == "apply_patch_call_output"]
    assert [o["call_id"] for o in outs] == [p["call_id"] for p in patches]
    assert all(o["status"] == "completed" for o in outs)
    assert "beta" in text_of(r["output"])


def test_json_schema_result_is_the_message(tmp_path):
    r = run_fixture("json_schema.jsonl", tmp_path)
    assert r["status"] == "completed"
    assert types(r["output"]) == ["message"]                 # the StructuredOutput tool call is not an item
    assert json.loads(text_of(r["output"])) == {"capital": "Paris", "population": 68000000}


def test_max_turns_is_incomplete(tmp_path):
    r = run_fixture("max_turns.jsonl", tmp_path)
    assert r["status"] == "incomplete"
    assert r["incomplete"] == {"reason": "max_messages"}
    assert r["meta"]["claude_stop"] == "error_max_turns"
    assert "shell_call_output" in types(r["output"])


def test_mcp_call(tmp_path):
    r = run_fixture("mcp_call.jsonl", tmp_path, labels=["deepwiki"])
    assert r["status"] == "completed"
    mcp = [i for i in r["output"] if i["type"] == "mcp_call"]
    assert len(mcp) == 1
    assert mcp[0]["server_label"] == "deepwiki" and mcp[0]["name"] == "read_wiki_structure"
    assert json.loads(mcp[0]["arguments"]) == {"repoName": "anthropics/claude-code"}
    assert mcp[0]["status"] == "completed" and "Claude Code Overview" in mcp[0]["output"]
    added = [e for e in r["events"] if e["type"] == "response.output_item.added" and e["item"]["type"] == "mcp_call"]
    assert added[0]["item"]["status"] == "in_progress"         # added when called, done when the result came


def test_mcp_server_unreachable(tmp_path):
    r = run_fixture("mcp_unreachable.jsonl", tmp_path, labels=["deepwiki"])
    assert r["status"] == "completed"
    fco = next(i for i in r["output"] if i["type"] == "function_call_output")
    assert "failed to connect" in fco["output"]


# ── end to end, offline: map.py run as request.sh runs it, against a fake `claude` that replays a recorded stream ──
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, subprocess, sys, time
log = os.environ["FAKE_LOG"]
with open(log, "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "prompt": sys.stdin.read(),
                        "env": {k: os.environ.get(k, "") for k in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY",
                                                                   "ANTHROPIC_SMALL_FAST_MODEL")}}) + "\n")
if os.environ.get("FAKE_HANG"):
    subprocess.Popen(["setsid", "sleep", "300"])      # a tool command in a process group of its own
    time.sleep(300)
if "--resume" in sys.argv:
    sid = sys.argv[sys.argv.index("--resume") + 1]
    print("No conversation found with session ID: " + sid, flush=True)
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "session_id": sid,
                      "errors": ["No conversation found with session ID: " + sid], "usage": {}}), flush=True)
    sys.exit(1)
sys.stdout.write(open(os.environ["FAKE_STREAM"]).read())
'''


def _sandbox(tmp_path: Path, request: dict, **extra: str) -> tuple[dict, Path]:
    import os
    import stat
    bin_dir, ws, st = tmp_path / "bin", tmp_path / "workspace", tmp_path / "state"
    for d in (bin_dir, ws, st):
        d.mkdir()
    cli = bin_dir / "claude"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "request.json").write_text(json.dumps(request))
    env = {**os.environ, "HG_REQUEST": str(tmp_path / "request.json"), "HG_STATE": str(st), "HG_WORKSPACE": str(ws),
           "HG_FILES": str(tmp_path / "files"), "HG_MODEL": "deepseek-flash", "HG_PROVIDER_URL": "http://hg-egress:8080",
           "HG_PROVIDER_TOKEN": "turn-token", "HG_TURN": "2", "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "PYTHONPATH": str(REPO / "sandbox"), "FAKE_LOG": str(tmp_path / "calls.jsonl"),
           "FAKE_STREAM": str(HERE / "fixtures" / "turn1_write_bash.jsonl"), **extra}
    return env, st


def test_run_end_to_end_with_lost_session(tmp_path):
    import subprocess
    env, st = _sandbox(tmp_path, {"model": "claude-code/deepseek/deepseek-flash", "input": "make hello.py",
                                  "instructions": "Be brief."})
    (st / "session").write_text("old-session")
    proc = subprocess.run([sys.executable, str(PACKAGE / "map.py")], env=env, cwd=env["HG_WORKSPACE"],
                          capture_output=True, text=True, timeout=60)
    wire = TurnOutput()
    for line in proc.stdout.splitlines():
        wire.line(line)
    status, output, usage, *_ = wire.outcome()
    assert status == "completed" and "shell_call" in types(output) and usage["output_tokens"] == 181
    calls = [json.loads(x) for x in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert len(calls) == 2                                       # resume failed (transcript gone), then a fresh session
    assert calls[0]["argv"][calls[0]["argv"].index("--resume") + 1] == "old-session"
    assert "--resume" not in calls[1]["argv"]
    assert calls[1]["prompt"] == "make hello.py"
    assert "--append-system-prompt-file" in calls[1]["argv"]
    assert calls[1]["env"] == {"ANTHROPIC_BASE_URL": "http://hg-egress:8080", "ANTHROPIC_API_KEY": "turn-token",
                               "ANTHROPIC_SMALL_FAST_MODEL": "deepseek-flash"}
    assert (st / "session").read_text() == "62582618-2f40-4f27-8bb4-387392706e9d"
    assert any(line.startswith("claude-stream {") for line in proc.stdout.splitlines())   # raw stream in the log


def test_sigterm_stops_the_whole_tree(tmp_path):
    import os
    import signal
    import subprocess
    import time
    env, _ = _sandbox(tmp_path, {"model": "claude-code/deepseek/deepseek-flash", "input": "wait"}, FAKE_HANG="1")
    proc = subprocess.Popen([sys.executable, str(PACKAGE / "map.py")], env=env, cwd=env["HG_WORKSPACE"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def tree() -> list[int]:
        return [p for p in cmap._descendants(proc.pid) if cmap._alive(p)]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and not any("sleep" in Path(f"/proc/{p}/cmdline").read_text(errors="ignore")
                                                  for p in tree() if Path(f"/proc/{p}").exists()):
        time.sleep(0.2)
    time.sleep(1)                                                # let map.py's watcher see the tool process
    victims = tree()
    assert victims
    os.killpg(proc.pid, signal.SIGTERM)                          # what the node does to the turn's process group
    assert proc.wait(10) == 128 + signal.SIGTERM
    time.sleep(0.3)
    assert not [p for p in victims if cmap._alive(p)]
