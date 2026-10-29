"""Who is asking: an organization's API key (or whatever an extension authenticates), or a node."""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import Forbidden


@dataclass(frozen=True)
class Principal:
    org_id: str
    scope: str                       # admin | run
    actor: str                       # key:<id>, or what an extension names

    def require_admin(self) -> None:
        if self.scope != "admin":
            raise Forbidden("an admin key does this")


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    pool: str
