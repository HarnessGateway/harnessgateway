"""A Responses request, checked against the OpenAI SDK's own description of one."""

import pytest

from harnessgateway.domain.request import file_ids, input_items, validate
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


def test_a_requests_input_is_listed_as_items_with_ids():
    assert input_items("resp_ABC", BODY) == [
        {"type": "message", "id": "msg_ABC_0", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}]
    items = input_items("resp_ABC", {**BODY, "input": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]})
    assert [item["id"] for item in items] == ["msg_ABC_0", "msg_ABC_1"] and items[1]["content"] == "b"
