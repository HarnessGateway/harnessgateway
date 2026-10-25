import io
import json

import pytest
from hgkit import Emitter

from harnessgateway.domain import wire
from harnessgateway.errors import Invalid, NotFound


def test_model_path():
    assert wire.ModelPath.parse("claude-code") == wire.ModelPath("claude-code")
    assert wire.ModelPath.parse("pi/openai") == wire.ModelPath("pi", "openai")
    assert wire.ModelPath.parse("pi/openrouter/anthropic/claude-x") == wire.ModelPath("pi", "openrouter", "anthropic/claude-x")
    for bad in ("", "Bad", "a//b", 3, None):
        with pytest.raises(Invalid):
            wire.ModelPath.parse(bad)


def test_routing_reads_only_what_it_needs_and_keeps_files():
    body = {"model": "shell", "input": [{"role": "user", "content": [
        {"type": "input_text", "text": "hi"}, {"type": "input_file", "file_id": "file_abc"},
        {"type": "input_file", "file_id": "file_abc"}]}], "stream": True, "store": False, "previous_response_id": "resp_1"}
    r = wire.Routing.parse(body)
    assert (r.stream, r.background, r.store, r.previous_response_id, r.file_ids) == (True, False, False, "resp_1", ("file_abc",))


def test_routing_refuses_invalid_requests():
    with pytest.raises(Invalid):
        wire.Routing.parse({"model": "shell", "input": 5})
    with pytest.raises(Invalid):
        wire.Routing.parse({"model": "shell", "input": "x", "previous_response_id": "nope"})
    with pytest.raises(Invalid):
        wire.Routing.parse([])


def emitted(fn) -> list[str]:
    buf = io.StringIO()
    out = Emitter(buf)
    fn(out)
    return buf.getvalue().splitlines()


def test_hgkit_events_validate_and_assemble():
    def run(out: Emitter):
        out.log("starting the CLI")
        out.text("Hel")
        out.text("lo")
        out.reasoning("thinking...")
        out.shell("ls", stdout="a\n", exit_code=0)
        out.patch("create_file", "a.py", "+print(1)")
        out.function("search", {"q": "x"}, "found")
        out.mcp("srv", "tool", {"a": 1}, output="ok")
        out.completed(usage=Emitter.usage(10, 5, cached=2))
    lines = emitted(run)
    t = wire.TurnOutput()
    kinds = [t.line(x)[0] for x in lines]
    assert kinds[0] == "log" and kinds[-1] == "terminal" and "event" in kinds
    status, output, usage, error, incomplete, meta = t.outcome()
    assert status == "completed" and usage["input_tokens"] == 10
    types = [i["type"] for i in output]
    assert types[0] == "message" and output[0]["content"][0]["text"] == "Hello"
    assert {"reasoning", "shell_call", "shell_call_output", "apply_patch_call", "function_call", "mcp_call"} <= set(types)


def test_invalid_events_are_refused():
    t = wire.TurnOutput()
    with pytest.raises(wire.HarnessOutputError, match="delta"):
        t.line(json.dumps({"type": "response.output_text.delta", "item_id": "m", "output_index": 0}))
    with pytest.raises(wire.HarnessOutputError, match="not a Responses stream event"):
        t.line(json.dumps({"type": "response.made_up"}))
    t2 = wire.TurnOutput()
    t2.line(json.dumps({"type": "response.completed", "response": {}}))
    with pytest.raises(wire.HarnessOutputError, match="after the terminal"):
        t2.line(json.dumps({"type": "response.output_item.done", "output_index": 0, "item": {}}))


def test_envelope_events_from_packages_are_dropped():
    t = wire.TurnOutput()
    assert t.line(json.dumps({"type": "response.created", "response": {}})) == ("envelope", "response.created")


def test_bad_usage_is_refused():
    t = wire.TurnOutput()
    t.line(json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 1}}}))
    with pytest.raises(wire.HarnessOutputError, match="usage"):
        t.outcome()


def test_response_echoes_request_and_maps_unknown_error_codes():
    body = {"model": "shell", "input": "x", "instructions": "be brief", "metadata": {"k": "v"}, "temperature": 0.2}
    doc = wire.response(rid="resp_x", body=body, created=1.0, status="failed",
                        error={"code": "harness_crashed", "message": "boom"}, metadata={"session_id": "cntr_1"})
    assert doc["instructions"] == "be brief" and doc["temperature"] == 0.2
    assert doc["error"]["code"] == "server_error" and doc["metadata"]["hg_error_code"] == "harness_crashed"
    assert doc["metadata"]["k"] == "v" and doc["metadata"]["session_id"] == "cntr_1"


def test_sse_frame():
    frame = wire.sse({"type": "response.created", "sequence_number": 0}).decode()
    assert frame.startswith("event: response.created\ndata: {") and frame.endswith("\n\n")


def test_ids_reject_wrong_kinds():
    from harnessgateway import ids
    assert ids.check(ids.new("turn"), "turn").startswith("resp_")
    with pytest.raises(NotFound):
        ids.check(ids.new("session"), "turn")
