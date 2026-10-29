"""API keys (SPEC §9): `hg_` + 40 characters, kept as an HMAC under the server's pepper; scope admin or run."""
from __future__ import annotations

import time
from typing import Any

from .. import ids
from ..db import Database
from ..errors import Invalid, NotFound
from .pages import ts
from .principal import Principal

SCOPES = ("admin", "run")
TOUCH_S = 60


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": row["id"], "object": "api_key", "label": row["label"], "scope": row["scope"], "hint": row["hint"],
            "created_at": ts(row["created_at"]), "last_used_at": ts(row["last_used_at"])}


class Keys:
    def __init__(self, db: Database, pepper: bytes):
        self.db, self.pepper = db, pepper
        self._touched: dict[str, float] = {}

    async def create(self, org_id: str, scope: str, label: str = "") -> dict[str, Any]:
        """A new key; the secret is in the result once and never again."""
        if scope not in SCOPES:
            raise Invalid("scope is admin or run", param="scope")
        secret = ids.new_secret("key")
        key_id = ids.new("key")
        async with self.db.tenant(org_id) as tx:
            await tx.run("INSERT INTO api_keys (id, org_id, digest, scope, label, hint) VALUES (%s, %s, %s, %s, %s, %s)",
                         key_id, org_id, ids.digest(secret, self.pepper), scope, str(label)[:100], secret[-4:])
            row = await tx.get("SELECT * FROM api_keys WHERE id = %s", key_id)
        return {**_view(row), "key": secret}

    async def list(self, org_id: str) -> list[dict[str, Any]]:
        async with self.db.tenant(org_id) as tx:
            return [_view(r) for r in await tx.all("SELECT * FROM api_keys WHERE revoked_at IS NULL ORDER BY created_at")]

    async def revoke(self, org_id: str, key_id: str) -> None:
        async with self.db.tenant(org_id) as tx:
            if not await tx.run("UPDATE api_keys SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
                                ids.check(key_id, "key")):
                raise NotFound(f"no key {key_id}")

    async def authenticate(self, secret: str) -> Principal | None:
        if not secret.startswith(ids.SECRET["key"]):
            return None
        async with self.db.platform() as tx:
            row = await tx.one("SELECT k.id, k.org_id, k.scope FROM api_keys k JOIN orgs o ON o.id = k.org_id "
                               "WHERE k.digest = %s AND k.revoked_at IS NULL AND o.status = 'active'",
                               ids.digest(secret, self.pepper))
            if row and time.monotonic() - self._touched.get(row["id"], 0) > TOUCH_S:
                self._touched[row["id"]] = time.monotonic()
                await tx.run("UPDATE api_keys SET last_used_at = now() WHERE id = %s", row["id"])
        return Principal(row["org_id"], row["scope"], f"key:{row['id']}") if row else None
