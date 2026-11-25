"""Providers, environments and harnesses: one table and one set of verbs, with their secrets sealed beside them.
Also what a harness is made from (its package) and made into (its install for an image)."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.change import Change
from harnessgateway.domain.line import NONE, Line
from harnessgateway.domain.package import read_archive, write_archive
from harnessgateway.domain.resolve import Resolved, resolve
from harnessgateway.domain.resources import KINDS, SINGULAR, VALUE_MAX, Environment, Harness, Provider
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import check_name, new_id
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Connection, Database, Tell

LOG_MAX = 64 * 1024
_ROW = "kind, name, spec, sealed, created_at, updated_at"
_UPSERT = ("INSERT INTO resources (org_id, kind, name, spec, sealed) VALUES (%s, %s, %s, %s, %s) "
           "ON CONFLICT (org_id, kind, name) DO UPDATE SET spec = EXCLUDED.spec, sealed = EXCLUDED.sealed, "
           "updated_at = now()")


@dataclass(frozen=True)
class Resource:
    kind: str
    name: str
    spec: dict[str, Any]
    sealed: dict[str, str]                     # secret name -> sealed value
    created_at: datetime
    updated_at: datetime

    def shown(self) -> dict[str, Any]:
        """The resource as the API shows it: its definition in full, and which secrets have a value. Never the
        values."""
        return {"name": self.name, **KINDS[self.kind].load(self.spec).dump(), "secrets_set": sorted(self.sealed),
                "created_at": self.created_at.isoformat(), "updated_at": self.updated_at.isoformat()}


@dataclass(frozen=True)
class Install:
    """A package installed, or being installed, for an image, by whoever holds it."""

    id: str
    ready: bool
    _conn: Connection

    async def end(self, *, ready: bool, log: str) -> None:
        """Record how an attempt to install it ended."""
        await self._conn.execute("UPDATE installs SET status = %s, log = %s, updated_at = now() WHERE id = %s",
                                 ["ready" if ready else "failed", log[-LOG_MAX:], self.id])


def package_key(org_id: str, digest: str) -> str:
    return f"orgs/{org_id}/packages/{digest}"


def _split(kind: str, sent: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """A document as it was sent, as its definition and the secret values it carried (None: remove that one).
    A provider's is `key`; an environment's are `secrets.<NAME>.value`."""
    fields, values = dict(sent), {}
    if kind == "providers" and "key" in fields:
        values = {"key": fields.pop("key")}
    elif kind == "environments" and isinstance(fields.get("secrets"), dict):
        fields["secrets"] = {name: dict(secret) if isinstance(secret, dict) else secret
                             for name, secret in fields["secrets"].items()}
        values = {name: secret.pop("value") for name, secret in fields["secrets"].items()
                  if isinstance(secret, dict) and "value" in secret}
    if any(value is not None and not (isinstance(value, str) and 0 < len(value) <= VALUE_MAX)
           for value in values.values()):
        raise Invalid(f"a secret's value is text of 1 to {VALUE_MAX} characters, or null to remove it", param="secrets")
    return fields, values


class Resources:
    def __init__(self, db: Database, blobs: BlobStore, sealer: Sealer, tell: Tell) -> None:
        self._db = db
        self._blobs = blobs
        self._sealer = sealer
        self._tell = tell

    async def find(self, org_id: str, kind: str, name: str) -> Resource | None:
        row = await self._db.one(org_id, f"SELECT {_ROW} FROM resources WHERE org_id = %s AND kind = %s AND name = %s",
                                 [org_id, kind, name])
        return Resource(**row) if row else None

    async def get(self, org_id: str, kind: str, name: str) -> Resource:
        found = await self.find(org_id, kind, name)
        if found is None:
            raise NotFound(f"there is no {SINGULAR[kind]} named {name!r}")
        return found

    async def all(self, org_id: str, kind: str) -> list[Resource]:
        rows = await self._db.all(org_id, f"SELECT {_ROW} FROM resources WHERE org_id = %s AND kind = %s ORDER BY name",
                                  [org_id, kind])
        return [Resource(**row) for row in rows]

    async def apply(self, org_id: str, kind: str, name: str, sent: Mapping[str, Any], *, by: str) -> Resource:
        """Make a definition, or replace the one of that name. A secret that is not sent keeps its value; null
        removes it. A harness is not made this way: it is its package."""
        check_name(name, "name")
        if kind == "harnesses":
            raise Invalid("a harness is its package: send the archive, or a session and the folder in its work")
        if kind == "providers" and name == NONE:
            raise Invalid(f"{NONE} is not a provider's name: in a model line it means no provider", param="name")
        fields, values = _split(kind, sent)
        async with self._db.tx(org_id) as conn:
            row = await (await conn.execute(
                "SELECT spec, sealed FROM resources WHERE org_id = %s AND kind = %s AND name = %s FOR UPDATE",
                [org_id, kind, name])).fetchone()
            definition = KINDS[kind].load(fields)
            declared = definition.secret_names()
            sealed = {secret: value for secret, value in (row["sealed"] if row else {}).items()
                      if secret in declared and secret not in values}
            for secret, value in values.items():
                if secret not in declared:
                    raise Invalid(f"{secret} is not a secret this {SINGULAR[kind]} declares", param=secret)
                if value is not None:
                    sealed[secret] = self._sealer.seal(value, org_id, kind, name, secret)
            if isinstance(definition, Environment):
                if unset := sorted(set(declared) - set(sealed)):
                    raise Invalid(f"secrets.{unset[0]}.value is required", param=f"secrets.{unset[0]}.value")
                have = await (await conn.execute(
                    "SELECT name FROM resources WHERE org_id = %s AND kind = 'harnesses' AND name = ANY(%s)",
                    [org_id, list(definition.harnesses)])).fetchall()
                if unknown := sorted(set(definition.harnesses) - {found["name"] for found in have}):
                    raise Invalid(f"harnesses: there is no harness named {unknown[0]!r}", param="harnesses")
            await conn.execute(_UPSERT, [org_id, kind, name, Jsonb(definition.dump()), Jsonb(sealed)])
            await self._tell(conn, Change(f"{kind}.applied", org_id, by, name))
        return await self.get(org_id, kind, name)

    async def add_package(self, org_id: str, name: str, archive: bytes, *, by: str) -> Resource:
        """Add a package as a harness, or replace the harness of that name with it. The package is checked in
        full first, and kept as its files alone, written one way: not as the archive that happened to carry
        them."""
        check_name(name, "name")
        package = read_archive(archive)
        archive = write_archive(package.files)
        await self._blobs.put(package_key(org_id, package.digest), archive)
        async with self._db.tx(org_id) as conn:
            await conn.execute("INSERT INTO packages (org_id, digest, bytes) VALUES (%s, %s, %s) "
                               "ON CONFLICT DO NOTHING", [org_id, package.digest, len(archive)])
            await conn.execute(_UPSERT, [org_id, "harnesses", name, Jsonb(Harness(digest=package.digest).dump()),
                                         Jsonb({})])
            await self._tell(conn, Change("harnesses.applied", org_id, by, name))
        return await self.get(org_id, "harnesses", name)

    async def delete(self, org_id: str, kind: str, name: str, *, by: str) -> None:
        """Remove a definition. A harness is also taken off every environment that carries it."""
        async with self._db.tx(org_id) as conn:
            gone = await conn.execute("DELETE FROM resources WHERE org_id = %s AND kind = %s AND name = %s",
                                      [org_id, kind, name])
            if gone.rowcount == 0:
                raise NotFound(f"there is no {SINGULAR[kind]} named {name!r}")
            if kind == "harnesses":
                await conn.execute(
                    "UPDATE resources SET spec = jsonb_set(spec, '{harnesses}', (spec -> 'harnesses') - %s::text), "
                    "updated_at = now() WHERE org_id = %s AND kind = 'environments' AND spec -> 'harnesses' ? %s",
                    [name, org_id, name])
            await self._tell(conn, Change(f"{kind}.deleted", org_id, by, name))

    async def secrets(self, org_id: str, kind: str, name: str) -> dict[str, str]:
        """A definition's secret values, opened. Only what the egress proxy is to be told ever asks."""
        found = await self.get(org_id, kind, name)
        return {secret: self._sealer.open(value, org_id, kind, name, secret) for secret, value in found.sealed.items()}

    async def package(self, org_id: str, digest: str) -> bytes:
        return await self._blobs.get(package_key(org_id, digest))

    async def resolve(self, org_id: str, line: Line) -> Resolved:
        """What a model line means for this organization, as its definitions stand now."""
        environment = await self.find(org_id, "environments", line.environment)
        harness = await self.find(org_id, "harnesses", line.harness)
        provider = await self.find(org_id, "providers", line.provider) if line.provider != NONE else None
        return resolve(line, Environment.load(environment.spec) if environment else None,
                       Harness.load(harness.spec) if harness else None,
                       Provider.load(provider.spec) if provider else None)

    # installs: a package installed for an image

    @asynccontextmanager
    async def installing(self, org_id: str, digest: str, image_id: str) -> AsyncIterator["Install"]:
        """The install of a package for an image (the one there is, or a new one), held for as long as the block
        lasts: no other turn, in this process or another, installs the same thing meanwhile."""
        async with self._db.tx(org_id) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                               [f"{org_id}/{digest}/{image_id}"])
            await conn.execute(
                "INSERT INTO installs (id, org_id, digest, image_id, status) VALUES (%s, %s, %s, %s, 'pending') "
                "ON CONFLICT (org_id, digest, image_id) DO NOTHING", [new_id("install"), org_id, digest, image_id])
            row = await (await conn.execute(
                "SELECT id, status FROM installs WHERE org_id = %s AND digest = %s AND image_id = %s",
                [org_id, digest, image_id])).fetchone()
            assert row is not None
            yield Install(row["id"], row["status"] == "ready", conn)

    async def unused_installs(self) -> list[str]:
        """Forget the packages no harness has been made from for an hour. The ids of their installs, whose
        folders the caller removes."""
        async with self._db.tx() as conn:
            gone = await (await conn.execute(
                "DELETE FROM packages p WHERE NOT EXISTS (SELECT 1 FROM resources r WHERE r.org_id = p.org_id "
                "AND r.kind = 'harnesses' AND r.spec ->> 'digest' = p.digest) "
                "AND p.created_at < now() - interval '1 hour' RETURNING org_id, digest")).fetchall()
            installs = []
            for row in gone:
                installs += await (await conn.execute("DELETE FROM installs WHERE org_id = %s AND digest = %s "
                                                      "RETURNING id", [row["org_id"], row["digest"]])).fetchall()
        for row in gone:
            await self._blobs.delete(package_key(row["org_id"], row["digest"]))
        return [install["id"] for install in installs]
