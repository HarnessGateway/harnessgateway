"""Offline tests for the codex package's mapper: recorded `codex exec --json` streams (tests/fixtures, captured from
real turns on the platform with Codex 0.164.0 and gpt-5.4-mini) go through map.py, and every printed line is checked
with the platform's own reader (harnessgateway.domain.wire.TurnOutput), then the final outcome().

    .venv/bin/python -m pytest packages/codex/tests -q
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tomllib
from pathlib import Path

import pytest

PKG = Path(__file__).resolve().parents[1]
REPO = PKG.parents[1]
FIXTURES = PKG / "tests" / "fixtures"
sys.path.insert(0, str(REPO / "sandbox"))          # hgkit, as in the sandbox image
sys.path.insert(0, str(REPO))

from hgkit import Emitter, State  # noqa: E402

from harnessgateway.domain.wire import TurnOutput  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


codex_map = _load("codex_map", PKG / "map.py")


def _codex_run():
    saved = sys.modules.get("map")
    sys.modules["map"] = codex_map                  # run.py does `from map import ...`
    try:
        return _load("codex_run", PKG / "run.py")
    finally:
        if saved is None:
            sys.modules.pop("map", None)
        else:
            sys.modules["map"] = saved


def run(lines: list[str], state_dir: Path, exit_code: int = 0, workspace: str = "/workspace"):
    """Feed Codex lines through the mapper; validate every printed line like the platform does."""
    buf = io.StringIO()
    mapper = codex_map.CodexMapper(out=Emitter(stream=buf), state=State(str(state_dir)), workspace=workspace)
    for line in lines:
        mapper.feed(line)
    mapper.finish(exit_code)
    reader = TurnOutput()
    events = []
    for line in buf.getvalue().splitlines():
        kind, value = reader.line(line)             # raises HarnessOutputError on anything the platform refuses
        if kind == "event":
            events.append(value)
    assert reader.terminal is not None, "no terminal event"
    status, output, usage, error, incomplete, meta = reader.outcome()
    return status, output, usage, error, meta, events, mapper


def fixture(name: str) -> list[str]:
    return (FIXTURES / f"{name}.jsonl").read_text().splitlines()


@pytest.mark.parametrize("name", sorted(p.stem for p in FIXTURES.glob("*.jsonl")))
def test_every_fixture_maps_to_valid_events(name, tmp_path):
    status, output, usage, error, meta, events, _ = run(fixture(name), tmp_path)
    assert status in ("completed", "failed")
    assert all(e["type"] != "response.completed" for e in events)


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


def test_config_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-egress:8080")
    monkeypatch.setenv("HG_PROVIDER_TOKEN", "tok-123")
    monkeypatch.setenv("HG_TOOLS", str(tmp_path))
    (tmp_path / "codex-models.json").write_text("{}")
    from hgkit import Request
    codex_run = _codex_run()
    req = Request({"model": "codex/openai/gpt-5.4-mini", "input": "hi",
                   "instructions": 'Say "bonjour"\nalways — 🇫🇷 \\ ok',
                   "reasoning": {"effort": "high", "summary": "detailed"}, "text": {"verbosity": "low"},
                   "tools": [{"type": "mcp", "server_label": "my.docs", "server_url": "https://mcp.example.com/mcp",
                              "authorization": "secret", "headers": {"X-Team": "a\"b"},
                              "allowed_tools": {"tool_names": ["search"]}},
                             {"type": "web_search"}]})
    env: dict[str, str] = {}
    doc = tomllib.loads(codex_run.build_config(req, "gpt-5.4-mini", env))
    assert doc["model_provider"] == "hg" and doc["model"] == "gpt-5.4-mini"
    assert doc["approval_policy"] == "never" and doc["sandbox_mode"] == "danger-full-access"
    assert doc["developer_instructions"] == 'Say "bonjour"\nalways — 🇫🇷 \\ ok'
    assert doc["model_reasoning_effort"] == "high" and doc["model_reasoning_summary"] == "detailed"
    assert doc["model_verbosity"] == "low" and doc["web_search"] == "live"
    assert doc["model_catalog_json"] == str(tmp_path / "codex-models.json")
    provider = doc["model_providers"]["hg"]
    assert provider["base_url"] == "http://hg-egress:8080/v1" and provider["wire_api"] == "responses"
    assert env[provider["env_key"]] == "tok-123"
    mcp = doc["mcp_servers"]["my.docs"]
    assert mcp["url"] == "https://mcp.example.com/mcp" and mcp["http_headers"] == {"X-Team": 'a"b'}
    assert env[mcp["bearer_token_env_var"]] == "secret" and mcp["enabled_tools"] == ["search"]
    assert doc["analytics"]["enabled"] is False and doc["features"]["unbounded_connection_retries"] is False
