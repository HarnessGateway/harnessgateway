"""Names and identifiers (spec §4): what they look like, never how they are made."""

import re

from harnessgateway.errors import Invalid

DEFAULT_ORG = "org_default"
DEFAULT_ENVIRONMENT = "default"

_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_ID_BODY = "[0-9A-HJKMNP-TV-Z]{26}"                       # Crockford base32, as a ULID writes it

PREFIXES = {
    "turn": "resp",
    "session": "sess",
    "snapshot": "snap",
    "file": "file",
    "key": "key",
    "org": "org",
    "install": "inst",
}
_CONTAINER_PREFIX = "cntr"


def check_name(value: object, what: str) -> str:
    """`value` if it is a name; otherwise a refusal that says what `what` must look like."""
    if isinstance(value, str) and _NAME.fullmatch(value):
        return value
    raise Invalid(
        f"{what} is 1 to 63 characters of a-z, 0-9 and '-', starting with a letter or a digit: {value!r}",
        param=what,
    )


def check_id(value: object, kind: str) -> str:
    """`value` if it is an identifier of `kind`; otherwise a refusal."""
    prefix = PREFIXES[kind]
    if isinstance(value, str) and re.fullmatch(f"{prefix}_{_ID_BODY}", value):
        return value
    raise Invalid(f"not a {kind} id ({prefix}_ and 26 characters): {value!r}")


def container_id(session_id: str) -> str:
    """The id a session's work is read under in the Containers API (spec §13.10)."""
    return f"{_CONTAINER_PREFIX}_{check_id(session_id, 'session').partition('_')[2]}"


def session_of_container(value: object) -> str:
    if isinstance(value, str) and re.fullmatch(f"{_CONTAINER_PREFIX}_{_ID_BODY}", value):
        return f"{PREFIXES['session']}_{value.partition('_')[2]}"
    raise Invalid(f"not a container id ({_CONTAINER_PREFIX}_ and 26 characters): {value!r}")
