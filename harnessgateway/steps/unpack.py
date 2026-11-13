"""The unpack step: put a package's files, and the kit, into an install's volume (spec §8.5)."""

from pathlib import Path

from harnessgateway.domain.archive import read_archive
from harnessgateway.errors import Refused
from harnessgateway.packaging import lay_out
from harnessgateway.steps.client import StepClient, read_object
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.folders import empty, give_tree


async def unpack(client: StepClient, install_id: str, target: Path) -> None:
    try:
        package = read_archive(await read_object(client, await client.unpack_plan(install_id)))
        empty(target)
        lay_out(package, target)
        give_tree(target)
    except OSError as e:
        raise StepError(f"the package could not be unpacked: {e}") from e
    except Refused as e:
        raise StepError(f"the package could not be unpacked: {e.message}") from e
