"""API keys: a member's credential (`hg_…`), shown once and kept as a digest, and the deployer's key
(`hgd_…`). A key opens the organization as its member for as long as the key, the member and the
organization stand."""
from __future__ import annotations

import re
import secrets
import time
from dataclasses import asdict, dataclass

from ..backends.records import Records
from ..errors import NotFound
from ..kernel.ids import digest_of, short_id
from . import PLATFORM
from .members import Member, Members
from .orgs import Orgs

KEY_PREFIX = "hg_"
DEPLOYER_PREFIX = "hgd_"


@dataclass(frozen=True)
class ApiKey:
    id: str
    org: str
    member: str
    label: str
    created: float
    revoked: bool = False


class Keys:
    def __init__(self, records: Records, orgs: Orgs, members: Members):
        self.records, self.orgs, self.members = records, orgs, members

    def mint(self, org: str, member: str, label: str = "") -> tuple[ApiKey, str]:
        """A new key for a member; the secret is returned once and only its digest is kept."""
        self.members.get(org, member)
        secret = KEY_PREFIX + secrets.token_urlsafe(32)
        key = ApiKey(short_id(), org, member, label[:80], time.time())
        self.records.put("key", org, key.id, asdict(key))
        self.records.put("login", PLATFORM, digest_of(secret), {"org": org, "id": key.id})
        return key, secret

    def list(self, org: str) -> list[ApiKey]:
        return sorted((ApiKey(**d) for d in self.records.list("key", org)), key=lambda k: k.created)

    def revoke(self, org: str, key_id: str) -> ApiKey:
        d = self.records.get("key", org, key_id) if re.fullmatch(r"[0-9a-f]{12}", key_id or "") else None
        if d is None:
            raise NotFound(f"no key {key_id}")
        k = ApiKey(**{**d, "revoked": True})
        self.records.put("key", org, key_id, asdict(k))
        return k

    def revoke_all(self, org: str, member: str) -> None:
        for k in self.list(org):
            if k.member == member and not k.revoked:
                self.revoke(org, k.id)

    def resolve(self, secret: str) -> Member | None:
        """The member a presented secret belongs to, if it is a live key of an active organization."""
        if not secret.startswith(KEY_PREFIX):
            return None
        login = self.records.get("login", PLATFORM, digest_of(secret))
        if not login:
            return None
        key = self.records.get("key", login["org"], login["id"])
        if key is None or key["revoked"]:
            return None
        try:
            if self.orgs.get(key["org"]).status != "active":
                return None
            return self.members.get(key["org"], key["member"])
        except NotFound:
            return None

    # the deployer's
    def set_deployer(self) -> str:
        """A new deployer key, shown once; replaces the previous one."""
        secret = DEPLOYER_PREFIX + secrets.token_urlsafe(32)
        self.records.put("deployer", PLATFORM, "key", {"digest": digest_of(secret), "created": time.time()})
        return secret

    def has_deployer(self) -> bool:
        return self.records.get("deployer", PLATFORM, "key") is not None

    def is_deployer(self, secret: str) -> bool:
        d = self.records.get("deployer", PLATFORM, "key")
        return bool(d) and secrets.compare_digest(d["digest"], digest_of(secret))
