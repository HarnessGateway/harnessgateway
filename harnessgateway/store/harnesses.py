"""Harnesses: uploaded packages, what the organization has set on each, and their installs (spec §8.5, §14.2)."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb

from harnessgateway.domain.archive import Package, read_archive
from harnessgateway.domain.harness import Harness
from harnessgateway.domain.modelpath import MODEL_MAX
from harnessgateway.domain.names import check_name
from harnessgateway.domain.package import Manifest
from harnessgateway.domain.sealing import Sealer
from harnessgateway.errors import Invalid, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.blobs import BlobStore
from harnessgateway.store.db import Database
from harnessgateway.store.sealed import check_value, open_all, seal

LOG_TAIL = 64 * 1024
_CHANGEABLE = frozenset({"provider", "model", "settings", "secrets"})
_COLUMNS = "h.digest, h.provider, h.model, h.settings, h.secrets_sealed, h.created_at, p.manifest"
_INSTALL_COLUMNS = "id, digest, image_id, status, log_tail, updated_at"

InstallStatus = Literal["pending", "ready", "failed"]


@dataclass(frozen=True)
class Install:
    id: str
    digest: str
    image_id: str
    status: InstallStatus
    log_tail: str
    updated_at: datetime


def _kind(name: str) -> str:
    return f"harness:{name}"


def package_key(org_id: str, digest: str) -> str:
    return f"orgs/{org_id}/packages/{digest}"


def _harness(row: Mapping[str, Any]) -> Harness:
    return Harness(manifest=Manifest.load(row["manifest"]), digest=row["digest"], provider=row["provider"],
                   model=row["model"], settings_values=row["settings"], secrets_set=frozenset(row["secrets_sealed"]),
                   added_at=row["created_at"])


def _declared(changes: object, declared: Mapping[str, object], what: str) -> dict[str, object]:
    """Changes to settings or secrets, by name: each must be one the manifest declares."""
    if not isinstance(changes, dict):
        raise Invalid(f"{what} is an object of name to value, or null to unset", param=what)
    for name in changes:
        if name not in declared:
            known = ", ".join(sorted(declared)) or "none"
            raise Invalid(f"{what}.{name} is not declared by the package; declared: {known}", param=f"{what}.{name}")
    return changes


class Harnesses:
    def __init__(self, db: Database, blobs: BlobStore, sealer: Sealer) -> None:
        self._db = db
        self._blobs = blobs
        self._sealer = sealer

    async def add(self, org_id: str, archive: bytes) -> Harness:
        """Add a package as a harness, or replace the harness of the same name. The package is validated in
        full first. A replaced harness keeps its defaults, and each setting and secret the new manifest still
        declares."""
        package = read_archive(archive)
        manifest = package.manifest
        key = package_key(org_id, package.digest)
        await self._blobs.put(key, archive)
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "INSERT INTO packages (org_id, digest, manifest, object_key, bytes) VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT DO NOTHING", [org_id, package.digest, Jsonb(manifest.dump()), key, len(archive)])
            row = await (await conn.execute(
                "SELECT settings, secrets_sealed FROM harnesses WHERE org_id = %s AND name = %s FOR UPDATE",
                [org_id, manifest.name])).fetchone()
            if row is None:
                await conn.execute("INSERT INTO harnesses (org_id, name, digest) VALUES (%s, %s, %s)",
                                   [org_id, manifest.name, package.digest])
            else:
                settings = {n: v for n, v in row["settings"].items() if n in manifest.settings}
                secrets = {n: v for n, v in row["secrets_sealed"].items() if n in manifest.secrets}
                await conn.execute(
                    "UPDATE harnesses SET digest = %s, settings = %s, secrets_sealed = %s, updated_at = now() "
                    "WHERE org_id = %s AND name = %s",
                    [package.digest, Jsonb(settings), Jsonb(secrets), org_id, manifest.name])
        return await self.get(org_id, manifest.name)

    async def update(self, org_id: str, name: str, changes: Mapping[str, Any]) -> Harness:
        """Set the default provider and model, setting values and secret values. `null` unsets."""
        unknown = sorted(set(changes) - _CHANGEABLE)
        if unknown:
            raise Invalid(f"{unknown[0]} cannot be changed; what can: {', '.join(sorted(_CHANGEABLE))}")
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM harnesses h JOIN packages p USING (org_id, digest) "
                "WHERE h.org_id = %s AND h.name = %s FOR UPDATE OF h", [org_id, name])).fetchone()
            if row is None:
                raise NotFound(f"there is no harness named {name!r}")
            manifest = Manifest.load(row["manifest"])
            provider, model = row["provider"], row["model"]
            settings, secrets = dict(row["settings"]), dict(row["secrets_sealed"])
            if "provider" in changes:
                provider = None if changes["provider"] is None else check_name(changes["provider"], "provider")
            if "model" in changes:
                model = changes["model"]
                if model is not None and (not isinstance(model, str) or not 0 < len(model) <= MODEL_MAX):
                    raise Invalid(f"model is 1 to {MODEL_MAX} characters, or null", param="model")
            for setting, value in _declared(changes.get("settings", {}), manifest.settings, "settings").items():
                if value is None:
                    settings.pop(setting, None)
                else:
                    settings[setting] = check_value(value, f"settings.{setting}")
            for secret, value in _declared(changes.get("secrets", {}), manifest.secrets, "secrets").items():
                if value is None:
                    secrets.pop(secret, None)
                else:
                    secrets[secret] = seal(self._sealer, check_value(value, f"secrets.{secret}"), org=org_id,
                                           kind=_kind(name), name=secret)
            await conn.execute(
                "UPDATE harnesses SET provider = %s, model = %s, settings = %s, secrets_sealed = %s, "
                "updated_at = now() WHERE org_id = %s AND name = %s",
                [provider, model, Jsonb(settings), Jsonb(secrets), org_id, name])
        return await self.get(org_id, name)

    async def delete(self, org_id: str, name: str) -> None:
        async with self._db.org(org_id) as conn:
            cursor = await conn.execute("DELETE FROM harnesses WHERE org_id = %s AND name = %s", [org_id, name])
        if cursor.rowcount == 0:
            raise NotFound(f"there is no harness named {name!r}")

    async def get(self, org_id: str, name: str) -> Harness:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM harnesses h JOIN packages p USING (org_id, digest) "
                "WHERE h.org_id = %s AND h.name = %s", [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no harness named {name!r}")
        return _harness(row)

    async def package(self, org_id: str, name: str) -> Package:
        """The package a harness was added from, as it was uploaded."""
        harness = await self.get(org_id, name)
        return read_archive(await self._blobs.get(package_key(org_id, harness.digest)))

    async def all(self, org_id: str) -> list[Harness]:
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM harnesses h JOIN packages p USING (org_id, digest) "
                "WHERE h.org_id = %s ORDER BY h.name", [org_id])).fetchall()
        return [_harness(row) for row in rows]

    async def secret_values(self, org_id: str, name: str) -> dict[str, str]:
        """The harness's secret values, opened. Only the egress proxy's policy ever asks."""
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT secrets_sealed FROM harnesses WHERE org_id = %s AND name = %s", [org_id, name])).fetchone()
        if row is None:
            raise NotFound(f"there is no harness named {name!r}")
        return open_all(self._sealer, row["secrets_sealed"], org=org_id, kind=_kind(name))

    # installs

    async def install_for(self, org_id: str, digest: str, image_id: str) -> Install:
        """The install of a package for an image: the one there is, or a new one, pending."""
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "INSERT INTO installs (id, org_id, digest, image_id, status) VALUES (%s, %s, %s, %s, 'pending') "
                "ON CONFLICT (org_id, digest, image_id) DO NOTHING", [new_id("install"), org_id, digest, image_id])
            row = await (await conn.execute(
                f"SELECT {_INSTALL_COLUMNS} FROM installs WHERE org_id = %s AND digest = %s AND image_id = %s",
                [org_id, digest, image_id])).fetchone()
        assert row is not None
        return Install(**row)

    async def install(self, org_id: str, install_id: str) -> Install:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_INSTALL_COLUMNS} FROM installs WHERE org_id = %s AND id = %s",
                [org_id, install_id])).fetchone()
        if row is None:
            raise NotFound(f"there is no install {install_id}")
        return Install(**row)

    async def installs(self, org_id: str, digest: str) -> list[Install]:
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                f"SELECT {_INSTALL_COLUMNS} FROM installs WHERE org_id = %s AND digest = %s ORDER BY created_at",
                [org_id, digest])).fetchall()
        return [Install(**row) for row in rows]

    async def begin_install(self, org_id: str, install_id: str, *, environment: str, step_token_hash: str,
                            proxy_token_hash: str) -> None:
        """An attempt starts: the install is pending, runs with this environment, and its tokens are these."""
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "UPDATE installs SET status = 'pending', log_tail = '', environment = %s, step_token_hash = %s, "
                "proxy_token_hash = %s, updated_at = now() WHERE org_id = %s AND id = %s",
                [environment, step_token_hash, proxy_token_hash, org_id, install_id])

    async def end_install(self, org_id: str, install_id: str, *, ready: bool, log: str) -> None:
        """An attempt is over: the install is ready or failed, its log is kept, and its tokens are dead."""
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "UPDATE installs SET status = %s, log_tail = %s, step_token_hash = NULL, proxy_token_hash = NULL, "
                "updated_at = now() WHERE org_id = %s AND id = %s",
                ["ready" if ready else "failed", log[-LOG_TAIL:], org_id, install_id])

    async def install_of_token(self, token_hash: str) -> tuple[str, str, str] | None:
        """Whose install a step token belongs to: organization, install id and the key of its package archive."""
        async with self._db.system() as conn:
            row = await (await conn.execute(
                "SELECT i.org_id, i.id, p.object_key FROM installs i JOIN packages p USING (org_id, digest) "
                "WHERE i.step_token_hash = %s", [token_hash])).fetchone()
        return (row["org_id"], row["id"], row["object_key"]) if row else None

    async def install_of_proxy_token(self, install_id: str, token_hash: str) -> tuple[str, str, Manifest] | None:
        """The install a proxy token belongs to, while it runs: organization, environment name, and manifest."""
        async with self._db.system() as conn:
            row = await (await conn.execute(
                "SELECT i.org_id, i.environment, p.manifest FROM installs i JOIN packages p USING (org_id, digest) "
                "WHERE i.id = %s AND i.proxy_token_hash = %s", [install_id, token_hash])).fetchone()
        return (row["org_id"], row["environment"], Manifest.load(row["manifest"])) if row else None

    async def unused_packages(self) -> list[tuple[str, str, str, list[str]]]:
        """Uploaded packages no harness uses, each as (organization, digest, archive key, its installs' ids)."""
        async with self._db.system() as conn:
            rows = await (await conn.execute(
                "SELECT p.org_id, p.digest, p.object_key, "
                "COALESCE(array_agg(i.id) FILTER (WHERE i.id IS NOT NULL), '{}') AS installs "
                "FROM packages p LEFT JOIN installs i ON i.org_id = p.org_id AND i.digest = p.digest "
                "WHERE NOT EXISTS (SELECT 1 FROM harnesses h WHERE h.org_id = p.org_id AND h.digest = p.digest) "
                "AND NOT EXISTS (SELECT 1 FROM installs b WHERE b.org_id = p.org_id AND b.digest = p.digest "
                "                AND b.status = 'pending') GROUP BY p.org_id, p.digest, p.object_key")).fetchall()
        return [(row["org_id"], row["digest"], row["object_key"], list(row["installs"])) for row in rows]

    async def remove_package(self, org_id: str, digest: str, object_key: str) -> None:
        """Forget an uploaded package and its installs. The caller removes the installs' volumes."""
        async with self._db.org(org_id) as conn:
            await conn.execute("DELETE FROM installs WHERE org_id = %s AND digest = %s", [org_id, digest])
            await conn.execute("DELETE FROM packages WHERE org_id = %s AND digest = %s", [org_id, digest])
        await self._blobs.delete(object_key)
