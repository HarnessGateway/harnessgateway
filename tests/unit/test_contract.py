import json

import pytest

from harnessgateway.domain.contract import ModelPath, OutputError, Routing, outcome, read_line, response
from harnessgateway.errors import Invalid


def test_model_paths():
    assert ModelPath.parse("claude-code") == ModelPath("claude-code")
    assert ModelPath.parse("pi/deepseek/deepseek/v4-flash") == ModelPath("pi", "deepseek", "deepseek/v4-flash")
    for bad in ("", "Bad", "x//m", 3):
        with pytest.raises(Invalid):
            ModelPath.parse(bad)


def test_routing_reads_only_what_routes_a_turn():
    r = Routing.parse({"model": "shell", "input": [{"role": "user", "content": [
        {"type": "input_file", "file_id": "file_a"}, {"type": "input_file", "file_id": "file_a"}]}],
        "tools": [{"type": "mcp", "server_label": "x"}], "stream": True, "store": False})
    assert (r.path.harness, r.stream, r.store, r.file_ids, len(r.mcp_tools)) == ("shell", True, False, ("file_a",), 1)


@pytest.mark.parametrize("body, match", [
    ([], "JSON object"), ({"model": "x", "input": 5}, "not a valid Responses request"),
    ({"model": "x", "conversation": "conv_1"}, "previous_response_id"),
    ({"model": "x", "previous_response_id": "abc"}, "previous_response_id"),
])
def test_routing_refusals(body, match):
    with pytest.raises(Invalid, match=match):
        Routing.parse(body)


def test_lines_are_events_terminals_envelopes_or_logs():
    assert read_line("hello").kind == "log"
    assert read_line('{"type": "other"}').kind == "log"
    assert read_line('{"type": "response.created", "response": {}}').kind == "envelope"
    t = read_line('{"type": "response.completed", "response": {"usage": null}}')
    assert t.kind == "terminal" and t.status == "completed"
    delta = '{"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0, ' \
            '"delta": "hi", "logprobs": []}'
    assert read_line(delta).kind == "event"


def test_an_invalid_event_names_its_field():
    with pytest.raises(OutputError, match="delta"):
        read_line('{"type": "response.output_text.delta", "item_id": "m", "output_index": 0, "content_index": 0}')
    with pytest.raises(OutputError, match="not a Responses stream event"):
        read_line('{"type": "response.made_up"}')


def test_outcome_and_response():
    item = {"id": "msg_1", "type": "message", "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": "hi", "annotations": []}]}
    term = json.loads('{"type": "response.failed", "response": {"error": {"code": "cli_error", "message": "boom"}}}')
    o = outcome(term, [item])
    doc = response(rid="resp_" + "0" * 32, body={"model": "shell", "metadata": {"a": "b"}}, created=1, status=o.status,
                   output=o.output, error=o.error, metadata={"session_id": "cntr_x"}, completed=2)
    assert doc["status"] == "failed" and doc["output"][0]["content"][0]["text"] == "hi"
    assert doc["error"]["code"] == "server_error" and doc["metadata"]["hg_error_code"] == "cli_error"
    assert doc["metadata"]["a"] == "b" and doc["completed_at"] == 2
