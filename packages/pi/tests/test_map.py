"""Offline: recorded `pi -p --mode json` streams (captured from real turns on the platform with PI_RAW_EVENTS=1) fed
through map.py's Mapper; every printed line must pass the platform's own reader (TurnOutput) and its outcome()."""
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
sys.path.insert(0, str(REPO / "sandbox"))             # hgkit, as on the sandbox's PYTHONPATH
sys.path.insert(0, str(REPO))

from hgkit import Emitter  # noqa: E402

from harnessgateway.domain import wire  # noqa: E402

_spec = importlib.util.spec_from_file_location("pi_map", PACKAGE / "map.py")
pi_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi_map)


def run(fixture: str, returncode: int = 0, stderr: str = "") -> tuple[list[dict], wire.TurnOutput, tuple]:
    buf = io.StringIO()
    mapper = pi_map.Mapper(Emitter(stream=buf))
    for line in (HERE / "fixtures" / f"{fixture}.jsonl").read_text().splitlines():
        if line.strip():
            mapper.feed(json.loads(line))
    mapper.finish(returncode, stderr)
    reader, events = wire.TurnOutput(), []
    for line in buf.getvalue().splitlines():
        kind, value = reader.line(line)                 # raises HarnessOutputError on anything invalid
        if kind == "event":
            events.append(value)
    assert reader.terminal is not None, "exactly one terminal event"
    return events, reader, reader.outcome()


def types(output: list[dict]) -> list[str]:
    return [i["type"] for i in output]


def text(output: list[dict]) -> str:
    return "".join(c["text"] for i in output if i["type"] == "message" for c in i["content"])


def test_hello_write_bash_text_usage():
    events, _, (status, output, usage, error, _, meta) = run("deepseek_anthropic_hello")
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
    events, _, (status, output, usage, *_rest) = run("deepseek_anthropic_french_stream")
    assert status == "completed"
    assert types(output) == ["reasoning", "message"]
    assert "French" in output[0]["summary"][0]["text"]
    assert text(output).startswith("Une table de hachage")
    assert sum(e["type"] == "response.output_text.delta" for e in events) > 100
    assert usage["output_tokens"] > 0


def test_edit_is_an_update_patch_and_exit_code_is_kept():
    _, _, (status, output, *_rest) = run("deepseek_anthropic_edit_exit3")
    assert status == "completed"
    patches = [i for i in output if i["type"] == "apply_patch_call"]
    assert [p["operation"]["type"] for p in patches] == ["create_file", "update_file"]
    assert "-color: red\n+color: blue" in patches[1]["operation"]["diff"]
    shell_out = next(i for i in output if i["type"] == "shell_call_output")["output"][0]
    assert shell_out["outcome"] == {"type": "exit", "exit_code": 3}
    assert shell_out["stdout"].strip() == "color: blue" and "Command exited" not in shell_out["stdout"]


def test_openai_responses_reasoning_and_tools():
    _, _, (status, output, usage, *_rest) = run("openai_responses_hello")
    assert status == "completed"
    assert types(output)[0] == "reasoning"
    assert {"apply_patch_call", "shell_call", "shell_call_output", "message"} <= set(types(output))
    assert usage["output_tokens_details"]["reasoning_tokens"] > 0


def test_reasoning_that_ends_after_the_text_is_still_emitted_first():
    _, _, (status, output, *_rest) = run("deepseek_openai_recall_turn2")
    assert status == "completed"
    assert types(output) == ["reasoning", "message"]
    assert "hello.py" in text(output)


def test_mcp_tool_is_an_mcp_call():
    _, _, (status, output, *_rest) = run("deepseek_anthropic_mcp_deepwiki")
    assert status == "completed"
    call = next(i for i in output if i["type"] == "mcp_call")
    assert call["server_label"] == "deepwiki" and call["name"] == "read_wiki_structure"
    assert json.loads(call["arguments"]) == {"repoName": "octocat/Hello-World"}
    assert call["status"] == "completed" and "Overview" in call["output"]


def test_provider_error_is_a_clean_failure():
    _, _, (status, output, usage, error, *_rest) = run("deepseek_anthropic_no_such_model", returncode=1)
    assert status == "failed"
    assert error["code"] == "provider_error" and "no-such-model" in error["message"]


def test_a_stream_cut_off_mid_tool_still_ends_with_one_terminal_event():
    _, _, (status, output, _, error, *_rest) = run("deepseek_anthropic_sleep_cancelled", returncode=-15,
                                                   stderr="terminated")
    assert types(output) == ["reasoning", "shell_call"]
    assert output[1]["action"]["commands"][0].startswith("sleep 300")
    assert status == "completed" or error is not None


def test_cli_crash_without_output_fails_with_stderr():
    buf = io.StringIO()
    m = pi_map.Mapper(Emitter(stream=buf))
    m.feed({"type": "session", "id": "cntr_x", "version": 3})
    m.finish(1, "Error: Model hg/foo not found\n")
    reader = wire.TurnOutput()
    for line in buf.getvalue().splitlines():
        reader.line(line)
    status, _, _, error, _, _ = reader.outcome()
    assert status == "failed" and "not found" in error["message"]


def test_length_stop_is_incomplete():
    buf = io.StringIO()
    m = pi_map.Mapper(Emitter(stream=buf))
    m.feed({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "contentIndex": 0, "delta": "abc"}})
    m.feed({"type": "message_end", "message": {"role": "assistant", "stopReason": "length",
                                               "usage": {"input": 5, "output": 7, "cacheRead": 0, "cacheWrite": 0}}})
    m.finish(0)
    reader = wire.TurnOutput()
    for line in buf.getvalue().splitlines():
        reader.line(line)
    status, output, usage, _, incomplete, _ = reader.outcome()
    assert status == "incomplete" and incomplete == {"reason": "max_output_tokens"}
    assert usage["total_tokens"] == 12 and text(output) == "abc"


@pytest.mark.parametrize("effort,flag", [("none", "off"), ("low", "low"), ("xhigh", "xhigh")])
def test_argv_maps_request_fields(monkeypatch, tmp_path, effort, flag):
    monkeypatch.setenv("HG_MODEL", "deepseek-flash")
    monkeypatch.setenv("HG_SESSION", "cntr_abc")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HG_FILES", str(tmp_path / "files"))
    req = pi_map.Request({"model": "pi/deepseek/deepseek-flash", "input": "-hi", "instructions": "Be French.",
                          "reasoning": {"effort": effort},
                          "text": {"format": {"type": "json_schema", "name": "out", "schema": {"type": "object"}}}})
    argv = pi_map.build_argv(req)
    assert argv[:4] == ["pi", "-p", "--mode", "json"]
    assert argv[argv.index("--session-id") + 1] == "cntr_abc"
    assert argv[argv.index("--thinking") + 1] == flag
    system = argv[argv.index("--append-system-prompt") + 1]
    assert system.startswith("Be French.") and '"type": "object"' in system
    assert argv[-2:] == ["--", "-hi"] and "--no-mcp" in argv
    stateless = pi_map.build_argv(pi_map.Request({"input": "x", "store": False}))
    assert "--no-session" in stateless and "--session-id" not in stateless


def test_models_json_picks_the_api_from_the_provider_shape(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-egress:8080")
    monkeypatch.setenv("HG_TOOLS", str(tmp_path / "no-tools"))
    for shape, model, api, base in [("anthropic", "deepseek-flash", "anthropic-messages", "http://hg-egress:8080"),
                                    ("openai", "deepseek-flash", "openai-completions", "http://hg-egress:8080/v1")]:
        monkeypatch.setenv("HG_PROVIDER_API", shape)
        monkeypatch.setenv("HG_MODEL", model)
        pi_map.write_models_json(pi_map.Request({"max_output_tokens": 999}), {})
        doc = json.loads((tmp_path / ".pi/agent/models.json").read_text())["providers"]["hg"]
        assert (doc["api"], doc["baseUrl"], doc["apiKey"]) == (api, base, "$HG_PROVIDER_TOKEN")
        assert doc["models"][0]["id"] == model and doc["models"][0]["maxTokens"] == 999
        if shape == "openai":
            assert doc["models"][0]["compat"]["thinkingFormat"] == "deepseek"


def test_mcp_servers_keep_credentials_out_of_the_file(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".pi/agent").mkdir(parents=True)
    env: dict[str, str] = {}
    req = pi_map.Request({"tools": [{"type": "mcp", "server_label": "docs", "server_url": "https://x.example/mcp",
                                     "authorization": "s3cret", "allowed_tools": ["search"]}]})
    pi_map.write_mcp_json(req, env)
    raw = (tmp_path / ".pi/agent/mcp.json").read_text()
    assert "s3cret" not in raw and "Bearer s3cret" in env.values()
    server = json.loads(raw)["mcpServers"]["docs"]
    assert server["exposure"] == "hidden" and server["toolExposure"] == {"search": "direct"}
    pi_map.write_mcp_json(pi_map.Request({}), env)
    assert not (tmp_path / ".pi/agent/mcp.json").exists()
