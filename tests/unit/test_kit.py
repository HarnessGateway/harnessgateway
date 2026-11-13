"""Spec §8.8: the kit. Every line its Emitter prints must be one the platform accepts (§8.7)."""

import ast
import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from harnessgateway.domain.events import Line, OutputEvents, read_line
from harnessgateway.kit import hgkit


class Printed:
    """An Emitter writing to memory, and what the platform makes of each line it printed."""

    def __init__(self) -> None:
        self._out = io.StringIO()
        counter = iter(range(1, 10_000))
        self.emit = hgkit.Emitter(out=self._out, ids=lambda: f"{next(counter):04d}")

    def lines(self) -> list[Line]:
        return [read_line(text) for text in self._out.getvalue().splitlines()]

    def events(self) -> list[dict[str, Any]]:
        accepted = OutputEvents(first_sequence_number=2)
        return [accepted.accept(line.event) for line in self.lines() if line.kind == "event" and line.event]

    def output(self) -> list[dict[str, Any]]:
        accepted = OutputEvents(first_sequence_number=2)
        for line in self.lines():
            if line.kind == "event" and line.event:
                accepted.accept(line.event)
        return accepted.output()


@pytest.fixture
def printed() -> Printed:
    return Printed()


def test_text_said_in_pieces_is_one_message(printed: Printed) -> None:
    printed.emit.say("Hello")
    printed.emit.say(", world")
    printed.emit.done()
    assert [event["type"] for event in printed.events()] == [
        "response.output_item.added", "response.content_part.added",
        "response.output_text.delta", "response.output_text.delta",
        "response.output_text.done", "response.content_part.done", "response.output_item.done",
    ]
    assert printed.output() == [{
        "id": "msg_0001", "type": "message", "role": "assistant", "status": "completed",
        "content": [{"type": "output_text", "text": "Hello, world", "annotations": []}],
    }]
    assert [event["delta"] for event in printed.events() if event["type"].endswith("delta")] == ["Hello", ", world"]


def test_thinking_is_a_reasoning_item_with_a_summary(printed: Printed) -> None:
    printed.emit.think("First, ")
    printed.emit.think("look around.")
    printed.emit.done()
    assert [event["type"] for event in printed.events()] == [
        "response.output_item.added", "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta", "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done", "response.reasoning_summary_part.done", "response.output_item.done",
    ]
    assert printed.output() == [{
        "id": "rs_0001", "type": "reasoning", "summary": [{"type": "summary_text", "text": "First, look around."}]}]


def test_a_different_kind_of_output_ends_the_text_before_it(printed: Printed) -> None:
    printed.emit.think("Plan.")
    printed.emit.say("Doing it.")
    printed.emit.ran("ls", stdout="a\n")
    printed.emit.say("Done.")
    printed.emit.done()
    output = printed.output()
    assert [item["type"] for item in output] == ["reasoning", "message", "shell_call", "shell_call_output", "message"]
    assert [event["output_index"] for event in printed.events() if event["type"] == "response.output_item.done"] == [
        0, 1, 2, 3, 4]
    assert output[1]["content"][0]["text"] == "Doing it."
    assert output[4]["content"][0]["text"] == "Done."


def test_a_command_is_a_shell_call_and_its_output(printed: Printed) -> None:
    printed.emit.ran("make test", stdout="ok\n", stderr="warning\n", exit_code=2)
    printed.emit.done()
    call, result = printed.output()
    assert call == {"id": "sh_0001", "type": "shell_call", "call_id": "call_0001", "status": "completed",
                    "action": {"commands": ["make test"]}}
    assert result == {"id": "sho_0001", "type": "shell_call_output", "call_id": "call_0001", "status": "completed",
                      "output": [{"stdout": "ok\n", "stderr": "warning\n",
                                  "outcome": {"type": "exit", "exit_code": 2}}]}


def test_file_changes_are_apply_patch_calls(printed: Printed) -> None:
    printed.emit.wrote("src/new.py", "+print('hi')\n")
    printed.emit.edited("src/old.py", "-a\n+b\n")
    printed.emit.deleted("src/gone.py")
    printed.emit.done()
    output = printed.output()
    assert [item["type"] for item in output] == ["apply_patch_call", "apply_patch_call_output"] * 3
    assert [item["operation"] for item in output[::2]] == [
        {"type": "create_file", "path": "src/new.py", "diff": "+print('hi')\n"},
        {"type": "update_file", "path": "src/old.py", "diff": "-a\n+b\n"},
        {"type": "delete_file", "path": "src/gone.py"},
    ]
    assert all(item["call_id"] == result["call_id"] for item, result in zip(output[::2], output[1::2], strict=True))


def test_an_mcp_call_names_its_server_and_tool(printed: Printed) -> None:
    printed.emit.called("github", "create_issue", {"title": "Bug"}, output="created #12")
    printed.emit.called("github", "close_issue", {"number": 12}, error="not allowed")
    printed.emit.done()
    ok, failed = printed.output()
    assert ok == {"id": "mcp_0001", "type": "mcp_call", "server_label": "github", "name": "create_issue",
                  "arguments": '{"title": "Bug"}', "output": "created #12", "status": "completed"}
    assert (failed["status"], failed["error"]) == (
        "failed", {"type": "mcp_tool_execution_error", "content": "not allowed"})


def test_any_other_tool_is_a_custom_tool_call_and_its_output(printed: Printed) -> None:
    printed.emit.used("web_search", "weather in Austin", "Sunny, 24C")
    printed.emit.done()
    assert printed.output() == [
        {"id": "ctc_0001", "type": "custom_tool_call", "call_id": "call_0001", "name": "web_search",
         "input": "weather in Austin"},
        {"id": "ctco_0001", "type": "custom_tool_call_output", "call_id": "call_0001", "status": "completed",
         "output": "Sunny, 24C"},
    ]


def test_a_log_line_is_kept_apart_from_the_output(printed: Printed) -> None:
    printed.emit.log("started the CLI")
    printed.emit.done()
    first = printed.lines()[0]
    assert (first.kind, first.text) == ("log", "started the CLI")
    assert printed.output() == []


@pytest.mark.parametrize(("end", "expected"), [
    (lambda e: e.done(), ("done", None, None)),
    (lambda e: e.done({"input_tokens": 10, "output_tokens": 5}), ("done", None, None)),
    (lambda e: e.incomplete("max_output_tokens"), ("incomplete", "max_output_tokens", None)),
    (lambda e: e.failed("provider_error", "The provider refused the key."), ("failed", None, "provider_error")),
])
def test_the_terminal_line_is_last_and_closes_open_text(printed: Printed, end: Any, expected: Any) -> None:
    printed.emit.say("partial")
    end(printed.emit)
    last = printed.lines()[-1]
    assert last.kind == "terminal"
    assert last.terminal is not None
    assert (last.terminal.kind, last.terminal.reason, last.terminal.code) == expected
    assert printed.output()[0]["content"][0]["text"] == "partial"


def test_usage_is_passed_through(printed: Printed) -> None:
    printed.emit.done({"input_tokens": 10, "output_tokens": 5})
    terminal = printed.lines()[-1].terminal
    assert terminal is not None
    assert terminal.usage is not None
    assert (terminal.usage["input_tokens"], terminal.usage["total_tokens"]) == (10, 15)


def test_nothing_can_be_printed_after_the_terminal_line(printed: Printed) -> None:
    printed.emit.done()
    with pytest.raises(RuntimeError, match="ended"):
        printed.emit.say("more")
    with pytest.raises(RuntimeError, match="ended"):
        printed.emit.done()


def test_empty_text_prints_nothing(printed: Printed) -> None:
    printed.emit.say("")
    printed.emit.think("")
    printed.emit.done()
    assert printed.output() == []


# ── the request ────────────────────────────────────────────────────────────────────────────────────────────


def turn(tmp_path: Path, body: dict[str, Any], *, handover: str = "", history: list[Any] | None = None,
         files: dict[str, dict[str, bytes]] | None = None, **variables: str) -> dict[str, str]:
    """A turn's inputs laid out as the platform lays them out, and the variables that point at them."""
    folders = {name: tmp_path / name for name in ("turn", "mind", "work", "harness")}
    for folder in folders.values():
        folder.mkdir(exist_ok=True)
    (folders["turn"] / "request.json").write_text(json.dumps(body))
    (folders["turn"] / "history.json").write_text(json.dumps(history or []))
    (folders["turn"] / "handover.txt").write_text(handover)
    (folders["turn"] / "files").mkdir(exist_ok=True)
    for file_id, named in (files or {}).items():
        (folders["turn"] / "files" / file_id).mkdir()
        for name, content in named.items():
            (folders["turn"] / "files" / file_id / name).write_bytes(content)
    return {
        "HG_TURN": str(folders["turn"]), "HG_MIND": str(folders["mind"]), "HG_WORK": str(folders["work"]),
        "HG_HARNESS": str(folders["harness"]), "HG_STATE": str(folders["mind"] / ".harnessgateway" / "demo"),
        "HG_HANDOVER": handover, "HG_MODEL": "deepseek-flash", "HG_PROVIDER": "deepseek",
        "HG_PROVIDER_API": "anthropic", "HG_PROVIDER_URL": "https://api.deepseek.com/anthropic",
        "HG_PROVIDER_KEY": "hgs_0123456789abcdef0123456789abcdef", **variables,
    }


def test_a_string_input_is_the_prompt(tmp_path: Path) -> None:
    request = hgkit.Request(turn(tmp_path, {"model": "demo", "input": "List the files.", "instructions": "Be brief."}))
    assert (request.prompt, request.instructions, request.model) == ("List the files.", "Be brief.", "deepseek-flash")
    assert request.body["model"] == "demo"
    assert (request.provider.name, request.provider.api, request.provider.url, request.provider.key) == (
        "deepseek", "anthropic", "https://api.deepseek.com/anthropic", "hgs_0123456789abcdef0123456789abcdef")


def test_a_list_input_gives_the_text_of_its_user_messages(tmp_path: Path) -> None:
    body = {"model": "demo", "input": [
        {"role": "user", "content": "First part."},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "An earlier answer."}]},
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "Second part."},
            {"type": "input_file", "file_id": "file_1"},
            {"type": "input_text", "text": "Third part."},
        ]},
    ]}
    assert hgkit.Request(turn(tmp_path, body)).prompt == "First part.\n\nSecond part.\n\nThird part."


def test_the_brief_is_the_handover_the_instructions_and_the_prompt(tmp_path: Path) -> None:
    handed = hgkit.Request(turn(tmp_path, {"input": "Carry on.", "instructions": "Be brief."},
                                handover="Handover from pi. Review /mind/.pi."))
    assert handed.handover == "Handover from pi. Review /mind/.pi."
    assert handed.brief() == (
        "Handover from pi. Review /mind/.pi.\n\nInstructions for this request:\nBe brief.\n\nCarry on.")
    assert handed.brief(instructions=False) == "Handover from pi. Review /mind/.pi.\n\nCarry on."
    assert hgkit.Request(turn(tmp_path, {"input": "Carry on."})).brief() == "Carry on."


def test_the_brief_says_where_the_request_s_files_are(tmp_path: Path) -> None:
    variables = turn(tmp_path, {"input": "Read it."}, files={"file_1": {"report.pdf": b"PDF"}})
    assert hgkit.Request(variables).brief() == "Files given with this request: inputs/report.pdf\n\nRead it."
    assert (Path(variables["HG_WORK"]) / "inputs" / "report.pdf").read_bytes() == b"PDF"


def test_reasoning_effort_history_and_mcp_servers_are_read(tmp_path: Path) -> None:
    history = [{"id": "resp_1", "harness": "pi", "status": "completed", "input": "hi", "output": []}]
    body = {"input": "x", "reasoning": {"effort": "high"}, "tools": [
        {"type": "mcp", "server_label": "github", "server_url": "https://mcp.example.com"}]}
    request = hgkit.Request(turn(tmp_path, body, history=history))
    assert request.effort == "high"
    assert request.history == history
    assert request.mcp_servers == [{"type": "mcp", "server_label": "github", "server_url": "https://mcp.example.com"}]
    plain = hgkit.Request(turn(tmp_path, {"input": "x"}))
    assert (plain.effort, plain.mcp_servers, plain.instructions) == (None, [], None)


def test_input_files_are_listed_and_placed_in_the_work_folder(tmp_path: Path) -> None:
    variables = turn(tmp_path, {"input": "Read them."}, files={
        "file_1": {"report.pdf": b"PDF"}, "file_2": {"data.csv": b"a,b"}, "file_3": {"report.pdf": b"OTHER"}})
    request = hgkit.Request(variables)
    assert [(f.id, f.filename) for f in request.files] == [
        ("file_1", "report.pdf"), ("file_2", "data.csv"), ("file_3", "report.pdf")]
    placed = request.place_files()
    work = Path(variables["HG_WORK"])
    assert placed == [str(work / "inputs" / "report.pdf"), str(work / "inputs" / "data.csv"),
                      str(work / "inputs" / "file_3-report.pdf")]
    assert (work / "inputs" / "report.pdf").read_bytes() == b"PDF"
    assert (work / "inputs" / "file_3-report.pdf").read_bytes() == b"OTHER"
    assert request.place_files() == placed                              # placing again changes nothing


def test_files_and_images_sent_inline_are_written_to_the_work_folder(tmp_path: Path) -> None:
    data = base64.b64encode(b'{"customers": 75}').decode()
    variables = turn(tmp_path, {"input": [{"role": "user", "content": [
        {"type": "input_text", "text": "Read them."},
        {"type": "input_file", "filename": "customers.json", "file_data": f"data:application/json;base64,{data}"},
        {"type": "input_file", "filename": "../../etc/bare.txt", "file_data": base64.b64encode(b"bare").decode()},
        {"type": "input_image", "image_url": "data:image/png;base64," + base64.b64encode(b"PNG").decode()},
        {"type": "input_image", "image_url": "https://example.com/elsewhere.png"},
        {"type": "input_file", "file_id": "file_1"},
        {"type": "input_file", "file_url": "https://example.com/report.pdf"},
    ]}]}, files={"file_1": {"customers.json": b"another"}})
    request = hgkit.Request(variables)
    work = Path(variables["HG_WORK"])
    assert request.place_files() == [str(work / "inputs" / name) for name in (
        "customers.json", "inline-1-customers.json", "bare.txt", "image-3.png")]     # uploaded first, then inline
    assert (work / "inputs" / "customers.json").read_bytes() == b"another"
    assert (work / "inputs" / "inline-1-customers.json").read_bytes() == b'{"customers": 75}'
    assert (work / "inputs" / "bare.txt").read_bytes() == b"bare"           # a name never leaves the folder
    assert (work / "inputs" / "image-3.png").read_bytes() == b"PNG"
    assert request.place_files() == request.place_files()
    assert sorted(path.name for path in (work / "inputs").iterdir()) == [
        "bare.txt", "customers.json", "image-3.png", "inline-1-customers.json"]


def test_a_file_sent_again_in_a_later_turn_replaces_the_one_from_before(tmp_path: Path) -> None:
    def sent(content: bytes) -> dict[str, Any]:
        return {"input": [{"role": "user", "content": [{"type": "input_file", "filename": "customers.json",
                                                        "file_data": base64.b64encode(content).decode()}]}]}
    first = hgkit.Request(turn(tmp_path, sent(b"monday's customers")))
    assert first.brief() == "Files given with this request: inputs/customers.json"
    later = hgkit.Request(turn(tmp_path, sent(b"tuesday's customers")))          # the same work folder, a new turn
    assert later.brief() == "Files given with this request: inputs/customers.json"
    inputs = tmp_path / "work" / "inputs"
    assert [path.name for path in inputs.iterdir()] == ["customers.json"]
    assert (inputs / "customers.json").read_bytes() == b"tuesday's customers"


def test_inline_data_that_is_not_base64_is_refused_by_name(tmp_path: Path) -> None:
    variables = turn(tmp_path, {"input": [{"role": "user", "content": [
        {"type": "input_file", "filename": "broken.bin", "file_data": "data:application/octet-stream;base64,@@@"}]}]})
    with pytest.raises(ValueError, match=r"broken\.bin.*base64"):
        hgkit.Request(variables).place_files()


def test_state_keeps_small_named_values_in_the_mind(tmp_path: Path) -> None:
    variables = turn(tmp_path, {"input": "x"})
    state = hgkit.State(variables)
    assert state.get("session") is None
    state.set("session", "abc-123")
    assert hgkit.State(variables).get("session") == "abc-123"
    assert (Path(variables["HG_STATE"]) / "session").read_text() == "abc-123"
    state.clear("session")
    state.clear("session")
    assert state.get("session") is None
    with pytest.raises(ValueError, match="name"):
        state.set("../escape", "x")


def test_seeds_are_copied_into_the_mind_without_overwriting(tmp_path: Path) -> None:
    variables = turn(tmp_path, {"input": "x"})
    seeds = Path(variables["HG_HARNESS"]) / "seeds" / "mind"
    (seeds / ".demo" / "skills").mkdir(parents=True)
    (seeds / ".demo" / "settings.json").write_text("seeded")
    (seeds / ".demo" / "skills" / "one.md").write_text("skill")
    mind = Path(variables["HG_MIND"])
    (mind / ".demo").mkdir()
    (mind / ".demo" / "settings.json").write_text("the agent's own")

    hgkit.seed_mind(variables)
    assert (mind / ".demo" / "settings.json").read_text() == "the agent's own"
    assert (mind / ".demo" / "skills" / "one.md").read_text() == "skill"
    hgkit.seed_mind(turn(tmp_path, {"input": "x"}, HG_HARNESS=str(tmp_path / "no-seeds")))     # no seeds: nothing


# ── running and replaying a CLI ────────────────────────────────────────────────────────────────────────────


def test_a_cli_s_lines_are_read_as_it_prints_them(tmp_path: Path) -> None:
    cli = hgkit.Cli([sys.executable, "-c", "print('one'); print('two'); import sys; sys.exit(3)"], env={})
    assert list(cli.lines()) == ["one", "two"]
    assert cli.exit_code == 3


def test_with_a_recording_the_cli_is_not_started(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    recording.write_text('{"type":"start"}\n{"type":"end"}\n')
    cli = hgkit.Cli(["/no/such/program"], env={"HG_REPLAY": str(recording)})
    assert list(cli.lines()) == ['{"type":"start"}', '{"type":"end"}']
    assert cli.exit_code == 0


def test_a_cli_s_output_can_be_copied_to_a_file_to_make_a_recording(tmp_path: Path) -> None:
    recording = tmp_path / "recording"
    cli = hgkit.Cli([sys.executable, "-c", "print('one'); print('two')"], env={"HGKIT_RECORD": str(recording)})
    assert list(cli.lines()) == ["one", "two"]
    assert recording.read_text() == "one\ntwo\n"
    replayed = hgkit.Cli(["/no/such/program"], env={"HG_REPLAY": str(recording)})
    assert list(replayed.lines()) == ["one", "two"]


def test_under_a_package_check_ids_are_counted_so_output_is_repeatable() -> None:
    def printed_under_check() -> str:
        out = io.StringIO()
        emit = hgkit.Emitter(out=out, env={"HG_CHECK": "1"})
        emit.say("a")
        emit.ran("ls")
        emit.done()
        return out.getvalue()
    assert printed_under_check() == printed_under_check()
    assert '"msg_0001"' in printed_under_check()
    different = io.StringIO()
    hgkit.Emitter(out=different, env={}).say("a")
    assert '"msg_0001"' not in different.getvalue()


def test_output_that_is_not_text_does_not_stop_the_reading() -> None:
    cli = hgkit.Cli([sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'ok \\xff\\xfe\\n')"], env={})
    assert list(cli.lines()) == ["ok \ufffd\ufffd"]


def test_the_kit_runs_on_python_3_9_syntax_and_the_standard_library_alone() -> None:
    source = Path(hgkit.__file__).read_text()
    ast.parse(source, feature_version=(3, 9))
    script = (
        "import sys, importlib.util\n"
        "spec = importlib.util.spec_from_file_location('hgkit', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['hgkit'] = module\n"
        "spec.loader.exec_module(module)\n"
        "ours = ('hgkit', '__main__')\n"
        "outside = sorted(n for n in sys.modules if n.split('.')[0] not in sys.stdlib_module_names and n not in ours)\n"
        "print(outside)\n"
    )
    result = subprocess.run([sys.executable, "-I", "-S", "-c", script, hgkit.__file__], capture_output=True,
                            text=True, check=True, env={"PATH": os.environ["PATH"]})
    assert result.stdout.strip() == "[]"
