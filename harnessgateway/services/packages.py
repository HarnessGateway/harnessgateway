"""Harness packages an organization can run. A package version is content-addressed (its digest) and stored once,
shared by every organization that installs the same files; an install is an organization's pointer to one digest,
plus its settings and defaults. Packages come from the platform's built-in catalog, a public git repository (the free
marketplace is just git URLs) or a private upload. A session runs the digest it started on."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .. import ids
from ..blobs import Blobs
from ..crypto import Sealer
from ..db import Database
from ..domain import package as pkg
from ..domain.package import Manifest
from ..errors import Invalid, NotFound
from ..netguard import check_url
from .principal import Principal
from .tenancy import audit

GIT_TIMEOUT_S = 120
log = logging.getLogger("harnessgateway.packages")


def settings_context(org_id: str, name: str, key: str) -> str:
    return f"install:{org_id}:{name}:{key}"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    m = row["manifest"]
    settings = row["settings"] or {}
    return {"id": row["id"], "object": "harness", "name": row["name"], "version": m["version"], "digest": row["digest"],
            "description": m.get("description", ""), "provider_apis": (m.get("provider") or {}).get("apis", []),
            "source": row["source"], "default_provider": row["default_provider"],
            "default_model": row["default_model"] or (m.get("defaults") or {}).get("model", ""),
            "settings": {k: {"set": k in settings, "secret": spec.get("secret", False), "required": spec.get("required", False),
                             "description": spec.get("description", "")}
                         for k, spec in (m.get("settings") or {}).items()},
            "created_at": int(row["created_at"].timestamp()), "updated_at": int(row["updated_at"].timestamp())}


class Packages:
    def __init__(self, db: Database, blobs: Blobs, sealer: Sealer, builtin_dir: Path, *, allow_private: bool):
        self.db, self.blobs, self.sealer, self.builtin_dir = db, blobs, sealer, builtin_dir
        self.allow_private = allow_private
        self.builtins: dict[str, str] = {}                 # name -> digest

    # publishing (platform) ────────────────────────────────────────────────────────────────────────────────────────
    async def publish(self, folder: Path) -> tuple[str, Manifest]:
        manifest = await asyncio.to_thread(pkg.validate, folder)
        digest = await asyncio.to_thread(pkg.digest, folder)
        async with self.db.platform() as tx:
            known = await tx.one("SELECT 1 FROM package_blobs WHERE digest = %s", digest)
        if not known:
            data = await asyncio.to_thread(pkg.pack, folder)
            key = f"packages/{digest}.tgz"
            await self.blobs.put(key, data, org_id=None, kind="package")
            async with self.db.platform() as tx:
                await tx.run("INSERT INTO package_blobs (digest, name, version, manifest, object_key, bytes) "
                             "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (digest) DO NOTHING",
                             digest, manifest.name, manifest.version, manifest.model_dump(), key, len(data))
        return digest, manifest

    async def publish_builtins(self) -> dict[str, str]:
        """Every valid package in the built-in directory. One that does not validate is logged and skipped: a broken
        built-in must not keep the platform from starting."""
        if self.builtin_dir.is_dir():
            for folder in sorted(p for p in self.builtin_dir.iterdir() if (p / "harness.json").is_file()):
                try:
                    digest, manifest = await self.publish(folder)
                except Exception as e:
                    log.error("built-in package %s is not valid and was skipped: %s", folder.name, e)
                    continue
                self.builtins[manifest.name] = digest
        return dict(self.builtins)

    async def catalog(self) -> list[dict[str, Any]]:
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT digest, manifest FROM package_blobs WHERE digest = ANY(%s)", list(self.builtins.values()))
        return sorted(({"name": r["manifest"]["name"], "version": r["manifest"]["version"], "digest": r["digest"],
                        "description": r["manifest"].get("description", ""),
                        "provider_apis": (r["manifest"].get("provider") or {}).get("apis", [])} for r in rows),
                      key=lambda x: x["name"])

    # installing (organization) ────────────────────────────────────────────────────────────────────────────────────
    async def install_builtin(self, org_id: str, name: str, actor: str) -> dict[str, Any]:
        if name not in self.builtins:
            raise NotFound(f"no built-in harness {name}; see GET /v1/catalog")
        return await self._install(org_id, self.builtins[name], {"builtin": name}, actor)

    async def install_git(self, p: Principal, url: str, ref: str, path: str) -> dict[str, Any]:
        p.require_admin()
        url = check_url(url, allow_private=self.allow_private)
        if not ref or len(ref) > 200 or ref.startswith("-") or any(c.isspace() for c in ref):
            raise Invalid("ref is a branch, tag or commit", param="ref")
        sub = Path(path or ".")
        if sub.is_absolute() or ".." in sub.parts:
            raise Invalid("path is a directory inside the repository", param="path")
        with tempfile.TemporaryDirectory(prefix="hg-git-") as tmp:
            await asyncio.to_thread(_git_fetch, url, ref, Path(tmp) / "repo")
            folder = Path(tmp) / "repo" / sub
            shutil.rmtree(Path(tmp) / "repo" / ".git", ignore_errors=True)
            if not folder.is_dir():
                raise Invalid(f"{path} is not a directory in the repository", param="path")
            digest, _ = await self.publish(folder)
        return await self._install(p.org_id, digest, {"git": {"url": url, "ref": ref, "path": str(sub)}}, p.actor)

    async def install_upload(self, p: Principal, archive: bytes) -> dict[str, Any]:
        p.require_admin()
        with tempfile.TemporaryDirectory(prefix="hg-upload-") as tmp:
            await asyncio.to_thread(pkg.unpack, archive, Path(tmp))
            digest, _ = await self.publish(Path(tmp))
        return await self._install(p.org_id, digest, {"upload": True}, p.actor)

    async def _install(self, org_id: str, digest: str, source: dict[str, Any], actor: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            blob = await tx.one("SELECT name, manifest FROM package_blobs WHERE digest = %s", digest)
            assert blob is not None, "an install points at a published digest"
            current = await tx.one("SELECT settings FROM installs WHERE name = %s", blob["name"])
            declared = set(blob["manifest"].get("settings") or {})
            kept = {k: v for k, v in ((current or {}).get("settings") or {}).items() if k in declared}
            await tx.run("INSERT INTO installs (id, org_id, name, digest, source, settings) VALUES (%s, %s, %s, %s, %s, %s) "
                         "ON CONFLICT (org_id, name) DO UPDATE SET digest = EXCLUDED.digest, source = EXCLUDED.source, "
                         "settings = EXCLUDED.settings, updated_at = now()",
                         ids.new("install"), org_id, blob["name"], digest, source, kept)
            await audit(tx, org_id, actor, "harness.install", blob["name"], digest=digest, source=source)
        return await self.get(org_id, blob["name"])

    async def get(self, org_id: str, name: str) -> dict[str, Any]:
        async with self.db.tenant(org_id) as tx:
            row = await tx.one("SELECT i.*, b.manifest FROM installs i JOIN package_blobs b ON b.digest = i.digest "
                               "WHERE i.name = %s", name)
        if not row:
            raise NotFound(f"no harness {name} installed; install it with POST /v1/harnesses", param="model")
        return _view(row)

    async def list(self, org_id: str) -> list[dict[str, Any]]:
        async with self.db.tenant(org_id) as tx:
            rows = await tx.all("SELECT i.*, b.manifest FROM installs i JOIN package_blobs b ON b.digest = i.digest "
                                "ORDER BY i.name")
        return [_view(r) for r in rows]

    async def uninstall(self, p: Principal, name: str) -> None:
        p.require_admin()
        async with self.db.tenant(p.org_id) as tx:
            if not await tx.run("DELETE FROM installs WHERE name = %s", name):
                raise NotFound(f"no harness {name}")
            await audit(tx, p.org_id, p.actor, "harness.uninstall", name)

    async def configure(self, p: Principal, name: str, *, settings: dict[str, str | None] | None,
                        default_provider: str | None, default_model: str | None) -> dict[str, Any]:
        """Set (a value), clear (None) or keep (absent) each setting; secrets are sealed per value."""
        p.require_admin()
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT i.settings, b.manifest FROM installs i JOIN package_blobs b ON b.digest = i.digest "
                               "WHERE i.name = %s FOR UPDATE OF i", name)
            if not row:
                raise NotFound(f"no harness {name}")
            manifest = Manifest.model_validate(row["manifest"])
            current = dict(row["settings"] or {})
            if settings:
                manifest.check_settings({k: v for k, v in settings.items() if v is not None})
                for k, v in settings.items():
                    if v is None:
                        current.pop(k, None)
                    elif manifest.settings[k].secret:
                        current[k] = {"secret": True, "value": self.sealer.seal(v, settings_context(p.org_id, name, k))}
                    else:
                        current[k] = {"secret": False, "value": v}
            if default_provider:
                ids.check_name(default_provider, "provider")
                if not await tx.one("SELECT 1 FROM providers WHERE name = %s", default_provider):
                    raise NotFound(f"no provider {default_provider}", param="default_provider")
            if default_model is not None and len(default_model) > 200:
                raise Invalid("default_model is at most 200 characters", param="default_model")
            await tx.run("UPDATE installs SET settings = %s, default_provider = coalesce(%s, default_provider), "
                         "default_model = coalesce(%s, default_model), updated_at = now() WHERE name = %s",
                         current, default_provider, default_model, name)
            await audit(tx, p.org_id, p.actor, "harness.configure", name, settings=sorted((settings or {}).keys()),
                        default_provider=default_provider, default_model=default_model)
        return await self.get(p.org_id, name)

    def open_settings(self, org_id: str, name: str, stored: dict[str, Any]) -> dict[str, str]:
        """The install's settings in the clear, for one turn's $HG_SETTINGS."""
        return {k: self.sealer.open(v["value"], settings_context(org_id, name, k)) if v.get("secret") else v["value"]
                for k, v in stored.items()}


def _git_fetch(url: str, ref: str, into: Path) -> None:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    base = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=always"]
    into.mkdir(parents=True)
    for args in (["init", "-q"], ["remote", "add", "origin", url], ["fetch", "-q", "--depth", "1", "origin", ref],
                 ["checkout", "-q", "FETCH_HEAD"]):
        result = subprocess.run([*base, *args], cwd=into, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        if result.returncode != 0:
            raise Invalid(f"git {args[0]} failed: {result.stderr.strip()[-300:]}", param="git")
