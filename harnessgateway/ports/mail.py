"""The mail port: how an invitation reaches the person it is for (SPEC §10.1)."""
from __future__ import annotations

from typing import Protocol


class Mailer(Protocol):
    def send(self, to: str, subject: str, body: str) -> None: ...
