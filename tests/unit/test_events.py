"""Spec §8.7: the lines a package prints, and how the platform reads, numbers and validates them."""

import json
from typing import Any

import pytest
from openai.types.responses import ResponseUsage

from harnessgateway.domain.events import LINE_MAX, LineError, OutputEvents, RunOutput, read_line

MESSAGE = {"id": "msg_1", "type": "message", "role": "assistant", "status": "in_progress", "content": []}
MESSAGE_DONE = {**MESSAGE, "status": "completed",
                "content": [{"type": "output_text", "text": "Hi", "annotations": []}]}
ADDED = {"type": "response.output_item.added", "output_index": 0, "item": MESSAGE}
DELTA = {"type": "response.output_text.delta", "item_id": "msg_1", "output_index": 0, "content_index": 0,
         "delta": "Hi", "logprobs": []}
DONE = {"type": "response.output_item.done", "output_index": 0, "item": MESSAGE_DONE}


def line(obj: Any) -> str:
    return json.dumps(obj)


def test_an_output_event_is_read_as_an_event() -> None:
    read = read_line(line(DELTA))
    assert (read.kind, read.event) == ("event", DELTA)


def test_a_log_line_is_read_as_a_log() -> None:
    read = read_line(line({"type": "hg.log", "text": "resuming session abc"}))
    assert (read.kind, read.text) == ("log", "resuming session abc")


def test_done_is_a_terminal_line_and_its_usage_is_completed() -> None:
    read = read_line(line({"type": "hg.done", "usage": {"input_tokens": 10, "output_tokens": 5}}))
    assert read.kind == "terminal"
    assert read.terminal is not None
    assert read.terminal.kind == "done"
    usage = read.terminal.usage
    assert usage is not None
    assert (usage["input_tokens"], usage["output_tokens"], usage["total_tokens"]) == (10, 5, 15)
    assert usage["input_tokens_details"]["cached_tokens"] == 0
    assert usage["output_tokens_details"]["reasoning_tokens"] == 0
    ResponseUsage.model_validate(usage)                         # the SDK accepts it as a complete usage object


def test_done_without_usage_has_none() -> None:
    read = read_line(line({"type": "hg.done"}))
    assert read.terminal is not None
    assert read.terminal.usage is None


def test_incomplete_and_failed_are_terminal_lines() -> None:
    incomplete = read_line(line({"type": "hg.incomplete", "reason": "max_output_tokens"})).terminal
    failed = read_line(line({"type": "hg.failed", "code": "provider_error", "message": "401"})).terminal
    assert incomplete is not None
    assert failed is not None
    assert (incomplete.kind, incomplete.reason) == ("incomplete", "max_output_tokens")
    assert (failed.kind, failed.code, failed.message) == ("failed", "provider_error", "401")


@pytest.mark.parametrize("text", [
    "plain text",
    "[1, 2]",
    '"a string"',
    line({"no": "type"}),
    line({"type": 5}),
    line({"type": "response.unknown.event"}),
    line({"type": "hg.unknown"}),
    line({"type": "response.created"}),                         # the platform writes response-level events
    line({"type": "response.completed"}),
    line({"type": "error"}),
    line({"type": "hg.incomplete", "reason": "bored"}),
    line({"type": "hg.failed", "code": "Bad Code", "message": "x"}),
    line({"type": "hg.failed", "code": "ok"}),                  # a failure says what went wrong
    line({"type": "hg.done", "usage": {"input_tokens": "many"}}),
    line({"type": "hg.done", "surprise": 1}),
    line({"type": "hg.log"}),
    "",
])
def test_a_line_that_is_not_allowed_is_refused(text: str) -> None:
    with pytest.raises(LineError):
        read_line(text)


def test_a_line_over_the_limit_is_refused_without_being_parsed() -> None:
    with pytest.raises(LineError, match="16 MiB"):
        read_line("x" * (LINE_MAX + 1))


def test_events_are_numbered_from_where_the_platform_left_off() -> None:
    events = OutputEvents(first_sequence_number=2)
    a = events.accept(dict(ADDED))
    b = events.accept(dict(DELTA))
    assert (a["sequence_number"], b["sequence_number"]) == (2, 3)
    assert events.next_sequence_number == 4


def test_an_event_that_does_not_fit_the_openai_type_is_refused() -> None:
    events = OutputEvents(first_sequence_number=0)
    broken = {k: v for k, v in DELTA.items() if k != "delta"}
    with pytest.raises(LineError, match="delta"):
        events.accept(broken)
    assert events.next_sequence_number == 0                     # a refused event takes no number


def test_a_package_may_not_number_events_itself() -> None:
    with pytest.raises(LineError, match="sequence_number"):
        OutputEvents(first_sequence_number=0).accept({**DELTA, "sequence_number": 9})


def test_the_output_is_the_finished_items_in_order() -> None:
    events = OutputEvents(first_sequence_number=0)
    reasoning = {"id": "rs_1", "type": "reasoning", "summary": []}
    events.accept({"type": "response.output_item.added", "output_index": 0, "item": reasoning})
    events.accept({"type": "response.output_item.done", "output_index": 0, "item": reasoning})
    events.accept({**ADDED, "output_index": 1})
    events.accept({**DELTA, "output_index": 1})
    events.accept({**DONE, "output_index": 1})
    assert [item["type"] for item in events.output()] == ["reasoning", "message"]
    assert events.output()[1] == MESSAGE_DONE


def test_an_item_that_was_started_but_never_finished_is_not_output() -> None:
    events = OutputEvents(first_sequence_number=0)
    events.accept(dict(ADDED))
    assert events.output() == []


def test_a_run_s_output_is_read_line_by_line_until_its_terminal_line() -> None:
    """Spec §8.7, the five things the platform does with each line."""
    run = RunOutput(first_sequence_number=2)
    assert run.terminal is None

    event = run.read(json.dumps(ADDED))
    assert event is not None
    assert (event.kind, event.event and event.event["sequence_number"]) == ("event", 2)
    log = run.read('{"type": "hg.log", "text": "working"}')
    assert log is not None
    assert (log.kind, log.text) == ("log", "working")
    assert run.read(json.dumps(DONE)) is not None

    end = run.read('{"type": "hg.done"}')
    assert end is not None
    assert end.kind == "terminal"
    assert run.terminal is not None
    assert run.terminal.kind == "done"
    assert run.read("anything at all, even this") is None                 # after the terminal line: ignored
    assert [item["id"] for item in run.output()] == ["msg_1"]
    assert run.next_sequence_number == 4


def test_a_bad_line_in_a_run_s_output_is_refused() -> None:
    with pytest.raises(LineError):
        RunOutput(first_sequence_number=2).read("plain text")
