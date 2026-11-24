"""What a harness prints: output items, stream events and the stream's last event, as the Responses API has them."""

import json
import re

import pytest

from harnessgateway import shipped
from harnessgateway.domain.events import EVENTS, ITEMS, LineError, RunOutput, storable
from tests.helpers import DONE, TEXT, lines

SAYS = {"type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Done.", "annotations": []}]}
RAN = {"type": "shell_call", "call_id": "c1", "status": "completed", "action": {"commands": ["ls"]}}


def read(*printed: dict) -> tuple[RunOutput, list[dict]]:
    output = RunOutput()
    return output, [event for text in lines(*printed) for event in output.read(text)]


def test_stream_events_are_placed_numbered_checked_and_gathered_into_the_output():
    output, events = read({**TEXT[0], "output_index": 7, "sequence_number": 9}, *TEXT[1:], DONE)
    assert [(event["sequence_number"], event["output_index"]) for event in events] == [(2, 0), (3, 0), (4, 0)]
    assert output.terminal.status == "completed" and output.terminal.usage["total_tokens"] == 5
    assert output.terminal.usage["input_tokens_details"]["cached_tokens"] == 0
    assert output.output() == [TEXT[2]["item"]] and output.events == 3
    assert output.read(json.dumps(TEXT[0])) == []                      # whatever follows the last event is ignored


def test_an_item_printed_whole_is_given_its_id_its_place_and_its_events():
    output, events = read(RAN, SAYS)
    kinds = [event["type"].removeprefix("response.") for event in events]
    assert kinds == ["output_item.added", "output_item.done", "output_item.added", "content_part.added",
                     "output_text.delta", "output_text.done", "content_part.done", "output_item.done"]
    ran, says = output.output()
    assert ran == {**RAN, "id": ran["id"]} and says == {**SAYS, "id": says["id"]} and ran["id"] != says["id"]
    assert [event["output_index"] for event in events] == [0, 0, 1, 1, 1, 1, 1, 1]
    assert events[2]["item"]["status"] == "in_progress" and events[4]["delta"] == "Done."     # read as a stream is read
    assert read({**RAN, "id": "mine"})[0].output()[0]["id"] == "mine"


def test_text_that_names_no_item_is_kept_in_one_until_something_else_is_printed():
    piece = {"type": "response.output_text.delta"}
    output, events = read({**piece, "delta": ""}, {"type": "response.reasoning_summary_text.delta", "delta": "hm"},
                          {**piece, "delta": "hel"}, {**piece, "delta": "lo"}, RAN, {**piece, "delta": "bye"},
                          {"type": "response.completed"})                # a piece with no text begins nothing
    thought, said, ran, last = output.output()
    assert thought["summary"] == [{"type": "summary_text", "text": "hm"}] and ran["type"] == "shell_call"
    assert (said["status"], said["content"][0]["text"], last["content"][0]["text"]) == ("completed", "hello", "bye")
    assert [event["output_index"] for event in events if event["type"] == "response.output_item.done"] == [0, 1, 2, 3]
    assert [event["delta"] for event in events if event["type"] == "response.output_text.delta"] == ["hel", "lo", "bye"]
    assert all(EVENTS[event["type"]].model_validate(event) for event in events)


def test_how_a_run_says_it_ended():
    usage = {"input_tokens": 3, "output_tokens": 2, "input_tokens_details": {"cached_tokens": 1}}
    cut = read({"type": "response.incomplete", "response": {"usage": usage, "incomplete_details": {"reason": "max_output_tokens"}}})[0]
    assert (cut.terminal.status, cut.terminal.reason, cut.terminal.usage["input_tokens_details"]["cached_tokens"]) == ("incomplete", "max_output_tokens", 1)
    failed = read({"type": "response.failed", "response": {"error": {"code": "no_auth", "message": "refused"}}})[0].terminal
    assert (failed.status, failed.code, failed.message, failed.usage) == ("failed", "no_auth", "refused", None)
    assert read({"type": "response.failed", "response": {"error": {"message": "refused"}}})[0].terminal.code is None


@pytest.mark.parametrize(("line", "why"), [
    ("hello", "not JSON"), ("[1]", "JSON object with a type"), ('{"type": "response.created"}', "written by the platform"),
    ('{"type": "hg.done"}', "neither an output item nor a stream event"),
    ('{"type": "shell_call", "call_id": "c"}', "shell_call: action: Field required"),
    ('{"type": "response.output_text.delta"}', "response.output_text.delta: delta"),
    (json.dumps(TEXT[1]), "names an item that has not been added"), (json.dumps(TEXT[2]), "is added once, and then done once"),
    ('{"type": "response.output_item.added", "item": {"type": "message"}}', "carries an output item, with its id"),
    ('{"type": "response.shell_call_command.delta", "command_index": 0, "delta": "ls"}', "comes after the item it is about"),
    ('{"type": "response.completed", "usage": {}}', "carries a response, and nothing else"),
    ('{"type": "response.completed", "response": {"usage": {"input_tokens": "3"}}}', "usage is an object"),
    ('{"type": "response.completed", "response": {"output": []}}', "response.output is the platform's to write: a harness says usage"),
    ('{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "tired"}}}', "max_output_tokens"),
    ('{"type": "response.failed", "response": {"error": {"code": "Bad Code", "message": "m"}}}', "response.error.code is"),
    ('{"type": "response.failed", "response": {"error": {"code": "ok"}}}', "says what went wrong"),
])
def test_a_line_the_contract_does_not_allow_is_refused(line: str, why: str):
    with pytest.raises(LineError, match=re.escape(why)):
        RunOutput().read(line)


def test_text_that_was_cut_short_is_still_the_answer_so_far():
    assert read(*TEXT[:2])[0].output() == [{**TEXT[0]["item"], "status": "incomplete", "content": [{"type": "output_text", "annotations": [], "text": "hel"}]}]
    cut = read({"type": "response.output_text.delta", "delta": "hel"})[0].output()
    assert [(item["status"], item["content"][0]["text"]) for item in cut] == [("incomplete", "hel")]
    assert storable({"a\x00": ["b\x00c", 1]}) == {"a�": ["b�c", 1]}


def test_every_line_the_contract_shows_is_one_the_platform_takes():
    shown = re.findall(r"^\{.*\}$", (shipped.folder("contract") / "PACKAGES.md").read_text(), re.M)
    output = read(*map(json.loads, shown[:3]))[0]                 # the items, then each way a run ends
    assert len(shown) == 6 and [item["type"] for item in output.output()] == ["message", "shell_call", "shell_call_output"]
    assert [read(json.loads(line))[0].terminal.status for line in shown[3:]] == ["completed", "incomplete", "failed"]
    assert len(ITEMS) == 28 and "message" in ITEMS and "response.output_text.delta" in EVENTS
