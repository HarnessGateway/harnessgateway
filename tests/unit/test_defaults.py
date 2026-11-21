"""What an organization starts with: each is a package or a definition the platform itself would take, and what
the builder is taught is what the platform has."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import harnessgateway
from harnessgateway import defaults
from harnessgateway.domain import sandbox
from harnessgateway.domain.package import read_archive
from harnessgateway.domain.resources import Environment
from harnessgateway.kit import hgkit

HERE = Path(defaults.__file__).parent
BUILDER = HERE / "harness-package-builder"
SHIPPED = json.loads((HERE / "environment.json").read_text())


def test_the_default_environment_is_a_valid_one_and_carries_exactly_the_packages_that_ship():
    assert Environment.load(SHIPPED).harnesses == ("harness-package-builder", "shell")
    assert sorted(SHIPPED["harnesses"]) == sorted(folder.name for folder in HERE.iterdir() if (folder / "harness.json").exists())


@pytest.mark.parametrize("name", SHIPPED["harnesses"])
def test_each_package_that_ships_is_one_the_platform_takes(name: str):
    package = read_archive(defaults.archive(name))
    assert package.manifest.name == name and package.digest == read_archive(defaults.archive(name)).digest
    assert {file.path for file in package.files if file.executable} >= {"install", "run"}


def test_the_guide_names_only_what_the_kit_and_the_contract_have(tmp_path: Path):
    for name, content in (("request.json", "{}"), ("history.json", "[]"), ("handover.txt", "")):
        (tmp_path / name).write_text(content)
    env = {"HG_TURN": str(tmp_path), "HG_STATE": str(tmp_path)}
    kit = {"Request": hgkit.Request(env), "State": hgkit.State(env), "Emitter": hgkit.Emitter()}
    guide = (BUILDER / "guide" / "PACKAGES.md").read_text()
    rows = {row.split("|")[1].strip(" `"): row.split("|")[2] for row in guide.splitlines() if row.startswith("| `")}
    for kind, made in kit.items():
        named = re.findall(r"`(\w+)", rows[kind])
        assert named and all(hasattr(made, member) for member in named), kind
    given = set(re.findall(r'"(HG_\w+)"', Path(sandbox.__file__).read_text()))        # what a sandbox is given
    for variable in re.findall(r"\$(HG_\w+)", guide + (BUILDER / "guide" / "BUILDER.md").read_text()):
        assert variable in given, variable


def trial(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    harness = tmp_path / "builder"                     # the builder as installed: its files, and the kit beside them
    if not harness.exists():
        shutil.copytree(BUILDER, harness)
        shutil.copytree(Path(harnessgateway.__file__).parent / "kit", harness / "kit")
    (tmp_path / "work").mkdir(exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HG_HARNESS": str(harness), "HG_WORK": str(tmp_path / "work"), "HG_CONTRACT": "1"}
    return subprocess.run([sys.executable, str(harness / "guide" / "trial.py"), *args], env=env, capture_output=True, text=True)


def test_the_trial_runs_a_package_as_the_platform_would_and_says_whether_its_lines_are_taken(tmp_path: Path):
    shell = str(HERE / "shell")
    installed = trial(tmp_path, "install", shell)
    assert installed.returncode == 0 and "it is ready" in installed.stdout
    first = trial(tmp_path, "turn", shell, "echo kept > note.txt; echo hello")
    assert first.returncode == 0 and "said: hello" in first.stdout and '"hg.done"' in first.stdout
    again = trial(tmp_path, "turn", shell, "cat note.txt")                 # the same session: its work is still there
    assert "said: kept" in again.stdout
    assert "No such file" in trial(tmp_path, "turn", shell, "cat note.txt", "--fresh").stdout
    failed = trial(tmp_path, "turn", shell, "exit 3")
    assert failed.returncode == 0 and '"hg.failed"' in failed.stdout       # a failed turn is still a well-formed one
    stopped = trial(tmp_path, "turn", shell, "sleep 30", "--stop-after", "1")
    assert stopped.returncode == 0 and "stopped, with no terminal line" in stopped.stdout


def test_the_trial_says_what_is_wrong_with_a_run_the_platform_would_refuse(tmp_path: Path):
    package = tmp_path / "bad"
    shutil.copytree(HERE / "shell", package)
    (package / "run").write_text("#!/bin/sh\necho not json\necho '{\"type\": \"response.completed\"}'\nexit 2\n")
    said = trial(tmp_path, "turn", str(package), "anything")
    assert said.returncode == 1 and "PROBLEMS" in said.stdout
    for problem in ("line 1 is not a Responses event", "the platform's to print", "exited with code 2", "no terminal line"):
        assert problem in said.stdout, problem
