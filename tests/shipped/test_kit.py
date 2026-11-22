"""The kit, held to the contract: everything it prints is something the platform takes, and it reads a turn's
inputs as the platform writes them. It is imported as a package's `run` imports it: by its folder, not as part of
the platform."""

import io
import json
import sys

import hgkit
import pytest

from harnessgateway.domain.events import RunOutput
from harnessgateway.domain.outcome import Terminal


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
