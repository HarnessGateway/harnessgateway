"""What ships with the platform: every package in the repository's `packages/`, as a harness, and every definition
in `environments/`, as an environment of that file's name.

    packages/shell                       the smallest harness package: it runs the request's input as a command
    packages/harness-package-builder     builds every other harness package, and carries the contract they are built to
    environments/default.json            an environment on a common image, with an open network, that carries both

The packages are the platform's. Every start puts each at this version's, for every organization, and none is
changed in place: a copy under another name is the organization's own. The environments are what an organization
starts with: given once, at setup, and its own from then on, so an update of the platform leaves them as they are.
"""

import json
import os

from harnessgateway import shipped
from harnessgateway.domain.package import PackageFile, read_archive, write_archive
from harnessgateway.platform import Platform


def names() -> list[str]:
    """The harnesses that ship: each folder of `packages/` that holds a `run`."""
    return sorted(package.name for package in shipped.folder("packages").iterdir() if (package / "run").is_file())


def archive(name: str) -> bytes:
    """One of the packages that ship, as the archive it is added as."""
    package = shipped.folder("packages") / name
    return write_archive(PackageFile(file.relative_to(package).as_posix(), file.read_bytes(), os.access(file, os.X_OK))
                         for file in package.rglob("*") if file.is_file())


async def packages(platform: Platform, org_id: str, *, by: str) -> None:
    """Put each package that ships at this version's. One that is there already, unchanged, is left as it is."""
    for name in names():
        packed = archive(name)
        had = await platform.resources.find(org_id, "harnesses", name)
        if had is None or had.spec["digest"] != read_archive(packed).digest:
            await platform.resources.add_package(org_id, name, packed, by=by)


async def give(platform: Platform, org_id: str, *, by: str) -> None:
    """Give an organization what it starts with: the packages that ship, then the environments, which name them.
    Giving them again puts each environment back as it ships. `by` is who gives them: whoever made the
    organization."""
    await packages(platform, org_id, by=by)
    for definition in sorted(shipped.folder("environments").glob("*.json")):
        await platform.resources.apply(org_id, "environments", definition.stem, json.loads(definition.read_text()),
                                       by=by)
