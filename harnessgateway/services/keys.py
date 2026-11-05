"""API keys (SPEC §9.2): `hg_` + 40 characters, kept as an HMAC under the server's pepper. A key carries a role: an
organization key is `admin`; a project key is `developer`, and with `run_only` it only runs: what an application holds.
A key may expire; revocation is immediate."""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import ids
from ..db import Database
from ..domain.access import ADMIN, DEVELOPER, ROLES, Principal
from ..errors import Forbidden, Invalid, NotFound
from .audit import Audit
from .pages import ts

TOUCH_S = 60
EXPIRY_MAX_DAYS = 3650


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "api_key", "label": row["label"], "role": row["role"], "project": row["project_id"],
            "run_only": row["run_only"], "hint": row["hint"], "created_by": row["account_id"],
            "created_at": ts(row["created_at"]), "expires_at": ts(row["expires_at"]), "last_used_at": ts(row["last_used_at"])}


class Keys:
    def __init__(self, db: Database, pepper: bytes, audit: Audit):
        self.db, self.pepper, self.audit = db, pepper, audit
        self._touched: dict[str, float] = {}

    async def create(self, p: Principal, label: object, role: object, project: object, run_only: object,
                     expires_in_days: object) -> dict[str, Any]:
        """A new key; the secret is in the result once and never again. An admin creates any key; a developer creates
        developer keys of the project it addresses."""
        if role not in ROLES:
            raise Invalid(f"role is one of {', '.join(ROLES)}", param="role")
        if role == ADMIN and project:
            raise Invalid("an admin key is the organization's: it names no project", param="project")
        if role == DEVELOPER and not project:
            raise Invalid("a developer key belongs to a project", param="project")
        if run_only and role != DEVELOPER:
            raise Invalid("run_only is a developer key's flag", param="run_only")
        if expires_in_days is not None and (not isinstance(expires_in_days, int) or not 1 <= expires_in_days <= EXPIRY_MAX_DAYS):
            raise Invalid(f"expires_in_days is 1 to {EXPIRY_MAX_DAYS}", param="expires_in_days")
        if p.role != ADMIN and (role != DEVELOPER or project != p.project):
            raise Forbidden("a developer creates developer keys of its own project; an admin creates any key")
        async with self.db.tenant(p.org_id) as tx:
            if project and not await tx.one("SELECT 1 FROM projects WHERE id = %s", ids.check(project, "project")):
                raise NotFound(f"no project {project}", param="project")
            secret, key_id = ids.new_secret("key"), ids.new("key")
            expires = datetime.now(UTC) + timedelta(days=int(expires_in_days)) if expires_in_days else None
            await tx.run("INSERT INTO api_keys (id, org_id, project_id, account_id, digest, role, run_only, label, hint, "
                         "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", key_id, p.org_id, project or None,
                         p.account_id, ids.digest(secret, self.pepper), role, bool(run_only), str(label or "")[:100],
                         secret[-4:], expires)
            row = await tx.get("SELECT * FROM api_keys WHERE id = %s", key_id)
        await self.audit.write(p.org_id, p.actor, "key.create", key_id,
                               {"role": role, "project": project, "run_only": bool(run_only)})
        return {**_view(row), "key": secret}

    async def create_admin(self, org_id: str, label: str) -> dict[str, Any]:
        """The organization key `init` prints: for automation before any person has signed in."""
        return await self.create(Principal(org_id, "platform:init", ADMIN), label, ADMIN, None, False, None)

    async def list(self, p: Principal) -> list[dict[str, Any]]:
        """An admin's view is the organization's keys; a developer's, its project's."""
        async with self.db.tenant(p.org_id) as tx:
            rows = await tx.all("SELECT * FROM api_keys WHERE revoked_at IS NULL AND (%s::text IS NULL OR project_id = %s) "
                                "ORDER BY created_at", p.project, p.project)
        return [_view(r) for r in rows]

    async def revoke(self, p: Principal, key_id: str) -> None:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT project_id FROM api_keys WHERE id = %s AND revoked_at IS NULL", ids.check(key_id, "key"))
            if not row:
                raise NotFound(f"no key {key_id}")
            if p.role != ADMIN and row["project_id"] != p.project:
                raise Forbidden("a developer revokes keys of its own project; an admin revokes any key")
            await tx.run("UPDATE api_keys SET revoked_at = now() WHERE id = %s", key_id)
        await self.audit.write(p.org_id, p.actor, "key.revoke", key_id)

    async def authenticate(self, secret: str) -> Principal | None:
        if not secret.startswith(ids.SECRET["key"]):
            return None
        async with self.db.platform() as tx:
            row = await tx.one("SELECT k.* FROM api_keys k JOIN orgs o ON o.id = k.org_id WHERE k.digest = %s "
                               "AND k.revoked_at IS NULL AND (k.expires_at IS NULL OR k.expires_at > now()) "
                               "AND o.status = 'active'",
                               ids.digest(secret, self.pepper))
            if row and time.monotonic() - self._touched.get(row["id"], 0) > TOUCH_S:
                self._touched[row["id"]] = time.monotonic()
                await tx.run("UPDATE api_keys SET last_used_at = now() WHERE id = %s", row["id"])
        if not row:
            return None
        return Principal(row["org_id"], f"key:{row['id']}", row["role"], row["project_id"], run_only=row["run_only"],
                         name=row["label"])
