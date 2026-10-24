"""Layer 2: the Responses wire by the SDK's types: requests validated and read, packages' events and
shapes validated, the response built, the stream enveloped."""
import asyncio
import json

import pytest

from harnessgateway import wire
from harnessgateway.errors import Invalid

MSG = {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed", "content": [{"type": "output_text", "text": "done", "annotations": []}]}
SHELL = {"type": "shell_call", "id": "sh_1", "call_id": "c1", "status": "completed", "action": {"commands": ["ls"]}}


def test_request_parses_routing_text_and_files():
    r = wire.Request.parse({"model": "claude-code/deepseek/deepseek-chat", "stream": True, "metadata": {"a": "b"},
                            "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"},
                                                                   {"type": "input_file", "filename": "../a.txt", "file_data": "data:text/plain;base64,aGVsbG8="}]}]})
    assert (r.harness, r.provider, r.model, r.text, r.stream, r.store) == ("claude-code", "deepseek", "deepseek-chat", "hi", True, True)
    assert r.files == {"a.txt": b"hello"} and r.metadata == {"a": "b"}
    assert "characters" in r.for_trace()["input"][0]["content"][1]["file_data"]
    plain = wire.Request.parse({"model": "pi", "input": "just text", "store": False})
    assert (plain.provider, plain.model, plain.store, plain.files) == ("", "", False, {})


@pytest.mark.parametrize("body,text", [
    ({"model": "x", "input": "hi", "temperature": "hot"}, "not a valid Responses request"),
    ({"model": "a/b", "input": "hi"}, "model is a harness name"),
    ({"model": "Bad", "input": "hi"}, "model is a harness name"),
    ({"model": "a", "input": ""}, "input is required"),
    ({"model": "a", "input": [{"role": "user", "content": [{"type": "input_file", "filename": "f", "file_data": "data:x;base64,!!"}]}]}, "not valid base64"),
    ({"model": "a", "input": "x", "previous_response_id": "nope"}, "previous_response_id"),
    ({"model": "a", "input": "x", "metadata": {"k": 1}}, "not a valid Responses request"),
    ("nope", "JSON object"),
])
def test_request_refusals(body, text):
    with pytest.raises(Invalid, match=text):
        wire.Request.parse(body)


def test_events_and_shapes():
    out = "\n".join(["plain log line", json.dumps({"type": "response.output_text.delta", "delta": "x"}),
                     json.dumps({"type": "response.output_item.done", "item": SHELL}), json.dumps({"type": "response.output_item.done", "item": MSG}),
                     json.dumps({"type": "response.output_item.done", "item": MSG}),               # the same item twice is one item
                     json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 4}}}),
                     json.dumps({"type": "not_an_event"}), "{broken json"])
    events, log = wire.events_of(out)
    assert len(events) == 5 and log.splitlines() == ["plain log line", '{"type": "not_an_event"}', "{broken json"]
    shape = wire.from_events(events)
    assert [i["type"] for i in shape.output] == ["shell_call", "message"] and shape.usage["total_tokens"] == 7
    with pytest.raises(Invalid, match=r"output\[0\]"):
        wire.from_events([{"type": "response.output_item.done", "item": {"type": "message"}}])
    shaped = wire.from_response_sh(json.dumps({"output": [MSG], "status": "incomplete", "incomplete_details": {"reason": "max_turns"},
                                               "error": None, "metadata": {"k": "v"}, "usage": {"input_tokens": 1, "output_tokens": 1}}))
    assert shaped.status == "incomplete" and shaped.incomplete == {"reason": "max_turns"} and shaped.metadata == {"k": "v"}
    for bad in ("not json", "[]", '{"status": "weird"}', '{"nope": 1}', '{"error": "text"}', '{"metadata": {"k": 1}}'):
        with pytest.raises(Invalid):
            wire.from_response_sh(bad)


def test_response_object_and_sdk_literals():
    r = wire.response(rid="resp_" + "a" * 32, created=1.0, status="failed", model="h/p/m", previous="resp_" + "b" * 32, store=True,
                      instructions="x", metadata={"k": "v"}, output=[MSG], usage=None,
                      error={"code": "package_output_invalid", "message": "bad"}, incomplete=None, completed=2.0)
    assert r["error"] == {"code": "server_error", "message": "bad"} and r["metadata"]["error_code"] == "package_output_invalid"
    assert r["output"][0]["content"][0]["text"] == "done" and r["previous_response_id"].startswith("resp_b")
    r2 = wire.response(rid="resp_" + "a" * 32, created=1.0, status="incomplete", model="h", previous=None, store=False, instructions=None,
                       metadata={}, output=[], usage={"input_tokens": 1, "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
                                                      "output_tokens": 1, "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": 2},
                       error=None, incomplete={"reason": "timeout"}, completed=None)
    assert r2["metadata"]["incomplete_reason"] == "timeout" and "incomplete_details" not in r2 and r2["usage"]["total_tokens"] == 2


def test_stream_envelope():
    async def events():
        yield {"type": "response.created", "response": {}}                # a package's envelope events are dropped
        yield {"type": "response.output_text.delta", "delta": "hi", "sequence_number": 99}
        yield {"type": "response.output_item.done", "item": MSG}

    async def ended():
        return {"status": "completed", "id": "x"}

    async def collect():
        return [chunk async for chunk in wire.stream({"status": "in_progress"}, events(), ended)]
    chunks = asyncio.run(collect())
    kinds = [json.loads(c.split("data: ", 1)[1])["type"] for c in chunks]
    assert kinds == ["response.created", "response.in_progress", "response.output_text.delta", "response.output_item.done", "response.completed"]
    assert [json.loads(c.split("data: ", 1)[1])["sequence_number"] for c in chunks] == [1, 2, 3, 4, 5]
