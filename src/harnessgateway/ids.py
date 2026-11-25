"""Names and identifiers: what they look like, how they are made, and how one is told from another."""

import hashlib
import re
import secrets
import time

from harnessgateway.errors import Invalid

DEFAULT_ORG = "org_default"                    # the one organization a community installation has
PREFIXES = {"response": "resp", "session": "sess", "container": "cntr", "snapshot": "snap", "file": "file",
            "key": "key", "install": "inst", "item": "item"}
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"          # Crockford base32, as a ULID is written
_BODY = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


def check_name(value: object, what: str) -> str:
    """`value`, if it is a name a resource may have; otherwise a refusal that says what `what` must look like."""
    if isinstance(value, str) and _NAME.fullmatch(value):
        return value
    raise Invalid(f"{what} is 1 to 63 characters of a-z, 0-9 and '-', starting with a letter or a digit: {value!r}",
                  param=what)


def ulid() -> str:
    """A ULID: 48 bits of milliseconds, then 80 random bits, as 26 characters. Those made later sort later."""
    number = (time.time_ns() // 1_000_000 << 80) | secrets.randbits(80)
    return "".join(_ALPHABET[(number >> (5 * place)) & 31] for place in reversed(range(26)))


def new_id(kind: str) -> str:
    """A prefix and a ULID. Ids of one kind sort by when they were made, which is what paging through a list
    relies on."""
    return f"{PREFIXES[kind]}_{ulid()}"


def is_id(value: object, kind: str) -> bool:
    if not isinstance(value, str):
        return False
    prefix, _, body = value.partition("_")
    return prefix == PREFIXES[kind] and _BODY.fullmatch(body) is not None


def container_of(session_id: str) -> str:
    """The id a session's work is read under in the Containers API: the same id, under that API's prefix."""
    return f"{PREFIXES['container']}_{session_id.partition('_')[2]}"


def session_of(container_id: str) -> str:
    return f"{PREFIXES['session']}_{container_id.partition('_')[2]}"


def new_api_key() -> str:
    return "hg_" + secrets.token_urlsafe(30)


def new_token() -> str:
    """What one sandbox's connections prove themselves to the egress proxy with."""
    return secrets.token_urlsafe(32)


def new_placeholder() -> str:
    """What a sandbox holds in place of a secret: worth nothing anywhere but in the egress proxy."""
    return f"hgs_{secrets.token_hex(16)}"


def fingerprint(secret: str) -> str:
    """What is kept in place of an API key or a token. They are long and random, so a plain hash cannot be
    reversed by guessing, and it can be looked up, which a salted one cannot."""
    return hashlib.sha256(secret.encode()).hexdigest()
