"""A handover: a turn whose harness is not the harness of the turn before it. The new harness is given nothing of
what was said: it is told who worked on the session and where each keeps its memory, and reads up by itself."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from harnessgateway.domain import contract

NOTE_MAX = 16 * 1024                           # the longest note of where a harness's memory is that is repeated


@dataclass(frozen=True)
class Stint:
    """Consecutive turns of a session run with the same model line."""

    harness: str
    line: str
    responses: tuple[str, ...]


def stints(past: Sequence[tuple[str, str, str]]) -> list[Stint]:
    """A chain as its stints. `past` is each earlier turn's response id, harness and model line, oldest first."""
    found: list[Stint] = []
    for response, harness, line in past:
        if found and found[-1].line == line:
            found[-1] = replace(found[-1], responses=(*found[-1].responses, response))
        else:
            found.append(Stint(harness, line, (response,)))
    return found


def note_path(harness: str) -> str:
    """Where a harness's package says where its memory is: what its `run` writes as `$HG_STATE/memory`."""
    return f"{contract.MIND}/{contract.STATE_DIR}/{harness}/{contract.MEMORY}"


def pointer(harness: str) -> str:
    """What stands in for a note too long to repeat: where the note itself is."""
    return f"it says where in {note_path(harness)}"


def handover(worked: Sequence[Stint], harness: str, notes: Mapping[str, str | None]) -> str:
    """What a harness is told when it takes a session over: the session's handover history, in words. Nothing
    when the turn before was its own, or there was none. `notes` is each other harness's note of where its
    memory is; None for one that left none."""
    if not worked or worked[-1].harness == harness:
        return ""

    def memory(stint: Stint) -> str:
        if stint.harness == harness:
            return "This is the harness you are: its memory is your own."
        note = (notes.get(stint.harness) or "").strip()
        return "Its memory: " + (note.replace("\n", "\n   ") or f"it left no note of where; look in {contract.MIND}.")

    return "\n".join([
        "Handover. Other agent harnesses worked on this session before this turn. To the person you are all one "
        "assistant: carry on from where the last one stopped.",
        "",
        "Nothing of what was said or done is repeated here. Every harness of a session shares its two folders: the "
        f"mind ({contract.MIND}, which is $HOME), where each keeps its own memory of the session, and the work "
        f"({contract.WORK}), which holds what the session is working on. Read what you need from them before you "
        "answer.",
        "",
        "Who worked on the session, oldest first:",
        "",
        *(f"{number}. {stint.harness}, as {stint.line}: {', '.join(stint.responses)}\n   {memory(stint)}"
          for number, stint in enumerate(worked, 1)),
    ]) + "\n"
