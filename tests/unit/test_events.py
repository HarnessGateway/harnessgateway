"""What a harness prints."""

import json

import pytest

from harnessgateway.domain.events import Event, LineError, Log, RunOutput, storable
from harnessgateway.domain.outcome import Terminal
from tests.helpers import DONE, TEXT, lines


def test_events_are_numbered_checked_and_gathered_into_the_output():
    output = RunOutput()
    read = [output.read(text) for text in lines(*TEXT, {"type": "hg.log", "text": "note"}, DONE)]
    assert [type(each) for each in read] == [Event, Event, Event, Log, Terminal]
    assert [each.data["sequence_number"] for each in read[:3]] == [2, 3, 4] and read[3].text == "note"
    assert output.terminal.usage["total_tokens"] == 5 and output.terminal.usage["input_tokens_details"]["cached_tokens"] == 0
    assert output.output() == [TEXT[2]["item"]] and output.events == 3
    assert output.read(json.dumps(TEXT[0])) is None                    # whatever follows the terminal line is ignored


@pytest.mark.parametrize(("line", "why"), [
    ("hello", "not JSON"), ("[1]", "JSON object with a type"), ('{"type": "response.completed"}', "written by the platform"),
    ('{"type": "response.made_up"}', "not an event"), (json.dumps({**TEXT[0], "sequence_number": 9}), "does not number"),
    ('{"type": "response.output_text.delta"}', "response.output_text.delta: "), ('{"type": "hg.log", "text": 1}', "hg.log has a text"),
    ('{"type": "hg.done", "usage": {"input_tokens": "3"}}', "usage is an object"), ('{"type": "hg.done", "extra": 1}', "no field 'extra'"),
    ('{"type": "hg.incomplete", "reason": "tired"}', "max_output_tokens"), ('{"type": "hg.failed", "code": "Bad Code", "message": "m"}', "hg.failed has a code"),
    ('{"type": "hg.failed", "code": "ok"}', "has a message"),
])
def test_a_line_the_contract_does_not_allow_is_refused(line: str, why: str):
    with pytest.raises(LineError, match=why):
        RunOutput().read(line)


def test_text_that_was_cut_short_is_still_the_answer_so_far():
    cut = RunOutput()
    for text in lines(*TEXT[:2]):
        cut.read(text)
    assert cut.output() == [{**TEXT[0]["item"], "status": "incomplete", "content": [{"type": "output_text", "annotations": [], "text": "hel"}]}]
    assert storable({"a\x00": ["b\x00c", 1]}) == {"a�": ["b�c", 1]}
