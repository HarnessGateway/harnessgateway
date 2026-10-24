"""No billing: the usage records are the bill, for the operator to read."""
from __future__ import annotations

from typing import Mapping


class NoBilling:
    enabled = False

    def report(self, customer: str, readings: Mapping[str, float], at: float) -> None:
        pass
