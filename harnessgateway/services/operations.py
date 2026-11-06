"""What the platform scope sees of the whole platform (SPEC §9.4, §14.4): health, nodes, every organization, usage
across them, the running configuration and versions, Prometheus metrics, and one action an operator needs on a session
(evicting its warm box). Reads cross organizations, so everything here runs on the platform path."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from .. import __version__, ids
from ..config import Settings
from ..db import Database
from ..domain.meters import Meter
from ..errors import Conflict, Invalid, NotFound
from ..ports.blob import BlobStore
from .pages import ts

PROBE_KEY = "platform/health-probe"
GROUPS = ("org", "day")


class Operations:
    def __init__(self, db: Database, store: BlobStore, settings: Settings, extensions: list[str]):
        self.db, self.store, self.settings, self.extensions = db, store, settings, extensions

    def platform(self) -> dict[str, Any]:
        """What any key may learn: the version and the extensions loaded (the consoles read this to know what to show)."""
        return {"object": "platform", "version": __version__, "extensions": self.extensions}

    async def health(self) -> dict[str, Any]:
        doc: dict[str, Any] = {"object": "health", "version": __version__, "extensions": self.extensions, "at": int(time.time())}
        try:
            async with self.db.platform() as tx:
                nodes = await tx.get("SELECT count(*) AS count, count(*) FILTER (WHERE status = 'active' AND last_seen_at > "
                                     "now() - interval '45 seconds') AS live, coalesce(sum(slots), 0) AS slots FROM nodes")
                running = await tx.value("SELECT count(*) FROM turns WHERE status = 'in_progress'")
                warm = await tx.value("SELECT count(*) FROM sessions WHERE node_id IS NOT NULL AND running_turn_id IS NULL")
                queue = await tx.get("SELECT count(*) AS queued, coalesce(extract(epoch FROM now() - min(created_at)), 0) "
                                     "AS oldest_s FROM turns WHERE status = 'queued'")
                day = await tx.all("SELECT status, hg_reason, count(*) AS count FROM turns WHERE created_at > now() - "
                                   "interval '24 hours' GROUP BY 1, 2")
            doc["database"] = {"ok": True}
        except Exception as e:
            doc["database"] = {"ok": False, "error": type(e).__name__}
            return doc
        try:
            await self.store.put(PROBE_KEY, b"ok")
            await self.store.delete(PROBE_KEY)
            doc["objects"] = {"ok": True, "url": self.settings.blob_url.split("?")[0]}
        except Exception as e:
            doc["objects"] = {"ok": False, "error": type(e).__name__}
        doc["nodes"] = {"count": int(nodes["count"]), "live": int(nodes["live"]), "slots": int(nodes["slots"]),
                        "running": int(running), "warm": int(warm)}
        doc["queue"] = {"queued": int(queue["queued"]), "oldest_s": round(float(queue["oldest_s"]), 1)}
        doc["turns_24h"] = {r["status"]: int(r["count"]) for r in day if r["status"] != "failed"} | \
            {"failed": sum(int(r["count"]) for r in day if r["status"] == "failed")}
        doc["failures_24h"] = {r["hg_reason"] or "harness": int(r["count"]) for r in day if r["status"] == "failed"}
        return doc

    async def metrics(self) -> str:
        """The platform's gauges in Prometheus text exposition: what a scraper reads at GET /internal/metrics (SPEC §11)."""
        h = await self.health()
        lines = [f'hg_up{{version="{__version__}"}} {int(h["database"]["ok"] and h.get("objects", {}).get("ok", False))}']
        if h["database"]["ok"]:
            n, q = h["nodes"], h["queue"]
            lines += [f"hg_nodes_total {n['count']}", f"hg_nodes_live {n['live']}", f"hg_node_slots {n['slots']}",
                      f"hg_turns_running {n['running']}", f"hg_sandboxes_warm {n['warm']}", f"hg_turns_queued {q['queued']}",
                      f"hg_queue_oldest_seconds {q['oldest_s']}"]
            lines += [f'hg_turns_24h{{status="{k}"}} {v}' for k, v in sorted(h["turns_24h"].items())]
            lines += [f'hg_failures_24h{{reason="{k}"}} {v}' for k, v in sorted(h["failures_24h"].items())]
            async with self.db.platform() as tx:
                orgs = await tx.all("SELECT o.name, (SELECT count(*) FROM sessions s WHERE s.org_id = o.id AND s.status = "
                                    "'active') AS sessions, (SELECT count(*) FROM turns t WHERE t.org_id = o.id AND t.status = "
                                    "'in_progress') AS running FROM orgs o ORDER BY o.name")
                people = await tx.get("SELECT count(*) FILTER (WHERE status = 'active') AS active, count(*) AS total "
                                      "FROM accounts")
            for o in orgs:
                lines += [f'hg_org_sessions{{org="{o["name"]}"}} {int(o["sessions"])}',
                          f'hg_org_turns_running{{org="{o["name"]}"}} {int(o["running"])}']
            lines += [f"hg_accounts_total {int(people['total'])}", f"hg_accounts_active {int(people['active'])}"]
        return "\n".join(lines) + "\n"

    async def nodes(self) -> dict[str, Any]:
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT n.*, (SELECT count(*) FROM turns t WHERE t.node_id = n.id AND t.status = 'in_progress') "
                                "AS running, (SELECT count(*) FROM sessions s WHERE s.node_id = n.id AND s.running_turn_id IS "
                                "NULL) AS warm FROM nodes n ORDER BY n.created_at")
        return {"object": "list", "data": [
            {"id": r["id"], "object": "node", "name": r["name"], "pool": r["pool"], "slots": r["slots"],
             "running": int(r["running"]), "warm": int(r["warm"]), "version": r["version"], "status": r["status"],
             "live": (time.time() - r["last_seen_at"].timestamp()) < 45, "last_seen_at": ts(r["last_seen_at"]),
             "created_at": ts(r["created_at"])} for r in rows]}

    async def orgs(self) -> dict[str, Any]:
        async with self.db.platform() as tx:
            rows = await tx.all("SELECT o.*, (SELECT count(*) FROM sessions s WHERE s.org_id = o.id AND s.status = 'active') "
                                "AS sessions, (SELECT count(*) FROM turns t WHERE t.org_id = o.id AND t.status = 'in_progress') "
                                "AS running, (SELECT count(*) FROM sessions s WHERE s.org_id = o.id AND s.node_id IS NOT NULL) "
                                "AS boxes FROM orgs o ORDER BY o.created_at")
        return {"object": "list", "data": [self._org(r) for r in rows]}

    async def org(self, org_id: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            row = await tx.one("SELECT o.*, (SELECT count(*) FROM sessions s WHERE s.org_id = o.id AND s.status = 'active') "
                               "AS sessions, (SELECT count(*) FROM turns t WHERE t.org_id = o.id AND t.status = 'in_progress') "
                               "AS running, (SELECT count(*) FROM sessions s WHERE s.org_id = o.id AND s.node_id IS NOT NULL) "
                               "AS boxes FROM orgs o WHERE o.id = %s", ids.check(org_id, "org"))
        if not row:
            raise NotFound(f"no organization {org_id}")
        return self._org(row)

    def _org(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"id": r["id"], "object": "organization", "name": r["name"], "status": r["status"],
                "capacity": {**self.settings.defaults.model_dump(), **r["capacity"]}, "capacity_set": r["capacity"],
                "egress": r["egress"], "sessions": int(r["sessions"]), "running": int(r["running"]),
                "boxes": int(r["boxes"]), "created_at": ts(r["created_at"])}

    async def usage(self, start: datetime, end: datetime, group_by: str | None) -> dict[str, Any]:
        """The organizations' usage together, by org or by day."""
        if end <= start:
            raise Invalid("end is after start")
        if group_by is not None and group_by not in GROUPS:
            raise Invalid(f"group_by is one of {', '.join(GROUPS)}", param="group_by")
        column = {"org": "org_id", "day": "to_char(end_at, 'YYYY-MM-DD')"}.get(group_by or "", "NULL")
        async with self.db.platform() as tx:
            rows = await tx.all(f"SELECT {column} AS grp, meter, sum(quantity) AS quantity FROM usage WHERE end_at >= %s "
                                f"AND end_at < %s GROUP BY 1, 2 ORDER BY 1, 2", start, end)
        out: dict[str, dict[str, float]] = {}
        for r in rows:
            out.setdefault(str(r["grp"]) if group_by else "total", {})[r["meter"]] = float(r["quantity"])
        return {"object": "usage", "start": int(start.timestamp()), "end": int(end.timestamp()), "group_by": group_by,
                "meters": [m.value for m in Meter], "data": out}

    def config(self) -> dict[str, Any]:
        """The settings as this process runs them, secrets left out, and the capacity every organization starts from."""
        return {"object": "config", "settings": self.settings.describe(), "defaults": self.settings.defaults.model_dump()}

    async def versions(self) -> dict[str, Any]:
        async with self.db.platform() as tx:
            migrations = await tx.all("SELECT package, max(version) AS version FROM schema_migrations GROUP BY 1 ORDER BY 1")
        return {"object": "versions", "platform": __version__, "sandbox_image": self.settings.sandbox_image,
                "platform_image": self.settings.platform_image,
                "migrations": {r["package"]: int(r["version"]) for r in migrations}}

    async def evict(self, sid: str) -> dict[str, Any]:
        """Free a session's warm box: the node holding it removes it at its next heartbeat. The session itself stays and
        restores from its last save on its next turn."""
        async with self.db.platform() as tx:
            row = await tx.one("SELECT node_id, running_turn_id, saving_turn_id FROM sessions WHERE id = %s AND status = "
                               "'active' FOR UPDATE", ids.check(sid, "session"))
            if not row:
                raise NotFound(f"no session {sid}")
            if row["running_turn_id"] or row["saving_turn_id"]:
                raise Conflict(f"{sid} is running or saving; evict it once it is idle")
            await tx.run("UPDATE sessions SET node_id = NULL WHERE id = %s", sid)
        return {"id": sid, "object": "session", "evicted": row["node_id"] is not None}
