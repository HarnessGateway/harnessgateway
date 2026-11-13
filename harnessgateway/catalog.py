"""The packages that ship with the platform (spec §21): folders in the platform image, added on request."""

import json
from pathlib import Path

from harnessgateway import packaging
from harnessgateway.domain.package import Manifest
from harnessgateway.errors import NotFound, Refused


class Catalog:
    def __init__(self, folder: Path) -> None:
        self._folder = folder

    def entries(self) -> list[Manifest]:
        """Every valid package in the catalog, by name."""
        found = []
        for path in sorted(self._folder.glob("*/harness.json")) if self._folder.is_dir() else []:
            try:
                found.append(Manifest.load(json.loads(path.read_text())))
            except (Refused, ValueError):
                continue                                        # not a package: not offered
        return sorted(found, key=lambda manifest: manifest.name)

    def archive(self, name: str) -> bytes:
        for path in self._folder.glob("*/harness.json") if self._folder.is_dir() else []:
            try:
                if Manifest.load(json.loads(path.read_text())).name == name:
                    return packaging.pack(path.parent)
            except (Refused, ValueError):
                continue
        raise NotFound(f"the catalog has no package named {name!r}")
