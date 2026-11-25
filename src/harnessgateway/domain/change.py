"""A change: something someone, or the platform itself, did to what an organization has. Extensions are told of
each one inside the transaction that makes it, so a record of it is kept with it or the change is not made."""

from dataclasses import dataclass, field
from typing import Any

HOST = "host"                                  # who made a change `harnessgateway init` makes: someone on the host
PLATFORM = "platform"                          # who made a change the platform makes by itself, when it starts


@dataclass(frozen=True)
class Change:
    name: str                                  # what was done: `providers.applied`, `keys.revoked`, …
    org_id: str
    by: str                                    # the caller's subject, or HOST, or PLATFORM
    target: str                                # what it was done to: a name, or an id
    about: dict[str, Any] = field(default_factory=dict)        # more, and never a secret

    def __post_init__(self) -> None:
        noun, dot, verb = self.name.partition(".")
        assert noun and dot and verb, f"a change is named noun.verb: {self.name!r}"
        assert self.org_id and self.by and self.target, f"{self.name} names its organization, who, and what"
