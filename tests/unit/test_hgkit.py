import base64
import io
import json

import pytest
from hgkit import Emitter, Request

from harnessgateway.domain.contract import OutputError, check_output


def test_prompt_is_user_text_and_instructions_gather_system_and_developer_messages():
    req = Request({"instructions": "Be brief.", "input": [
        {"role": "developer", "content": "Use tabs."},
        {"role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]},
        {"type": "message", "role": "system", "content": [{"type": "input_text", "text": "No network."}]}]})
    assert req.prompt == "fix the bug"
    assert req.instructions == "Be brief.\n\nUse tabs.\n\nNo network."
    assert Request({"input": "hi"}).prompt == "hi" and Request({"input": "hi"}).instructions == ""


def test_text_format_only_when_structured():
    assert Request({"text": {"format": {"type": "text"}}}).text_format is None
    fmt = {"type": "json_schema", "name": "x", "schema": {"type": "object"}}
    assert Request({"text": {"format": fmt}}).text_format == fmt


def test_mcp_servers_come_from_the_platform_file(tmp_path):
    (tmp_path / "mcp.json").write_text(json.dumps([{"label": "docs", "url": "https://d.example.com/mcp"}]))
    assert Request.mcp_servers(str(tmp_path / "mcp.json"))[0]["label"] == "docs"
    assert Request.mcp_servers(str(tmp_path / "absent.json")) == []


def test_place_inputs_writes_by_name_with_suffixes_and_no_duplicates(tmp_path, monkeypatch):
    files, ws = tmp_path / "files", tmp_path / "ws"
    (files / "file_1").mkdir(parents=True)
    ws.mkdir()
    (files / "file_1" / "a.txt").write_text("from id")
    (files / "index.json").write_text(json.dumps({"file_1": {"filename": "a.txt", "path": str(files / "file_1" / "a.txt"),
                                                             "bytes": 7}}))
    monkeypatch.setenv("HG_FILES", str(files))
    inline = "data:text/plain;base64," + base64.b64encode(b"inline").decode()
    png = "data:image/png;base64," + base64.b64encode(b"\x89PNG").decode()
    req = Request({"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "look"},
        {"type": "input_file", "file_id": "file_1"},
        {"type": "input_file", "filename": "a.txt", "file_data": inline},
        {"type": "input_image", "image_url": png, "detail": "auto"},
        {"type": "input_image", "image_url": "https://example.com/x.png", "detail": "auto"}]}]})
    assert req.place_inputs(str(ws)) == ["a.txt", "a-2.txt", "input-3.png"]
    assert (ws / "a.txt").read_text() == "from id" and (ws / "a-2.txt").read_text() == "inline"
    assert req.place_inputs(str(ws)) == ["a.txt", "a-2.txt", "input-3.png"]       # the same bytes: nothing new
    assert sorted(p.name for p in ws.iterdir()) == ["a-2.txt", "a.txt", "input-3.png"]


def test_emitter_output_passes_the_platform_validator():
    buf = io.StringIO()
    out = Emitter(buf)
    out.reasoning("thinking")
    out.text("Hel")
    out.text("lo")
    out.shell("ls", stdout="a\n", exit_code=0)
    out.patch("create_file", "a.txt", "+x\n")
    out.mcp("docs", "search", {"q": "x"}, output="found")
    out.function("lookup", {"k": 1}, "v")
    out.ignored(["temperature", "temperature", "top_p"])
    out.completed(usage=Emitter.usage(10, 5), metadata={"cli_session": "s1"})
    events, outcome = check_output(buf.getvalue())
    assert outcome.status == "completed" and outcome.usage["total_tokens"] == 15
    assert outcome.metadata == {"cli_session": "s1", "hg_ignored_fields": "temperature,top_p"}
    assert [i["type"] for i in outcome.output] == ["reasoning", "message", "shell_call", "shell_call_output",
                                                   "apply_patch_call", "apply_patch_call_output", "mcp_call",
                                                   "function_call", "function_call_output"]
    assert outcome.output[1]["content"][0]["text"] == "Hello"
    with pytest.raises(AssertionError):
        out.message("after the end")


def test_failed_and_the_validator_refuses_bad_output():
    buf = io.StringIO()
    Emitter(buf).failed("rate_limit_exceeded", "slow down")
    assert check_output(buf.getvalue())[1].error == {"code": "rate_limit_exceeded", "message": "slow down"}
    with pytest.raises(OutputError, match="no terminal event"):
        check_output("a log line\n")
    bad = json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {"type": "nonsense"}})
    with pytest.raises(OutputError, match="nonsense"):
        check_output(bad + "\n")
