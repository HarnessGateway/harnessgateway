"""When a session changes harness: what the new one is told about those before it, and how much it has not seen."""

from collections.abc import Sequence


def _last(pasts: Sequence[str], harness: str) -> int:
    return max((index for index, name in enumerate(pasts) if name == harness), default=-1)


def unseen(pasts: Sequence[str], harness: str) -> int:
    """How many turns at the end of a session's chain this harness has not seen: those after its own last one."""
    return len(pasts) - _last(pasts, harness) - 1


def handover_line(pasts: Sequence[str], harness: str) -> str:
    """One line naming the harnesses that worked on the session since this one last did, most recent first; or
    nothing, when none did. `pasts` is each earlier turn's harness, oldest first."""
    others = list(dict.fromkeys(reversed(pasts[_last(pasts, harness) + 1:])))
    if not others:
        return ""
    before = others[1:]
    listed = before[0] if len(before) == 1 else f"{', '.join(before[:-1])} and {before[-1]}" if before else ""
    return f"Handover from {others[0]}." + (f" Before it: {listed}." if listed else "")
