"""Who is calling. Every authenticated request resolves to one, however an edition authenticates it."""

from dataclasses import dataclass

OWNER = "owner"                                # the one person a community installation has: every key of it is theirs


@dataclass(frozen=True)
class Principal:
    org_id: str                                # whose data the request may touch
    subject: str                               # who, within it: the owner, or whoever an edition names
    key: str | None = None                     # the API key they came with, when they came with one
    scopes: tuple[str, ...] = ()               # what that key is limited to, as patterns; empty: it is not limited
