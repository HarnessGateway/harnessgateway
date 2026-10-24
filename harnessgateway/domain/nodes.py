"""A node: a worker machine that joined the platform with the join token and runs turns with its own
credential (`hgw_…`). It holds nothing of the platform's but that credential."""
from __future__ import annotations

import secrets
import time
from dataclasses import asdict, dataclass

from ..backends.records import Records
from ..kernel.ids import digest_of, short_id
from . import PLATFORM

NODE_PREFIX = "hgw_"
LOST_AFTER_S = 45


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    created: float
    last_seen: float
    slots: int = 0
    running: int = 0
    version: str = ""

    @property
    def alive(self) -> bool:
        return time.time() - self.last_seen < LOST_AFTER_S


class Nodes:
    def __init__(self, records: Records):
        self.records = records

    def register(self, name: str, slots: int, version: str) -> tuple[Node, str]:
        secret = NODE_PREFIX + secrets.token_urlsafe(32)
        n = Node(short_id(), name, time.time(), time.time(), slots, 0, version)
        self.records.put("worker", PLATFORM, n.id, {**asdict(n), "digest": digest_of(secret)})
        return n, secret

    def by_secret(self, secret: str) -> Node | None:
        if not secret.startswith(NODE_PREFIX):
            return None
        for d in self.records.list("worker", PLATFORM):
            if secrets.compare_digest(d["digest"], digest_of(secret)):
                return Node(**{k: v for k, v in d.items() if k != "digest"})
        return None

    def touch(self, n: Node, running: int) -> None:
        d = self.records.get("worker", PLATFORM, n.id)
        if d:
            self.records.put("worker", PLATFORM, n.id, {**d, "last_seen": time.time(), "running": running})

    def list(self) -> list[Node]:
        return [Node(**{k: v for k, v in d.items() if k != "digest"}) for d in self.records.list("worker", PLATFORM)]

    def forget(self, node_id: str) -> None:
        self.records.delete("worker", PLATFORM, node_id)
