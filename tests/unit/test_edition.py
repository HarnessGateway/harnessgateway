"""What an extension may import: `harnessgateway.edition`, and `harnessgateway.testing` for its tests."""

import ast
import importlib
from pathlib import Path

import harnessgateway
from harnessgateway import edition

SOURCE = Path(harnessgateway.__file__).parent
EXTENSIONS = sorted((Path(__file__).parent.parent / "integration").glob("*.py"))


def imports(path: Path) -> set[str]:
    """Every module of the core a file imports."""
    tree = ast.parse(path.read_text())
    found = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module}
    found |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    return {name for name in found if name == "harnessgateway" or name.startswith("harnessgateway.")}


def test_everything_the_surface_names_is_there_and_everything_it_holds_is_named():
    assert len(set(edition.__all__)) == len(edition.__all__)
    for name in edition.__all__:
        assert getattr(edition, name) is not None, name
    held = {name for name, value in vars(edition).items() if not name.startswith("_") and not hasattr(value, "__path__")}
    assert held <= set(edition.__all__), "what it imports and does not name"


def test_the_sample_extensions_import_the_core_only_through_the_surface():
    """They are how an edition is written: if they need more of the core than this, the surface is what is missing."""
    for name in ("edition.py", "second.py"):
        path = next(found for found in EXTENSIONS if found.name == name)
        assert imports(path) <= {"harnessgateway.edition", "harnessgateway.testing"}, path.name


def test_nothing_in_the_core_imports_the_surface_or_the_test_kit():
    for path in SOURCE.rglob("*.py"):
        if path.name not in ("edition.py", "testing.py"):
            assert not imports(path) & {"harnessgateway.edition", "harnessgateway.testing"}, path.relative_to(SOURCE)
    assert importlib.import_module("harnessgateway.testing").FakeDriver.name == "fake"
