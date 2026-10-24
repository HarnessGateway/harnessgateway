"""The scheduler: the node's side of a turn. Nodes pull: `claim` hands out the oldest queued turn
whose organization has a free slot, preferring the node that already holds the session's trees, with
URLs good for that turn's blobs and a token good for that turn's provider; `commands` carries cancels
and steers back on each heartbeat; `result` ends the turn, mapping the package's events and shape
onto the response through `contract.wire`. A node that stops heartbeating loses its turns, not the
sessions."""
from __future__ import annotations

import time
from dataclasses import asdict
from typing import Any

from ..backends.sandboxes.docker import PROVIDER_URL
from ..contract import wire
from ..domain.harnesses import Harnesses
from ..domain.nodes import LOST_AFTER_S, Node, Nodes
from ..domain.orgs import Orgs
from ..domain.providers import Providers
from ..domain.sessions import RUNNING, Sessions, Turn, home_blob, snapshot_blob, turn_blob
from ..errors import Conflict, Invalid, NotFound

AFFINITY_S = 3                                   # how long a queued turn waits for the node that holds its session


class Scheduler:
    def __init__(self, sessions: Sessions, harnesses: Harnesses, providers: Providers, orgs: Orgs, nodes: Nodes, gateway_url: str,
                 on_final=None):
        self.sessions, self.harnesses, self.providers, self.orgs, self.nodes = sessions, harnesses, providers, orgs, nodes
        self.records, self.blobs, self.gateway_url = sessions.records, sessions.blobs, gateway_url
        self.on_final = on_final                     # called with the final Turn: metering, webhooks

    def claim(self, node: Node) -> dict[str, Any] | None:
        """The oldest queued turn whose organization has a free slot, as a job, or None. A turn whose
        session's trees are on another live node waits a moment for that node first."""
        alive = {n.id for n in self.nodes.list() if n.alive}
        for org, rid, doc in self.records.where("response", "status", "queued", limit=50):
            try:
                ceilings = self.orgs.get(org).ceilings
            except NotFound:
                continue
            if self.records.count("response", org, "running", RUNNING) >= ceilings.max_runs:
                continue
            holder = self.records.get("session", org, doc["session"]) or {}
            held_by = holder.get("node", "")
            if held_by and held_by != node.id and held_by in alive and time.time() - doc["created"] < AFFINITY_S:
                continue
            r = Turn(**{**doc, "status": "claimed", "running": RUNNING, "worker": node.id, "started": time.time()})
            if self.records.update_if("response", org, rid, "status", "queued", asdict(r)):
                return self.job(r, ceilings)
        return None

    def job(self, r: Turn, ceilings) -> dict[str, Any]:
        s = self.sessions.session_by_id(r.session)
        config = self.harnesses.config_at(r.org, r.harness, r.version)
        provider = self.providers.get(r.org, r.provider)
        timeout = min(config.timeout_s, ceilings.run_timeout_s)
        token = self.providers.issue_token(r.org, provider.name, s.id, r.id, provider.api, timeout + 120)
        home = home_blob(r.org, r.member, r.harness)
        get, put = self.blobs.presign_get, self.blobs.presign_put
        ttl = int(timeout) + 600
        return {"id": r.id, "session": s.id, "org": r.org, "team": r.team, "member": r.member, "harness": r.harness, "version": r.version,
                "turn": r.turn, "new_session": r.turn == 1, "previous": r.previous or "", "model": r.model,
                "store": r.store, "config": config.as_json(), "limits": ceilings.as_dict(), "timeout_s": timeout, "token": token,
                "provider": {"name": provider.name, "api": provider.api, "path": provider.path, "url": PROVIDER_URL + provider.path},
                "gateway_url": self.gateway_url, "snapshot": s.snapshot,
                "urls": {"folder": get(self.harnesses.blob_key(r.org, r.harness, r.version), ttl),
                         "request": get(turn_blob(r.org, s.id, r.id, "request.json"), ttl),
                         "snapshot": get(s.snapshot_key, ttl) if s.snapshot_key else "",
                         "home": get(home, ttl) if self.blobs.exists(home) else "",
                         "files": get(r.files, ttl) if r.files else "",
                         "snapshot_put": put(snapshot_blob(r.org, s.id), ttl) if r.store else "",
                         "home_put": put(home, ttl),
                         "door_log_put": put(turn_blob(r.org, s.id, r.id, "door.log"), ttl)}}

    def started(self, node: Node, rid: str) -> None:
        r = self.sessions.turn_by_id(rid)
        if r.worker == node.id and r.status == "claimed":
            r.status = "in_progress"
            self.sessions.put_turn(r)

    def chunks_in(self, node: Node, rid: str, chunks: list[dict[str, Any]]) -> None:
        r = self.sessions.turn_by_id(rid)
        if r.worker != node.id or r.final:
            raise Conflict(f"{rid} is not this node's running turn")
        for c in chunks:
            self.records.put("chunk", r.org, f"{rid}:{int(c['seq']):08d}", {"stream": c["stream"], "text": c["text"]})

    def commands(self, node: Node, running: list[str]) -> dict[str, dict[str, Any]]:
        """What the node should do to the turns it reports; also its heartbeat."""
        self.nodes.touch(node, len(running))
        out: dict[str, dict[str, Any]] = {}
        for rid in running:
            try:
                r = self.sessions.turn_by_id(rid)
            except NotFound:
                out[rid] = {"cancel": True, "interrupts": []}
                continue
            out[rid] = {"cancel": r.cancel or r.final, "interrupts": r.interrupts}
        return out

    def result(self, node: Node, rid: str, body: dict[str, Any]) -> Turn:
        """The node's report ends the turn: the package's events and shape become the response."""
        r = self.sessions.turn_by_id(rid)
        if r.worker != node.id:
            raise Conflict(f"{rid} is not this node's turn")
        if r.final:
            return r
        s = self.sessions.session_by_id(r.session)
        ceilings = self.orgs.get(r.org).ceilings
        cap = ceilings.output_mb * 1024 * 1024
        kind, code = body.get("outcome", "failed"), body.get("exit_code")
        stdout, stderr = str(body.get("stdout", ""))[:cap], str(body.get("stderr", ""))[:cap]
        r.truncated = bool(body.get("truncated")) or any(len(str(body.get(k, ""))) > cap for k in ("stdout", "stderr", "response"))
        r.exit_code, r.ended = code, time.time()
        events, r.log = wire.events_of(stdout)
        r.stderr = stderr
        shape = wire.Shape()
        try:
            if body.get("shaped"):
                shape = wire.from_response_sh(str(body.get("response", "")))
            else:
                shape = wire.from_events(events)
            if not shape.output and not events and r.log.strip() and kind in ("exited", "stopped", "timed_out"):
                shape.output = [wire.message_item(rid, r.log)]   # a package that printed plain text: its text is the answer
                shape.metadata["raw_stdout"] = "true"
        except Invalid as e:
            shape = wire.Shape(status="failed", error={"code": "package_output_invalid", "message": str(e)})
        r.output, r.usage, r.shaped = shape.output, shape.usage, shape.metadata
        if shape.status:
            r.status, r.error, r.incomplete = shape.status, shape.error, shape.incomplete
        elif kind == "failed":
            r.status, r.error = "failed", {"code": "server_error", "message": str(body.get("error", "the turn could not run"))}
        elif kind == "timed_out":
            r.status, r.incomplete = "incomplete", {"reason": "timeout"}
        elif kind in ("stopped", "killed"):
            r.status, r.incomplete = "cancelled", {"reason": r.stop_reason or "cancelled"}
        elif code == 0:
            r.status = "completed"
        else:
            r.status, r.error = "failed", {"code": "exit_status", "message": f"the package exited {code}"}
        if r.status == "completed" and shape.error:
            r.status = "failed"
        r.running = "0"
        self.sessions.put_turn(r)
        if r.store and body.get("snapshot"):
            s.snapshot_key, s.snapshot = snapshot_blob(r.org, s.id), rid
        if not r.store:
            s.stored = False
        s.running, s.node = "", node.id
        self.sessions.put_session(s)
        self.sessions.trace_in(s, "response", rid, self.sessions.response_object(r))
        if self.on_final:
            self.on_final(r, body)
        return r

    def sweep_lost(self) -> list[str]:
        """Turns whose node stopped heartbeating are failed; their sessions go on."""
        lost: list[str] = []
        stale = {n.id for n in self.nodes.list() if time.time() - n.last_seen > LOST_AFTER_S}
        for org, rid, doc in self.records.where("response", "running", RUNNING, limit=1000):
            if doc["worker"] in stale:
                node = Node(doc["worker"], "", 0, 0)
                self.result(node, rid, {"outcome": "failed", "error": "the node running it was lost"})
                lost.append(rid)
        return lost
