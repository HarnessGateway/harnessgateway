"""Harnesses: packages installed in an organization under a name, with its settings and secret values (SPEC §5, §14.2).

A package is content-addressed and stored once, whoever installs it. A harness points at a base package and, for an
overlay, at the overlay package too, with the composed manifest. Packages come from the built-in directory, an upload
or a git repository. A session runs the composed package it started on, whatever the harness points at later."""
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
from ..db import Database, Tx
from ..domain import package as pkg
from ..domain.manifest import Manifest, compose
from ..errors import Conflict, Invalid, NotFound
from ..limits import SETTING_VALUE_MAX
from ..netguard import check_url
from .objects import Objects
from .pages import ts
from .vault import Vault

GIT_TIMEOUT_S = 120
log = logging.getLogger("harnessgateway.harnesses")


Layers = list[tuple[str, Path]]                    # (base|overlay, unpacked folder)
Files = list[dict[str, Any]]


def archive_key(digest: str) -> str:
    return f"packages/{digest}.tgz"


def secret_context(name: str, secret: str) -> str:
    return f"harness/{name}/secret/{secret}"


def _view(row: dict[str, Any]) -> dict[str, Any]:
    m = Manifest.model_validate(row["manifest"])
    return {"id": row["id"], "object": "harness", "name": row["name"], "version": m.version,
            "description": m.description, "extends": row["extends"], "base_digest": row["base_digest"],
            "overlay_digest": row["overlay_digest"], "provider_apis": m.provider.apis if m.provider else [],
            "default_provider": row["default_provider"], "default_model": row["default_model"] or m.defaults.model,
            "persist": m.persist, "egress": m.run_entries(), "install_egress": m.install.egress,
            "settings": {k: {"description": s.description, "default": s.default, "required": s.required,
                             "set": k in row["settings"], "value": row["settings"].get(k)} for k, s in m.settings.items()},
            "secrets": {k: {"description": s.description, "hosts": s.hosts, "required": s.required,
                            "set": k in row["secrets"]} for k, s in m.secrets.items()},
            "mcp": {label: {"url": s.url, "allowed_tools": s.allowed_tools} for label, s in m.mcp.items()},
            "source": row["source"], "created_at": ts(row["created_at"]), "updated_at": ts(row["updated_at"])}


class Harnesses:
    def __init__(self, db: Database, objects: Objects, vault: Vault, builtin_dir: Path, *, allow_private: bool):
        self.db, self.objects, self.vault, self.builtin_dir = db, objects, vault, builtin_dir
        self.allow_private = allow_private

    def builtins(self) -> list[str]:
        if not self.builtin_dir.is_dir():
            return []
        return sorted(p.name for p in self.builtin_dir.iterdir() if (p / "harness.json").is_file())

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
    async def install_builtin(self, org_id: str, name: str) -> dict[str, Any]:
        if name not in self.builtins():
            raise NotFound(f"no built-in harness {name}; the built-ins are {', '.join(self.builtins()) or 'none'}")
        digest, manifest = await self.publish(self.builtin_dir / name, None)
        return await self._install(org_id, digest, manifest, {"builtin": name})

    async def install_upload(self, org_id: str, archive: bytes) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="hg-upload-") as tmp:
            folder = Path(tmp) / "p"
            await asyncio.to_thread(pkg.unpack, archive, folder)
            digest, manifest = await self.publish(folder, org_id)
        return await self._install(org_id, digest, manifest, {"upload": True})

    async def install_git(self, org_id: str, url: object, ref: object, path: object) -> dict[str, Any]:
        clean = await asyncio.to_thread(check_url, str(url), allow_private=self.allow_private, param="git.url")
        if not isinstance(ref, str) or not ref or len(ref) > 200 or ref.startswith("-") or any(c.isspace() for c in ref):
            raise Invalid("git.ref is a branch, tag or commit", param="git.ref")
        sub = Path(str(path or "."))
        if sub.is_absolute() or ".." in sub.parts:
            raise Invalid("git.path is a directory inside the repository", param="git.path")
        with tempfile.TemporaryDirectory(prefix="hg-git-") as tmp:
            repo = Path(tmp) / "repo"
            commit = await asyncio.to_thread(_git_fetch, clean, ref, repo)
            shutil.rmtree(repo / ".git", ignore_errors=True)
            if not (repo / sub).is_dir():
                raise Invalid(f"{sub} is not a directory in the repository", param="git.path")
            digest, manifest = await self.publish(repo / sub, org_id)
        return await self._install(org_id, digest, manifest,
                                   {"git": {"url": clean, "ref": ref, "path": str(sub), "commit": commit}})

    async def _install(self, org_id: str, digest: str, manifest: Manifest, source: dict[str, Any]) -> dict[str, Any]:
        """Install or upgrade. An overlay composes over the organization's harness it extends, pinned by digest.
        Settings and secret values the new version still declares are kept."""
        async with self.db.tenant(org_id) as tx:
            if manifest.is_overlay:
                base = await tx.one("SELECT * FROM harnesses WHERE name = %s", manifest.extends)
                if not base:
                    raise NotFound(f"overlay {manifest.name} extends {manifest.extends}, which is not installed",
                                   param="harness.json")
                if base["extends"]:
                    raise Invalid(f"{manifest.extends} is itself an overlay; an overlay extends a base package",
                                  param="harness.json")
                base_manifest = await self._package_manifest(tx, base["base_digest"])
                composed = compose(base_manifest, manifest)
                base_digest, overlay_digest = base["base_digest"], digest
            else:
                composed, base_digest, overlay_digest = manifest, digest, None
            if await tx.one("SELECT 1 FROM harnesses WHERE extends = %s AND name <> %s", manifest.name, manifest.name) \
                    and manifest.is_overlay:
                raise Conflict(f"overlays extend {manifest.name}; an overlay cannot be extended")
            current = await tx.one("SELECT settings, secrets FROM harnesses WHERE name = %s", manifest.name)
            settings = {k: v for k, v in ((current or {}).get("settings") or {}).items() if k in composed.settings}
            secrets = {k: v for k, v in ((current or {}).get("secrets") or {}).items() if k in composed.secrets}
            await tx.run("INSERT INTO harnesses (id, org_id, name, base_digest, overlay_digest, extends, manifest, source, "
                         "settings, secrets) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (org_id, name) DO "
                         "UPDATE SET base_digest = EXCLUDED.base_digest, overlay_digest = EXCLUDED.overlay_digest, "
                         "extends = EXCLUDED.extends, manifest = EXCLUDED.manifest, source = EXCLUDED.source, "
                         "settings = EXCLUDED.settings, secrets = EXCLUDED.secrets, updated_at = now()",
                         ids.new("harness"), org_id, manifest.name, base_digest, overlay_digest, manifest.extends,
                         composed.model_dump(), source, settings, secrets)
        log.info("installed %s for %s (%s)", manifest.name, org_id, digest[:12])
        return await self.get(org_id, manifest.name)

    @staticmethod
    async def _package_manifest(tx: Tx, digest: str) -> Manifest:
        row = await tx.get("SELECT manifest FROM packages WHERE digest = %s", digest)
        return Manifest.model_validate(row["manifest"])

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
    async def _layers(self, org_id: str, name: str) -> Layers:
        """The installed package's archives unpacked, base first: what a console shows and forks."""
        row = await self.row(org_id, name)
        layers = []
        for layer, digest in (("base", row["base_digest"]), ("overlay", row["overlay_digest"])):
            if digest:
                folder = Path(tempfile.mkdtemp(prefix="hg-read-")) / "p"
                await asyncio.to_thread(pkg.unpack, await self.objects.get(archive_key(digest)), folder)
                layers.append((layer, folder))
        return layers

    @staticmethod
    def _discard(layers: Layers) -> None:
        for _, folder in layers:
            shutil.rmtree(folder.parent, ignore_errors=True)

    async def files(self, org_id: str, name: str) -> Files:
        """Every file of the installed package: path, size, executable bit, and which layer it comes from (an overlay's
        file replaces the base's at the same path)."""
        layers = await self._layers(org_id, name)
        try:
            out: dict[str, dict[str, Any]] = {}
            for layer, folder in layers:
                for rel, path in pkg.entries(folder):
                    st = path.stat()
                    out[rel] = {"path": rel, "bytes": st.st_size, "executable": bool(st.st_mode & 0o111), "layer": layer}
            return [out[k] for k in sorted(out)]
        finally:
            self._discard(layers)

    async def file(self, org_id: str, name: str, rel: str) -> bytes:
        clean = pkg.clean_member(rel)
        layers = await self._layers(org_id, name)
        try:
            for _, folder in reversed(layers):           # the overlay's file wins
                p = folder / clean
                if p.is_file():
                    return p.read_bytes()
            raise NotFound(f"{name} has no file {rel}", param="path")
        finally:
            self._discard(layers)

    async def uninstall(self, org_id: str, name: str) -> None:
        async with self.db.tenant(org_id) as tx:
            overlay = await tx.one("SELECT name FROM harnesses WHERE extends = %s LIMIT 1", name)
            if overlay:
                raise Conflict(f"overlay {overlay['name']} extends {name}; uninstall it first")
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


def _git_fetch(url: str, ref: str, into: Path) -> str:
    """A shallow fetch of one ref, without prompts, system config or protocols other than http(s); the commit."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    base = ["git", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.http.allow=always"]
    into.mkdir(parents=True)
    for args in (["init", "-q"], ["remote", "add", "origin", url], ["fetch", "-q", "--depth", "1", "origin", ref],
                 ["checkout", "-q", "FETCH_HEAD"]):
        result = subprocess.run([*base, *args], cwd=into, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        if result.returncode != 0:
            raise Invalid(f"git {args[0]} failed: {result.stderr.strip()[-300:]}", param="git")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=into, capture_output=True, text=True, check=True).stdout.strip()
