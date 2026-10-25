"""The node's side of the platform. Nodes join with a join token and then pull: `claim` hands a node the next turn it may
run (organizations taken in turn by how few turns they have running, each session's turns in order, a session's warm
sandbox kept on the node that holds it); `heartbeat` reconciles which sandboxes a node holds and carries cancels back;
`sweep` fails the turns of nodes that went silent. A node holds its own credential and nothing else."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from .. import ids
from ..blobs import Blobs, org_key
from ..crypto import UrlSigner
from ..db import Database, Tx
from ..domain import wire
from ..domain.orglimits import OrgLimits
from ..domain.package import Manifest
from ..domain.turn import Reason, Status
from ..errors import Forbidden, Unauthenticated
from .packages import Packages
from .principal import NodeIdentity
from .tenancy import audit
from .turns import CHANNEL, _append_event, conclude

LOST_AFTER_S = 45
BLOB_TTL_S = 3600
TOKEN_GRACE_S = 600


def _blob(signer: UrlSigner, key: str, method: str, ttl: int = BLOB_TTL_S) -> str:
    return f"/internal/v1/blobs/{signer.sign(key, method, ttl)}"


class Scheduler:
    def __init__(self, db: Database, blobs: Blobs, packages: Packages, signer: UrlSigner, pepper: bytes):
        self.db, self.blobs, self.packages, self.signer, self.pepper = db, blobs, packages, signer, pepper

    # ── joining ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def create_join_token(self, org_id: str | None, label: str, uses: int, ttl_days: int | None, actor: str
                                ) -> dict[str, Any]:
        token = ids.new_secret("jointoken")
        expires = datetime.now(UTC) + timedelta(days=ttl_days) if ttl_days else None
        jid = ids.new("jointoken")
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO node_join_tokens (id, digest, org_id, label, uses_left, expires_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", jid, ids.digest(token, self.pepper), org_id, label[:200],
                         uses, expires)
            await audit(tx, org_id, actor, "node_token.create", jid, label=label, dedicated=org_id is not None)
        return {"id": jid, "object": "node_join_token", "token": token, "org_id": org_id, "uses_left": uses}

    async def ensure_join_token(self, token: str) -> None:
        """A pre-shared join token from the environment (HG_NODE_JOIN_TOKEN on the api): nodes of the shared pool that
        start with the same value join without a manual step. Re-registering it is a no-op."""
        if not token.startswith(ids.SECRET_PREFIXES["jointoken"]) or len(token) < 32:
            raise SystemExit("configuration error: HG_NODE_JOIN_TOKEN is hgj_ followed by at least 28 characters "
                             "(harnessgateway keygen prints one)")
        async with self.db.platform() as tx:
            await tx.run("INSERT INTO node_join_tokens (id, digest, org_id, label, uses_left) VALUES (%s, %s, NULL, "
                         "'environment', 1000000000) ON CONFLICT (digest) DO NOTHING",
                         ids.new("jointoken"), ids.digest(token, self.pepper))

    async def join(self, token: str, name: str, slots: int, version: str) -> dict[str, Any]:
        async with self.db.platform() as tx:
            jt = await tx.one("UPDATE node_join_tokens SET uses_left = uses_left - 1 WHERE digest = %s AND uses_left > 0 "
                              "AND (expires_at IS NULL OR expires_at > now()) RETURNING id, org_id",
                              ids.digest(token or "", self.pepper))
            if not jt:
                raise Forbidden("the node join token is unknown, used up or expired")
            node_id, secret = ids.new("node"), ids.new_secret("node")
            await tx.run("INSERT INTO nodes (id, name, org_id, digest, slots, version, last_seen_at) "
                         "VALUES (%s, %s, %s, %s, %s, %s, now())", node_id, name[:100], jt["org_id"],
                         ids.digest(secret, self.pepper), slots, version[:40])
            await audit(tx, jt["org_id"], f"join_token:{jt['id']}", "node.join", node_id, name=name)
        return {"id": node_id, "credential": secret, "org_id": jt["org_id"]}

    async def authenticate(self, secret: str) -> NodeIdentity:
        async with self.db.platform() as tx:
            row = await tx.one("SELECT id, org_id FROM nodes WHERE digest = %s AND status <> 'removed'",
                               ids.digest(secret or "", self.pepper))
        if not row:
            raise Unauthenticated("unknown node credential")
        return NodeIdentity(row["id"], row["org_id"])

    # ── heartbeat ──────────────────────────────────────────────────────────────────────────────────────────────────
    async def heartbeat(self, node: NodeIdentity, running: list[str], sandboxes: list[str], slots: int, version: str
                        ) -> dict[str, Any]:
        async with self.db.platform() as tx:
            await tx.run("UPDATE nodes SET last_seen_at = now(), slots = %s, version = %s WHERE id = %s",
                         slots, version[:40], node.node_id)
            # sessions this node no longer holds lose it; sessions it holds that are gone are evicted
            await tx.run("UPDATE sessions SET node_id = NULL WHERE node_id = %s AND NOT (id = ANY(%s)) "
                         "AND running_turn_id IS NULL", node.node_id, sandboxes)
            keep = {r["id"] for r in await tx.all("SELECT id FROM sessions WHERE id = ANY(%s) AND node_id = %s "
                                                  "AND status = 'active'", sandboxes, node.node_id)}
            turns = {r["id"]: r for r in await tx.all("SELECT id, status, cancel FROM turns WHERE id = ANY(%s) AND node_id = %s",
                                                      running, node.node_id)}
            # turns this node holds but no longer runs (it restarted): failed after a grace that covers a claim whose
            # reply crossed this heartbeat
            for turn in await tx.all("SELECT * FROM turns WHERE node_id = %s AND status = 'in_progress' AND NOT (id = ANY(%s)) "
                                     "AND started_at < now() - interval '30 seconds' FOR UPDATE", node.node_id, running):
                await conclude(tx, self.blobs, turn, Status.FAILED, reason=Reason.NODE_LOST,
                               message="the node running this turn restarted")
        cancel = [rid for rid in running if rid not in turns or turns[rid]["cancel"] or turns[rid]["status"] != Status.IN_PROGRESS]
        return {"cancel": cancel, "evict": [s for s in sandboxes if s not in keep]}

    # ── claim ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def claim(self, node: NodeIdentity) -> dict[str, Any] | None:
        async with self.db.platform() as tx:
            if await tx.value("SELECT status FROM nodes WHERE id = %s", node.node_id) != "active":
                return None                               # draining: finish what it runs, take nothing new
            live = [r["id"] for r in await tx.all("SELECT id FROM nodes WHERE last_seen_at > now() - %s * interval '1 second' "
                                                  "AND status = 'active'", LOST_AFTER_S)]
            candidates = await tx.all(
                """SELECT t.id, t.org_id, t.session_id, s.node_id AS held_by, o.limits FROM turns t
                   JOIN sessions s ON s.id = t.session_id JOIN orgs o ON o.id = t.org_id
                   WHERE t.status = 'queued' AND o.status = 'active' AND s.running_turn_id IS NULL
                     AND (s.node_id IS NULL OR s.node_id = %s OR NOT (s.node_id = ANY(%s)))
                     AND t.seq = (SELECT min(q.seq) FROM turns q WHERE q.session_id = t.session_id AND q.status = 'queued')
                     AND ((%s::text IS NULL AND coalesce((o.limits->>'dedicated_nodes')::boolean, false) = false)
                          OR t.org_id = %s::text)
                   ORDER BY (SELECT count(*) FROM turns r WHERE r.org_id = t.org_id AND r.status = 'in_progress'), t.created_at
                   LIMIT 20 FOR UPDATE OF t SKIP LOCKED""",
                node.node_id, live, node.org_id, node.org_id)
            for c in candidates:
                limits = OrgLimits.parse(c["limits"])
                running = await tx.value("SELECT count(*) FROM turns WHERE org_id = %s AND status = 'in_progress'", c["org_id"])
                if running >= limits.max_running_turns:
                    continue
                if c["held_by"] != node.node_id:
                    held = await tx.value("SELECT count(*) FROM sessions WHERE org_id = %s AND node_id IS NOT NULL", c["org_id"])
                    if held >= limits.max_sandboxes:
                        continue
                taken = await tx.run("UPDATE sessions SET running_turn_id = %s, node_id = %s WHERE id = %s "
                                     "AND running_turn_id IS NULL", c["id"], node.node_id, c["session_id"])
                if not taken:
                    continue
                return await self._start(tx, node, c["id"], limits)
        return None

    async def _start(self, tx: Tx, node: NodeIdentity, rid: str, limits: OrgLimits) -> dict[str, Any]:
        await tx.run("UPDATE turns SET status = 'in_progress', node_id = %s, started_at = now() WHERE id = %s",
                     node.node_id, rid)
        turn = await tx.one("SELECT * FROM turns WHERE id = %s", rid)
        session = await tx.one("SELECT * FROM sessions WHERE id = %s", turn["session_id"])
        blob = await tx.one("SELECT manifest, object_key FROM package_blobs WHERE digest = %s", session["digest"])
        install = await tx.one("SELECT settings FROM installs WHERE org_id = %s AND name = %s",
                               turn["org_id"], session["install_name"])
        manifest = Manifest.model_validate(blob["manifest"])
        body = json.loads(await self.blobs.get(turn["request_key"]))
        partial = wire.response(rid=rid, body=body, created=turn["created_at"].timestamp(), status="in_progress",
                                metadata={"session_id": session["id"]})
        await _append_event(tx, turn, {"type": "response.in_progress", "response": partial})
        await tx.run("UPDATE turns SET events = %s WHERE id = %s", turn["events"], rid)
        await tx.notify(CHANNEL, rid)
        timeout = min(manifest.run.timeout_s, limits.turn_timeout_s)
        provider = None
        if session["provider_id"]:
            p = await tx.one("SELECT id, name, api FROM providers WHERE id = %s", session["provider_id"])
            if p:
                token = ids.new_secret("turn")
                await tx.run("INSERT INTO turn_tokens (digest, turn_id, org_id, session_id, provider_id, capture, expires_at) "
                             "VALUES (%s, %s, %s, %s, %s, %s, now() + %s * interval '1 second')",
                             ids.digest(token, self.pepper), rid, turn["org_id"], session["id"], p["id"],
                             limits.observability, timeout + TOKEN_GRACE_S)
                provider = {"name": p["name"], "api": p["api"], "token": token}
        files = await tx.all("SELECT id, filename, object_key FROM files WHERE id = ANY(%s) AND deleted_at IS NULL",
                             list(wire.Routing.parse(body).file_ids))
        ops = await tx.all("SELECT id, op, path, object_key FROM container_ops WHERE session_id = %s AND applied_at IS NULL "
                           "ORDER BY created_at", session["id"])
        org, sid = turn["org_id"], session["id"]
        observe = limits.observability
        return {
            "turn_id": rid, "org_id": org, "session_id": sid, "seq": turn["seq"], "model": turn["provider_model"],
            "store": session["store"], "timeout_s": timeout, "observability": observe,
            "package": {"name": manifest.name, "digest": session["digest"], "manifest": manifest.model_dump(),
                        "url": _blob(self.signer, blob["object_key"], "GET")},
            "request_url": _blob(self.signer, turn["request_key"], "GET"),
            "settings": self.packages.open_settings(org, session["install_name"], (install or {}).get("settings") or {}),
            "provider": provider,
            "limits": {"vcpus": limits.vcpus, "memory_mb": limits.memory_mb, "pids": limits.pids,
                       "idle_ttl_s": limits.idle_ttl_s, "workspace_mb": limits.workspace_mb},
            "files": [{"id": f["id"], "filename": f["filename"], "url": _blob(self.signer, f["object_key"], "GET")} for f in files],
            "ops": [{"id": o["id"], "op": o["op"], "path": o["path"],
                     "url": _blob(self.signer, o["object_key"], "GET") if o["object_key"] else None} for o in ops],
            "snapshot": {
                "get": _blob(self.signer, session["snapshot_key"], "GET") if session["snapshot_key"] else None,
                "put": _blob(self.signer, org_key(org, "sessions", sid, "snapshot.tgz"), "PUT", timeout + BLOB_TTL_S),
                "manifest_put": _blob(self.signer, org_key(org, "sessions", sid, "manifest.json"), "PUT", timeout + BLOB_TTL_S),
                "key": org_key(org, "sessions", sid, "snapshot.tgz"),
                "manifest_key": org_key(org, "sessions", sid, "manifest.json"),
            },
            "logs": {"stdout": _blob(self.signer, org_key(org, "turns", rid, "stdout.log"), "PUT", timeout + BLOB_TTL_S),
                     "stderr": _blob(self.signer, org_key(org, "turns", rid, "stderr.log"), "PUT", timeout + BLOB_TTL_S)}
            if observe else None,
        }

    # ── usage from nodes ───────────────────────────────────────────────────────────────────────────────────────────
    async def check_usage_scope(self, node: NodeIdentity, rows: list[dict[str, Any]]) -> None:
        """A dedicated node reports usage only for its own organization."""
        if node.org_id and any(r.get("org_id") != node.org_id for r in rows):
            raise Forbidden("a dedicated node reports usage only for its organization")

    # ── lost nodes ─────────────────────────────────────────────────────────────────────────────────────────────────
    async def sweep(self) -> list[str]:
        failed: list[str] = []
        async with self.db.platform() as tx:
            lost = [r["id"] for r in await tx.all("SELECT id FROM nodes WHERE status = 'active' AND "
                                                  "last_seen_at < now() - %s * interval '1 second'", LOST_AFTER_S)]
            if not lost:
                return failed
            for turn in await tx.all("SELECT * FROM turns WHERE status = 'in_progress' AND node_id = ANY(%s) FOR UPDATE",
                                     lost):
                await conclude(tx, self.blobs, turn, Status.FAILED, reason=Reason.NODE_LOST,
                               message="the node running this turn stopped responding")
                failed.append(turn["id"])
            await tx.run("UPDATE sessions SET node_id = NULL WHERE node_id = ANY(%s)", lost)
        return failed
