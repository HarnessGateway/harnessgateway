"""What an organization starts with: one environment, and the two harnesses it carries.

    default                     an environment on a common image, with the default limits and an open network
    shell                       the smallest harness package: it runs the request's input as a command
    harness-package-builder     an agent that writes harness packages for other agent CLIs

They are enough to try an environment before any agent is set up, and to make every other harness. Once given
they are the organization's own, to change or delete: nothing brings one back, and an update of the platform
leaves them as they are.
"""

import json
import os
from pathlib import Path

from harnessgateway.domain.package import PackageFile, write_archive
from harnessgateway.platform import Platform

_HERE = Path(__file__).parent
ENVIRONMENT = "default"


def archive(name: str) -> bytes:
    """One of the packages kept here, as the archive it is added as."""
    folder = _HERE / name
    return write_archive(PackageFile(file.relative_to(folder).as_posix(), file.read_bytes(), os.access(file, os.X_OK))
                         for file in folder.rglob("*") if file.is_file())


async def give(platform: Platform, org_id: str) -> None:
    """Give an organization the defaults. Giving them again puts each back as it ships."""
    definition = json.loads((_HERE / "environment.json").read_text())
    for name in definition["harnesses"]:
        await platform.resources.add_package(org_id, name, archive(name))
    await platform.resources.apply(org_id, "environments", ENVIRONMENT, definition)
