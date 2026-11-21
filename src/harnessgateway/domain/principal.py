"""Who is calling. Every authenticated request resolves to one, however an edition authenticates it."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    org_id: str                                # whose data the request may touch
    subject: str                               # who, within it: an API key's id, or whatever an edition names
