"""The restore step: make the session's volumes hold a snapshot, and write the turn's inputs (spec §12.4)."""

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path

from harnessgateway.domain.snapshot import Entry, ManifestError, SnapshotManifest
from harnessgateway.errors import Refused
from harnessgateway.steps.client import InputFile, RestorePlan, StepClient, read_object
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.folders import empty, give


async def _write_file(client: StepClient, entry: Entry, location: Path) -> None:
    size = 0
    with location.open("xb") as file:
        for digest in entry.hashes:
            data = await client.chunk(digest)
            if hashlib.sha256(data).hexdigest() != digest:
                raise StepError(f"{entry.path}: a chunk arrived changed")
            file.write(data)
            size += len(data)
    if size != entry.size:
        raise StepError(f"{entry.path}: {size} bytes arrived, {entry.size} were expected")
    os.chmod(location, entry.mode)
    os.utime(location, ns=(entry.mtime, entry.mtime))


async def _rebuild(client: StepClient, manifest: SnapshotManifest, roots: Mapping[str, Path]) -> None:
    for root in roots.values():
        empty(root)
        give(root)
    directories = []
    for path in sorted(manifest.entries):                    # a directory sorts before what it holds
        entry = manifest.entries[path]
        name, _, rest = path.partition("/")
        location = roots[name] / rest
        if entry.kind == "d":
            location.mkdir(mode=0o700)
            directories.append((location, entry.mode))
        elif entry.kind == "l":
            os.symlink(entry.target, location)
        else:
            await _write_file(client, entry, location)
        give(location)
    for location, mode in reversed(directories):             # last, so a read-only directory can be filled first
        os.chmod(location, mode)


async def _write_file_input(client: StepClient, file: InputFile, folder: Path) -> None:
    if file.filename in ("", ".", "..") or "/" in file.filename or "\x00" in file.filename:
        raise StepError(f"{file.id}: {file.filename!r} is not a plain file name")
    target = folder / file.id
    target.mkdir()
    give(target)
    with (target / file.filename).open("xb") as out:
        async for piece in client.object(file.key):
            out.write(piece)
    give(target / file.filename)


async def _write_inputs(client: StepClient, plan: RestorePlan, turn: Path) -> None:
    turn.mkdir(exist_ok=True)
    empty(turn)
    texts = {
        "request.json": json.dumps(plan.request),
        "history.json": json.dumps(plan.history),
        "handover.txt": plan.handover,
    }
    for name, text in texts.items():
        (turn / name).write_text(text)
    (turn / "files").mkdir()
    for path in (turn, *turn.iterdir()):
        give(path)
    for file in plan.files:
        await _write_file_input(client, file, turn / "files")


async def restore(client: StepClient, turn_id: str, roots: Mapping[str, Path], turn: Path) -> None:
    try:
        plan = await client.restore_plan(turn_id)
        if not plan.warm:
            text = (await read_object(client, plan.manifest_key)).decode() if plan.manifest_key else ""
            await _rebuild(client, SnapshotManifest.loads(text), roots)
        await _write_inputs(client, plan, turn)
    except (ManifestError, OSError) as e:
        raise StepError(f"the folders could not be restored: {e}") from e
    except Refused as e:
        raise StepError(f"the platform refused: {e.message}") from e
