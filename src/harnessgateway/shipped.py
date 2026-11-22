"""Where what ships beside the program is: the console's files, the packages and the environments an organization
starts with. Each is one of the repository's top-level folders. An installed package holds a copy of each beside
its modules (pyproject.toml puts them there); a checkout uses the repository's own."""

from pathlib import Path

_PACKAGE = Path(__file__).resolve().parent


def folder(name: str) -> Path:
    """The folder of that name: `console`, `packages` or `environments`."""
    return next(found for found in (_PACKAGE / name, _PACKAGE.parents[1] / name) if found.is_dir())
