"""Nodes, from the API's side (SPEC §10.2). A node joins with a join token, then pulls work with its own credential.

    claim      the next turn the node may run: organizations of the node's pool by fewest running turns, a session's
               turns in order, a session kept on the node holding its warm box or saving its state, within the
               organization's capacity. The job carries everything the turn needs, by signed links, plus the secret
               bindings only the node's door receives.
    heartbeat  reconciles what the node holds and carries cancels and evictions
    sweep      releases what silent nodes held: their turns fail, their sessions restore elsewhere
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import ids
from ..db import Database, Tx
from ..domain import contract, egress
from ..domain.capacity import Capacity
from ..domain.manifest import Manifest
from ..domain.turn import Reason, Status
from ..errors import Forbidden, Invalid, Unauthenticated
from ..extensions import Extensions, TurnSummary
from .harnesses import archive_key, secret_context
from .objects import Objects, org_key
from .principal import NodeIdentity
from .turns import conclude, record_events, request_body
from .vault import Vault

LOST_AFTER_S = 45
RESTART_GRACE_S = 30
TOKEN_GRACE_S = 600
CANDIDATES = 20
_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def effective_timeout(manifest: Manifest, capacity: Capacity) -> int | None:
    bounds = [b for b in (manifest.run.timeout_s, capacity.turn_timeout_s) if b]
    return min(bounds) if bounds else None


class Scheduler:
    def __init__(self, db: Database, objects: Objects, vault: Vault, extensions: Extensions, pepper: bytes,
                 defaults: Capacity):
        self.db, self.objects, self.vault, self.ext, self.pepper = db, objects, vault, extensions, pepper
        self.defaults = defaults

    # ── joining ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def create_token(self, pool: str, label: str, uses: int, ttl_days: int | None) -> str:
        if pool:
            ids.check_name(pool, "pool")
        token = ids.new_secret("node_token")
        expires = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO node_tokens (id, digest, pool, label, uses_left, expires_at) VALUES "
                         "(%s, %s, %s, %s, %s, %s)", ids.new("node_token"), ids.digest(token, self.pepper), pool,
                         label[:200], uses, expires)
        return token

    async def ensure_token(self, token: str) -> None:
        """HG_NODE_JOIN_TOKEN shared by the API and the default pool's nodes: registered once, for the default pool."""
        if not token.startswith(ids.SECRET["node_token"]) or len(token) < 32:
            raise SystemExit("configuration error: HG_NODE_JOIN_TOKEN is hgj_ and at least 28 more characters "
                             "(harnessgateway keygen prints one)")
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO node_tokens (id, digest, pool, label, uses_left) VALUES (%s, %s, '', 'environment', "
                         "1000000000) ON CONFLICT (digest) DO NOTHING", ids.new("node_token"),
                         ids.digest(token, self.pepper))

    async def join(self, token: str, name: str, slots: int, version: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            jt = await tx.one("UPDATE node_tokens SET uses_left = uses_left - 1 WHERE digest = %s AND uses_left > 0 "
                              "AND (expires_at IS NULL OR expires_at > now()) RETURNING pool",
                              ids.digest(token, self.pepper))
            if not jt:
                raise Forbidden("the node join token is unknown, used up or expired")
            node_id, secret = ids.new("node"), ids.new_secret("node")
            await tx.run("INSERT INTO nodes (id, name, pool, digest, slots, version) VALUES (%s, %s, %s, %s, %s, %s)",
                         node_id, name[:100], jt["pool"], ids.digest(secret, self.pepper), slots, version[:40])
        return {"id": node_id, "credential": secret, "pool": jt["pool"]}

    async def authenticate(self, secret: str) -> NodeIdentity:
        if not secret.startswith(ids.SECRET["node"]):
            raise Unauthenticated("a node credential is required")
        async with self.db.platform() as tx:
            row = await tx.one("SELECT id, pool FROM nodes WHERE digest = %s", ids.digest(secret, self.pepper))
        if not row:
            raise Unauthenticated("unknown node credential; join again with a join token")
        return NodeIdentity(row["id"], row["pool"])

    async def drain(self, node_id: str) -> None:
        async with self.db.platform() as tx:
            await tx.run("UPDATE nodes SET status = 'draining' WHERE id = %s", ids.check(node_id, "node"))

    # ── heartbeat ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def heartbeat(self, node: NodeIdentity, running: list[str], boxes: list[str], saving: list[str], slots: int,
                        version: str) -> dict[str, Any]:
        """running: turns whose process runs; boxes: sessions with a warm box; saving: sessions being saved."""
        async with self.db.platform() as tx:
            row = await tx.get("UPDATE nodes SET last_seen_at = now(), slots = %s, version = %s WHERE id = %s "
                               "RETURNING status", slots, version[:40], node.node_id)
            ended = await self._release(tx, "s.node_id = %s AND s.running_turn_id IS NOT NULL AND NOT "
                                            "(s.running_turn_id = ANY(%s)) AND t.started_at < now() - %s * interval '1 second'",
                                        node.node_id, running, RESTART_GRACE_S)
            await tx.run("UPDATE sessions SET node_id = NULL, saving_turn_id = NULL WHERE node_id = %s AND "
                         "running_turn_id IS NULL AND NOT (id = ANY(%s))", node.node_id, boxes + saving)
            keep = {r["id"] for r in await tx.all("SELECT id FROM sessions WHERE id = ANY(%s) AND node_id = %s "
                                                  "AND status = 'active'", boxes, node.node_id)}
            turns = {r["id"]: r for r in await tx.all("SELECT id, cancel FROM turns WHERE id = ANY(%s) AND node_id = %s",
                                                      running, node.node_id)}
        for e in ended:
            await self.ext.turn_ended(e)
        return {"cancel": [rid for rid in running if rid not in turns or turns[rid]["cancel"]],
                "evict": [s for s in boxes if s not in keep], "draining": row["status"] == "draining"}

    async def _release(self, tx: Tx, where: str, *args: Any) -> list[TurnSummary]:
        ended = []
        rows = await tx.all(f"SELECT s.id AS sid, t.* FROM sessions s JOIN turns t ON t.id = s.running_turn_id "
                            f"WHERE {where} FOR UPDATE OF s, t", *args)
        for turn in rows:
            turn.pop("sid")
            if not Status(turn["status"]).final:
                ended.append(await conclude(tx, self.objects, turn, Status.FAILED, reason=Reason.NODE_LOST,
                                            message="the node running this turn stopped or restarted"))
            await tx.run("UPDATE turn_tokens SET revoked_at = now() WHERE turn_id = %s AND revoked_at IS NULL", turn["id"])
            await tx.run("DELETE FROM turn_bindings WHERE turn_id = %s", turn["id"])
            await tx.run("UPDATE sessions SET running_turn_id = NULL, saving_turn_id = NULL, node_id = NULL WHERE id = %s",
                         turn["session_id"])
        return ended

    # ── claim ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def claim(self, node: NodeIdentity) -> dict[str, Any] | None:
        async with self.db.platform() as tx:
            if await tx.value("SELECT status FROM nodes WHERE id = %s", node.node_id) != "active":
                return None
            candidates = await tx.all(
                """SELECT t.id, t.org_id, t.session_id, s.node_id AS held_by, o.capacity FROM turns t
                   JOIN sessions s ON s.id = t.session_id JOIN orgs o ON o.id = t.org_id
                   WHERE t.status = 'queued' AND o.status = 'active'
                     AND coalesce(o.capacity->>'node_pool', '') = %s
                     AND s.running_turn_id IS NULL
                     AND (s.saving_turn_id IS NULL OR s.node_id = %s)
                     AND (s.node_id IS NULL OR s.node_id = %s OR NOT EXISTS (
                          SELECT 1 FROM nodes n WHERE n.id = s.node_id AND n.status = 'active'
                          AND n.last_seen_at > now() - %s * interval '1 second'))
                     AND t.seq = (SELECT min(q.seq) FROM turns q WHERE q.session_id = t.session_id AND q.status = 'queued')
                   ORDER BY (SELECT count(*) FROM turns r WHERE r.org_id = t.org_id AND r.status = 'in_progress'), t.created_at
                   LIMIT %s FOR UPDATE OF t, s SKIP LOCKED""",
                node.pool, node.node_id, node.node_id, LOST_AFTER_S, CANDIDATES)
            for c in candidates:
                capacity = Capacity.parse(c["capacity"], self.defaults)
                if capacity.max_running_turns and await tx.value(
                        "SELECT count(*) FROM turns WHERE org_id = %s AND status = 'in_progress'", c["org_id"]
                ) >= capacity.max_running_turns:
                    continue
                if capacity.max_sandboxes and c["held_by"] != node.node_id and await tx.value(
                        "SELECT count(*) FROM sessions WHERE org_id = %s AND node_id IS NOT NULL", c["org_id"]
                ) >= capacity.max_sandboxes:
                    continue
                await tx.run("UPDATE sessions SET running_turn_id = %s, node_id = %s WHERE id = %s",
                             c["id"], node.node_id, c["session_id"])
                return await self._job(tx, node, c["id"], capacity, held=c["held_by"] == node.node_id)
        return None

    async def _job(self, tx: Tx, node: NodeIdentity, rid: str, capacity: Capacity, held: bool) -> dict[str, Any]:
        await tx.run("UPDATE turns SET status = 'in_progress', node_id = %s, started_at = now() WHERE id = %s",
                     node.node_id, rid)
        turn = await tx.get("SELECT * FROM turns WHERE id = %s", rid)
        session = await tx.get("SELECT * FROM sessions WHERE id = %s", turn["session_id"])
        org = await tx.get("SELECT egress FROM orgs WHERE id = %s", turn["org_id"])
        harness = await tx.one("SELECT settings, secrets FROM harnesses WHERE org_id = %s AND name = %s",
                               turn["org_id"], session["harness"]) or {"settings": {}, "secrets": {}}
        manifest = Manifest.model_validate(session["manifest"])
        body = await request_body(self.objects, turn)
        partial = contract.response(rid=rid, body=body, created=turn["created_at"].timestamp(), status="in_progress",
                                    metadata={"session_id": session["id"]})
        await record_events(tx, turn, [{"type": "response.in_progress", "response": partial}])
        timeout = effective_timeout(manifest, capacity)
        provider = None
        if turn["provider_id"] and (p := await tx.one("SELECT id, api FROM providers WHERE id = %s", turn["provider_id"])):
            token, door = ids.new_secret("turn"), ids.new_secret("door")
            await tx.run("INSERT INTO turn_tokens (digest, door_digest, turn_id, org_id, provider_id, node_id, capture, "
                         "expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s, now() + %s * interval '1 second')",
                         ids.digest(token, self.pepper), ids.digest(door, self.pepper), rid, turn["org_id"], p["id"],
                         node.node_id, capacity.observability, (timeout or 7 * 86400) + TOKEN_GRACE_S)
            provider = {"api": p["api"], "token": token, "door_token": door}
        settings = {k: harness["settings"].get(k, s.default) for k, s in manifest.settings.items()
                    if k in harness["settings"] or s.default is not None}
        secrets = await self._secrets(turn["org_id"], session["harness"], manifest, harness["secrets"])
        placeholders = {k: b["placeholder"] for k, b in secrets.items()}
        bindings = list(secrets.values()) + await self._caller_bindings(tx, turn)
        files = await tx.all("SELECT id, filename, object_key FROM files WHERE id = ANY(%s) AND deleted_at IS NULL",
                             list(contract.file_ids(body.get("input"))))
        ops = await tx.all("SELECT id, op, path, object_key FROM container_ops WHERE session_id = %s AND applied_at IS NULL "
                           "ORDER BY created_at", session["id"])
        org_id, sid, o = turn["org_id"], session["id"], self.objects
        packages = {"base": {"digest": session["base_digest"], "url": o.link_get(archive_key(session["base_digest"]))},
                    "overlay": {"digest": session["overlay_digest"], "url": o.link_get(archive_key(session["overlay_digest"]))}
                    if session["overlay_digest"] else None, "manifest": manifest.model_dump()}
        return {
            "turn_id": rid, "org_id": org_id, "session_id": sid, "seq": turn["seq"], "harness": session["harness"],
            "model": turn["provider_model"], "store": session["store"], "held": held, "timeout_s": timeout,
            "observability": capacity.observability, "package": packages, "request_url": o.link_get(turn["request_key"]),
            "box": {"vcpus": capacity.vcpus, "memory_mb": capacity.memory_mb, "pids": capacity.pids,
                    "tmp_mb": capacity.tmp_mb, "idle_ttl_s": capacity.idle_ttl_s},
            "egress": {"run": egress.Policy.of(org["egress"], manifest.run_entries()).entries(),
                       "install": egress.Policy.of(org["egress"], manifest.install.egress).entries()},
            "env": {**settings, **placeholders}, "mcp": _render_mcp(turn["mcp"], placeholders),
            "bindings": bindings, "provider": provider,
            "files": [{"id": f["id"], "filename": f["filename"], "url": o.link_get(f["object_key"])} for f in files],
            "ops": [{"id": op["id"], "op": op["op"], "path": op["path"],
                     "url": o.link_get(op["object_key"]) if op["object_key"] else None} for op in ops],
            "snapshot": {"url": o.link_get(session["snapshot_key"]) if session["snapshot_key"] else None},
            "logs": {name: o.link_put(org_key(org_id, "turns", rid, f"{name}.log")) for name in ("stdout", "stderr")}
            if capacity.observability == "full" else None,
        }

    async def _secrets(self, org_id: str, harness: str, manifest: Manifest, sealed: dict[str, str]
                       ) -> dict[str, dict[str, Any]]:
        """Each declared secret the organization set, bound to a fresh placeholder, to the hosts it declares and the
        hosts of the catalog MCP servers that refer to it."""
        out: dict[str, dict[str, Any]] = {}
        for name, spec in manifest.secrets.items():
            if name not in sealed:
                continue
            hosts = set(spec.hosts) | {s.host() for s in manifest.mcp.values() if name in s.secret_refs()}
            out[name] = {"placeholder": ids.new_secret("placeholder"), "hosts": sorted(hosts),
                         "value": await self.vault.open(org_id, secret_context(harness, name), sealed[name])}
        return out

    async def _caller_bindings(self, tx: Tx, turn: dict[str, Any]) -> list[dict[str, Any]]:
        row = await tx.one("SELECT sealed FROM turn_bindings WHERE turn_id = %s", turn["id"])
        if not row:
            return []
        return list(json.loads(await self.vault.open(turn["org_id"], f"turn/{turn['id']}/bindings", row["sealed"])))

    async def snapshot(self, node: NodeIdentity, rid: str) -> dict[str, Any]:
        """The session's saved state as it is now, for a node that waited for its own saves before restoring."""
        async with self.db.platform() as tx:
            row = await tx.one("SELECT s.snapshot_key FROM turns t JOIN sessions s ON s.id = t.session_id "
                               "WHERE t.id = %s AND t.node_id = %s", ids.check(rid, "turn"), node.node_id)
        if row is None:
            raise Forbidden(f"{rid} is not this node's turn")
        return {"url": self.objects.link_get(row["snapshot_key"]) if row["snapshot_key"] else None}

    async def check_orgs(self, node: NodeIdentity, org_ids: set[str]) -> None:
        """A node reports only for organizations its pool serves."""
        async with self.db.platform() as tx:
            served = {r["id"] for r in await tx.all("SELECT id FROM orgs WHERE id = ANY(%s) AND "
                                                    "coalesce(capacity->>'node_pool', '') = %s", list(org_ids), node.pool)}
        if org_ids - served:
            raise Forbidden("a node reports only for the organizations of its pool")

    # ── lost nodes ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def sweep(self) -> int:
        async with self.db.platform() as tx:
            lost = [r["id"] for r in await tx.all("SELECT id FROM nodes WHERE last_seen_at < now() - %s * interval "
                                                  "'1 second'", LOST_AFTER_S)]
            if not lost:
                return 0
            ended = await self._release(tx, "s.node_id = ANY(%s)", lost)
            await tx.run("UPDATE sessions SET node_id = NULL, saving_turn_id = NULL WHERE node_id = ANY(%s)", lost)
        for e in ended:
            await self.ext.turn_ended(e)
        return len(ended)


def _render_mcp(servers: list[dict[str, Any]], placeholders: dict[str, str]) -> list[dict[str, Any]]:
    """The turn's MCP servers with each ${SECRET} replaced by its placeholder; a header naming a secret the
    organization has not set is left out."""
    out = []
    for s in servers:
        headers = {}
        for name, value in (s.get("headers") or {}).items():
            refs = _REF.findall(value)
            if all(r in placeholders for r in refs):
                headers[name] = _REF.sub(lambda m: placeholders[m.group(1)], value)
        out.append({**s, "headers": headers})
    return out


def check_report(report: object) -> dict[str, Any]:
    if not isinstance(report, dict):
        raise Invalid("a report is a JSON object")
    return report
