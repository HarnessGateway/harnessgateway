"""Harnesses: packages installed in an organization under a name, with its settings and secret values (SPEC §5, §14.2).

A package is content-addressed and stored once, whoever installs it. A harness points at the package it was installed
from; a session runs the package it started on, whatever the harness points at later. Packages arrive as uploads: an
archive of the folder an author wrote, in the console's editor or anywhere else."""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .. import ids
from ..db import Database
from ..domain import package as pkg
from ..domain.manifest import Manifest
from ..errors import Invalid, NotFound
from ..limits import SETTING_VALUE_MAX
from .objects import Objects
from .pages import ts
from .vault import Vault

log = logging.getLogger("harnessgateway.harnesses")

Files = list[dict[str, Any]]


def archive_key(digest: str) -> str:
    return f"packages/{digest}.tgz"


def secret_context(name: str, secret: str) -> str:
    return f"harness/{name}/secret/{secret}"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    m = Manifest.model_validate(row["manifest"])
    return {"id": row["id"], "object": "harness", "name": row["name"], "version": m.version,
            "description": m.description, "digest": row["digest"], "provider_apis": m.provider.apis if m.provider else [],
            "default_provider": row["default_provider"], "default_model": row["default_model"] or m.defaults.model,
            "persist": m.persist, "egress": m.run_entries(), "install_egress": m.install.egress,
            "settings": {k: {"description": s.description, "default": s.default, "required": s.required,
                             "set": k in row["settings"], "value": row["settings"].get(k)} for k, s in m.settings.items()},
            "secrets": {k: {"description": s.description, "hosts": s.hosts, "required": s.required,
                            "set": k in row["secrets"]} for k, s in m.secrets.items()},
            "mcp": {label: {"url": s.url, "allowed_tools": s.allowed_tools} for label, s in m.mcp.items()},
            "created_at": ts(row["created_at"]), "updated_at": ts(row["updated_at"])}


class Harnesses:
    def __init__(self, db: Database, objects: Objects, vault: Vault):
        self.db, self.objects, self.vault = db, objects, vault

    # ── publishing a package ───────────────────────────────────────────────────────────────────────────────────────
    async def publish(self, folder: Path, owner: str | None) -> tuple[str, Manifest]:
        manifest = await asyncio.to_thread(pkg.validate, folder)
        digest = await asyncio.to_thread(pkg.digest, folder)
        async with self.db.platform() as tx:
            known = await tx.one("SELECT 1 FROM packages WHERE digest = %s", digest)
        if not known:
            data = await asyncio.to_thread(pkg.pack, folder)
            await self.objects.put(archive_key(digest), data, org_id=owner, kind="package")
            async with self.db.platform() as tx:
                await tx.run("INSERT INTO packages (digest, name, version, manifest, object_key, bytes) VALUES "
                             "(%s, %s, %s, %s, %s, %s) ON CONFLICT (digest) DO NOTHING", digest, manifest.name,
                             manifest.version, manifest.model_dump(), archive_key(digest), len(data))
        return digest, manifest

    # ── installing ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def install(self, org_id: str, archive: bytes) -> dict[str, Any]:
        """Install or upgrade from a package archive. Settings and secret values the new version still declares are
        kept."""
        with tempfile.TemporaryDirectory(prefix="hg-upload-") as tmp:
            folder = Path(tmp) / "p"
            await asyncio.to_thread(pkg.unpack, archive, folder)
            digest, manifest = await self.publish(folder, org_id)
        async with self.db.tenant(org_id) as tx:
            current = await tx.one("SELECT settings, secrets FROM harnesses WHERE name = %s", manifest.name)
            settings = {k: v for k, v in ((current or {}).get("settings") or {}).items() if k in manifest.settings}
            secrets = {k: v for k, v in ((current or {}).get("secrets") or {}).items() if k in manifest.secrets}
            await tx.run("INSERT INTO harnesses (id, org_id, name, digest, manifest, settings, secrets) VALUES "
                         "(%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (org_id, name) DO UPDATE SET digest = EXCLUDED.digest, "
                         "manifest = EXCLUDED.manifest, settings = EXCLUDED.settings, secrets = EXCLUDED.secrets, "
                         "updated_at = now()", ids.new("harness"), org_id, manifest.name, digest, manifest.model_dump(),
                         settings, secrets)
        log.info("installed %s for %s (%s)", manifest.name, org_id, digest[:12])
        return await self.get(org_id, manifest.name)

    async def install_folder(self, org_id: str, folder: Path) -> dict[str, Any]:
        """Install or upgrade from a package folder on this host: the built-in harness package builder, at `init`."""
        def skip(d: str, names: list[str]) -> set[str]:          # the package's own tests, not its seeds' (the template's)
            return {n for n in names if n == "__pycache__" or (n == "tests" and Path(d) == folder)}
        with tempfile.TemporaryDirectory(prefix="hg-folder-") as tmp:
            copy = Path(tmp) / "p"
            shutil.copytree(folder, copy, ignore=skip)
            data = await asyncio.to_thread(pkg.pack, copy)
        return await self.install(org_id, data)

    # ── reading ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def row(self, org_id: str, name: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT * FROM harnesses WHERE name = %s", name)
        if not row:
            raise NotFound(f"no harness {name} installed", param="model", code="model_not_found")
        return row

    async def get(self, org_id: str, name: str) -> dict[str, Any]:
        return _view(await self.row(org_id, name))

    async def list(self, org_id: str) -> list[dict[str, Any]]:
        async with self.db.tenant(org_id) as tx:
            return [_view(r) for r in await tx.all("SELECT * FROM harnesses ORDER BY name")]

    # ── reading a package ──────────────────────────────────────────────────────────────────────────────────────────
    async def _unpacked(self, org_id: str, name: str) -> Path:
        """The installed package's archive unpacked into a fresh folder: what the console shows and edits."""
        row = await self.row(org_id, name)
        folder = Path(tempfile.mkdtemp(prefix="hg-read-")) / "p"
        await asyncio.to_thread(pkg.unpack, await self.objects.get(archive_key(row["digest"])), folder)
        return folder

    async def files(self, org_id: str, name: str) -> Files:
        """Every file of the installed package: path, size and executable bit."""
        folder = await self._unpacked(org_id, name)
        try:
            out = []
            for rel, path in pkg.entries(folder):
                st = path.stat()
                out.append({"path": rel, "bytes": st.st_size, "executable": bool(st.st_mode & 0o111)})
            return out
        finally:
            shutil.rmtree(folder.parent, ignore_errors=True)

    async def file(self, org_id: str, name: str, rel: str) -> bytes:
        clean = pkg.clean_member(rel)
        folder = await self._unpacked(org_id, name)
        try:
            p = folder / clean
            if not p.is_file():
                raise NotFound(f"{name} has no file {rel}", param="path")
            return p.read_bytes()
        finally:
            shutil.rmtree(folder.parent, ignore_errors=True)

    async def uninstall(self, org_id: str, name: str) -> None:
        async with self.db.tenant(org_id) as tx:
            if not await tx.run("DELETE FROM harnesses WHERE name = %s", name):
                raise NotFound(f"no harness {name}")

    # ── configuring ────────────────────────────────────────────────────────────────────────────────────────────────
    async def configure(self, org_id: str, name: str, *, settings: object = None, secrets: object = None,
                        default_provider: object = None, default_model: object = None) -> dict[str, Any]:
        """Each setting or secret given is set (a string) or cleared (null); the rest are kept. Secret values are
        sealed and never shown again."""
        row = await self.row(org_id, name)
        m = Manifest.model_validate(row["manifest"])
        current_settings, current_secrets = dict(row["settings"]), dict(row["secrets"])
        for what, given, declared in (("settings", settings, m.settings), ("secrets", secrets, m.secrets)):
            if given is None:
                continue
            if not isinstance(given, dict) or not all(v is None or isinstance(v, str) for v in given.values()):
                raise Invalid(f"{what} maps names to strings, or null to clear", param=what)
            unknown = sorted(set(given) - set(declared))
            if unknown:
                raise Invalid(f"{what} not declared by {name}: {', '.join(unknown)}", param=what)
            if any(v is not None and len(v) > SETTING_VALUE_MAX for v in given.values()):
                raise Invalid(f"a value is at most {SETTING_VALUE_MAX} characters", param=what)
        for k, v in (settings or {}).items():  # type: ignore[attr-defined]
            if v is None:
                current_settings.pop(k, None)
            else:
                current_settings[k] = v
        for k, v in (secrets or {}).items():  # type: ignore[attr-defined]
            if v is None:
                current_secrets.pop(k, None)
            else:
                current_secrets[k] = await self.vault.seal(org_id, secret_context(name, k), v)
        async with self.db.tenant(org_id) as tx:
            if default_provider:
                ids.check_name(default_provider, "provider")
                if not await tx.one("SELECT 1 FROM providers WHERE name = %s", default_provider):
                    raise NotFound(f"no provider {default_provider}", param="default_provider")
            if default_model is not None and (not isinstance(default_model, str) or len(default_model) > 200):
                raise Invalid("default_model is a model name of at most 200 characters", param="default_model")
            await tx.run("UPDATE harnesses SET settings = %s, secrets = %s, default_provider = coalesce(%s, default_provider), "
                         "default_model = coalesce(%s, default_model), updated_at = now() WHERE name = %s",
                         current_settings, current_secrets, default_provider, default_model, name)
        return await self.get(org_id, name)

    async def open_secrets(self, org_id: str, name: str, sealed: dict[str, str]) -> dict[str, str]:
        return {k: await self.vault.open(org_id, secret_context(name, k), v) for k, v in sealed.items()}
