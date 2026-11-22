"""What an organization starts with, held to the contract: each is a package or a definition the platform itself
would take, what the builder is taught is what the platform has, and none of it leans on the platform's code."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import hgkit
import pytest

from harnessgateway import defaults, shipped
from harnessgateway.domain import sandbox
from harnessgateway.domain.package import read_archive
from harnessgateway.domain.resources import Environment

HERE = shipped.folder("packages")
BUILDER = HERE / "harness-package-builder"
SKILL = BUILDER / "skills" / "building-harnessgateway-harness-package"
SHIPPED = json.loads((shipped.folder("environments") / "default.json").read_text())


def test_the_default_environment_is_a_valid_one_and_carries_exactly_the_packages_that_ship():
    assert Environment.load(SHIPPED).harnesses == ("harness-package-builder", "shell")
    assert sorted(SHIPPED["harnesses"]) == sorted(folder.name for folder in HERE.iterdir() if (folder / "harness.json").exists())


@pytest.mark.parametrize("name", SHIPPED["harnesses"])
def test_each_package_that_ships_is_one_the_platform_takes(name: str):
    package = read_archive(defaults.archive(name))
    assert package.manifest.name == name and package.digest == read_archive(defaults.archive(name)).digest
    assert {file.path for file in package.files if file.executable} >= {"install", "run"}


def test_the_skill_names_only_what_the_kit_and_the_contract_have(tmp_path: Path):
    for name, content in (("request.json", "{}"), ("history.json", "[]"), ("handover.txt", "")):
        (tmp_path / name).write_text(content)
    env = {"HG_TURN": str(tmp_path), "HG_STATE": str(tmp_path)}
    kit = {"Request": hgkit.Request(env), "State": hgkit.State(env), "Emitter": hgkit.Emitter()}
    contract = (shipped.folder("docs") / "PACKAGES.md").read_text()
    rows = {row.split("|")[1].strip(" `"): row.split("|")[2] for row in contract.splitlines() if row.startswith("| `")}
    for kind, made in kit.items():
        named = re.findall(r"`(\w+)", rows[kind])
        assert named and all(hasattr(made, member) for member in named), kind
    given = set(re.findall(r'"(HG_\w+)"', Path(sandbox.__file__).read_text()))        # what a sandbox is given
    told = contract + (SKILL / "SKILL.md").read_text() + (BUILDER / "AGENTS.md").read_text()
    for variable in re.findall(r"\$(HG_\w+)", told):
        assert variable in given, variable
    for path in re.findall(r"/harness/([\w./-]+\.(?:md|py|json))", told):               # every file it is pointed at is there
        assert path.startswith("kit/") or (BUILDER / path).is_file(), path
    assert f"name: {SKILL.name}\n" in (SKILL / "SKILL.md").read_text() and SKILL.name in (BUILDER / "AGENTS.md").read_text()


def verify(tmp_path: Path, *args: str, api: str = "") -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "builder"                     # the builder as installed: its files, and the kit beside them
    if not harness.exists():
        shutil.copytree(BUILDER, harness)
        shutil.copytree(shipped.folder("kit"), harness / "kit")
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
import os, sys
sys.path.insert(0, os.path.join(os.environ["HG_HARNESS"], "kit"))
import hgkit
request, emit, state = hgkit.Request(), hgkit.Emitter(), hgkit.State()
emit.say(f"Hello. Before this you said: {state.get('last') or 'nothing'}. Installed: {os.path.exists(os.path.join(os.environ['HG_HARNESS'], 'tools.txt'))}")
state.set("last", request.prompt)
emit.done({"input_tokens": 1, "output_tokens": 1})
"""


def test_a_package_is_verified_by_its_install_and_a_hello_world_through_the_kit(tmp_path: Path):
    package = agent(tmp_path, GREETS)
    verified = verify(tmp_path, package, api="anthropic")
    assert verified.returncode == 0 and "VERIFIED: greeter 1.0-1 installs, and answered a hello world" in verified.stdout
    assert "Before this you said: nothing. Installed: True" in verified.stdout
    again = verify(tmp_path, package, "--say", "And again?", api="anthropic")          # the same session: it remembers
    assert again.returncode == 0 and "Before this you said: Say hello in one short sentence." in again.stdout
    assert "Before this you said: nothing" in verify(tmp_path, package, api="anthropic").stdout      # verifying starts over


def test_a_package_whose_api_the_session_does_not_speak_is_verified_as_far_as_its_install(tmp_path: Path):
    said = verify(tmp_path, agent(tmp_path, GREETS, '["openai-responses"]'), api="anthropic")
    assert said.returncode == 2 and "INSTALL VERIFIED ONLY" in said.stdout and "must be run after it is added" in said.stdout


def test_what_the_platform_would_refuse_is_not_verified_and_says_why(tmp_path: Path):
    bad = verify(tmp_path, agent(tmp_path, "#!/bin/sh\necho not json\necho '{\"type\": \"response.completed\"}'\nexit 2\n"), api="anthropic")
    assert bad.returncode == 1 and "NOT VERIFIED" in bad.stdout
    for problem in ("line 1 is not a Responses event", "the platform's to print", "exited with code 2", "no terminal line"):
        assert problem in bad.stdout, problem
    silent = verify(tmp_path, agent(tmp_path, GREETS.replace("emit.say", "emit.log")), api="anthropic")
    assert silent.returncode == 1 and "did not finish with an answer" in silent.stdout
    broken = agent(tmp_path, GREETS)
    Path(broken, "install").write_text("#!/bin/sh\nexit 7\n")
    assert "NOT VERIFIED: greeter 1.0-1 does not install" in verify(tmp_path, broken, api="anthropic").stdout
    stopped = verify(tmp_path, str(HERE / "shell"), "--say", "sleep 30", "--stop-after", "1")
    assert stopped.returncode == 0 and "stopped, with no terminal line" in stopped.stdout


def test_what_ships_stands_apart_from_the_platform():
    """The kit, the packages and the environments know the platform only by its contract, so they can live in a
    repository of their own; and the platform's code finds them in one place only."""
    for folder in ("kit", "packages", "environments"):
        for file in shipped.folder(folder).rglob("*"):
            if file.is_file() and file.suffix != ".pyc":
                assert not re.search(r"^\s*(import|from) harnessgateway\b", file.read_text(errors="replace"), re.M), file
    source = Path(shipped.__file__).parent
    reach = sorted(module.relative_to(source).as_posix() for module in source.rglob("*.py") if "shipped.folder(" in module.read_text())
    assert reach == ["api/console.py", "defaults.py", "runner/installer.py"]       # who serves, gives and installs them
