"""Backend: a running turn's output as it arrives, for any number of followers. Chunks are appended
in order and read back by anyone; `wait` blocks a follower until there is more or the time is up.
`records` keeps chunks as records and polls; `postgres` keeps them the same way and wakes followers
with LISTEN/NOTIFY instead of polling."""
from __future__ import annotations

from typing import Any, Protocol

Chunk = dict[str, Any]                               # {"seq": int, "stream": "stdout" | "stderr", "text": str}


class Events(Protocol):
    def append(self, org: str, rid: str, chunks: list[Chunk]) -> None: ...
    def read(self, org: str, rid: str) -> list[Chunk]: ...
    async def wait(self, org: str, rid: str, seen: int, timeout_s: float) -> None:
        """Return when more than `seen` chunks are there, or after `timeout_s`."""
        ...
    def clear(self, org: str, rid: str) -> None: ...


def open_events(records, url: str) -> Events:
    """The events backend that fits the records backend: Postgres wakes followers, anything else polls."""
    if url.startswith(("postgresql://", "postgres://")):
        from .postgres import PostgresEvents
        return PostgresEvents(records, url)
    from .records import RecordEvents
    return RecordEvents(records)
