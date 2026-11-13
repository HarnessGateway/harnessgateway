"""Spec §13.8: what a request's input refers to."""

from harnessgateway.domain.request import file_ids


def test_a_string_input_refers_to_no_files() -> None:
    assert file_ids({"input": "hello"}) == []
    assert file_ids({}) == []


def test_file_and_image_parts_name_their_files_once_in_order() -> None:
    body = {"input": [
        {"role": "user", "content": "plain"},
        {"role": "user", "content": [
            {"type": "input_text", "text": "Read these."},
            {"type": "input_file", "file_id": "file_B"},
            {"type": "input_image", "file_id": "file_A", "detail": "auto"},
            {"type": "input_file", "file_id": "file_B"},
            {"type": "input_file", "file_url": "https://example.com/x.pdf"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
        ]},
        {"type": "function_call_output", "call_id": "c", "output": "x"},
        "not an item",
    ]}
    assert file_ids(body) == ["file_B", "file_A"]


# ── validating a request (spec §13.3) ───────────────────────────────────────────────────────────────

import pytest  # noqa: E402

from harnessgateway.domain.request import validate  # noqa: E402
from harnessgateway.errors import Invalid  # noqa: E402

MCP = {"type": "mcp", "server_label": "github", "server_url": "https://mcp.example.com"}


def test_a_valid_request_is_returned_exactly_as_it_was_sent() -> None:
    body = {
        "model": "claude-code/deepseek", "input": [{"role": "user", "content": [
            {"type": "input_text", "text": "Read it."}, {"type": "input_file", "file_id": "file_1"}]}],
        "instructions": "Be brief.", "previous_response_id": "resp_1", "stream": True, "background": False,
        "store": True, "metadata": {"ticket": "42"}, "tools": [MCP], "reasoning": {"effort": "high"},
        "max_output_tokens": 1000, "temperature": 0.2, "truncation": "auto", "user": "u1",
    }
    assert validate(body) == body
    assert validate({"model": "shell", "input": "echo hi"}) == {"model": "shell", "input": "echo hi"}


@pytest.mark.parametrize(("body", "message", "param"), [
    ([], "the request body is a JSON object", None),
    ({"input": "x"}, "model is required", "model"),
    ({"model": "shell"}, "input is required", "input"),
    ({"model": "shell", "input": 5}, "input", "input"),
    ({"model": "shell", "input": "x", "bogus": 1}, "bogus is not a field of the Responses API", "bogus"),
    ({"model": "shell", "input": "x", "temperature": "hot"}, "temperature", "temperature"),
    ({"model": "shell", "input": "x", "stream": "yes"}, "stream", "stream"),
    ({"model": "shell", "input": "x", "conversation": "conv_1"}, "conversation is not supported", "conversation"),
    ({"model": "shell", "input": "x", "prompt": {"id": "pmpt_1"}}, "prompt is not supported", "prompt"),
    ({"model": "shell", "input": "x", "tools": [{"type": "function", "name": "f", "parameters": {},
                                                 "strict": True}]},
     "only tools of type mcp are supported", "tools"),
    ({"model": "shell", "input": "x", "tools": [MCP, {"type": "web_search"}]},
     "only tools of type mcp are supported", "tools"),
    ({"model": "shell", "input": "x", "metadata": {f"k{n}": "v" for n in range(17)}}, "at most 16", "metadata"),
    ({"model": "shell", "input": "x", "metadata": {"k" * 65: "v"}}, "64 characters", "metadata"),
    ({"model": "shell", "input": "x", "metadata": {"k": "v" * 513}}, "512 characters", "metadata"),
    ({"model": "shell", "input": "x", "metadata": {"hg_session": "sess_x"}}, "hg_", "metadata"),
    ({"model": "shell", "input": "x", "metadata": {"k": 5}}, "metadata", "metadata"),
])
def test_what_is_not_allowed_is_refused_naming_the_field(body: object, message: str, param: str | None) -> None:
    with pytest.raises(Invalid, match=message) as refusal:
        validate(body)
    assert refusal.value.param == param
