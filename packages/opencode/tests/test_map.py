"""Offline tests for the opencode mapper: OpenCode event streams recorded from real turns (OPENCODE_RAW_EVENTS=1, the
`/event` stream of `opencode serve` 1.19.4) fed through map.Mapper; the whole output must pass the platform's own
reader (contract.check_output). Also the request -> OpenCode config and prompt mapping.

Run from the repo root: .venv/bin/pytest -q packages/opencode
"""
from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

import pytest
from hgkit import Emitter, Request

from harnessgateway.domain.contract import check_output

HERE = Path(__file__).resolve().parent
PACKAGE = HERE.parent

_spec = importlib.util.spec_from_file_location("opencode_map", PACKAGE / "map.py")
oc_map = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oc_map)


def events(fixture: str) -> list[dict]:
    return [json.loads(x) for x in (HERE / "fixtures" / f"{fixture}.jsonl").read_text().splitlines() if x.strip()]


def run(fixture: str, mcp: dict[str, str] | None = None, failure: str = "") -> tuple[list[dict], tuple]:
    evs = events(fixture)
    session = next(e["properties"]["info"]["sessionID"] for e in evs
                   if e["type"] == "message.updated" and e["properties"]["info"]["role"] == "user")
    buf = io.StringIO()
    mapper = oc_map.Mapper(Emitter(stream=buf), session, mcp)
    for e in evs:
        mapper.feed(e)
    mapper.finish(failure)
    printed, o = check_output(buf.getvalue())          # raises OutputError on anything invalid
    return printed, (o.status, o.output, o.usage, o.error, o.incomplete, o.metadata)


def types(output: list[dict]) -> list[str]:
    return [i["type"] for i in output]


def text(output: list[dict]) -> str:
    return "".join(c["text"] for i in output if i["type"] == "message" for c in i["content"])


def deltas(printed: list[dict]) -> int:
    return sum(e["type"] == "response.output_text.delta" for e in printed)


def test_write_bash_streamed_text_and_usage():
    printed, (status, output, usage, error, _, meta) = run("deepseek_anthropic_hello")
    assert status == "completed" and error is None
    assert types(output) == ["apply_patch_call", "apply_patch_call_output", "shell_call", "shell_call_output", "message"]
    assert output[0]["operation"] == {"type": "create_file", "path": "/tmp/ws-a/hello.py", "diff": '+print("hello")\n'}
    shell = output[3]["output"][0]
    assert shell["stdout"] == "hello\n" and shell["outcome"] == {"type": "exit", "exit_code": 0}
    assert output[2]["call_id"] == output[3]["call_id"]
    assert text(output).endswith("BANANA") and deltas(printed) > 3      # instructions honoured, text streamed
    assert usage["input_tokens"] == 22294 and usage["output_tokens"] == 134   # summed over the turn's 3 steps
    assert usage["input_tokens_details"]["cached_tokens"] == 16256
    assert meta["opencode_session"].startswith("ses_")


def test_resumed_session_recalls_the_first_turn():
    _, (status, output, *_rest) = run("deepseek_anthropic_recall")
    assert status == "completed" and "hello.py" in text(output)


def test_thinking_is_a_reasoning_item_before_the_streamed_text():
    printed, (status, output, *_rest) = run("deepseek_anthropic_french_effort")
    assert status == "completed" and types(output) == ["reasoning", "message"]
    assert "French" in output[0]["summary"][0]["text"]
    assert text(output).startswith("Une table de hachage") and deltas(printed) > 50


def test_edit_is_an_update_patch_and_the_exit_code_is_kept():
    _, (status, output, *_rest) = run("deepseek_anthropic_edit_exit3")
    assert status == "completed"
    patches = [i["operation"] for i in output if i["type"] == "apply_patch_call"]
    assert [p["type"] for p in patches] == ["create_file", "update_file"]
    assert "-color: red\n+color: blue" in patches[1]["diff"]
    assert any(i["type"] == "function_call" and i["name"] == "read" for i in output)
    shell = next(i for i in output if i["type"] == "shell_call_output")["output"][0]
    assert shell["outcome"] == {"type": "exit", "exit_code": 3} and shell["stdout"] == "color: blue\n"


def test_apply_patch_is_split_per_file_with_v4a_diffs():
    _, (status, output, usage, *_rest) = run("openai_responses_apply_patch")
    assert status == "completed" and types(output)[0] == "reasoning"
    patches = [i["operation"] for i in output if i["type"] == "apply_patch_call"]
    assert patches[0] == {"type": "create_file", "path": "hello.py", "diff": '+print("hello")\n'}
    assert patches[1]["type"] == "update_file" and '+print("hello world")' in patches[1]["diff"]
    assert "hello world" in text(output)
    assert usage["output_tokens_details"]["reasoning_tokens"] > 0


def test_chat_completions_turn():
    _, (status, output, *_rest) = run("deepseek_openai_hello")
    assert status == "completed" and "shell_call_output" in types(output) and "hello" in text(output)


def test_json_schema_answer_is_the_structured_output():
    _, (status, output, *_rest) = run("deepseek_anthropic_json_schema")
    assert status == "completed"
    assert json.loads(text(output)) == {"city": "Paris", "population_millions": 2.1}


def test_mcp_tool_is_an_mcp_call():
    _, (status, output, *_rest) = run("deepseek_anthropic_mcp_deepwiki", mcp={"deepwiki": "deep.wiki"})
    call = next(i for i in output if i["type"] == "mcp_call")
    assert status == "completed"
    assert call["server_label"] == "deep.wiki" and call["name"] == "read_wiki_structure"
    assert json.loads(call["arguments"]) == {"repoName": "octocat/Hello-World"} and "Overview" in call["output"]


def test_provider_error_is_a_clean_failure():
    _, (status, output, _, error, *_rest) = run("deepseek_anthropic_no_such_model")
    assert status == "failed" and output == []
    assert error["code"] == "provider_error" and "no-such-model" in error["message"]


def test_a_stream_cut_off_mid_tool_still_ends_with_one_terminal_event():
    _, (status, output, _, error, *_rest) = run("deepseek_anthropic_sleep_cancelled", failure="terminated")
    assert types(output) == ["reasoning", "shell_call"]
    assert output[1]["action"] == {"commands": ["sleep 300"], "timeout_ms": 305000}
    assert status == "failed" and error["message"] == "terminated"


def _mapper() -> tuple[io.StringIO, object]:
    buf = io.StringIO()
    m = oc_map.Mapper(Emitter(stream=buf), "ses_1")
    m.feed({"type": "message.updated", "properties": {"info": {"id": "msg_a", "sessionID": "ses_1", "role": "assistant"}}})
    return buf, m


def _outcome(buf: io.StringIO) -> tuple:
    o = check_output(buf.getvalue())[1]
    return o.status, o.output, o.usage, o.error, o.incomplete, o.metadata


def _tool(m, status: str, **state) -> None:
    m.feed({"type": "message.part.updated", "properties": {"part": {
        "id": "prt_t", "messageID": "msg_a", "sessionID": "ses_1", "type": "tool", "tool": "bash", "callID": "c1",
        "state": {"status": status, "input": {"command": "sleep 9", "timeout": 1000}, **state}}}})


def test_shell_timeout_is_a_timeout_outcome():
    buf, m = _mapper()
    _tool(m, "running")
    _tool(m, "completed", output="partial\n\n<shell_metadata>\nshell tool terminated command after exceeding timeout "
                                 "1000 ms.\n</shell_metadata>", metadata={"exit": None})
    m.finish()
    _, output, *_ = _outcome(buf)
    out = output[1]["output"][0]
    assert out["outcome"] == {"type": "timeout"} and out["stdout"] == "partial" and "exceeding timeout" in out["stderr"]


def test_output_length_error_is_incomplete_and_child_usage_counts():
    buf, m = _mapper()
    m.feed({"type": "session.created", "properties": {"info": {"id": "ses_child", "parentID": "ses_1"}}})
    m.feed({"type": "message.part.updated", "properties": {"part": {
        "id": "prt_s", "messageID": "msg_c", "sessionID": "ses_child", "type": "step-finish",
        "tokens": {"input": 5, "output": 7, "reasoning": 1, "cache": {"read": 2, "write": 0}}}}})
    m.feed({"type": "session.error", "properties": {"sessionID": "ses_1",
                                                    "error": {"name": "MessageOutputLengthError", "data": {}}}})
    m.finish()
    status, _, usage, _, incomplete, _ = _outcome(buf)
    assert status == "incomplete" and incomplete == {"reason": "max_output_tokens"}
    assert usage["input_tokens"] == 7 and usage["output_tokens"] == 8


def test_rate_limit_and_server_failure():
    buf, m = _mapper()
    m.feed({"type": "session.error", "properties": {"sessionID": "ses_1", "error": {
        "name": "APIError", "data": {"message": "Too Many Requests", "statusCode": 429, "isRetryable": True}}}})
    m.finish()
    assert _outcome(buf)[3]["code"] == "rate_limit_exceeded"
    buf, m = _mapper()
    m.finish("opencode serve exited")
    assert _outcome(buf)[3] == {"code": "harness_error", "message": "opencode serve exited"}


def test_v4a_patch_split():
    patch = "*** Begin Patch\n*** Add File: a.py\n+x\n*** Delete File: b.py\n*** Update File: c.py\n@@\n-1\n+2\n*** End Patch"
    assert oc_map.v4a_files(patch) == [("create_file", "a.py", "+x\n"), ("delete_file", "b.py", ""),
                                       ("update_file", "c.py", "@@\n-1\n+2\n")]


# ── the request -> OpenCode ───────────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def turn_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HG_PROVIDER_URL", "http://hg-door:8080")
    monkeypatch.setenv("HG_FILES", str(tmp_path / "files"))
    monkeypatch.setenv("HG_MCP", str(tmp_path / "mcp.json"))
    monkeypatch.setenv("HG_WORKSPACE", str(tmp_path / "ws"))
    monkeypatch.delenv("OPENCODE_OPENAI_API", raising=False)
    (tmp_path / "ws").mkdir()
    return tmp_path


@pytest.mark.parametrize("shape,model,setting,npm", [
    ("anthropic", "deepseek-flash", "", "@ai-sdk/anthropic"),
    ("openai", "gpt-5.4-mini", "", "@ai-sdk/openai"),
    ("openai", "deepseek-flash", "", "@ai-sdk/openai-compatible"),
    ("openai", "gpt-5.4-mini", "chat", "@ai-sdk/openai-compatible"),
])
def test_config_picks_the_ai_sdk_package(monkeypatch, turn_env, shape, model, setting, npm):
    monkeypatch.setenv("HG_PROVIDER_API", shape)
    monkeypatch.setenv("HG_MODEL", model)
    monkeypatch.setenv("OPENCODE_OPENAI_API", setting)          # the package's setting, an environment variable
    cfg, labels = oc_map.write_config(Request({"input": "x", "max_tool_calls": 7}), turn_env / "c" / "opencode.json")
    hg = cfg["provider"]["hg"]
    assert hg["npm"] == npm and hg["options"] == {"baseURL": "http://hg-door:8080/v1",
                                                  "apiKey": "{env:HG_PROVIDER_TOKEN}"}
    assert list(hg["models"]) == [model] and cfg["enabled_providers"] == ["hg"] and cfg["model"] == f"hg/{model}"
    assert cfg["agent"] == {"build": {"steps": 7}} and "mcp" not in cfg and labels == {}
    assert json.loads((turn_env / "c" / "opencode.json").read_text()) == cfg


def test_mcp_servers_come_from_the_turns_mcp_file(monkeypatch, turn_env):
    monkeypatch.setenv("HG_PROVIDER_API", "anthropic")
    monkeypatch.setenv("HG_MODEL", "m")
    (turn_env / "mcp.json").write_text(json.dumps([
        {"label": "my.docs", "url": "https://x.example/mcp", "transport": "http",
         "headers": {"Authorization": "Bearer hgs_0123"}, "allowed_tools": ["search"]},
        {"label": "wiki", "url": "https://wiki.example/sse", "transport": "sse", "headers": {}, "allowed_tools": None}]))
    req = Request({"input": "x", "tools": [{"type": "mcp", "server_label": "other", "server_url": "https://y.example"}]})
    cfg, labels = oc_map.write_config(req, turn_env / "opencode.json")
    assert cfg["mcp"] == {"my_docs": {"type": "remote", "url": "https://x.example/mcp", "enabled": True, "oauth": False,
                                      "headers": {"Authorization": "Bearer hgs_0123"}},
                          "wiki": {"type": "remote", "url": "https://wiki.example/sse", "enabled": True, "oauth": False}}
    assert cfg["tools"] == {"my_docs_*": False, "my_docs_search": True}
    assert labels == {"my_docs": "my.docs", "wiki": "wiki"}


@pytest.mark.parametrize("shape,effort,variant", [
    ("anthropic", "high", "high"), ("anthropic", "xhigh", "max"), ("anthropic", "low", ""),
    ("openai", "low", "low"), ("openai", "max", "xhigh"), ("openai", "", ""),
])
def test_prompt_body(monkeypatch, turn_env, shape, effort, variant):
    monkeypatch.setenv("HG_PROVIDER_API", shape)
    monkeypatch.setenv("HG_MODEL", "m")
    req = Request({"input": [
        {"role": "system", "content": "Be French."},
        {"role": "developer", "content": [{"type": "input_text", "text": "Be brief."}]},
        {"role": "user", "content": [
            {"type": "input_text", "text": "-hi"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {"type": "input_image", "image_url": "https://example.com/a.png"},
            {"type": "input_file", "filename": "n.txt", "file_data": "aGk="}]}],
        "instructions": "Be kind.", "reasoning": {"effort": effort},
        "text": {"format": {"type": "json_schema", "name": "o", "schema": {"type": "object"}}}})
    ws = turn_env / "ws"
    body = oc_map.prompt_body(req, oc_map.prompt_parts(req, req.place_inputs()))
    assert body["parts"] == [
        {"type": "file", "mime": "image/png", "filename": "input-1.png", "url": (ws / "input-1.png").as_uri()},
        {"type": "file", "mime": "text/plain", "filename": "n.txt", "url": (ws / "n.txt").as_uri()},
        {"type": "text", "text": "-hi"}]
    assert (ws / "n.txt").read_text() == "hi" and (ws / "input-1.png").is_file()
    assert body["system"] == "Be kind.\n\nBe French.\n\nBe brief." and body["model"] == {"providerID": "hg",
                                                                                         "modelID": "m"}
    assert body.get("variant", "") == variant
    assert body["format"] == {"type": "json_schema", "schema": {"type": "object"}}
    if shape == "anthropic" and variant:
        assert oc_map.provider_block(req)["models"]["m"]["limit"]["output"] == oc_map.ANTHROPIC_THINKING_OUTPUT


def test_ignored_fields_are_the_ones_present():
    req = Request({"input": "x", "tool_choice": "none", "temperature": 0.3, "text": {"verbosity": "low"},
                   "reasoning": {"effort": "high", "summary": "auto"}, "tools": [{"type": "function", "name": "f"}]})
    assert oc_map.ignored(req) == ["tool_choice", "text.verbosity", "reasoning.summary", "tools.function"]
    assert oc_map.ignored(Request({"input": "x", "max_output_tokens": 50, "top_p": 0.5})) == []
