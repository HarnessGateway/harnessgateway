"""Accounts, the people who sign in with an email. An account has an email, a name, a password (hashed,
scrypt) and memberships: the organizations it belongs to and its member name in each. Members and
API keys are unchanged; an account is one more way to be a member. Platform-level records, so an
email is one account across orgs.

    account/<id>                   the account
    email/<digest(email)>          email -> account id
    invite/<org>/<digest>          an org's invitation to an email: the role it gets

The session token is signed with a key derived from the master key and carries its purpose
and expiry; nothing is stored for it.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from ..errors import Conflict, Invalid, NotFound
from ..kernel.ids import NAME, check_name, digest_of
from ..kernel.vault import Sealer
from . import PLATFORM, load
from .members import Member, Members
from .orgs import Orgs

EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
SESSION_TTL_S = 7 * 86400
MIN_PASSWORD = 8


def normalize_email(email: Any) -> str:
    email = (email or "").strip().lower() if isinstance(email, str) else ""
    if not EMAIL.fullmatch(email) or len(email) > 254:
        raise Invalid("an email address is needed")
    return email


def handle_of(email: str) -> str:
    """A member name from an email's local part: lowercase, dots, dashes, underscores."""
    local = re.sub(r"[^a-z0-9._-]+", "-", email.split("@", 1)[0].lower()).strip("._-")[:63]
    return local if local and NAME.fullmatch(local) else "user"


def hash_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise Invalid(f"a password is at least {MIN_PASSWORD} characters")
    salt = secrets.token_bytes(16)
    return "scrypt$" + salt.hex() + "$" + hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1).hex()


def check_password(stored: str, password: str) -> bool:
    try:
        kind, salt, digest = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1).hex()
        return hmac.compare_digest(got, digest)
    except (ValueError, AttributeError):
        return False


@dataclass(frozen=True)
class Account:
    id: str
    email: str
    name: str
    created: float
    password: str = field(default="", repr=False)   # hashed
    memberships: list[dict[str, str]] = field(default_factory=list)   # [{org, member}]

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "email": self.email, "name": self.name, "created": self.created}


class Accounts:
    def __init__(self, records, vault: Sealer, orgs: Orgs, members: Members):
        self.records, self.orgs, self.members = records, orgs, members
        self._secret = vault.derive("accounts")

    # accounts
    def create(self, email: str, name: str, password: str) -> Account:
        email = normalize_email(email)
        if self.by_email(email):
            raise Conflict("an account with that email exists; sign in")
        a = Account(uuid.uuid4().hex[:12], email, (name or "").strip()[:80] or handle_of(email), time.time(), hash_password(password))
        self.records.put("account", PLATFORM, a.id, asdict(a))
        self.records.put("email", PLATFORM, digest_of(email), {"account": a.id})
        return a

    def _put(self, a: Account) -> None:
        self.records.put("account", PLATFORM, a.id, asdict(a))

    def by_id(self, account_id: str) -> Account:
        d = self.records.get("account", PLATFORM, account_id) if re.fullmatch(r"[0-9a-f]{12}", account_id or "") else None
        if d is None:
            raise NotFound("no such account")
        return load(Account, d)

    def by_email(self, email: str) -> Account | None:
        try:
            email = normalize_email(email)
        except Invalid:
            return None
        ref = self.records.get("email", PLATFORM, digest_of(email))
        return self.by_id(ref["account"]) if ref else None

    def login(self, email: str, password: str) -> Account | None:
        """The account, if the password is right; None says nothing about which part was wrong."""
        a = self.by_email(email)
        return a if a and a.password and check_password(a.password, password or "") else None

    # memberships
    def memberships(self, a: Account) -> list[dict[str, Any]]:
        """The account's orgs with its member name and role in each; a membership whose org
        or member is gone is skipped."""
        out = []
        for m in a.memberships:
            try:
                t = self.orgs.get(m["org"])
                member = self.members.get(m["org"], m["member"])
            except NotFound:
                continue
            out.append({"org": t.name, "title": t.title, "member": member.name, "role": member.role, "status": t.status})
        return out

    def member_of(self, a: Account, org: str) -> Member | None:
        for m in a.memberships:
            if m["org"] == org:
                try:
                    return self.members.get(org, m["member"])
                except NotFound:
                    return None
        return None

    def _free_member_name(self, org: str, wanted: str) -> str:
        taken = {m.name for m in self.members.list(org)}
        name, i = wanted, 2
        while name in taken:
            name, i = f"{wanted[:60]}-{i}", i + 1
        return name

    def join(self, a: Account, org: str, role: str, member: str = "") -> Member:
        """Make the account a member of the org (a new member record named after it), or return
        the membership it has."""
        if existing := self.member_of(a, org):
            return existing
        name = self._free_member_name(org, member or handle_of(a.email))
        m = self.members.create(org, name, role)
        a = Account(**{**asdict(a), "memberships": [*a.memberships, {"org": org, "member": name}]})
        self._put(a)
        return m

    def create_org(self, a: Account, name: str, limits: dict[str, Any] | None = None) -> Member:
        """An org of the account's own, with the account as its first owner."""
        self.orgs.create(check_name(name, "org"), limits)
        return self.join(a, name, "owner")

    def free_org_name(self, wanted: str) -> str:
        """`wanted` as an org name nobody has: as is, or with a suffix."""
        base = re.sub(r"[^a-z0-9._-]+", "-", wanted.lower()).strip("._-")[:60] or "team"
        taken = {t.name for t in self.orgs.list()}
        name, i = base, 2
        while name in taken:
            name, i = f"{base}-{i}", i + 1
        return name

    # invitations
    def invite(self, org: str, email: str, role: str) -> dict[str, Any]:
        email = normalize_email(email)
        if role not in ("owner", "member"):
            raise Invalid("a role is owner or member")
        self.orgs.get(org)
        d = {"org": org, "email": email, "role": role, "created": time.time()}
        self.records.put("invite", org, digest_of(email), d)
        return d

    def invites(self, org: str) -> list[dict[str, Any]]:
        return sorted(self.records.list("invite", org), key=lambda d: d["created"])

    def revoke_invite(self, org: str, email: str) -> None:
        if not self.records.delete("invite", org, digest_of(normalize_email(email))):
            raise NotFound("no such invitation")

    def invited(self, email: str) -> list[dict[str, Any]]:
        """Every org's invitation to this email."""
        try:
            email = normalize_email(email)
        except Invalid:
            return []
        return [d for _, _, d in self.records.where("invite", "email", email, limit=100)]

    def redeem_invites(self, a: Account) -> list[Member]:
        """Join every org that invited the account's email; the invitations are spent."""
        joined = []
        for inv in self.invited(a.email):
            try:
                joined.append(self.join(self.by_id(a.id), inv["org"], inv["role"]))
            except NotFound:
                pass
            self.records.delete("invite", inv["org"], digest_of(a.email))
        return joined

    # tokens
    def token(self, purpose: str, account_id: str, ttl_s: float, **more: Any) -> str:
        claim = json.dumps({"p": purpose, "a": account_id, "e": int(time.time() + ttl_s), **more}, separators=(",", ":")).encode()
        mac = hmac.new(self._secret, claim, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(claim + mac).decode().rstrip("=")

    def open_token(self, token: str, purpose: str) -> dict[str, Any] | None:
        """The claim a token carries for `purpose`, or None when it is not valid or has expired."""
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            claim, mac = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.new(self._secret, claim, hashlib.sha256).digest(), mac):
                return None
            c = json.loads(claim)
            return c if c.get("p") == purpose and c.get("e", 0) > time.time() else None
        except (ValueError, KeyError, TypeError):
            return None

    def session(self, a: Account, org: str, ttl_s: float = SESSION_TTL_S) -> str:
        """A session: this account, as its member of this org, until the expiry."""
        return self.token("session", a.id, ttl_s, t=org)

    def resolve_session(self, token: str) -> tuple[Account, Member] | None:
        """The account and the member a session token names, if both still stand."""
        c = self.open_token(token or "", "session")
        if not c:
            return None
        try:
            a = self.by_id(c["a"])
        except NotFound:
            return None
        m = self.member_of(a, c.get("t", ""))
        if m is None:
            return None
        try:
            if self.orgs.get(m.org).status != "active":
                return None
        except NotFound:
            return None
        return a, m
