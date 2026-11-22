"""Where what ships beside the program is. None of it is the server's code, and each is one of the repository's
top-level folders:

    console/        the pages people use the platform through
    kit/            hgkit, the helper a package's `run` may use
    packages/       the harnesses an organization starts with
    environments/   the environments it starts with
    docs/           of which PACKAGES.md, the package contract, is put beside the kit in every install

This module is the only place the server finds them, so any of them can come from somewhere else (a repository
of its own) by changing what this returns. An installed package holds a copy of each beside its modules
(pyproject.toml puts them there); a checkout uses the repository's own."""

from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


def folder(name: str) -> Path:
    """The folder of that name."""
    return next(found for found in (_PACKAGE / name, _PACKAGE.parents[1] / name) if found.is_dir())
