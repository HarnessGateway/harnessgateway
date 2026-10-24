"""Layer 3: the packages the platform ships, read-only templates an organization forks into a harness
of its own. A directory of folders, each a valid package with a `catalog.json` beside it saying what
it is and how it was verified; the platform never runs them as themselves."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import harness
from .errors import NotFound
from .store import Store, check_name

DEFAULT = Path(__file__).with_name("catalog")       # inside the wheel; a checkout has it at the repository root
if not DEFAULT.is_dir():
    DEFAULT = Path(__file__).resolve().parents[1] / "catalog"


class Catalog:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("HG_CATALOG_DIR") or DEFAULT)

    def names(self) -> list[str]:
        return sorted(p.name for p in self.root.iterdir() if (p / "harness.json").is_file()) if self.root.is_dir() else []

    def path(self, name: str) -> Path:
        check_name(name, "catalog package")
        p = self.root / name
        if not (p / "harness.json").is_file():
            raise NotFound(f"no catalog package {name}")
        return p

    def info(self, name: str) -> dict[str, Any]:
        p = self.path(name)
        config = harness.load_config((p / "harness.json").read_text())
        notes = json.loads((p / "catalog.json").read_text()) if (p / "catalog.json").is_file() else {}
        return {"name": name, "title": notes.get("title", name), "intro": notes.get("intro", ""), **config.as_json(),
                "verified": notes.get("verified", {}), "notes": notes.get("notes", []), "docs": notes.get("docs", "")}

    def files(self, name: str) -> list[dict[str, Any]]:
        p = self.path(name)
        return [{"path": f.relative_to(p).as_posix(), "size": f.stat().st_size} for f in sorted(p.rglob("*"))
                if f.is_file() and "__pycache__" not in f.parts]

    def file(self, name: str, rel: str) -> bytes:
        p = self.path(name)
        target = (p / rel).resolve()
        if not target.is_file() or p.resolve() not in target.parents:
            raise NotFound(f"no file {rel}")
        return target.read_bytes()

    def fork(self, store: Store, org: str, name: str, into: str, author: str, team: str = "") -> None:
        """The catalog package `name` as the organization's harness `into`."""
        store.create_harness_from_folder(org, into, self.path(name), author, team=team, origin=f"catalog/{name}",
                                         message=f"fork of the catalog package {name}")
