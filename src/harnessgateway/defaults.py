"""What an organization starts with: every package in the repository's `packages/` as a harness, and every
definition in `environments/` as an environment of that file's name.

    packages/shell                       the smallest harness package: it runs the request's input as a command
    packages/hg-fde                      HG FDE: the engineer that builds every other harness package
    environments/default.json            an environment on a common image, with an open network, that carries both

They are enough to try an environment before any agent is set up, and to make every other harness. Once given
they are the organization's own, to change or delete: nothing brings one back, and an update of the platform
leaves them as they are.
"""

import json
import os

from harnessgateway import shipped
from harnessgateway.domain.package import PackageFile, write_archive
from harnessgateway.platform import Platform


def archive(name: str) -> bytes:
    """One of the packages that ship, as the archive it is added as."""
    package = shipped.folder("packages") / name
    return write_archive(PackageFile(file.relative_to(package).as_posix(), file.read_bytes(), os.access(file, os.X_OK))
                         for file in package.rglob("*") if file.is_file())


async def give(platform: Platform, org_id: str) -> None:
    """Give an organization the defaults. Giving them again puts each back as it ships."""
    for package in sorted(shipped.folder("packages").iterdir()):       # the harnesses first: an environment names them
        if (package / "harness.json").is_file():
            await platform.resources.add_package(org_id, package.name, archive(package.name))
    for definition in sorted(shipped.folder("environments").glob("*.json")):
        await platform.resources.apply(org_id, "environments", definition.stem, json.loads(definition.read_text()))
