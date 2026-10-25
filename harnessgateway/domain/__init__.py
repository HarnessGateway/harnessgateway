"""Layer 3, the domain: what the platform keeps and the rules it keeps it by, one object per module.
Each module holds a frozen dataclass and the class that keeps it in records and blobs; nothing here
runs anything or speaks HTTP. A platform-level record (a login, a worker, a token) belongs to the
empty tenant."""
from __future__ import annotations

from dataclasses import fields
from typing import Any, TypeVar

PLATFORM = ""                                        # the tenant of platform-level records
T = TypeVar("T")


def load(cls: type[T], d: dict[str, Any]) -> T:
    """A record as its dataclass; a field a previous version kept and this one dropped is ignored,
    so a data directory from an earlier release reads as it was."""
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in known})
