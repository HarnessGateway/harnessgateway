"""Builds: an environment's setup script run on a base image, and the image it made (spec §9.4)."""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb

from harnessgateway.errors import NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.db import Database

LOG_TAIL = 64 * 1024
_COLUMNS = "id, base_image_id, script_digest, status, image_id, log_tail, updated_at"

BuildStatus = Literal["pending", "ready", "failed"]


@dataclass(frozen=True)
class Build:
    id: str
    base_image_id: str
    script_digest: str
    status: BuildStatus
    image_id: str | None
    log_tail: str
    updated_at: datetime


def script_digest(script: str) -> str:
    return hashlib.sha256(script.encode()).hexdigest()


class Builds:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def build_for(self, org_id: str, base_image_id: str, script: str) -> Build:
        """The build of a script on a base image: the one there is, or a new one, pending."""
        digest = script_digest(script)
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "INSERT INTO builds (id, org_id, base_image_id, script_digest, status) "
                "VALUES (%s, %s, %s, %s, 'pending') ON CONFLICT (org_id, base_image_id, script_digest) DO NOTHING",
                [new_id("build"), org_id, base_image_id, digest])
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM builds WHERE org_id = %s AND base_image_id = %s AND script_digest = %s",
                [org_id, base_image_id, digest])).fetchone()
        assert row is not None
        return Build(**row)

    async def get(self, org_id: str, build_id: str) -> Build:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(f"SELECT {_COLUMNS} FROM builds WHERE org_id = %s AND id = %s",
                                            [org_id, build_id])).fetchone()
        if row is None:
            raise NotFound(f"there is no build {build_id}")
        return Build(**row)

    async def begin(self, org_id: str, build_id: str, *, environment: str, proxy_token_hash: str,
                    placeholders: Mapping[str, Any]) -> None:
        """An attempt starts: the build is pending, runs for this environment, and its proxy token is this."""
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "UPDATE builds SET status = 'pending', image_id = NULL, log_tail = '', environment = %s, "
                "proxy_token_hash = %s, placeholders = %s, updated_at = now() WHERE org_id = %s AND id = %s",
                [environment, proxy_token_hash, Jsonb(dict(placeholders)), org_id, build_id])

    async def end(self, org_id: str, build_id: str, *, image_id: str | None, log: str) -> None:
        """An attempt is over: ready with the image it made, or failed. Its log is kept; its token is dead."""
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "UPDATE builds SET status = %s, image_id = %s, log_tail = %s, proxy_token_hash = NULL, "
                "placeholders = '{}', updated_at = now() WHERE org_id = %s AND id = %s",
                ["ready" if image_id else "failed", image_id, log[-LOG_TAIL:], org_id, build_id])

    async def of_proxy_token(self, build_id: str, token_hash: str) -> tuple[str, str, dict[str, Any]] | None:
        """The build a proxy token belongs to, while it runs: organization, environment and placeholders."""
        async with self._db.system() as conn:
            row = await (await conn.execute(
                "SELECT org_id, environment, placeholders FROM builds WHERE id = %s AND proxy_token_hash = %s",
                [build_id, token_hash])).fetchone()
        return (row["org_id"], row["environment"], row["placeholders"]) if row else None

    async def unused(self, *, grace_s: float) -> list[tuple[str, str, str | None]]:
        """Builds no environment is prepared as, not running and untouched for `grace_s`, as (organization, id,
        image)."""
        async with self._db.system() as conn:
            rows = await (await conn.execute(
                "SELECT b.org_id, b.id, b.image_id FROM builds b WHERE b.status <> 'pending' "
                "AND b.updated_at < now() - make_interval(secs => %s) AND NOT EXISTS ("
                "  SELECT 1 FROM environments e WHERE e.org_id = b.org_id AND e.build_id = b.id)",
                [grace_s])).fetchall()
        return [(row["org_id"], row["id"], row["image_id"]) for row in rows]

    async def remove(self, org_id: str, build_id: str) -> None:
        async with self._db.org(org_id) as conn:
            await conn.execute("DELETE FROM builds WHERE org_id = %s AND id = %s", [org_id, build_id])
