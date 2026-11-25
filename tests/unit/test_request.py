"""A Responses request, checked against the OpenAI SDK's own description of one."""

import pytest

from harnessgateway.domain.request import file_ids, input_items, validate, without_credentials
from harnessgateway.errors import Invalid

BODY = {"model": "a/b/c/d", "input": "hello"}


def test_what_the_responses_api_accepts_is_accepted_as_it_was_sent():
    full = {**BODY, "instructions": "be brief", "stream": True, "background": False, "store": True, "metadata": {"k": "v"},
            "reasoning": {"effort": "low"}, "previous_response_id": "resp_x", "temperature": 0.5, "max_output_tokens": 100,
            "tools": [{"type": "mcp", "server_label": "docs", "server_url": "https://mcp.example.com"},
                      {"type": "function", "name": "f", "parameters": {}, "strict": True}]}       # the harness's to act on
    assert validate(full) is full
    listed = {**BODY, "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}, {"type": "input_file", "file_id": "file_1"}]}]}
    assert validate(listed) is listed and file_ids(listed) == ["file_1"] and file_ids(BODY) == []
    answer = {**BODY, "previous_response_id": "resp_x", "input": [{"type": "function_call_output", "call_id": "c", "output": "sunny"},
                                                                 {"type": "mcp_approval_response", "approval_request_id": "a", "approve": True}]}
    assert validate(answer) is answer                                  # what a turn ended by asking, answered


@pytest.mark.parametrize(("change", "param"), [
    ({"temperatur": 1}, "temperatur"), ({"stream": "yes"}, "stream"), ({"input": None}, "input"), ({"model": None}, "model"),
    ({"conversation": "conv_1"}, "conversation"), ({"prompt": {"id": "p"}}, "prompt"),
    ({"metadata": {"hg_session": "x"}}, "metadata"), ({"metadata": {str(n): "v" for n in range(17)}}, "metadata"),
    ({"store": False, "background": True}, "store"),
])
def test_what_it_does_not_is_refused_by_the_field(change: dict, param: str):
    with pytest.raises(Invalid) as refusal:
        validate({**BODY, **change})
    assert refusal.value.param == param, refusal.value.message
    with pytest.raises(Invalid):
        validate([BODY])


def test_what_is_left_out_is_not_refused_since_openais_server_fills_some_of_it_in():
    """The SDK's types call an image's `detail` required; OpenAI's own examples leave it out."""
    def said(*parts: dict) -> dict:
        return {**BODY, "input": [{"role": "user", "content": list(parts)}]}
    for body in (said({"type": "input_image", "image_url": "https://example.com/a.png"}), said({"type": "input_image", "file_id": "file_1"}),
                 {**BODY, "tools": [{"type": "function", "name": "f"}]}, {**BODY, "input": [{"role": "user"}]}):
        assert validate(body) is body


@pytest.mark.parametrize(("change", "says"), [
    ({"input": [{"role": "user", "content": [{"type": "input_image", "image_url": "https://x/a.png", "detail": "huge"}]}]}, "input.0.content.0.detail: Input should be"),
    ({"input": [{"role": "user", "content": [{"type": "input_text", "text": "hi", "colour": "red"}]}]}, "input.0.content.0.colour is not a field"),
    ({"input": [{"role": "user", "content": [{"type": "input_picture"}]}]}, "input.0.content"),
    ({"input": [{"role": "user", "content": [{"image_url": "https://x/a.png"}]}]}, "input.0.content"),            # nothing says what it is
    ({"input": [{"type": "function_call_output", "call_id": 5, "output": "x"}]}, "input.0.call_id: Input should be a valid string"),
    ({"tools": [{"type": "function", "name": 5}]}, "tools.0.name: Input should be a valid string"),
    ({"tools": [5]}, "tools.0"), ({"tools": [{"type": "mcp", "server_label": "d", "server_url": 7}]}, "tools.0.server_url"),
    ({"reasoning": {"effort": "extreme"}}, "reasoning.effort: Input should be"),
    ({"input": 5}, "input: Input should be"),
])
def test_what_is_wrong_inside_a_request_is_refused_by_where_it_is(change: dict, says: str):
    with pytest.raises(Invalid) as refusal:
        validate({**BODY, **change})
    assert says in refusal.value.message and refusal.value.param == next(iter(change)), refusal.value.message


def test_an_item_a_response_held_is_taken_back_as_it_was():
    """A caller that keeps the conversation sends back what it was sent. An output item may hold a field its input
    form does not have: it is taken, and the request reaches its harness as it was sent."""
    printed = [{"type": "custom_tool_call", "id": "ctc_1", "call_id": "c1", "name": "Read", "input": "{}"},
               {"type": "custom_tool_call_output", "id": "cto_1", "call_id": "c1", "status": "completed", "output": "ok"},
               {"type": "function_call", "id": "fc_1", "call_id": "c2", "name": "f", "arguments": "{}", "status": "completed"}]
    body = {**BODY, "input": [{"role": "user", "content": "go"}, *printed, {"type": "function_call_output", "call_id": "c2", "output": "x"}]}
    assert validate(body) is body and body["input"][2]["status"] == "completed"
    for wrong, says in (({**printed[1], "colour": "red"}, "input.1.colour is not a field"), ({**printed[0], "status": 5}, "input.1")):
        with pytest.raises(Invalid) as refusal:
            validate({**BODY, "input": [{"role": "user", "content": "go"}, wrong]})
        assert says in refusal.value.message, refusal.value.message


def test_a_requests_input_is_listed_as_items_with_ids():
    assert input_items("resp_ABC", BODY) == [
        {"type": "message", "id": "msg_ABC_0", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    items = input_items("resp_ABC", {**BODY, "input": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]})
    assert [item["id"] for item in items] == ["msg_ABC_0", "msg_ABC_1"] and items[1]["content"] == "b"


def test_a_requests_credentials_are_bound_to_the_server_they_were_sent_for():
    made = iter(f"hgs_{number}" for number in range(9))
    tools = [{"type": "mcp", "server_label": "docs", "server_url": "https://mcp.example.com/mcp", "authorization": "tok", "headers": {"X-Key": "key"}},
             {"type": "mcp", "server_label": "local", "server_url": "http://tools.internal:8765/mcp", "headers": {"X-Key": "other"}},
             {"type": "mcp", "server_label": "drive", "connector_id": "connector_googledrive", "authorization": "nowhere"},
             {"type": "function", "name": "f", "parameters": {}, "strict": True}]
    body = {**BODY, "tools": tools}
    cleaned, bound = without_credentials(body, lambda: next(made))
    assert [(secret.value, secret.hosts, secret.placeholder) for secret in bound] == [
        ("tok", ("mcp.example.com",), "hgs_0"), ("key", ("mcp.example.com",), "hgs_1"), ("other", ("tools.internal:8765",), "hgs_2")]
    assert [tool.get("authorization") for tool in cleaned["tools"]] == ["hgs_0", None, "hgs_3", None]       # one with nowhere to go is forgotten
    assert cleaned["tools"][0]["headers"] == {"X-Key": "hgs_1"} and cleaned["tools"][3] == tools[3] and body["tools"] == tools
    assert without_credentials(BODY, lambda: next(made)) == (BODY, [])
