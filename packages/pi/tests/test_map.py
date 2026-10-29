"""Offline: recorded `pi -p --mode json` streams (captured from real turns on the platform with PI_RAW_EVENTS=1) fed
through map.py's Mapper; every printed line must pass the platform's own reader (`check_output`). map.py is also run end
to end, as run.sh runs it, against a fake `pi`.

Run from the repo root: .venv/bin/pytest -q packages/pi
"""
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

_spec = importlib.util.spec_from_file_location("pi_map", PACKAGE / "map.py")
pi_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi_map)


def outcome(text: str) -> tuple[list[dict], tuple]:
    events, o = check_output(text)                      # raises OutputError on anything the platform refuses
    return events, (o.status, o.output, o.usage, o.error, o.incomplete, o.metadata)


def run(fixture: str, returncode: int = 0, stderr: str = "", labels: list[str] | None = None) -> tuple[list[dict], tuple]:
    buf = io.StringIO()
    mapper = pi_map.Mapper(Emitter(stream=buf), labels)
    for line in (HERE / "fixtures" / f"{fixture}.jsonl").read_text().splitlines():
        if line.strip():
            mapper.feed(json.loads(line))
    mapper.finish(returncode, stderr)
    return outcome(buf.getvalue())


def types(output: list[dict]) -> list[str]:
    return [i["type"] for i in output]


def text(output: list[dict]) -> str:
    return "".join(c["text"] for i in output if i["type"] == "message" for c in i["content"])


def test_hello_write_bash_text_usage():
    events, (status, output, usage, error, _, meta) = run("deepseek_anthropic_hello")
    assert status == "completed" and error is None
    assert types(output) == ["apply_patch_call", "apply_patch_call_output", "shell_call", "shell_call_output", "message"]
    write = output[0]["operation"]
    assert write == {"type": "create_file", "path": "/workspace/hello.py", "diff": "+print('hello from pi')\n"}
    shell_out = output[3]["output"][0]
    assert shell_out["stdout"] == "hello from pi\n" and shell_out["outcome"] == {"type": "exit", "exit_code": 0}
    assert output[2]["call_id"] == output[3]["call_id"]
    assert "hello from pi" in text(output)
    deltas = [e for e in events if e["type"] == "response.output_text.delta"]
    assert len(deltas) > 5                              # token by token, not one block
    assert usage["input_tokens"] == 4983 and usage["output_tokens"] == 137     # summed over the turn's 3 messages
    assert usage["input_tokens_details"]["cached_tokens"] == 3072
    assert meta["pi_session"].startswith("cntr_")


def test_thinking_becomes_reasoning_before_the_text():
    events, (status, output, usage, *_rest) = run("deepseek_anthropic_french_stream")
    assert status == "completed"
    assert types(output) == ["reasoning", "message"]
    assert "French" in output[0]["summary"][0]["text"]
    assert text(output).startswith("Une table de hachage")
    assert sum(e["type"] == "response.output_text.delta" for e in events) > 100
    assert usage["output_tokens"] > 0


def test_edit_is_an_update_patch_and_exit_code_is_kept():
    _, (status, output, *_rest) = run("deepseek_anthropic_edit_exit3")
    assert status == "completed"
    patches = [i for i in output if i["type"] == "apply_patch_call"]
    assert [p["operation"]["type"] for p in patches] == ["create_file", "update_file"]
    assert "-color: red\n+color: blue" in patches[1]["operation"]["diff"]
    shell_out = next(i for i in output if i["type"] == "shell_call_output")["output"][0]
    assert shell_out["outcome"] == {"type": "exit", "exit_code": 3}
    assert shell_out["stdout"].strip() == "color: blue" and "Command exited" not in shell_out["stdout"]


def test_openai_responses_reasoning_and_tools():
    _, (status, output, usage, *_rest) = run("openai_responses_hello")
    assert status == "completed"
    assert types(output)[0] == "reasoning"
    assert {"apply_patch_call", "shell_call", "shell_call_output", "message"} <= set(types(output))
    assert usage["output_tokens_details"]["reasoning_tokens"] > 0


def test_reasoning_that_ends_after_the_text_is_still_emitted_first():
    _, (status, output, *_rest) = run("deepseek_openai_recall_turn2")
    assert status == "completed"
    assert types(output) == ["reasoning", "message"]
    assert "hello.py" in text(output)


def test_mcp_tool_is_an_mcp_call():
    _, (status, output, *_rest) = run("deepseek_anthropic_mcp_deepwiki")
    assert status == "completed"
    call = next(i for i in output if i["type"] == "mcp_call")
    assert call["server_label"] == "deepwiki" and call["name"] == "read_wiki_structure"
    assert json.loads(call["arguments"]) == {"repoName": "octocat/Hello-World"}
    assert call["status"] == "completed" and "Overview" in call["output"]


def test_provider_error_is_a_clean_failure():
    _, (status, output, usage, error, *_rest) = run("deepseek_anthropic_no_such_model", returncode=1)
    assert status == "failed"
    assert error["code"] == "provider_error" and "no-such-model" in error["message"]


def test_a_stream_cut_off_mid_tool_still_ends_with_one_terminal_event():
    _, (status, output, _, error, *_rest) = run("deepseek_anthropic_sleep_cancelled", returncode=-15,
                                                   stderr="terminated")
    assert types(output) == ["reasoning", "shell_call"]
    assert output[1]["action"]["commands"][0].startswith("sleep 300")
    assert status == "completed" or error is not None


def test_cli_crash_without_output_fails_with_stderr():
    buf = io.StringIO()
    m = pi_map.Mapper(Emitter(stream=buf))
    m.feed({"type": "session", "id": "cntr_x", "version": 3})
    m.finish(1, "Error: Model hg/foo not found\n")
    _, (status, _, _, error, _, _) = outcome(buf.getvalue())
    assert status == "failed" and "not found" in error["message"]


def test_length_stop_is_incomplete():
    buf = io.StringIO()
    m = pi_map.Mapper(Emitter(stream=buf))
    m.feed({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "abc"}})
    m.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "length",
                                               "usage": {"input": 5, "output": 7, "cacheRead": 0, "cacheWrite": 0}}})
    m.finish(0)
    _, (status, output, usage, _, incomplete, _) = outcome(buf.getvalue())
    assert status == "incomplete" and incomplete == {"reason": "max_output_tokens"}
    assert usage["total_tokens"] == 12 and text(output) == "abc"


def test_failed_mcp_tool_and_label_mapped_back():
    buf = io.StringIO()
    m = pi_map.Mapper(Emitter(stream=buf), ["my-docs.v2"])
    m.feed({"type": "tool_execution_start", "toolCallId": "c1", "toolName": "mcp__my_docs_v2__search",
            "args": {"q": "x"}})
    m.feed({"type": "tool_execution_end", "toolCallId": "c1", "toolName": "mcp__my_docs_v2__search", "isError": True,
            "result": {"content": [{"type": "text", "text": "boom"}]}})
    m.finish(0)
    _, (status, output, *_rest) = outcome(buf.getvalue())
    (call,) = output
    assert call["server_label"] == "my-docs.v2" and call["status"] == "failed"
    assert call["error"]["type"] == "mcp_tool_execution_error"


# ── the request -> Pi ───────────────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("effort,flag", [("none", "off"), ("low", "low"), ("xhigh", "xhigh")])
def test_argv_maps_request_fields(monkeypatch, tmp_path, effort, flag):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HG_MODEL", "deepseek-flash")
    monkeypatch.setenv("HG_SESSION", "cntr_abc")
    req = pi_map.Request({"model": "pi/deepseek/deepseek-flash", "instructions": "Be French.",
                          "input": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "-hi"}],
                          "reasoning": {"effort": effort},
                          "text": {"format": {"type": "json_schema", "name": "out", "schema": {"type": "object"}}}})
    argv = pi_map.build_argv(req, ["notes.txt"], [])
    assert argv[:4] == ["pi", "-p", "--mode", "json"]
    assert argv[argv.index("--session-id") + 1] == "cntr_abc"
    assert argv[argv.index("--thinking") + 1] == flag
    system = argv[argv.index("--append-system-prompt") + 1]
    assert system.startswith("Be French.\n\nBe brief.\n\n") and '"type": "object"' in system
    assert argv[-3:] == ["--", "@notes.txt", "-hi"] and "--no-mcp" in argv
    stateless = pi_map.build_argv(pi_map.Request({"input": "x", "store": False}), [], [{"label": "docs"}])
    assert "--no-session" in stateless and "--session-id" not in stateless and "--no-mcp" not in stateless


def test_ignored_names_only_fields_the_request_sets():
    assert pi_map.ignored(pi_map.Request({"input": "hi", "tools": [{"type": "mcp", "server_label": "d"}]})) == []
    req = pi_map.Request({"input": "hi", "temperature": 0, "max_tool_calls": 3, "tools": [{"type": "function"}],
                          "reasoning": {"summary": "auto"}, "max_output_tokens": 50})
    assert pi_map.ignored(req) == ["temperature", "max_tool_calls", "tools", "reasoning.summary"]


def test_models_json_picks_the_api_from_the_provider_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-door:8080")
    monkeypatch.setenv("HG_TOOLS", str(tmp_path / "no-tools"))
    for shape, model, api, base in [("anthropic", "deepseek-flash", "anthropic-messages", "http://hg-door:8080"),
                                    ("openai", "deepseek-flash", "openai-completions", "http://hg-door:8080/v1")]:
        monkeypatch.setenv("HG_PROVIDER_API", shape)
        monkeypatch.setenv("HG_MODEL", model)
        pi_map.write_models_json(pi_map.Request({"max_output_tokens": 999}))
        doc = json.loads((tmp_path / ".pi/agent/models.json").read_text())["providers"]["hg"]
        assert (doc["api"], doc["baseUrl"], doc["apiKey"]) == (api, base, "$HG_PROVIDER_TOKEN")
        assert doc["models"][0]["id"] == model and doc["models"][0]["maxTokens"] == 999
        if shape == "openai":
            assert doc["models"][0]["compat"]["thinkingFormat"] == "deepseek"
    monkeypatch.setenv("PI_OPENAI_API", "openai-responses")         # the org setting, as an environment variable
    pi_map.write_models_json(pi_map.Request({}))
    assert json.loads((tmp_path / ".pi/agent/models.json").read_text())["providers"]["hg"]["api"] == "openai-responses"


SERVERS = [{"label": "docs.v2", "url": "https://x.example/mcp", "transport": "http",
            "headers": {"Authorization": "Bearer hgs_0123456789abcdef0123456789abcdef"}, "allowed_tools": ["search"]},
           {"label": "wiki", "url": "https://wiki.example/mcp", "transport": "http", "headers": {}, "allowed_tools": None},
           {"label": "old", "url": "https://old.example/sse", "transport": "sse", "headers": {}, "allowed_tools": None}]


def test_mcp_servers_from_hg_mcp(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    env: dict[str, str] = {}
    pi_map.write_mcp_json(SERVERS, env)
    raw = (tmp_path / ".pi/agent/mcp.json").read_text()
    assert "hgs_" not in raw and env == {"HG_MCP_0_0": "Bearer hgs_0123456789abcdef0123456789abcdef"}
    servers = json.loads(raw)["mcpServers"]
    assert set(servers) == {"docs_v2", "wiki"}                       # Pi has no SSE transport
    assert servers["docs_v2"] == {"url": "https://x.example/mcp", "exposure": "hidden",
                                  "headers": {"Authorization": "${HG_MCP_0_0}"}, "toolExposure": {"search": "direct"}}
    assert servers["wiki"] == {"url": "https://wiki.example/mcp", "exposure": "direct"}
    pi_map.write_mcp_json([], env)
    assert not (tmp_path / ".pi/agent/mcp.json").exists()


# ── end to end, offline: map.py run as run.sh runs it, against a fake `pi` that replays a recorded stream ──────────
FAKE_PI = r'''#!/usr/bin/env python3
import json, os, sys
home = os.environ["HOME"]
def read(p):
    return json.load(open(p)) if os.path.exists(p) else None
with open(os.environ["FAKE_LOG"], "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd(), "models": read(f"{home}/.pi/agent/models.json"),
               "mcp": read(f"{home}/.pi/agent/mcp.json"), "key": os.environ.get("HG_PROVIDER_TOKEN"),
               "mcp_env": {k: v for k, v in os.environ.items() if k.startswith("HG_MCP_")}}, f)
sys.stdout.write(open(os.environ["FAKE_STREAM"]).read())
'''


def test_run_end_to_end(tmp_path):
    bin_dir, ws, home, st = (tmp_path / d for d in ("bin", "workspace", "home", "state"))
    for d in (bin_dir, ws, home, st):
        d.mkdir()
    cli = bin_dir / "pi"
    cli.write_text(FAKE_PI)
    cli.chmod(cli.stat().st_mode | stat.S_IEXEC)
    request = {"model": "pi/deepseek/deepseek-flash", "temperature": 0.1, "instructions": "Be brief.",
               "input": [{"role": "developer", "content": "Answer in French."},
                         {"role": "user", "content": [{"type": "input_text", "text": "make hello.py"},
                                                      {"type": "input_file", "filename": "notes.txt",
                                                       "file_data": "data:text/plain;base64,aGVsbG8K"}]}],
               "tools": [{"type": "mcp", "server_label": "docs.v2", "server_url": "https://x.example/mcp"}]}
    (tmp_path / "request.json").write_text(json.dumps(request))
    (tmp_path / "mcp.json").write_text(json.dumps(SERVERS))
    env = {**os.environ, "HG_REQUEST": str(tmp_path / "request.json"), "HG_MCP": str(tmp_path / "mcp.json"),
           "HG_STATE": str(st), "HG_WORKSPACE": str(ws), "HG_FILES": str(tmp_path / "files"), "HOME": str(home),
           "HG_SESSION": "cntr_e2e", "HG_MODEL": "deepseek-flash", "HG_PROVIDER_API": "anthropic",
           "HG_PROVIDER_URL": "http://hg-door:8080", "HG_PROVIDER_TOKEN": "turn-token", "HG_TOOLS": str(tmp_path),
           "PATH": f"{bin_dir}:{os.environ['PATH']}", "PYTHONPATH": str(REPO / "sandbox"),
           "FAKE_LOG": str(tmp_path / "call.json"), "FAKE_STREAM": str(HERE / "fixtures" / "deepseek_anthropic_hello.jsonl")}
    proc = subprocess.run([sys.executable, str(PACKAGE / "map.py")], env=env, cwd=ws, capture_output=True, text=True,
                          timeout=60)
    _, (status, output, _, _, _, meta) = outcome(proc.stdout)
    assert status == "completed" and "shell_call" in types(output)
    assert meta["hg_ignored_fields"] == "temperature" and meta["pi_session"].startswith("cntr_")
    call = json.loads((tmp_path / "call.json").read_text())
    argv = call["argv"]
    assert call["cwd"] == str(ws) and (ws / "notes.txt").read_text() == "hello\n"
    assert argv[-3:] == ["--", "@notes.txt", "make hello.py"]
    assert argv[argv.index("--append-system-prompt") + 1] == "Be brief.\n\nAnswer in French."
    assert call["models"]["providers"]["hg"]["baseUrl"] == "http://hg-door:8080" and call["key"] == env["HG_PROVIDER_TOKEN"]
    assert set(call["mcp"]["mcpServers"]) == {"docs_v2", "wiki"}       # from $HG_MCP, not from `tools`
    assert call["mcp_env"] == {"HG_MCP_0_0": "Bearer hgs_0123456789abcdef0123456789abcdef"}
