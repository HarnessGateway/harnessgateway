"""What an organization starts with, held to the contract: each is a package or a definition the platform itself
would take, what the builder is taught is what the platform has, and none of it leans on the platform's code."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harnessgateway import defaults, shipped
from harnessgateway.domain import sandbox
from harnessgateway.domain.events import ENDINGS, EVENTS, ITEMS, LineError, RunOutput
from harnessgateway.domain.package import read_archive
from harnessgateway.domain.resources import Environment
from harnessgateway.runner.installer import placed

HERE = shipped.folder("packages")
BUILDER = HERE / "harness-package-builder"
SKILL = BUILDER / "skills" / "building-harnessgateway-harness-package"
SHIPPED = json.loads((shipped.folder("environments") / "default.json").read_text())
CONTRACT = shipped.folder("docs") / "PACKAGES.md"
PLACED = placed()                                      # what the platform puts beside every installed package


def test_the_default_environment_is_a_valid_one_and_carries_exactly_the_packages_that_ship():
    assert Environment.load(SHIPPED).harnesses == ("harness-package-builder", "shell")
    assert sorted(SHIPPED["harnesses"]) == sorted(folder.name for folder in HERE.iterdir() if (folder / "harness.json").exists())


@pytest.mark.parametrize("name", SHIPPED["harnesses"])
def test_each_package_that_ships_is_one_the_platform_takes(name: str):
    package = read_archive(defaults.archive(name))
    assert package.manifest.name == name and package.digest == read_archive(defaults.archive(name)).digest
    assert {file.path for file in package.files if file.executable} >= {"install", "run"}


def test_what_the_builder_is_taught_is_what_the_platform_has():
    given = set(re.findall(r'"(HG_\w+)"', Path(sandbox.__file__).read_text()))        # what a sandbox is given
    told = CONTRACT.read_text() + (SKILL / "SKILL.md").read_text() + (BUILDER / "AGENTS.md").read_text()
    for variable in re.findall(r"\$(HG_\w+)", told):
        assert variable in given, variable
    for path in re.findall(r"/harness/([\w./-]+\.(?:md|py|json))", told):               # every file it is pointed at is there
        assert path in PLACED or (BUILDER / path).is_file(), path
    for kind in re.findall(r"`((?:response\.)?[a-z_]+(?:\.[a-z_]+)*)`", CONTRACT.read_text().partition("### What it prints")[2]):
        if kind.startswith("response.") or kind.endswith(("_call", "_call_output")):   # every item and event it names
            assert kind in {*ITEMS, *EVENTS, *ENDINGS, "response.created", "response.queued", "response.in_progress"}, kind
    assert f"name: {SKILL.name}\n" in (SKILL / "SKILL.md").read_text() and SKILL.name in (BUILDER / "AGENTS.md").read_text()


def verify(tmp_path: Path, *args: str, api: str = "") -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "builder"                     # the builder as installed: its files, and the contract beside them
    if not harness.exists():
        shutil.copytree(BUILDER, harness)
        for path, content in PLACED.items():
            (harness / path).parent.mkdir(exist_ok=True)
            (harness / path).write_bytes(content)
    (tmp_path / "work").mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HG_HARNESS": str(harness), "HG_WORK": str(tmp_path / "work"), "HG_CONTRACT": "1", "HG_PROVIDER_API": api}
    return subprocess.run([sys.executable, str(harness / "skills" / SKILL.name / "verify.py"), *args], env=env, capture_output=True, text=True)


def agent(tmp_path: Path, run: str, apis: str = '["anthropic"]') -> str:
    """A package for an agent that needs no model to answer: its `run` is given."""
    package = tmp_path / "work" / "greeter"
    package.mkdir(parents=True, exist_ok=True)
    (package / "harness.json").write_text('{"contract": 1, "name": "greeter", "version": "1.0-1", "apis": ' + apis + "}")
    for name, text in (("install", "#!/bin/sh\necho installed > \"$HG_HARNESS/tools.txt\"\n"), ("run", run)):
        (package / name).write_text(text)
        (package / name).chmod(0o755)
    return str(package)


GREETS = """#!/usr/bin/env python3
import json, os
request = json.load(open(os.path.join(os.environ["HG_TURN"], "request.json")))
last = os.path.join(os.environ["HG_STATE"], "last")
before = open(last).read() if os.path.exists(last) else "nothing"
installed = os.path.exists(os.path.join(os.environ["HG_HARNESS"], "tools.txt"))
print(json.dumps({"type": "response.output_text.delta", "delta": f"Hello. Before this you said: {before}. Installed: {installed}. Told: {request.get('instructions')}"}))
print(json.dumps({"type": "shell_call", "call_id": "c1", "status": "completed", "action": {"commands": ["true"]}}))
os.makedirs(os.environ["HG_STATE"], exist_ok=True)
open(last, "w").write(str(request["input"]))
print(json.dumps({"type": "response.completed", "response": {"usage": {"input_tokens": 1, "output_tokens": 1}}}))
"""


def test_a_package_is_verified_by_its_install_and_a_hello_world(tmp_path: Path):
    package = agent(tmp_path, GREETS)
    verified = verify(tmp_path, package, api="anthropic")
    assert verified.returncode == 0 and "VERIFIED: greeter 1.0-1 installs, and answered a hello world" in verified.stdout
    assert "Before this you said: nothing. Installed: True" in verified.stdout and '[shell_call] {"commands": ["true"]}' in verified.stdout
    again = verify(tmp_path, package, "--say", "And again?", api="anthropic")          # the same session: it remembers
    assert again.returncode == 0 and "Before this you said: Say hello in one short sentence." in again.stdout
    (tmp_path / "request.json").write_text(json.dumps({"input": [{"role": "user", "content": "hi"}], "instructions": "be brief"}))
    whole = verify(tmp_path, package, "--request", str(tmp_path / "request.json"), api="anthropic")     # any request at all
    assert whole.returncode == 0 and "Told: be brief" in whole.stdout and "Before this you said: And again?" in whole.stdout
    assert "Before this you said: nothing" in verify(tmp_path, package, "--new", "--say", "First?", api="anthropic").stdout
    assert "Before this you said: nothing" in verify(tmp_path, package, api="anthropic").stdout      # verifying starts over
    shape = json.loads(verify(tmp_path, "--shape", "shell_call_output").stdout)
    assert shape["required"] == ["call_id", "output", "status", "type"] and "is not a line a run prints" in verify(tmp_path, "--shape", "nope").stderr


def test_a_package_whose_api_the_session_does_not_speak_is_verified_as_far_as_its_install(tmp_path: Path):
    said = verify(tmp_path, agent(tmp_path, GREETS, '["openai-responses"]'), api="anthropic")
    assert said.returncode == 2 and "INSTALL VERIFIED ONLY" in said.stdout and "must be run after it is added" in said.stdout


def test_what_the_platform_would_refuse_is_not_verified_and_says_why(tmp_path: Path):
    printed = ["not json", {"type": "response.created"}, {"type": "hg.done"}, {"type": "shell_call", "call_id": "c"},
               {"type": "response.output_text.delta", "item_id": "nobody", "delta": "x"},
               {"type": "message", "role": "assistant", "status": "finished", "content": []},
               {"type": "response.failed", "response": {"error": {"code": "no_auth"}}}, {"type": "response.completed"}]
    script = "#!/bin/sh\ncat <<'LINES'\n" + "\n".join(line if isinstance(line, str) else json.dumps(line) for line in printed) + "\nLINES\nexit 2\n"
    bad = verify(tmp_path, agent(tmp_path, script), api="anthropic")
    assert bad.returncode == 1 and "NOT VERIFIED" in bad.stdout
    for problem in ("line 1 is not a JSON object with a type", "line 2 is the platform's to print", "line 3: 'hg.done' is neither an output item",
                    "line 4, shell_call: action is required", "line 5, response.output_text.delta: it names an item that has not been added",
                    'line 6, message: status is one of "in_progress", "completed", "incomplete"',
                    "line 7, response.failed: response.error.message is required", "exited with code 2"):
        assert problem in bad.stdout, problem
    assert "line 8" not in bad.stdout                                              # a line that fits is not a problem
    silent = verify(tmp_path, agent(tmp_path, GREETS.replace("response.output_text.delta", "response.reasoning_summary_text.delta")), api="anthropic")
    assert silent.returncode == 1 and "did not finish with an answer" in silent.stdout
    quiet = verify(tmp_path, agent(tmp_path, "#!/bin/sh\ntrue\n"), api="anthropic")
    assert "did not print the stream's last event" in quiet.stdout
    broken = agent(tmp_path, GREETS)
    Path(broken, "install").write_text("#!/bin/sh\nexit 7\n")
    assert "NOT VERIFIED: greeter 1.0-1 does not install" in verify(tmp_path, broken, api="anthropic").stdout
    stopped = verify(tmp_path, str(HERE / "shell"), "--say", "echo begun; sleep 30", "--stop-after", "1")     # the command is stopped too
    assert stopped.returncode == 0 and "said: begun" in stopped.stdout and "stopped after 1 seconds" in stopped.stdout


def test_the_schema_a_package_is_given_says_what_the_platform_reads(tmp_path: Path):
    """The two are written apart, the schema for any language and the reader for the platform: a line is taken by
    both, or by neither."""
    fits = ['{"type": "response.completed"}', *re.findall(r"^\{.*\}$", CONTRACT.read_text(), re.M),
            '{"type": "response.output_text.delta", "delta": "hi"}', '{"type": "mcp_call", "server_label": "s", "name": "t", "arguments": "{}"}',
            '{"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"}']
    never = ['{"type": "shell_call", "call_id": "c"}', '{"type": "response.incomplete", "response": {"incomplete_details": {"reason": "tired"}}}',
             '{"type": "message", "role": "user", "status": "completed", "content": []}', '{"type": "response.failed"}']
    script = "#!/bin/sh\ncat <<'LINES'\n" + "\n".join([*never, *fits]) + "\nLINES\n"
    said = verify(tmp_path, agent(tmp_path, script), "--say", "x").stdout
    assert [f"line {number}," in said for number in range(1, len(never) + len(fits) + 1)] == [True] * len(never) + [False] * len(fits), said
    for line in never:
        with pytest.raises(LineError):
            RunOutput().read(line)
    for line in fits:
        RunOutput().read(line)


def test_what_ships_stands_apart_from_the_platform():
    """The packages and the environments know the platform only by its contract, so they can live in a
    repository of their own; and the platform's code finds them in one place only."""
    for folder in ("packages", "environments"):
        for file in shipped.folder(folder).rglob("*"):
            if file.is_file() and file.suffix != ".pyc":
                assert not re.search(r"^\s*(import|from) harnessgateway\b", file.read_text(errors="replace"), re.M), file
    source = Path(shipped.__file__).parent
    reach = sorted(module.relative_to(source).as_posix() for module in source.rglob("*.py") if "shipped.folder(" in module.read_text())
    assert reach == ["api/console.py", "defaults.py", "runner/installer.py"]       # who serves, gives and installs them
