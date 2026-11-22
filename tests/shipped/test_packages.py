"""What an organization starts with, held to the contract: each is a package or a definition the platform itself
would take, what HG FDE is taught is what the platform has, and none of it leans on the platform's code."""

import json
import os
import re
from pathlib import Path

import pytest

from harnessgateway import defaults, shipped
from harnessgateway.domain import sandbox
from harnessgateway.domain.events import ENDINGS, EVENTS, ITEMS
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
    assert sorted(SHIPPED["harnesses"]) == sorted(folder.name for folder in HERE.iterdir() if (folder / "harness.json").exists())


@pytest.mark.parametrize("name", SHIPPED["harnesses"])
def test_each_package_that_ships_is_one_the_platform_takes(name: str):
    package = read_archive(defaults.archive(name))
    assert package.manifest.name == name and package.digest == read_archive(defaults.archive(name)).digest
    assert {file.path for file in package.files if file.executable} >= {"install", "run"}


def test_what_hg_fde_is_taught_is_what_the_platform_has():
    given = set(re.findall(r'"(HG_\w+)"', Path(sandbox.__file__).read_text()))        # what a sandbox is given
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
    run = load_run(tmp_path)
    known = Path(run.knows({"instructions": "Answer in French."})).read_text()
    assert known.startswith("# HG FDE") and "# Harness packages" in known and known.endswith("Answer in French.")
    for name in ("request", "response", "stream", "lines"):
        whole = json.loads((GIVEN / f"{name}.schema.json").read_text())
        assert json.dumps(whole, separators=(",", ":")) in known, name            # whole: nothing of it left out
    args = run.command({"instructions": "x"}, "sess-1")
    assert args[args.index("--system-prompt-snapshot") + 1] == "off" and args[-2:] == ["--resume", "sess-1"]
    assert "--append-system-prompt" not in args                    # too long for an argument: it is given as a file
    assert run.asked({"input": "Carry on."}) == "Carry on."
    cut = load_run(tmp_path, **{"previous.json": '{"status": "cancelled"}', "handover.txt": "Handover from shell."})
    assert cut.asked({"input": "Carry on."}) == "Your last turn did not finish: it ended as cancelled.\n\nHandover from shell.\n\nCarry on."


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
