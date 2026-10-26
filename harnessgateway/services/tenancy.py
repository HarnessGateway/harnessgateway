"""Organizations, members, API keys and invitations. An organization is the tenant; everything else belongs to one.
Organizations are created by the operator or by redeeming an invite code (the pilot's invite-only sign-up)."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import ids
from ..db import Database, Tx
from ..domain.orglimits import OrgLimits
from ..errors import Conflict, Forbidden, Invalid, NotFound, Unauthenticated
from .principal import Principal

KEY_TOUCH_S = 60


async def audit(tx: Tx, org_id: str | None, actor: str, action: str, target: str = "", **detail: Any) -> None:
    await tx.run("INSERT INTO audit_log (org_id, actor, action, target, detail) VALUES (%s, %s, %s, %s, %s)",
                 org_id, actor, action, target, detail)


def _email(value: object) -> str:
    if not isinstance(value, str) or len(value) > 254 or value.count("@") != 1 or value.startswith("@") or \
            value.endswith("@") or any(c.isspace() for c in value):
        raise Invalid("an email address", param="email")
    return value.lower()


def _org_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "organization", "name": row["name"], "status": row["status"],
            "limits": OrgLimits.parse(row["limits"]).model_dump(), "created_at": int(row["created_at"].timestamp())}


def _key_view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "api_key", "name": row["name"], "role": row["role"], "hint": row["hint"],
            "member_id": row["member_id"], "created_at": int(row["created_at"].timestamp()),
            "last_used_at": int(row["last_used_at"].timestamp()) if row["last_used_at"] else None,
            "expires_at": int(row["expires_at"].timestamp()) if row["expires_at"] else None}


class Tenancy:
    def __init__(self, db: Database, pepper: bytes):
        self.db, self.pepper = db, pepper
        self._touched: dict[str, float] = {}

    # organizations ────────────────────────────────────────────────────────────────────────────────────────────────
    async def create_org(self, name: str, owner_email: str, limits: dict[str, Any] | None, actor: str
                         ) -> tuple[dict[str, Any], str]:
        """A new organization with its owner and the owner's first admin key (returned once)."""
        ids.check_name(name, "organization")
        email = _email(owner_email)
        org_limits = OrgLimits.parse(limits)
        org_id, member_id = ids.new("org"), ids.new("member")
        async with self.db.platform() as tx:
            if await tx.one("SELECT 1 FROM orgs WHERE name = %s", name):
                raise Conflict(f"an organization named {name} exists", param="name")
            await tx.run("INSERT INTO orgs (id, name, limits) VALUES (%s, %s, %s)", org_id, name, org_limits.model_dump())
            await tx.run("INSERT INTO members (id, org_id, email, role) VALUES (%s, %s, %s, 'owner')",
                         member_id, org_id, email)
            secret = await self._mint(tx, org_id, member_id, "owner", "admin", None)
            await audit(tx, org_id, actor, "org.create", org_id, name=name, owner=email)
            row = await tx.one("SELECT * FROM orgs WHERE id = %s", org_id)
        assert row is not None
        return _org_view(row), secret

    async def org(self, org_id: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            row = await tx.one("SELECT * FROM orgs WHERE id = %s", ids.check(org_id, "org"))
        if not row:
            raise NotFound(f"no organization {org_id}")
        return _org_view(row)

    async def limits(self, org_id: str) -> OrgLimits:
        return OrgLimits.parse((await self.org(org_id))["limits"])

    async def list_orgs(self) -> list[dict[str, Any]]:
        async with self.db.platform() as tx:
            return [_org_view(r) for r in await tx.all("SELECT * FROM orgs ORDER BY created_at")]

    async def update_org(self, org_id: str, *, limits: dict[str, Any] | None, status: str | None, actor: str
                         ) -> dict[str, Any]:
        current = await self.org(org_id)
        new_limits = OrgLimits.parse(current["limits"]).merged(limits or {})
        if status is not None and status not in ("active", "suspended"):
            raise Invalid("status is active or suspended", param="status")
        async with self.db.platform() as tx:
            await tx.run("UPDATE orgs SET limits = %s, status = coalesce(%s, status) WHERE id = %s",
                         new_limits.model_dump(), status, org_id)
            await audit(tx, org_id, actor, "org.update", org_id, limits=limits or {}, status=status)
        return await self.org(org_id)

    # invites ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def create_invite(self, label: str, uses: int, ttl_days: int | None, limits: dict[str, Any] | None,
                            actor: str) -> dict[str, Any]:
        if not 1 <= uses <= 1000:
            raise Invalid("uses is 1 to 1000", param="uses")
        OrgLimits.parse(limits)
        code = ids.new_secret("invite")
        expires = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None
        invite_id = ids.new("invite")
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO invites (id, digest, label, uses_left, limits, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                         invite_id, ids.digest(code, self.pepper), label[:200], uses, limits or {}, expires)
            await audit(tx, None, actor, "invite.create", invite_id, label=label, uses=uses)
        return {"id": invite_id, "object": "invite", "code": code, "label": label, "uses_left": uses,
                "expires_at": int(expires.timestamp()) if expires else None}

    async def redeem(self, code: str, org_name: str, owner_email: str) -> tuple[dict[str, Any], str]:
        async with self.db.platform() as tx:
            row = await tx.one("UPDATE invites SET uses_left = uses_left - 1 WHERE digest = %s AND uses_left > 0 "
                               "AND (expires_at IS NULL OR expires_at > now()) RETURNING id, limits",
                               ids.digest(code or "", self.pepper))
        if not row:
            raise Forbidden("the invite code is unknown, used up or expired", code="invalid_invite")
        try:
            return await self.create_org(org_name, owner_email, row["limits"], f"invite:{row['id']}")
        except Exception:
            async with self.db.platform() as tx:   # give the use back: a failed sign-up does not burn the code
                await tx.run("UPDATE invites SET uses_left = uses_left + 1 WHERE id = %s", row["id"])
            raise

    # keys ─────────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _mint(self, tx: Tx, org_id: str, member_id: str, name: str, role: str, expires: datetime | None) -> str:
        secret = ids.new_secret("key")
        await tx.run("INSERT INTO api_keys (id, org_id, member_id, name, role, digest, hint, expires_at) "
                     "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)", ids.new("key"), org_id, member_id, name[:100], role,
                     ids.digest(secret, self.pepper), secret[-4:], expires)
        return secret

    async def authenticate(self, secret: str) -> Principal:
        if not secret.startswith(ids.SECRET_PREFIXES["key"]):
            raise Unauthenticated("a valid API key is required (Authorization: Bearer sk-hg-...)")
        async with self.db.platform() as tx:
            row = await tx.one("SELECT k.id, k.org_id, k.member_id, k.role FROM api_keys k JOIN orgs o ON o.id = k.org_id "
                               "WHERE k.digest = %s AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now()) "
                               "AND o.status = 'active'", ids.digest(secret, self.pepper))
            if row and time.monotonic() - self._touched.get(row["id"], 0) > KEY_TOUCH_S:
                self._touched[row["id"]] = time.monotonic()
                await tx.run("UPDATE api_keys SET last_used_at = now() WHERE id = %s", row["id"])
        if not row:
            raise Unauthenticated("the API key is unknown, revoked or expired, or its organization is suspended")
        return Principal(row["org_id"], row["member_id"], row["id"], row["role"])

    async def mint_key(self, p: Principal, name: str, role: str, member_id: str | None, ttl_days: int | None
                       ) -> dict[str, Any]:
        p.require_admin()
        if role not in ("admin", "member"):
            raise Invalid("role is admin or member", param="role")
        if ttl_days is not None and not 1 <= ttl_days <= 3650:
            raise Invalid("ttl_days is 1 to 3650", param="ttl_days")
        expires = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None
        async with self.db.tenant(p.org_id) as tx:
            owner = member_id or p.member_id
            if not await tx.one("SELECT 1 FROM members WHERE id = %s", owner):
                raise NotFound(f"no member {owner}")
            secret = await self._mint(tx, p.org_id, owner, name or "key", role, expires)
            row = await tx.one("SELECT * FROM api_keys WHERE digest = %s", ids.digest(secret, self.pepper))
            await audit(tx, p.org_id, p.actor, "key.create", row["id"], role=role)
        return {**_key_view(row), "key": secret}

    async def list_keys(self, p: Principal) -> list[dict[str, Any]]:
        p.require_admin()
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all("SELECT * FROM api_keys WHERE revoked_at IS NULL ORDER BY created_at")
        return [_key_view(r) for r in rows]

    async def revoke_key(self, p: Principal, key_id: str) -> None:
        p.require_admin()
        async with self.db.tenant(p.org_id) as tx:
            n = await tx.run("UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                             ids.check(key_id, "key"))
            if not n:
                raise NotFound(f"no key {key_id}")
            await audit(tx, p.org_id, p.actor, "key.revoke", key_id)

    # members ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def add_member(self, p: Principal, email: str, role: str) -> dict[str, Any]:
        p.require_admin()
        if role not in ("admin", "member"):
            raise Invalid("role is admin or member", param="role")
        member_id = ids.new("member")
        async with self.db.tenant(p.org_id) as tx:
            if await tx.one("SELECT 1 FROM members WHERE email = %s", _email(email)):
                raise Conflict(f"{email} is a member already")
            await tx.run("INSERT INTO members (id, org_id, email, role) VALUES (%s, %s, %s, %s)",
                         member_id, p.org_id, _email(email), role)
            await audit(tx, p.org_id, p.actor, "member.add", member_id, email=email, role=role)
        return {"id": member_id, "object": "member", "email": _email(email), "role": role}

    async def list_members(self, p: Principal) -> list[dict[str, Any]]:
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all("SELECT id, email, role, created_at FROM members ORDER BY created_at")
        return [{"id": r["id"], "object": "member", "email": r["email"], "role": r["role"],
                 "created_at": int(r["created_at"].timestamp())} for r in rows]

    async def remove_member(self, p: Principal, member_id: str) -> None:
        p.require_admin()
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT role FROM members WHERE id = %s", ids.check(member_id, "member"))
            if not row:
                raise NotFound(f"no member {member_id}")
            if row["role"] == "owner":
                raise Forbidden("the owner cannot be removed")
            await tx.run("DELETE FROM members WHERE id = %s", member_id)
            await audit(tx, p.org_id, p.actor, "member.remove", member_id)
