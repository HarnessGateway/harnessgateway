"""The save step: snapshot the mind and the work (spec §12.3)."""

import hashlib
import os
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from itertools import batched
from pathlib import Path

from harnessgateway.domain.snapshot import CHUNK, Entry, ManifestError, SnapshotManifest
from harnessgateway.errors import Refused
from harnessgateway.steps.client import StepClient, read_object
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.tree import Found, walk

_BATCH = 10_000


@dataclass(frozen=True)
class Saved:
    snapshot_id: str
    uploaded: int                              # chunks the store did not have
    skipped: tuple[str, ...]                   # sockets, pipes and devices: not stored


def _chunks(location: Path) -> Iterator[bytes]:
    with location.open("rb") as file:
        while data := file.read(CHUNK):
            yield data


def _chunk_at(location: Path, offset: int) -> bytes:
    with location.open("rb") as file:
        file.seek(offset)
        return file.read(CHUNK)


class _Reader:
    """Turns what the walk finds into manifest entries, reading only files the parent snapshot does not explain."""

    def __init__(self, parent: SnapshotManifest) -> None:
        self._parent = parent
        self.sources: dict[str, tuple[Path, int]] = {}      # hash -> where one copy of that chunk can be read
        self.skipped: list[str] = []

    def entries(self, found: Iterator[Found]) -> Iterator[Entry]:
        for item in found:
            if item.kind == "d":
                yield Entry(path=item.path, kind="d", mode=item.mode)
            elif item.kind == "l":
                yield Entry(path=item.path, kind="l", target=os.readlink(item.location))
            elif item.kind == "f":
                yield self._file(item)
            else:
                self.skipped.append(item.path)

    def _file(self, item: Found) -> Entry:
        before = self._parent.entries.get(item.path)
        if before is not None and before.unchanged(size=item.size, mtime=item.mtime, mode=item.mode):
            return before
        hashes, size = [], 0
        for data in _chunks(item.location):
            digest = hashlib.sha256(data).hexdigest()
            self.sources.setdefault(digest, (item.location, size))
            hashes.append(digest)
            size += len(data)
        return Entry(path=item.path, kind="f", mode=item.mode, size=size, mtime=item.mtime, hashes=tuple(hashes))


async def save(client: StepClient, turn_id: str, roots: Mapping[str, Path]) -> Saved:
    try:
        key = await client.save_plan(turn_id)
        parent = SnapshotManifest.loads((await read_object(client, key)).decode()) if key else SnapshotManifest([])
        reader = _Reader(parent)
        manifest = SnapshotManifest(reader.entries(walk(roots)))
        uploaded = 0
        for batch in batched(reader.sources, _BATCH):
            for digest in await client.missing(list(batch)):
                location, offset = reader.sources[digest]
                data = _chunk_at(location, offset)
                if hashlib.sha256(data).hexdigest() != digest:
                    raise StepError(f"{location} changed while it was being saved")
                await client.put_chunk(digest, data)
                uploaded += 1
        snapshot_id = await client.record(turn_id, manifest.dumps())
    except (ManifestError, OSError) as e:
        raise StepError(f"the folders could not be saved: {e}") from e
    except Refused as e:
        raise StepError(f"the platform refused the snapshot: {e.message}") from e
    return Saved(snapshot_id=snapshot_id, uploaded=uploaded, skipped=tuple(reader.skipped))
