"""A node's identity, resolved from its credential (SPEC §10.1). People and keys resolve to `domain.access.Principal`."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class NodeIdentity:
    node_id: str
    pool: str
