"""What a harness prints, and the kit that prints it."""

import io
import json
import sys

import pytest

from harnessgateway.domain.events import Event, LineError, Log, RunOutput, storable
from harnessgateway.domain.outcome import Terminal
from harnessgateway.kit import hgkit
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


def test_everything_the_kit_prints_is_accepted(tmp_path, monkeypatch):
    out, read = io.StringIO(), RunOutput()
    emit = hgkit.Emitter(out)
    emit.think("let me see")
    emit.say("hel")
    emit.say("lo")
    emit.ran("ls", "a.txt\n", "", 0)
    emit.wrote("a.txt", "+hi")
    emit.edited("a.txt", "-hi\n+ho")
    emit.deleted("a.txt")
    emit.called("docs", "search", {"q": "x"}, output="found")
    emit.called("docs", "search", error="down")
    emit.used("browser", "open x", "opened")
    emit.log("a note")
    emit.done({"input_tokens": 3, "output_tokens": 2})
    results = [read.read(line) for line in out.getvalue().splitlines()]
    kinds = [item["type"] for item in read.output()]
    assert kinds == ["reasoning", "message", "shell_call", "shell_call_output", "apply_patch_call", "apply_patch_call_output",
                     "apply_patch_call", "apply_patch_call_output", "apply_patch_call", "apply_patch_call_output", "mcp_call",
                     "mcp_call", "custom_tool_call", "custom_tool_call_output"]
    assert read.output()[1]["content"][0]["text"] == "hello" and isinstance(results[-1], Terminal) and read.terminal.kind == "done"
    with pytest.raises(RuntimeError, match="has ended"):
        emit.say("more")
    turn = tmp_path / "turn"
    (turn / "files" / "file_1").mkdir(parents=True)
    (turn / "files" / "file_1" / "notes.txt").write_text("n")
    (turn / "request.json").write_text(json.dumps({"model": "m", "instructions": "be brief", "reasoning": {"effort": "low"}, "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "first"}]}, {"role": "user", "content": "second"}]}))
    (turn / "history.json").write_text("[]")
    (turn / "handover.txt").write_text("Handover from codex.\n")
    env = {"HG_TURN": str(turn), "HG_MODEL": "m", "HG_STATE": str(tmp_path / "state" / "echo")}
    request, state = hgkit.Request(env), hgkit.State(env)
    assert (request.prompt, request.instructions, request.effort, request.model) == ("first\n\nsecond", "be brief", "low", "m")
    assert request.brief().split("\n\n") == ["Handover from codex.", "Instructions for this request:\nbe brief",
                                              f"Files given with this request: {turn}/files/file_1/notes.txt", "first", "second"]
    assert state.get("session") is None
    state.set("session", "abc")
    assert state.get("session") == "abc"
    state.clear("session")
    assert state.get("session") is None
    cli = hgkit.Cli([sys.executable, "-c", "print('one'); print('two'); raise SystemExit(3)"])
    assert list(cli.lines()) == ["one", "two"] and cli.exit_code == 3
