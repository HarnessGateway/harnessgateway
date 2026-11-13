"""What a harness is told about the turns it did not take (spec §6.5)."""

from collections.abc import Sequence
from dataclasses import dataclass

from harnessgateway.domain.contract import MIND


@dataclass(frozen=True)
class PastTurn:
    """One earlier turn of the session's chain: which harness took it, and where that harness keeps its mind."""

    harness: str
    mind: str | None


def gap(chain: Sequence[PastTurn], harness: str) -> list[PastTurn]:
    """The turns after `harness`'s own most recent turn, oldest first; all of them if it has taken none."""
    for index in range(len(chain) - 1, -1, -1):
        if chain[index].harness == harness:
            return list(chain[index + 1:])
    return list(chain)


def _listed(items: Sequence[str]) -> str:
    """a | a and b | a, b and c"""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def handover_line(chain: Sequence[PastTurn], harness: str) -> str:
    """One line naming the other harnesses that ran since `harness`'s last turn, most recent first; or nothing."""
    others: dict[str, str | None] = {}
    for turn in reversed(gap(chain, harness)):
        if turn.harness != harness:
            others.setdefault(turn.harness, turn.mind)
    if not others:
        return ""
    names = list(others)
    line = f"Handover from {names[0]}."
    if len(names) > 1:
        line += f" Before it: {_listed(names[1:])}."
    records = [f"{MIND}/{mind}" for mind in others.values() if mind]
    if records:
        line += f" Review {_listed(records)}."
    return line
