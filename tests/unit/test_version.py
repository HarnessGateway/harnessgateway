"""Spec §18.6: every change that reaches `main` is a version. The version is written in three places, which
agree, and the changelog's first entry is for it."""

import re
from pathlib import Path

import harnessgateway

ROOT = Path(__file__).parents[2]


def test_the_version_is_the_same_wherever_it_is_written() -> None:
    project = re.search(r'^version = "(.+)"$', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    installer = re.search(r"^VERSION=(.+)$", (ROOT / "deploy" / "install.sh").read_text(), re.MULTILINE)
    assert project is not None
    assert installer is not None
    assert re.fullmatch(r"\d+\.\d+\.\d+", harnessgateway.__version__)
    assert (project.group(1), installer.group(1)) == (harnessgateway.__version__, harnessgateway.__version__)


def test_the_changelog_s_first_entry_is_for_this_version_and_says_something() -> None:
    entries = re.split(r"^## ", (ROOT / "CHANGELOG.md").read_text(), flags=re.MULTILINE)[1:]
    heading, _, body = entries[0].partition("\n")
    assert heading == harnessgateway.__version__
    assert body.strip()
    assert len({entry.partition("\n")[0] for entry in entries}) == len(entries)       # a version is entered once
