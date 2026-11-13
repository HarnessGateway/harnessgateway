"""Environments and their secrets (spec §9, §14.3)."""

from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.environment import Environment
from harnessgateway.domain.names import DEFAULT_ENVIRONMENT
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.store.db import Database
from harnessgateway.store.sealed import check_value, open_all, seal


def _kind(name: str) -> str:
    return f"environment:{name}"


def _split(body: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, object]]:
    """The definition without its secret values, and the values that were given, by secret name."""
    fields = dict(body)
    secrets = fields.get("secrets")
    values: dict[str, object] = {}
    if isinstance(secrets, dict):
        fields["secrets"] = {}
        for name, secret in secrets.items():
            if isinstance(secret, dict) and "value" in secret:
                secret = dict(secret)
                values[name] = secret.pop("value")
            fields["secrets"][name] = secret
    return fields, values


class Environments:
    def __init__(self, db: Database, sealer: Sealer, default_image: str) -> None:
        self._db = db
        self._sealer = sealer
        self._default_image = default_image

    def _sealed(self, org_id: str, environment: Environment, values: Mapping[str, object],
                kept: Mapping[str, str]) -> dict[str, str]:
        """The sealed value of each of the environment's secrets: the one given, else the one it had."""
        sealed = {}
        for name in environment.secrets:
            if name in values:
                value = check_value(values[name], f"secrets.{name}.value")
                sealed[name] = seal(self._sealer, value, org=org_id, kind=_kind(environment.name), name=name)
            elif name in kept:
                sealed[name] = kept[name]
            else:
                raise Invalid(f"secrets.{name}.value is required", param=f"secrets.{name}.value")
        return sealed

    async def create(self, org_id: str, body: Mapping[str, Any]) -> Environment:
        fields, values = _split(body)
        environment = Environment.load(fields, default_image=self._default_image)
        sealed = self._sealed(org_id, environment, values, {})
        async with self._db.org(org_id) as conn:
            cursor = await conn.execute(
                "INSERT INTO environments (org_id, name, spec, secrets_sealed) VALUES (%s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING", [org_id, environment.name, Jsonb(environment.dump()), Jsonb(sealed)])
        if cursor.rowcount == 0:
            raise Conflict(f"an environment named {environment.name!r} already exists")
        return environment

    async def ensure_default(self, org_id: str) -> None:
        """Make the environment named `default`, with the defaults, unless there is one."""
        with suppress(Conflict):
            await self.create(org_id, {"name": DEFAULT_ENVIRONMENT})

    async def update(self, org_id: str, name: str, changes: Mapping[str, Any]) -> Environment:
        """Replace the fields named. A secret listed without a value keeps the value it had."""
        if changes.get("name", name) != name:
            raise Invalid("an environment's name cannot be changed", param="name")
        fields, values = _split(changes)
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT spec, secrets_sealed FROM environments WHERE org_id = %s AND name = %s FOR UPDATE",
                [org_id, name])).fetchone()
            if row is None:
                raise NotFound(f"there is no environment named {name!r}")
            environment = Environment.load({**row["spec"], **fields}, default_image=self._default_image)
            sealed = self._sealed(org_id, environment, values, row["secrets_sealed"])
            await conn.execute(
                "UPDATE environments SET spec = %s, secrets_sealed = %s, updated_at = now() "
                "WHERE org_id = %s AND name = %s", [Jsonb(environment.dump()), Jsonb(sealed), org_id, name])
        return environment

    async def delete(self, org_id: str, name: str) -> None:
        if name == DEFAULT_ENVIRONMENT:
            raise Invalid(f"the environment named {DEFAULT_ENVIRONMENT!r} can be changed but not deleted")
        async with self._db.org(org_id) as conn:
            cursor = await conn.execute("DELETE FROM environments WHERE org_id = %s AND name = %s", [org_id, name])
        if cursor.rowcount == 0:
            raise NotFound(f"there is no environment named {name!r}")

    async def get(self, org_id: str, name: str) -> Environment:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT spec FROM environments WHERE org_id = %s AND name = %s", [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no environment named {name!r}")
        return Environment.load(row["spec"], default_image=self._default_image)

    async def all(self, org_id: str) -> list[Environment]:
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                "SELECT spec FROM environments WHERE org_id = %s ORDER BY name", [org_id])).fetchall()
        return [Environment.load(row["spec"], default_image=self._default_image) for row in rows]

    async def secret_values(self, org_id: str, name: str) -> dict[str, str]:
        """The environment's secret values, opened. Only the egress proxy's policy ever asks."""
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT secrets_sealed FROM environments WHERE org_id = %s AND name = %s", [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no environment named {name!r}")
        return open_all(self._sealer, row["secrets_sealed"], org=org_id, kind=_kind(name))
