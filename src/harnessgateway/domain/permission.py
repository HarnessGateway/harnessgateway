"""Permissions. Every route names what it does as an action, `noun.verb` (`responses.write`, `providers.read`). A
pattern is `noun.verb` in which either part may be `*`. An API key's scopes are patterns, and so is what an
edition's role allows: this is the one rule of what a pattern allows, and of what is no wider than something else.
There is no pattern that denies."""

import re
from collections.abc import Sequence

from harnessgateway.errors import Invalid

ANYONE = "me.read"                             # what every caller may do, whatever limits them: ask what they may do
PATTERNS_MAX = 200
_PART = r"(?:\*|[a-z][a-z0-9_]*)"
_PATTERN = re.compile(rf"{_PART}\.{_PART}")


def checked(patterns: object, what: str) -> tuple[str, ...]:
    """Patterns, each once, if that is what `patterns` is; or a refusal that names the first that is not one.
    `what` is the field they were sent as."""
    if not isinstance(patterns, list) or len(patterns) > PATTERNS_MAX:
        raise Invalid(f"{what} is a list of at most {PATTERNS_MAX} patterns, each noun.verb with * for either part",
                      param=what)
    for pattern in patterns:
        if not isinstance(pattern, str) or not _PATTERN.fullmatch(pattern):
            raise Invalid(f"{pattern!r} is not a pattern: noun.verb, with * for either part, as in providers.* or "
                          "*.read", param=what)
    return tuple(dict.fromkeys(patterns))


def _covers(pattern: str, other: str) -> bool:
    """Whether everything `other` matches, `pattern` matches too. `other` is an action, or a pattern."""
    (noun, verb), (other_noun, other_verb) = pattern.split("."), other.split(".")
    return noun in ("*", other_noun) and verb in ("*", other_verb)


def allows(patterns: Sequence[str], action: str) -> bool:
    """Whether one of these patterns matches an action."""
    return any(_covers(pattern, action) for pattern in patterns)


def within(patterns: Sequence[str], mine: Sequence[str]) -> bool:
    """Whether `patterns` allow nothing that `mine` do not: what someone who holds `mine` may give."""
    return all(any(_covers(held, pattern) for held in mine) for pattern in patterns)
