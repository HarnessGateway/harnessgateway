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
