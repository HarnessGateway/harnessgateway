"""The key wrapper port: wraps each organization's data key under a master key it never reveals (SPEC §8.1)."""
from __future__ import annotations

from typing import Protocol


class KeyWrapper(Protocol):
    def wrap(self, key: bytes, context: str) -> bytes: ...

    def unwrap(self, wrapped: bytes, context: str) -> bytes: ...
