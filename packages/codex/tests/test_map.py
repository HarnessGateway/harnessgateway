"""Offline tests for the codex mapper: recorded `codex exec --json` streams (tests/fixtures, captured from real turns on
the platform with Codex 0.164.0 and gpt-5.4-mini) fed through map.Mapper; the whole output must pass the platform's own
reader (contract.check_output). Also the request -> `-c` overrides and command line, and map.py end to end.

Run from the repo root: .venv/bin/pytest -q packages/codex
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from hgkit import Emitter, Request, State

from harnessgateway.domain.contract import check_output

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
FIXTURES = PKG / "tests" / "fixtures"

_spec = importlib.util.spec_from_file_location("codex_map", PKG / "map.py")
codex_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(codex_map)


def run(lines: list[str], state_dir: Path, exit_code: int = 0, workspace: str = "/workspace"):
    """Feed Codex lines through the mapper; validate every printed line like the platform does."""
    buf = io.StringIO()
    mapper = codex_map.Mapper(Emitter(buf), State(str(state_dir)), workspace)
    for line in lines:
        mapper.line(line)
    mapper.finish(exit_code)
    events, o = check_output(buf.getvalue())       # raises OutputError on anything the platform refuses
    return o.status, o.output, o.usage, o.error, o.metadata, events, mapper


def fixture(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text().splitlines()


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.jsonl")))
def test_every_fixture_maps_to_valid_events(name, tmp_path):
    status, output, usage, error, meta, events, _ = run(fixture(name), tmp_path)
    assert status in ("completed", "failed")
    assert events[-1]["type"] == f"response.{status}"


def test_turn1_create_run_report(tmp_path):
    status, output, usage, error, meta, events, mapper = run(fixture("turn1"), tmp_path)
    assert status == "completed" and error is None
    types = [o["type"] for o in output]
    assert types == ["reasoning", "message", "apply_patch_call", "apply_patch_call_output", "shell_call",
                     "shell_call_output", "message"]
    patch = output[2]
    assert patch["operation"]["type"] == "create_file" and patch["operation"]["path"] == "hello.py"
    assert output[3]["call_id"] == patch["call_id"] and output[3]["status"] == "completed"
    shell, shell_out = output[4], output[5]
    assert shell["status"] == "completed" and "python3 hello.py" in shell["action"]["commands"][0]
    assert shell_out["call_id"] == shell["call_id"]
    assert shell_out["output"][0]["stdout"] == "hello from codex\n"
    assert shell_out["output"][0]["outcome"] == {"type": "exit", "exit_code": 0}
    assert "hello from codex" in output[-1]["content"][0]["text"]
    assert output[0]["summary"][0]["text"].startswith("**")
    # the shell_call is announced while it runs (added in_progress), then done
    added = [e for e in events if e["type"] == "response.output_item.added" and e["item"]["type"] == "shell_call"]
    assert added and added[0]["item"]["status"] == "in_progress"
    assert usage == {"input_tokens": 22173, "output_tokens": 161, "total_tokens": 22334,
                     "input_tokens_details": {"cached_tokens": 14464, "cache_write_tokens": 0},
                     "output_tokens_details": {"reasoning_tokens": 40}}
    thread = "01a174d9-28eb-7a12-839d-06b98683e867"
    assert meta == {"codex_thread_id": thread}
    assert (tmp_path / codex_map.THREAD_KEY).read_text() == thread
    # text deltas are well formed: one delta per Codex agent_message (exec --json gives whole messages)
    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert len(deltas) == 2 and all(d["logprobs"] == [] for d in deltas)


def test_turn2_usage_is_this_turn_only(tmp_path):
    """`codex exec resume` reports the thread's cumulative usage; the mapper subtracts the previous turn's total."""
    run(fixture("turn1"), tmp_path)                 # records the thread's cumulative usage after turn 1
    status, output, usage, error, meta, events, _ = run(fixture("turn2_resume"), tmp_path)
    assert status == "completed"
    assert "hello.py" in output[-1]["content"][0]["text"] and "hello from codex" in output[-1]["content"][0]["text"]
    assert usage["input_tokens"] == 29667 - 22173 and usage["output_tokens"] == 191 - 161
    assert usage["input_tokens_details"]["cached_tokens"] == 21376 - 14464
    assert usage["output_tokens_details"]["reasoning_tokens"] == 48 - 40
    # without the earlier record (a different thread), the reported numbers are taken as they are
    status, _, usage2, *_ = run(fixture("turn2_resume"), tmp_path / "fresh")
    assert usage2["input_tokens"] == 29667


def test_provider_error_is_a_clean_failure(tmp_path):
    status, output, usage, error, meta, events, _ = run(fixture("bad_model"), tmp_path)
    assert status == "failed" and output == []
    assert error["code"] == "model_not_found"
    assert "does not exist" in error["message"]


def test_killed_turn_closes_the_open_shell_call(tmp_path):
    status, output, usage, error, meta, events, _ = run(fixture("cancelled_sleep"), tmp_path, exit_code=-15)
    assert status == "failed"
    shells = [o for o in output if o["type"] == "shell_call"]
    assert shells and shells[0]["status"] == "incomplete" and "sleep 300" in shells[0]["action"]["commands"][0]


def test_json_schema_answer(tmp_path):
    status, output, *_ = run(fixture("json_schema"), tmp_path)
    assert status == "completed"
    assert json.loads(output[-1]["content"][0]["text"]) == {"country": "France", "capital": "Paris"}


def test_web_search_and_edits(tmp_path):
    status, output, usage, *_ = run(fixture("web_search_edit"), tmp_path)
    assert status == "completed"
    searches = [o for o in output if o["type"] == "web_search_call"]
    assert len(searches) == 3 and all(s["id"].startswith("ws_") for s in searches)
    assert searches[0]["action"]["type"] == "search" and "python" in searches[0]["action"]["query"].lower()
    assert any(o["type"] == "apply_patch_call" and o["operation"]["path"] == "version.txt" for o in output)
    assert usage["output_tokens_details"]["reasoning_tokens"] > 0


def test_synthetic_items(tmp_path):
    """Item types the recorded runs did not exercise: streamed agent text, MCP, todo list, other tools, failures."""
    lines = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {"type": "item.started", "item": {"id": "item_0", "type": "agent_message", "text": ""}},
        {"type": "item.updated", "item": {"id": "item_0", "type": "agent_message", "text": "Hel"}},
        {"type": "item.updated", "item": {"id": "item_0", "type": "agent_message", "text": "Hello"}},
        {"type": "item.completed", "item": {"id": "item_0", "type": "agent_message", "text": "Hello there"}},
        {"type": "item.completed", "item": {"id": "item_1", "type": "mcp_tool_call", "server": "docs", "tool": "search",
                                            "arguments": {"q": "x"}, "status": "completed",
                                            "result": {"content": [{"type": "text", "text": "found"}]}}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "mcp_tool_call", "server": "docs", "tool": "fetch",
                                            "arguments": {}, "status": "failed", "error": {"message": "boom"}}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "todo_list",
                                            "items": [{"text": "a", "completed": True}]}},
        {"type": "item.completed", "item": {"id": "item_4", "type": "collab_tool_call", "tool": "spawn",
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_5", "type": "file_change", "status": "completed",
                                            "changes": [{"path": "/workspace/a.py", "kind": "update"},
                                                        {"path": "/workspace/b.py", "kind": "delete"}]}},
        {"type": "item.completed", "item": {"id": "item_6", "type": "error", "message": "just a warning"}},
        {"type": "error", "message": "Reconnecting... 1/2"},
        {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 4, "output_tokens": 3,
                                             "reasoning_output_tokens": 1}},
    ]
    status, output, usage, error, meta, events, _ = run([json.dumps(x) for x in lines] + ["not json"], tmp_path)
    assert status == "completed"
    assert [d["delta"] for d in events if d["type"] == "response.output_text.delta"] == ["Hel", "lo", " there"]
    types = [o["type"] for o in output]
    assert types == ["message", "mcp_call", "mcp_call", "function_call", "function_call_output", "function_call",
                     "function_call_output", "apply_patch_call", "apply_patch_call_output", "apply_patch_call",
                     "apply_patch_call_output"]
    assert output[1]["output"] == "found" and output[2]["status"] == "failed"
    assert output[2]["error"] == {"type": "mcp_tool_execution_error", "content": [{"type": "text", "text": "boom"}]}
    assert output[3]["name"] == "update_plan"
    assert output[7]["operation"] == {"type": "update_file", "path": "a.py", "diff": ""}
    assert output[9]["operation"] == {"type": "delete_file", "path": "b.py"}
    assert usage["input_tokens_details"]["cached_tokens"] == 4 and usage["total_tokens"] == 13

    failed = [{"type": "thread.started", "thread_id": "t-2"}, {"type": "turn.started"},
              {"type": "error", "message": "unexpected status 429 Too Many Requests"},
              {"type": "turn.failed", "error": {"message": "unexpected status 429 Too Many Requests"}}]
    status, output, usage, error, *_ = run([json.dumps(x) for x in failed], tmp_path / "f")
    assert status == "failed" and error["code"] == "rate_limit_exceeded"

    status, output, usage, error, *_ = run([], tmp_path / "empty", exit_code=1)
    assert status == "failed" and "without finishing" in error["message"]


def test_create_diff_from_disk(tmp_path):
    (tmp_path / "hello.py").write_text("print('hi')\nprint('bye')\n")
    line = json.dumps({"type": "item.completed", "item": {"id": "i", "type": "file_change", "status": "completed",
                                                          "changes": [{"path": str(tmp_path / "hello.py"),
                                                                       "kind": "add"}]}})
    status, output, *_ = run([line, json.dumps({"type": "turn.completed", "usage": {}})], tmp_path / "s",
                             workspace=str(tmp_path))
    assert output[0]["operation"] == {"type": "create_file", "path": "hello.py", "diff": "+print('hi')\n+print('bye')\n"}


def config(args: list[str]) -> dict:
    """The `-c key=value` overrides of an argv, read back as one TOML document (dotted keys nest)."""
    return tomllib.loads("\n".join(args[i + 1].replace("=", " = ", 1) for i, a in enumerate(args) if a == "-c"))


def test_overrides(tmp_path, monkeypatch):
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-door:8080")
    monkeypatch.setenv("HG_TOOLS", str(tmp_path))
    (tmp_path / "codex-models.json").write_text("{}")
    req = Request({"model": "codex/openai/gpt-5.4-mini", "input": "hi",
                   "instructions": 'Say "bonjour"\nalways — 🇫🇷 \\ ok',
                   "reasoning": {"effort": "high", "summary": "detailed"}, "text": {"verbosity": "low"},
                   "tools": [{"type": "web_search"}]})
    args, servers = codex_map.overrides(req, "gpt-5.4-mini")
    doc = config([x for a in args for x in ("-c", a)])
    assert doc["model_provider"] == "hg" and doc["model"] == "gpt-5.4-mini"
    assert doc["approval_policy"] == "never" and doc["sandbox_mode"] == "danger-full-access"
    assert doc["developer_instructions"] == 'Say "bonjour"\nalways — 🇫🇷 \\ ok'
    assert doc["model_reasoning_effort"] == "high" and doc["model_reasoning_summary"] == "detailed"
    assert doc["model_verbosity"] == "low" and doc["web_search"] == "live"
    assert doc["model_catalog_json"] == str(tmp_path / "codex-models.json")
    provider = doc["model_providers"]["hg"]
    assert provider["base_url"] == "http://hg-door:8080/v1" and provider["wire_api"] == "responses"
    assert provider["env_key"] == "HG_CODEX_API_KEY"
    assert doc["analytics"]["enabled"] is False and doc["features"]["unbounded_connection_retries"] is False
    assert "mcp_servers" not in doc and servers == {}


def test_instructions_take_system_and_developer_messages(monkeypatch):
    monkeypatch.setenv("HG_MCP", "/nonexistent")
    req = Request({"instructions": "Be brief.", "input": [
        {"role": "system", "content": "Answer in French."},
        {"role": "developer", "content": [{"type": "input_text", "text": "Never use emoji."}]},
        {"role": "user", "content": "hi"}]})
    doc = config([x for a in codex_map.overrides(req, "m")[0] for x in ("-c", a)])
    assert doc["developer_instructions"] == "Be brief.\n\nAnswer in French.\n\nNever use emoji."
    assert req.prompt == "hi"


def test_mcp_servers_come_from_the_turns_mcp_file(tmp_path, monkeypatch):
    (tmp_path / "mcp.json").write_text(json.dumps([
        {"label": "my.docs", "url": "https://mcp.example.com/mcp", "transport": "http",
         "headers": {"Authorization": "Bearer hgs_0123", "X-Team": 'a"b'}, "allowed_tools": ["search"]},
        {"label": "wiki", "url": "https://wiki.example.com/mcp", "transport": "http", "headers": {},
         "allowed_tools": None}]))
    monkeypatch.setenv("HG_MCP", str(tmp_path / "mcp.json"))
    req = Request({"input": "hi", "tools": [{"type": "mcp", "server_label": "ignored", "server_url": "https://x"}]})
    args, servers = codex_map.overrides(req, "m")
    mcp = config([x for a in args for x in ("-c", a)])["mcp_servers"]
    assert set(mcp) == {"my_docs", "wiki"} and servers == {"my_docs": "my.docs", "wiki": "wiki"}
    assert mcp["my_docs"]["url"] == "https://mcp.example.com/mcp"
    assert mcp["my_docs"]["http_headers"] == {"Authorization": "Bearer hgs_0123", "X-Team": 'a"b'}
    assert mcp["my_docs"]["enabled_tools"] == ["search"] and "enabled_tools" not in mcp["wiki"]
    # Codex reports the server by its config name; the mcp_call carries the turn's label
    buf = io.StringIO()
    mapper = codex_map.Mapper(Emitter(buf), State(str(tmp_path / "s")), servers=servers)
    mapper.line(json.dumps({"type": "item.completed", "item": {"id": "i", "type": "mcp_tool_call", "server": "my_docs",
                                                               "tool": "search", "arguments": {}, "status": "completed",
                                                               "result": {"content": []}}}))
    mapper.line(json.dumps({"type": "turn.completed", "usage": {}}))
    mapper.finish()
    _, o = check_output(buf.getvalue())
    assert o.output[0]["server_label"] == "my.docs"


def test_ignored_fields_are_the_ones_present():
    req = Request({"input": "hi", "temperature": 0.2, "max_output_tokens": 100, "store": False,
                   "reasoning": {"effort": "low"}, "tools": [{"type": "function", "name": "f"}, {"type": "web_search"}]})
    assert codex_map.ignored(req) == ["max_output_tokens", "temperature", "tools.function"]
    assert codex_map.ignored(Request({"input": "hi"})) == []


def test_command_maps_request_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_map, "TMP", tmp_path)
    req = Request({"input": "hi", "store": False,
                   "text": {"format": {"type": "json_schema", "name": "x", "schema": {"type": "object"}}}})
    argv = codex_map.command(req, "gpt-5.4-mini", "-hi", ["/tmp/a.png"], ['model="gpt-5.4-mini"'])
    assert argv == ["codex", "exec", "--json", "--skip-git-repo-check", "-m", "gpt-5.4-mini", "-c",
                    'model="gpt-5.4-mini"', "--output-schema", str(tmp_path / "schema.json"), "--ephemeral",
                    "-i", "/tmp/a.png", "--", "-hi"]
    assert json.loads((tmp_path / "schema.json").read_text()) == {"type": "object"}
    resumed = codex_map.command(Request({"input": "again"}), "gpt-5.4-mini", "again", [], [], thread="t-1")
    assert resumed == ["codex", "exec", "resume", "--json", "--skip-git-repo-check", "-m", "gpt-5.4-mini", "t-1",
                       "--", "again"]


def test_attachments_are_placed_in_the_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr(codex_map, "WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HG_FILES", str(tmp_path / "nofiles"))
    req = Request({"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "look"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        {"type": "input_image", "image_url": "https://example.com/a.png"},
        {"type": "input_file", "filename": "notes.txt", "file_data": "aGk="}]}]})
    images, notes = codex_map.attachments(req)
    assert images == [str(tmp_path / "input-1.png")] and (tmp_path / "input-1.png").read_bytes() == b"\x00\x00\x00"
    assert notes == ["./notes.txt", "https://example.com/a.png"] and (tmp_path / "notes.txt").read_text() == "hi"


# ── end to end, offline: map.py run as run.sh runs it, against a fake `codex` that replays a recorded stream ──
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "key": os.environ.get("HG_CODEX_API_KEY", ""),
                        "home": os.environ.get("CODEX_HOME", "")}) + "\n")
print("WARNING: proceeding, even though we could not create PATH aliases", file=sys.stderr)
sys.stdout.write(open(os.environ["FAKE_STREAM"]).read())
'''


def test_run_end_to_end(tmp_path):
    bin_dir, ws, st, home = tmp_path / "bin", tmp_path / "workspace", tmp_path / "state", tmp_path / "home"
    for d in (bin_dir, ws, st, home):
        d.mkdir()
    cli = bin_dir / "codex"
    cli.write_text(FAKE_CLI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "request.json").write_text(json.dumps({"model": "codex/openai/gpt-5.4-mini", "input": "make hello.py",
                                                       "instructions": "Be brief.", "top_p": 0.5}))
    env = {**os.environ, "HG_REQUEST": str(tmp_path / "request.json"), "HG_STATE": str(st), "HG_WORKSPACE": str(ws),
           "HOME": str(home), "HG_FILES": str(tmp_path / "files"), "HG_MCP": str(tmp_path / "mcp.json"),
           "HG_MODEL": "gpt-5.4-mini", "HG_PROVIDER_API": "openai", "HG_PROVIDER_URL": "http://hg-door:8080",
           "HG_PROVIDER_TOKEN": "turn-token", "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "PYTHONPATH": str(REPO / "sandbox"), "FAKE_LOG": str(tmp_path / "calls.jsonl"),
           "FAKE_STREAM": str(FIXTURES / "turn1.jsonl")}
    proc = subprocess.run([sys.executable, str(PKG / "map.py")], env=env, cwd=ws, capture_output=True, text=True,
                          timeout=60)
    _, o = check_output(proc.stdout)
    assert o.status == "completed" and "shell_call" in [i["type"] for i in o.output] and o.usage["output_tokens"] == 161
    assert o.metadata["hg_ignored_fields"] == "top_p"
    call = json.loads((tmp_path / "calls.jsonl").read_text())
    assert call["argv"][:2] == ["exec", "--json"] and call["argv"][-2:] == ["--", "make hello.py"]
    assert call["key"] == "turn-token" and call["home"] == str(home / ".codex") and (home / ".codex").is_dir()
    doc = config(call["argv"])
    assert doc["developer_instructions"] == "Be brief." and doc["model"] == "gpt-5.4-mini"
    assert not (home / ".codex" / "config.toml").exists()          # the organization's file, never written
    assert (st / codex_map.THREAD_KEY).read_text() == "01a174d9-28eb-7a12-839d-06b98683e867"
    assert any(line.startswith("codex> {") for line in proc.stdout.splitlines())   # the raw stream, in the log
