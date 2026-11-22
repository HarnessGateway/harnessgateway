"""When a session changes harness: what the new one is told about those before it, and how much it has not seen."""

from collections.abc import Sequence

from harnessgateway.domain.contract import MIND

Past = tuple[str, str | None]                  # an earlier turn's harness, and where it keeps its mind


def _last(pasts: Sequence[Past], harness: str) -> int:
    return max((index for index, (name, _) in enumerate(pasts) if name == harness), default=-1)


def unseen(pasts: Sequence[Past], harness: str) -> int:
    """How many turns at the end of a session's chain this harness has not seen: those after its own last one."""
    return len(pasts) - _last(pasts, harness) - 1


def handover_line(pasts: Sequence[Past], harness: str) -> str:
    """One line naming the harnesses that worked on the session since this one last did, most recent first, and
    where each kept its own notes; or nothing, when none did. `pasts` is oldest first."""
    others: dict[str, str | None] = {}
    for name, mind in reversed(pasts[_last(pasts, harness) + 1:]):
        others.setdefault(name, mind)
    if not others:
        return ""

    def listed(items: list[str]) -> str:
        return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"
    names, minds = list(others), [f"{MIND}/{mind}" for mind in others.values() if mind]
    return (f"Handover from {names[0]}." + (f" Before it: {listed(names[1:])}." if names[1:] else "")
            + (f" Review {listed(minds)}." if minds else ""))
