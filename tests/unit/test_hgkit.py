import base64
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from hgkit import Emitter, Request, check, validate
from hgkit.validate import OutputError

from harnessgateway import contract


def test_prompt_is_user_text_and_instructions_gather_system_and_developer_messages():
    req = Request({"instructions": "Be brief.", "input": [
        {"role": "developer", "content": "Use tabs."},
        {"role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]},
        {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "No network."}]}]}, env={})
    assert req.prompt == "fix the bug"
    assert req.instructions == "Be brief.\n\nUse tabs.\n\nNo network."
    assert Request({"input": "hi"}, env={}).prompt == "hi" and Request({"input": "hi"}, env={}).instructions == ""


def test_the_environment_is_read_through_the_request(tmp_path):
    env = {"HG_MODEL": "m1", "HG_PROVIDER_API": "anthropic", "HG_PROVIDER_URL": "http://hg-door:8080",
           "HG_PROVIDER_TOKEN": "hgt_x", "HG_STATE": str(tmp_path), "HG_TURN": "3", "HG_PACKAGE_VERSION": "2.1.310-2"}
    req = Request({"input": "hi", "max_tool_calls": 4}, env=env)
    assert req.model == "m1" and req.provider.api == "anthropic" and req.provider.url == "http://hg-door:8080"
    assert req.turn == 3 and req.max_tool_calls == 4 and req.cli_version == "2.1.310"
    req.state.set("session", "abc")
    assert req.state.get("session") == "abc" and (tmp_path / "session").read_text() == "abc"
    bare = Request({"input": "hi"}, env={})
    assert bare.provider is None and bare.model == "" and bare.turn == 1 and bare.max_tool_calls is None


def test_text_format_only_when_structured():
    assert Request({"text": {"format": {"type": "text"}}}, env={}).text_format is None
    fmt = {"type": "json_schema", "name": "x", "schema": {"type": "object"}}
    assert Request({"text": {"format": fmt}}, env={}).text_format == fmt


def test_mcp_servers_come_from_the_platform_file(tmp_path):
    (tmp_path / "mcp.json").write_text(json.dumps([{"label": "docs", "url": "https://d.example.com/mcp"}]))
    assert Request({}, env={}).mcp_servers(str(tmp_path / "mcp.json"))[0]["label"] == "docs"
    assert Request({}, env={"HG_MCP": str(tmp_path / "absent.json")}).mcp_servers() == []


def test_place_inputs_writes_by_name_with_suffixes_and_no_duplicates(tmp_path):
    files, ws = tmp_path / "files", tmp_path / "ws"
    (files / "file_1").mkdir(parents=True)
    ws.mkdir()
    (files / "file_1" / "a.txt").write_text("from id")
    (files / "index.json").write_text(json.dumps({"file_1": {"filename": "a.txt", "path": str(files / "file_1" / "a.txt"),
                                                             "bytes": 7}}))
    inline = "data:text/plain;base64," + base64.b64encode(b"inline").decode()
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()
    req = Request({"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "look"},
        {"type": "input_file", "file_id": "file_1"},
        {"type": "input_file", "filename": "a.txt", "file_data": inline},
        {"type": "input_image", "image_url": png, "detail": "auto"},
        {"type": "input_image", "image_url": "https://example.com/x.png", "detail": "auto"}]}]},
        env={"HG_FILES": str(files), "HG_WORKSPACE": str(ws)})
    assert req.place_inputs() == ["a.txt", "a-2.txt", "input-3.png"]
    assert (ws / "a.txt").read_text() == "from id" and (ws / "a-2.txt").read_text() == "inline"
    assert req.place_inputs() == ["a.txt", "a-2.txt", "input-3.png"]       # the same bytes: nothing new
    assert sorted(p.name for p in ws.iterdir()) == ["a-2.txt", "a.txt", "input-3.png"]


def narrate(out: Emitter) -> None:
    """One turn told with every verb: what a mapper does with a CLI's output."""
    out.think("Looking")
    out.think(" at it")
    out.say("Hel")
    out.say("lo")
    run = out.ran("pytest -q", timeout_ms=30000)
    run.done(stdout="3 passed\n", exit=0)
    out.ran("ls", stdout="a\n")
    out.wrote("a.txt", "x\ny\n")
    out.edited("b.txt", old="alpha", new="beta")
    out.deleted("c.txt")
    call = out.called("docs", "search", {"q": "x"})
    call.done("found")
    out.called("docs", "search", {"q": "y"}, error="boom")
    out.used("lookup", {"k": 1}, "v")
    pending = out.used("slow", {})
    pending.failed("timed out")
    out.ignored(["temperature", "temperature", "top_p"])
    out.done(input=10, output=5, cached=3, metadata={"cli_session": "s1"})


def test_the_emitter_narrates_a_turn_the_platform_accepts():
    buf = io.StringIO()
    narrate(Emitter(buf))
    events, outcome = check(buf.getvalue())
    assert outcome.status == "completed" and outcome.usage["total_tokens"] == 15
    assert outcome.usage["input_tokens_details"] == {"cached_tokens": 3, "cache_write_tokens": 0}
    assert outcome.metadata == {"cli_session": "s1", "hg_ignored_fields": "temperature,top_p"}
    assert [i["type"] for i in outcome.output] == [
        "reasoning", "message", "shell_call", "shell_call_output", "shell_call", "shell_call_output",
        "apply_patch_call", "apply_patch_call_output", "apply_patch_call", "apply_patch_call_output",
        "apply_patch_call", "apply_patch_call_output", "mcp_call", "mcp_call", "function_call", "function_call_output",
        "function_call", "function_call_output"]
    out = outcome.output
    assert out[0]["summary"][0]["text"] == "Looking at it" and out[1]["content"][0]["text"] == "Hello"
    assert out[2]["action"] == {"commands": ["pytest -q"], "timeout_ms": 30000}
    assert out[3]["call_id"] == out[2]["call_id"] and out[3]["output"][0]["stdout"] == "3 passed\n"
    assert out[6]["operation"] == {"type": "create_file", "path": "a.txt", "diff": "+x\n+y"}
    assert out[8]["operation"] == {"type": "update_file", "path": "b.txt", "diff": "@@\n-alpha\n+beta"}
    assert out[10]["operation"] == {"type": "delete_file", "path": "c.txt"}
    assert (out[12]["status"], out[12]["output"]) == ("completed", "found")
    assert out[13]["status"] == "failed" and out[13]["error"]["type"] == "mcp_tool_execution_error"
    assert out[15]["output"] == "v" and out[17]["status"] == "incomplete"
    # thinking and text stream as deltas; an MCP call is added when made and done when its result comes
    kinds = [e["type"] for e in events]
    assert kinds.count("response.reasoning_summary_text.delta") == 2 and kinds.count("response.output_text.delta") == 2
    added = [e for e in events if e["type"] == "response.output_item.added" and e["item"]["type"] == "mcp_call"]
    assert added[0]["item"]["status"] == "in_progress"
    with pytest.raises(AssertionError, match="terminal"):
        out_after_end(buf)


def out_after_end(buf):
    out = Emitter(buf)
    out.done()
    out.say("after the end")


def test_hgkit_and_the_platform_read_output_alike():
    buf = io.StringIO()
    narrate(Emitter(buf))
    assert check(buf.getvalue()) == contract.check(buf.getvalue())
    assert contract.read_line is validate.read_line and contract.OutputError is validate.OutputError


def test_validate_at_emit_time_names_the_field():
    out = Emitter(io.StringIO(), validate=True)
    out.say("fine")
    with pytest.raises(OutputError, match="shell_call"):
        out.item({"type": "shell_call", "call_id": "c"})            # no status, no action
    with pytest.raises(OutputError, match="nonsense"):
        out.emit({"type": "response.nonsense"})


def test_stopped_failed_and_the_reader_refuses_bad_output():
    buf = io.StringIO()
    Emitter(buf).failed("rate_limit_exceeded", "slow down", input=1, output=0)
    _, o = check(buf.getvalue())
    assert o.error == {"code": "rate_limit_exceeded", "message": "slow down"} and o.usage["total_tokens"] == 1
    buf = io.StringIO()
    Emitter(buf).stopped("max_output_tokens")
    assert check(buf.getvalue())[1].incomplete == {"reason": "max_output_tokens"}
    with pytest.raises(OutputError, match="no terminal event"):
        check("a log line\n")
    bad = json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"type": "nonsense"}})
    with pytest.raises(OutputError, match="nonsense"):
        check(bad + "\n")


def test_the_command_line_check(tmp_path):
    buf = io.StringIO()
    narrate(Emitter(buf))
    good, bad = tmp_path / "good.jsonl", tmp_path / "bad.jsonl"
    good.write_text(buf.getvalue())
    bad.write_text(buf.getvalue().replace('"logprobs":[]', '"logprobs":1', 1))
    env = {"PYTHONPATH": str(Path(__file__).resolve().parents[2] / "sandbox")}
    ok = subprocess.run([sys.executable, "-m", "hgkit", "check", str(good)], capture_output=True, text=True, env=env)
    assert ok.returncode == 0 and ok.stdout.startswith("ok: ") and "completed with 18 output items" in ok.stdout
    no = subprocess.run([sys.executable, "-m", "hgkit", "check", str(bad)], capture_output=True, text=True, env=env)
    assert no.returncode == 1 and no.stdout.startswith("line ") and "logprobs" in no.stdout
    for args in (["check", "--help"], ["check", str(tmp_path / "absent.jsonl")], []):
        r = subprocess.run([sys.executable, "-m", "hgkit", *args], capture_output=True, text=True, env=env)
        assert r.returncode == 2 and r.stdout == "" and r.stderr.strip()


PATCH = {"type": "create_file", "path": "hello.py", "diff": "+print(1)"}
SHELL_OUT = [{"stdout": "1\n", "stderr": "", "outcome": {"type": "exit", "exit_code": 0}}]
HISTORY = [
    {"id": "resp_1", "status": "completed",
     "input": [{"type": "message", "role": "system", "content": "Be brief."},
               {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "make hello.py"},
                                                                {"type": "input_file", "filename": "spec.md"}]}],
     "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
                {"type": "apply_patch_call", "call_id": "c1", "operation": PATCH},
                {"type": "apply_patch_call_output", "call_id": "c1", "status": "completed", "output": ""},
                {"type": "shell_call", "call_id": "c2", "action": {"commands": ["python3 hello.py"]}},
                {"type": "shell_call_output", "call_id": "c2", "output": SHELL_OUT},
                {"type": "mcp_call", "id": "m1", "server_label": "docs", "name": "search", "arguments": "{\"q\": \"x\"}",
                 "status": "completed", "output": "found"},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}]},
    {"id": "resp_2", "status": "cancelled", "input": [{"type": "message", "role": "user", "content": "now tests"}],
     "output": [{"type": "function_call", "call_id": "c3", "name": "WebSearch", "arguments": "{}"}]},
]


def test_history_and_transcript_render_the_conversation(tmp_path):
    (tmp_path / "history.json").write_text(json.dumps(HISTORY))
    req = Request({"input": "next"}, env={"HG_HISTORY": str(tmp_path / "history.json")})
    assert req.history() == HISTORY
    text = req.transcript()
    assert text.splitlines() == [
        "Instructions: Be brief.", "User: make hello.py [attached: spec.md]", "Created: hello.py", "Ran: python3 hello.py",
        "  -> exit 0: 1", "Used: docs.search({\"q\": \"x\"})", "Assistant: Done.", "User: now tests", "Used: WebSearch({})",
        "(this turn was interrupted before it finished)"]
    assert req.transcript(limit=20).startswith("…") and len(req.transcript(limit=20)) == 21
    assert Request({"input": "x"}, env={"HG_HISTORY": str(tmp_path / "absent.json")}).history() == [] \
        and Request({"input": "x"}, env={}).transcript() == ""
