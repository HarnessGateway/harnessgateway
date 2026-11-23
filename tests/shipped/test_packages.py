"""What an organization starts with, held to the contract: each is a package or a definition the platform itself
would take, what HG FDE is taught is what the platform has, and none of it leans on the platform's code."""

import json
import os
import re
import sys
from pathlib import Path
from typing import get_args

import pytest

from harnessgateway import defaults, shipped
from harnessgateway.domain import sandbox
from harnessgateway.domain.contract import ApiName
from harnessgateway.domain.events import ENDINGS, EVENTS, ITEMS, RunOutput
from harnessgateway.domain.package import read_archive
from harnessgateway.domain.resources import Environment

HERE = shipped.folder("packages")
FDE = HERE / "hg-fde"
SKILL = FDE / "skills" / "building-harness-packages"
SHIPPED = json.loads((shipped.folder("environments") / "default.json").read_text())
GIVEN = shipped.folder("contract")                     # what every sandbox is given at $HG_CONTRACT
CONTRACT = GIVEN / "PACKAGES.md"


def test_the_default_environment_is_a_valid_one_and_carries_exactly_the_packages_that_ship():
    assert Environment.load(SHIPPED).harnesses == ("hg-fde", "shell")
    assert sorted(SHIPPED["harnesses"]) == sorted(folder.name for folder in HERE.iterdir() if (folder / "run").exists())


@pytest.mark.parametrize("name", SHIPPED["harnesses"])
def test_each_package_that_ships_is_one_the_platform_takes(name: str):
    package = read_archive(defaults.archive(name))
    assert package.digest == read_archive(defaults.archive(name)).digest
    assert {file.path for file in package.files if file.executable} >= {"install", "run"}
    assert "harness.json" not in {file.path for file in package.files}        # a package declares nothing


def test_what_hg_fde_is_taught_is_what_the_platform_has():
    given = set(re.findall(r'"(HG_\w+)"', Path(sandbox.__file__).read_text()))        # what a sandbox is given,
    given |= {sandbox.provider_variable(api) for api in get_args(ApiName)}             # with a provider's base URLs
    told = CONTRACT.read_text() + (SKILL / "SKILL.md").read_text() + (FDE / "AGENTS.md").read_text()
    for variable in re.findall(r"\$(HG_\w+)", told):
        assert variable in given, variable
    for path in re.findall(r"/harness/([\w./-]+\.(?:md|py|json))", told):               # every file it is pointed at is there
        assert (FDE / path).is_file(), path
    for name in re.findall(r"`([a-z]+\.schema\.json)`", told):                          # and every schema it is told of
        assert (GIVEN / name).is_file(), name
    for kind in re.findall(r"`((?:response\.)?[a-z_]+(?:\.[a-z_]+)*)`", CONTRACT.read_text().partition("### What it prints")[2]):
        if kind.startswith("response.") or kind.endswith(("_call", "_call_output")):   # every item and event it names
            assert kind in {*ITEMS, *EVENTS, *ENDINGS, "response.created", "response.queued", "response.in_progress"}, kind
    assert f"name: {SKILL.name}\n" in (SKILL / "SKILL.md").read_text() and SKILL.name in (FDE / "AGENTS.md").read_text()


def load_run(tmp_path: Path, **turn_files: str):
    """HG FDE's `run`, as a module: with the variables a sandbox gives it, and a turn's inputs in place."""
    import importlib.machinery
    import importlib.util
    (tmp_path / "turn").mkdir(exist_ok=True)
    for name, text in {"previous.json": "null", "handover.txt": "", **turn_files}.items():
        (tmp_path / "turn" / name).write_text(text)
    os.environ.update(HG_HARNESS=str(FDE), HG_TURN=str(tmp_path / "turn"), HG_CONTRACT=str(GIVEN), HG_STATE=str(tmp_path / "state"))
    loader = importlib.machinery.SourceFileLoader("hg_fde_run", str(FDE / "run"))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("hg_fde_run", loader))
    loader.exec_module(module)
    return module


def test_hg_fde_is_told_who_it_is_the_contract_and_every_schema_in_full_each_turn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)          # loading it leaves nothing in the package's folder
    run = load_run(tmp_path)
    known = Path(run.knows({"instructions": "Answer in French."})).read_text()
    assert known.startswith("# HG FDE") and "# Harness packages" in known and known.endswith("Answer in French.")
    for name in ("request", "response", "stream", "lines"):
        whole = json.loads((GIVEN / f"{name}.schema.json").read_text())
        assert json.dumps(whole, separators=(",", ":")) in known, name            # whole: nothing of it left out
    args = run.command({"instructions": "x"}, "sess-1")
    assert args[args.index("--system-prompt-snapshot") + 1] == "off" and args[-2:] == ["--resume", "sess-1"]
    assert "--append-system-prompt" not in args                    # too long for an argument: it is given as a file
    assert run.asked({"input": "Carry on."}) == ([{"type": "text", "text": "Carry on."}], [])
    cut = load_run(tmp_path, **{"previous.json": '{"status": "cancelled"}', "handover.txt": "Handover from shell."})
    assert [block["text"] for block in cut.asked({"input": "Carry on."})[0]] == [
        "Your last turn did not finish: it ended as cancelled.", "Handover from shell.", "Carry on."]


def test_hg_fde_gives_its_cli_every_field_of_the_request_the_cli_has_a_flag_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                                capsys: pytest.CaptureFixture[str]):
    """It is the example every package it builds is written from: what it does with a request is what they will."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    (tmp_path / "turn" / "files" / "file_1").mkdir(parents=True)
    (tmp_path / "turn" / "files" / "file_1" / "shot.png").write_bytes(b"png")
    (tmp_path / "turn" / "files" / "file_2").mkdir()
    (tmp_path / "turn" / "files" / "file_2" / "notes.txt").write_text("notes")
    run = load_run(tmp_path)
    monkeypatch.setattr(run, "GIVEN", str(tmp_path / "given"))
    blocks, told = run.asked({"input": [
        {"role": "developer", "content": "Be brief."}, {"role": "assistant", "content": [{"type": "output_text", "text": "Hello."}]},
        {"role": "user", "content": [
            {"type": "input_text", "text": "Look."}, {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
            {"type": "input_image", "image_url": "https://example.com/a.png"}, {"type": "input_image", "file_id": "file_1"},
            {"type": "input_file", "file_id": "file_2"}, {"type": "input_file", "filename": "../word.txt", "file_data": "data:text/plain;base64,SEVST04="},
            {"type": "input_file", "file_url": "https://example.com/a.pdf"}, {"type": "input_file", "file_id": "file_gone"},
            {"type": "input_image", "file_id": "file_2"}]},      # an upload that is no image by its name
        {"type": "function_call_output", "call_id": "c", "output": "x"}]})
    assert told == ["Be brief."] and (tmp_path / "given" / "word.txt").read_text() == "HERON"      # a file's name is its last part
    assert blocks == [
        {"type": "text", "text": "Earlier, you said: Hello."}, {"type": "text", "text": "Look."},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "image", "source": {"type": "url", "url": "https://example.com/a.png"}},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "cG5n"}},
        {"type": "text", "text": f"A file given with this request: {tmp_path}/turn/files/file_2/notes.txt"},
        {"type": "text", "text": f"A file given with this request: {tmp_path}/given/word.txt"},
        {"type": "text", "text": "A file given with this request: https://example.com/a.pdf"}]
    assert Path(run.knows({"instructions": "Answer in French."}, told)).read_text().endswith("Answer in French.\n\nBe brief.")
    schema = {"type": "object", "properties": {"word": {"type": "string"}}}
    args = run.command({"reasoning": {"effort": "minimal"}, "text": {"format": {"type": "json_schema", "name": "n", "schema": schema}}, "tools": [
        {"type": "mcp", "server_label": "docs", "server_url": "https://mcp.example.com/mcp", "authorization": "hgs_1", "headers": {"X-Key": "hgs_2"}},
        {"type": "function", "name": "f", "parameters": {}}]}, None)
    assert args[args.index("--effort") + 1] == "low" and json.loads(args[args.index("--json-schema") + 1]) == schema
    assert json.loads(args[args.index("--mcp-config") + 1]) == {"mcpServers": {"docs": {
        "type": "http", "url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer hgs_1", "X-Key": "hgs_2"}}}}
    alone = run.command({"reasoning": {"effort": "none"}, "tool_choice": "none", "tools": [{"type": "mcp", "server_label": "d", "server_url": "https://x"}]}, None)
    assert "--effort" not in alone and "--mcp-config" not in alone and alone[alone.index("--tools") + 1] == ""
    assert args[args.index("--input-format") + 1] == "stream-json"            # how an image reaches it
    said = capsys.readouterr().err                                              # and what it has no use for, it names
    assert "a part of type input_file" in said and "a part of type input_image" in said and "an input item of type function_call_output" in said and "a tool of type function" in said


def test_what_hg_fde_prints_is_what_the_platform_takes_each_thing_as_the_item_that_says_what_it_was(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    run, steps = load_run(tmp_path), {}

    def call(name: str, given: dict, text: object, detail: object = None, failed: bool = False) -> None:
        call_id = f"call_{len(steps)}"
        steps[call_id] = begun = run.called(call_id, name, given)
        if begun:
            run.say({"type": "response.output_item.added", "item": {**begun, "status": "in_progress"}})
        run.returned(call_id, name, given, begun, {"content": text, "is_error": failed}, detail)

    run.servers({"mcp_servers": [{"name": "docs", "status": "connected"}, {"name": "gone", "status": "failed"}],
                 "tools": ["Bash", "mcp__docs__lookup"]})
    call("Bash", {"command": "ls", "timeout": 5000}, "a.txt", {"stdout": "a.txt", "stderr": "", "interrupted": False})
    call("Bash", {"command": "false"}, "Exit code 3\nbad", "Error: Exit code 3\nbad", failed=True)
    call("Bash", {"command": "sleep 99"}, "", {"stdout": "", "stderr": "", "interrupted": True})
    call("Write", {"file_path": "/work/a.txt", "content": "one\ntwo\n"}, "created", {"type": "create", "structuredPatch": []})
    patch = [{"oldStart": 1, "oldLines": 2, "newStart": 1, "newLines": 2, "lines": [" one", "-two", "+TWO"]}]
    call("Edit", {"file_path": "/work/a.txt", "old_string": "two", "new_string": "TWO"}, "updated", {"structuredPatch": patch})
    call("Edit", {"file_path": "/work/b.txt", "old_string": "x", "new_string": "y"}, "no such file", "Error: no such file", failed=True)
    call("mcp__docs__lookup", {"key": "bird"}, [{"type": "text", "text": "OSPREY"}], [{"type": "text", "text": "OSPREY"}])
    call("mcp__docs__broken", {}, "it broke", "Error: it broke", failed=True)
    call("WebSearch", {"query": "osprey"}, "Links: …", {"query": "osprey", "results": [{"content": [{"title": "t", "url": "https://example.com/o"}]}, "words"]})
    call("WebFetch", {"url": "https://example.com", "prompt": "title?"}, "Example Domain", {"url": "https://example.com", "code": 200})
    call("Read", {"file_path": "/work/a.txt"}, "1\tone", {"type": "text"})
    run.say(run.ending({"subtype": "success", "structured_output": {"word": "OSPREY"}, "usage": {
        "input_tokens": 10, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2, "output_tokens": 7,
        "output_tokens_details": {"thinking_tokens": 3}}}, schema=True))
    output = RunOutput()
    for line in capsys.readouterr().out.splitlines():
        output.read(line)                                              # each is a line the platform takes
    items = output.output()
    assert [item["type"] for item in items] == [
        "mcp_list_tools", "mcp_list_tools", *["shell_call", "shell_call_output"] * 3, *["apply_patch_call", "apply_patch_call_output"] * 3,
        "mcp_call", "mcp_call", "web_search_call", "web_search_call", "custom_tool_call", "custom_tool_call_output", "message"]
    lists, gone, ran, _, _, failed, _, slept = items[:8]
    assert lists["tools"] == [{"name": "lookup", "input_schema": {}}] and gone["error"] == "the server is failed"
    assert (ran["call_id"], ran["action"]) == ("call_0", {"commands": ["ls"], "timeout_ms": 5000})
    assert failed["output"] == [{"stdout": "bad", "stderr": "", "outcome": {"type": "exit", "exit_code": 3}}]      # its own exit code
    assert slept["output"][0]["outcome"] == {"type": "timeout"}
    made, _, edited, _, refused, said = items[8:14]
    assert made["operation"] == {"type": "create_file", "path": "/work/a.txt", "diff": "+one\n+two\n"}
    assert edited["operation"] == {"type": "update_file", "path": "/work/a.txt", "diff": "@@ -1,2 +1,2 @@\n one\n-two\n+TWO\n"}
    assert refused["operation"]["type"] == "update_file" and said == {**said, "status": "failed", "output": "no such file"}
    found, broke, searched, opened = items[14:18]
    assert (found["id"], found["server_label"], found["name"], found["status"], found["output"]) == ("call_6", "docs", "lookup", "completed", "OSPREY")
    assert (broke["status"], broke["error"]) == ("failed", {"type": "mcp_tool_execution_error", "content": "it broke"})
    assert searched["action"] == {"type": "search", "query": "osprey", "sources": [{"type": "url", "url": "https://example.com/o"}]}
    assert (opened["action"], opened["status"]) == ({"type": "open_page", "url": "https://example.com"}, "completed")
    assert items[-1]["content"][0]["text"] == '{"word": "OSPREY"}'             # with a schema, the answer is what fits it
    assert output.terminal.usage == {"input_tokens": 17, "output_tokens": 7, "total_tokens": 24,
                                     "input_tokens_details": {"cached_tokens": 5, "cache_write_tokens": 2},
                                     "output_tokens_details": {"reasoning_tokens": 3}}
    failed_turn = run.ending({"subtype": "error_during_execution", "is_error": True, "result": "it stopped"})
    assert RunOutput().read(json.dumps(failed_turn)) == [] and failed_turn["response"]["error"]["code"] == "error_during_execution"


def test_hg_fde_says_so_when_its_provider_does_not_speak_its_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]):
    """A package declares nothing, so the platform cannot refuse a provider for it: its `run` does, in words."""
    monkeypatch.setattr(os, "environ", {**os.environ, "HG_PROVIDER_OPENAI_CHAT": "https://api.example.com/v1"})
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    os.environ.pop("HG_PROVIDER_ANTHROPIC", None)
    load_run(tmp_path).main()
    said = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["type"] for line in said] == ["response.failed"] and "Anthropic API" in said[0]["response"]["error"]["message"]
    assert RunOutput().read(json.dumps(said[0])) == []                         # and it is a line the platform takes


def test_what_ships_stands_apart_from_the_platform():
    """The packages and the environments know the platform only by its contract, so they can live in a
    repository of their own; and the platform's code finds them in one place only."""
    for folder in ("packages", "environments"):
        for file in shipped.folder(folder).rglob("*"):
            if file.is_file() and file.suffix != ".pyc":
                assert not re.search(r"^\s*(import|from) harnessgateway\b", file.read_text(errors="replace"), re.M), file
    source = Path(shipped.__file__).parent
    reach = sorted(module.relative_to(source).as_posix() for module in source.rglob("*.py") if "shipped.folder(" in module.read_text())
    assert reach == ["api/console.py", "cli.py", "defaults.py", "serve.py"]        # who serves the console, writes the schemas, gives the defaults, and gives every sandbox the contract
