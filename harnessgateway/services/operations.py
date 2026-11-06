"""What the platform scope sees of the whole platform (SPEC §14.3): health, metrics, every organization and its
limits, the running configuration and versions. Reads cross organizations, so everything here runs on the platform
path. Boxes and usage across organizations are the Boxes and Usage services' own platform views."""
from __future__ import annotations

import time
from typing import Any

from .. import __version__, ids
from ..config import Settings
from ..domain.access import ADMIN
from ..errors import NotFound
from ..ports.blob import BlobStore
from ..store.db import Database
from ..store.moments import Moments
from .accounts import Accounts
from .orgs import Orgs
from .pages import ts

PROBE_KEY = "platform/health-probe"
INIT = "platform:init"
ORG = ("SELECT o.*, (SELECT count(*) FROM sessions s WHERE s.org_id = o.id AND s.status = 'active') AS sessions, "
       "(SELECT count(*) FROM turns t WHERE t.org_id = o.id AND t.status = 'in_progress') AS running, "
       "(SELECT count(*) FROM boxes b WHERE b.org_id = o.id AND b.status <> 'gone') AS boxes FROM orgs o")


class Operations:
    def __init__(self, db: Database, store: BlobStore, moments: Moments, orgs: Orgs, accounts: Accounts,
                 settings: Settings):
        self.db, self.store, self.moments, self.orgs, self.accounts, self.settings = db, store, moments, orgs, accounts, settings

    def platform(self) -> dict[str, Any]:
        """What any key may learn: the version and the extensions loaded (the consoles read this to know what to show)."""
        return {"object": "platform", "version": __version__, "extensions": list(self.settings.extensions)}

    async def ready(self) -> dict[str, bool]:
        """Postgres and Redis reachable: GET /readyz."""
        out = {}
        for name, probe in (("database", self._probe_db), ("redis", self.moments.ping)):
            try:
                await probe()
                out[name] = True
            except Exception:
                out[name] = False
        return out

    async def _probe_db(self) -> None:
        async with self.db.platform() as tx:
            await tx.value("SELECT 1")

    async def health(self) -> dict[str, Any]:
        doc: dict[str, Any] = {**self.platform(), "object": "health", "at": int(time.time())}
        try:
            async with self.db.platform() as tx:
                boxes = await tx.all("SELECT kind, status, count(*) AS count FROM boxes WHERE status <> 'gone' GROUP BY 1, 2")
                queue = await tx.get("SELECT count(*) AS queued, coalesce(extract(epoch FROM now() - min(created_at)), 0) "
                                     "AS oldest_s FROM turns WHERE status = 'queued'")
                running = await tx.value("SELECT count(*) FROM turns WHERE status = 'in_progress'")
                day = await tx.all("SELECT status, hg_reason, count(*) AS count FROM turns WHERE created_at > now() - "
                                   "interval '24 hours' GROUP BY 1, 2")
            doc["database"] = {"ok": True}
        except Exception as e:
            doc["database"] = {"ok": False, "error": type(e).__name__}
            return doc
        try:
            depth = await self.moments.queue_depth()
            doc["redis"] = {"ok": True, "jobs": depth}
        except Exception as e:
            doc["redis"] = {"ok": False, "error": type(e).__name__}
        try:
            await self.store.put(PROBE_KEY, b"ok")
            await self.store.delete(PROBE_KEY)
            doc["objects"] = {"ok": True, "url": self.settings.blob_url.split("?")[0]}
        except Exception as e:
            doc["objects"] = {"ok": False, "error": type(e).__name__}
        doc["boxes"] = {f"{r['kind']}.{r['status']}": int(r["count"]) for r in boxes}
        doc["turns"] = {"running": int(running), "queued": int(queue["queued"]),
                        "oldest_queued_s": round(float(queue["oldest_s"]), 1)}
        doc["turns_24h"] = {r["status"]: int(r["count"]) for r in day if r["status"] != "failed"} | \
            {"failed": sum(int(r["count"]) for r in day if r["status"] == "failed")}
        doc["failures_24h"] = {r["hg_reason"] or "harness": int(r["count"]) for r in day if r["status"] == "failed"}
        return doc

    async def metrics(self) -> str:
        """The platform's gauges in Prometheus text exposition, for a scraper at GET /internal/metrics."""
        h = await self.health()
        ok = h["database"]["ok"] and h.get("redis", {}).get("ok", False) and h.get("objects", {}).get("ok", False)
        lines = [f'hg_up{{version="{__version__}"}} {int(ok)}']
        if h["database"]["ok"]:
            t = h["turns"]
            lines += [f"hg_turns_running {t['running']}", f"hg_turns_queued {t['queued']}",
                      f"hg_queue_oldest_seconds {t['oldest_queued_s']}", f"hg_jobs_queued {h.get('redis', {}).get('jobs', 0)}"]
            lines += [f'hg_boxes{{state="{k}"}} {v}' for k, v in sorted(h["boxes"].items())]
            lines += [f'hg_turns_24h{{status="{k}"}} {v}' for k, v in sorted(h["turns_24h"].items())]
            lines += [f'hg_failures_24h{{reason="{k}"}} {v}' for k, v in sorted(h["failures_24h"].items())]
            async with self.db.platform() as tx:
                orgs = await tx.all(f"{ORG} ORDER BY o.name")
                people = await tx.get("SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) AS total "
                                      "FROM accounts")
            for o in orgs:
                lines += [f'hg_org_sessions{{org="{o["name"]}"}} {int(o["sessions"])}',
                          f'hg_org_turns_running{{org="{o["name"]}"}} {int(o["running"])}',
                          f'hg_org_boxes{{org="{o["name"]}"}} {int(o["boxes"])}']
            lines += [f"hg_accounts_total {int(people['total'])}", f"hg_accounts_active {int(people['active'])}"]
        return "\n".join(lines) + "\n"

    # ── organizations ──────────────────────────────────────────────────────────────────────────────────────────────
    def _org(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "object": "organization", "name": r["name"], "status": r["status"],
                "limits": self.orgs.limits_of(r).model_dump(), "limits_set": r["limits"], "egress": r["egress"],
                "sessions": int(r["sessions"]), "running": int(r["running"]), "boxes": int(r["boxes"]),
                "created_at": ts(r["created_at"])}

    async def list_orgs(self) -> dict[str, Any]:
        async with self.db.platform() as tx:
            rows = await tx.all(f"{ORG} ORDER BY o.created_at")
        return {"object": "list", "data": [self._org(r) for r in rows]}

    async def org(self, org_id: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            row = await tx.one(f"{ORG} WHERE o.id = %s", ids.check(org_id, "org"))
        if not row:
            raise NotFound(f"no organization {org_id}")
        return self._org(row)

    async def patch_org(self, org_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Limits, status and egress, each when given."""
        org_id = ids.check(org_id, "org")
        if "limits" in body or "status" in body:
            await self.orgs.update(org_id, limits=body.get("limits"), status=body.get("status"))
        if "egress" in body:
            await self.orgs.set_egress(org_id, body["egress"])
        return await self.org(org_id)

    async def invite_admin(self, org_id: str, actor: str, email: object, name: object) -> dict[str, Any]:
        await self.org(org_id)
        return await self.accounts.create(org_id, actor, email, name, ADMIN)

    # ── this process ───────────────────────────────────────────────────────────────────────────────────────────────
    def config(self) -> dict[str, Any]:
        """The settings as this process runs them, secrets left out, and the limits every organization starts from."""
        return {"object": "config", "settings": self.settings.describe(), "defaults": self.settings.defaults.model_dump()}

    async def versions(self) -> dict[str, Any]:
        async with self.db.platform() as tx:
            migrations = await tx.all("SELECT package, max(version) AS version FROM schema_migrations GROUP BY 1 ORDER BY 1")
        return {"object": "versions", "platform": __version__, "sandbox_image": self.settings.sandbox_image,
                "platform_image": self.settings.platform_image,
                "migrations": {r["package"]: int(r["version"]) for r in migrations}}
