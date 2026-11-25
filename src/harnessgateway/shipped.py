"""Where what ships beside the program is. None of it is the server's code, and each is one of the repository's
top-level folders:

    console/        the pages people use the platform through
    packages/       the harnesses that ship with the platform
    environments/   the environments an organization starts with

This module is the only place the server finds them, so any of them can come from somewhere else (a repository
of its own) by changing what this returns. An installed package holds a copy of each beside its modules
(pyproject.toml puts them there); a checkout uses the repository's own."""

from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


def folder(name: str) -> Path:
    """The folder of that name."""
    return next(found for found in (_PACKAGE / name, _PACKAGE.parents[1] / name) if found.is_dir())


def contract() -> Path:
    """The schema every package is written to: the Responses API, as files generated from the pinned SDK. Building
    a package is the one thing done from them, so they are carried by the package that builds packages."""
    return folder("packages") / "harness-package-builder" / "contract"
