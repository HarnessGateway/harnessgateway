"""Who is asking. An API key opens one organization as one member with one role."""
from __future__ import annotations

from dataclasses import dataclass

from ..errors import Forbidden


@dataclass(frozen=True)
class Principal:
    org_id: str
    member_id: str
    key_id: str
    role: str                                        # admin | member

    @property
    def admin(self) -> bool:
        return self.role == "admin"

    def require_admin(self) -> None:
        if not self.admin:
            raise Forbidden("an admin key does this")

    @property
    def actor(self) -> str:
        return f"key:{self.key_id}"


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    org_id: str | None                               # set: a dedicated (hybrid) node that runs only this organization
