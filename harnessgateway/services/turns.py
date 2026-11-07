"""Turns from the caller's side (SPEC §9.1, §9.3, §9.7): a Responses request in, a Response out.

`Turns.create` routes `model` to a harness and a provider, resolves the turn's MCP servers (taking the caller's own
credentials out of the request), lets extensions admit it, and queues it on its session: the one `previous_response_id`
names when that response is the session's last and the harness is the same, else a fork, a new session starting from
the snapshot after that response (its checkpoint). A fork onto another harness carries the workspace only, plus the
conversation, which every turn receives as its session's history. `record_events` and `conclude` are the writes every
side of a turn shares; events go to the durable log here and to the live stream after the transaction commits."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from .. import ids
from ..contract import (
    DELTAS,
    FINAL,
    TERMINAL,
    TERMINAL_EVENT,
    ModelPath,
    OutputError,
    Routing,
    history_entry,
    input_items,
    response,
)
from ..domain import egress, mcp
from ..domain.access import Principal
from ..domain.manifest import Manifest
from ..domain.turn import Reason, Status
from ..errors import Conflict, Invalid, NotFound
from ..extensions import Admission, Extensions, TurnSummary
from ..store.db import Database, Tx, dumps
from ..store.moments import Moments
from ..store.objects import Objects, org_key
from .harnesses import find
from .pages import own
from .snapshots import Snapshots
from .vault import Vault

if TYPE_CHECKING:
    from .boxes import Boxes

KEEPALIVE_S = 15
POLL_S = 1.0
IDEMPOTENCY_KEY_MAX = 200
log = logging.getLogger("harnessgateway.turns")


def _ts(dt: Any) -> float:
    return float(dt.timestamp()) if dt else 0.0


def end_user(body: dict[str, Any]) -> str | None:
    """The request's `user`: the application's own end user, kept on the session and the turn."""
    user = body.get("user")
    if user is None:
        return None
    if not isinstance(user, str) or not 1 <= len(user) <= 256:
        raise Invalid("user is a string of at most 256 characters", param="user")
    return user


async def record_events(tx: Tx, turn: dict[str, Any], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number events from the turn's counter; store the non-deltas durably; return them all, numbered, for the
    live stream once the transaction has committed."""
    numbered: list[dict[str, Any]] = []
    kept: list[tuple[Any, ...]] = []
    for event in events:
        seq = turn["events"]
        turn["events"] = seq + 1
        ev = {**event, "sequence_number": seq}
        numbered.append(ev)
        if event["type"] not in DELTAS:
            kept.append((turn["id"], seq, turn["org_id"], ev))
    if numbered:
        await tx.many("INSERT INTO turn_events (turn_id, seq, org_id, data) VALUES (%s, %s, %s, %s)", kept)
        await tx.run("UPDATE turns SET events = %s WHERE id = %s", turn["events"], turn["id"])
    return numbered


async def request_body(objects: Objects, turn: dict[str, Any]) -> dict[str, Any]:
    return json.loads(await objects.get(turn["request_key"]))  # type: ignore[no-any-return]


async def history(tx: Tx, objects: Objects, session: dict[str, Any], before_seq: int) -> list[dict[str, Any]]:
    """The session's earlier turns in order, as a package receives them (SPEC §5.3): through every fork back to the
    root, each turn as `history_entry`. A turn whose request is gone is left out."""
    chain: list[tuple[dict[str, Any], int]] = []
    s, upto = session, before_seq - 1
    while True:
        chain.append((s, upto))
        if not s.get("forked_from"):
            break
        prev = await tx.one("SELECT session_id, seq FROM turns WHERE id = %s", s["forked_from"])
        parent = await tx.one("SELECT * FROM sessions WHERE id = %s", prev["session_id"]) if prev else None
        if not prev or not parent:
            break
        s, upto = parent, prev["seq"]
    out: list[dict[str, Any]] = []
    for s, upto in reversed(chain):
        rows = await tx.all("SELECT id, status, request_key, response FROM turns WHERE session_id = %s AND seq <= %s "
                            "AND status <> 'queued' ORDER BY seq", s["id"], upto)
        for t in rows:
            try:
                body = await request_body(objects, t)
            except KeyError:
                continue
            out.append(history_entry(t["id"], t["status"], body, t["response"]))
    return out


async def completed_items(tx: Tx, rid: str) -> list[dict[str, Any]]:
    """The output items the package finished, in order, each once (the last version of an id wins)."""
    items: dict[str, dict[str, Any]] = {}
    for r in await tx.all("SELECT data FROM turn_events WHERE turn_id = %s AND data->>'type' = "
                          "'response.output_item.done' ORDER BY seq", rid):
        item = r["data"]["item"]
        items[str(item.get("id") or len(items))] = item
    return list(items.values())


async def conclude(tx: Tx, objects: Objects, turn: dict[str, Any], status: Status, *,
                   output: list[dict[str, Any]] | None = None, usage: dict[str, Any] | None = None,
                   error: dict[str, Any] | None = None, incomplete: dict[str, Any] | None = None,
                   metadata: dict[str, str] | None = None, reason: Reason | None = None, message: str = ""
                   ) -> tuple[TurnSummary, list[dict[str, Any]]]:
    """End a turn, in the transaction holding its row: the final Response stored and its terminal event recorded.
    Returns the summary for extensions and the numbered events for the stream."""
    assert status.final and not Status(turn["status"]).final, f"{turn['id']}: {turn['status']} -> {status}"
    body = await request_body(objects, turn)
    meta = {**(metadata or {}), "session_id": turn["session_id"]}
    if reason:
        meta["hg_reason"] = reason.value
        if status == Status.FAILED and error is None:
            error = {"code": "server_error", "message": message or reason.value.replace("_", " ")}
    now = time.time()
    try:
        doc = response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=status.value, output=output,
                       usage=usage, error=error, incomplete=incomplete, metadata=meta, completed=now)
    except OutputError as e:
        status, message, reason = Status.FAILED, str(e), Reason.INVALID_OUTPUT
        meta["hg_reason"] = reason.value
        doc = response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=status.value,
                       error={"code": "server_error", "message": message}, metadata=meta, completed=now)
    moved = await tx.run("UPDATE turns SET status = %s, response = %s, hg_reason = %s, hg_message = %s, ended_at = now() "
                         "WHERE id = %s AND status = %s", status.value, doc, meta.get("hg_reason"), message[:4000] or None,
                         turn["id"], turn["status"])
    assert moved == 1, "the turn row is held by this transaction"
    turn["status"] = status.value
    events = await record_events(tx, turn, [{"type": TERMINAL_EVENT[status.value], "response": doc}])
    session = await tx.get("SELECT harness FROM sessions WHERE id = %s", turn["session_id"])
    summary = TurnSummary(id=turn["id"], org_id=turn["org_id"], session_id=turn["session_id"], harness=session["harness"],
                          model=turn["model"], status=status.value, reason=meta.get("hg_reason"), usage=doc.get("usage"),
                          created_at=_ts(turn["created_at"]), ended_at=now, metadata=doc.get("metadata") or {})
    return summary, events


class Turns:
    def __init__(self, db: Database, objects: Objects, vault: Vault, moments: Moments, extensions: Extensions,
                 boxes: Boxes, snapshots: Snapshots):
        self.db, self.objects, self.vault, self.moments, self.ext = db, objects, vault, moments, extensions
        self.boxes, self.snapshots = boxes, snapshots

    async def publish(self, rid: str, events: list[dict[str, Any]], *, ended: bool = False) -> None:
        """Numbered events onto the live stream, after their transaction committed."""
        await self.moments.publish(rid, events)
        if ended:
            await self.moments.end_stream(rid)

    # ── create ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def create(self, p: Principal, body: dict[str, Any], idempotency_key: str = "") -> dict[str, Any]:
        p.require("responses", "create")
        routing = Routing.parse(body)
        user = end_user(body)
        if len(idempotency_key) > IDEMPOTENCY_KEY_MAX:
            raise Invalid(f"Idempotency-Key is at most {IDEMPOTENCY_KEY_MAX} characters", param="Idempotency-Key")
        digest = ids.sha256(dumps(body).encode())
        if idempotency_key and (replay := await self._replay(p, digest, idempotency_key)):
            return replay
        await self.ext.admit(p, Admission(p.org_id, routing.path.harness, str(body.get("model")),
                                          routing.previous_response_id, routing.background))
        rid = ids.new("turn")
        request_key = org_key(p.org_id, "turns", rid, "request.json")
        try:
            async with self.db.tenant(p.org_id) as tx:
                turn, events, after = await self._queue(tx, p, rid, body, routing, request_key, user)
                if idempotency_key:
                    await tx.run("INSERT INTO idempotency (org_id, key, request_digest, turn_id) VALUES (%s, %s, %s, %s)",
                                 p.org_id, idempotency_key, digest, rid)
        except BaseException:
            await self.objects.delete(request_key)
            raise
        await self.publish(rid, events)
        await self.boxes.after_commit(after)
        return turn

    async def _replay(self, p: Principal, digest: str, key: str) -> dict[str, Any] | None:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT request_digest, turn_id FROM idempotency WHERE key = %s", key)
        if not row:
            return None
        if row["request_digest"] != digest:
            raise Conflict("this Idempotency-Key was used with a different request", param="Idempotency-Key")
        return await self.row(p, row["turn_id"])

    async def _queue(self, tx: Tx, p: Principal, rid: str, body: dict[str, Any], routing: Routing, request_key: str,
                     user: str | None) -> tuple[dict[str, Any], list[dict[str, Any]], Any]:
        path = routing.path
        harness = await find(tx, path.harness)
        prev, session, fork_from, switch = None, None, None, False
        if routing.previous_response_id:
            prev = await tx.one("SELECT id, session_id, seq, status, provider_id, provider_model, snapshot_id "
                                "FROM turns WHERE id = %s", routing.previous_response_id)
            session = await tx.one("SELECT * FROM sessions WHERE id = %s FOR UPDATE", prev["session_id"]) if prev else None
            if not prev or not session or (p.owns_only and session["created_by"] != p.actor):
                raise NotFound(f"no response {routing.previous_response_id}", param="previous_response_id")
            if session["status"] != "active" or not session["store"]:
                raise Invalid(f"{routing.previous_response_id} belongs to a session that was not stored or has ended",
                              param="previous_response_id")
            switch = session["harness"] != path.harness      # another harness: a fork onto it, with the workspace
            if switch or prev["seq"] != session["turns"]:    # and the conversation; an earlier response: the state after it
                fork_from = self._checkpoint_of(prev, session)
        manifest = Manifest.model_validate(harness["manifest"] if session is None or switch else session["manifest"])
        missing = [k for k, s in manifest.settings.items() if s.required and s.default is None and k not in
                   harness["settings"]] + [k for k, s in manifest.secrets.items() if s.required and k not in harness["secrets"]]
        if missing:
            raise Invalid(f"harness {path.harness} needs {', '.join(missing)} set (PATCH /v1/harnesses/{path.harness})",
                          param="model")
        provider = await self._provider(tx, manifest, path, harness, session)
        provider_id = provider["id"] if provider else None
        kept = prev["provider_model"] if prev and session and (prev["provider_id"] or session["provider_id"]) == provider_id \
            else ""
        model = path.model or kept or harness["default_model"] or manifest.defaults.model
        if routing.file_ids:
            found = {r["id"] for r in await tx.all("SELECT id FROM files WHERE id = ANY(%s) AND deleted_at IS NULL",
                                                   list(routing.file_ids))}
            missing_files = [f for f in routing.file_ids if f not in found]
            if missing_files:
                raise NotFound(f"no file {missing_files[0]}", param="input")
        org = await tx.get("SELECT egress, limits, status FROM orgs WHERE id = %s", p.org_id)
        if org["status"] != "active":
            raise Invalid("the organization is suspended", param="model")
        resolved = mcp.resolve(manifest.mcp, body, egress.Policy.of(org["egress"], manifest.run_entries()))
        await self.objects.put(request_key, dumps(resolved.body).encode(), org_id=p.org_id, kind="request")
        if fork_from is not None and session is not None:   # a new session from the checkpoint, pinned like its parent
            parent, sid = session, ids.new("session")       # (or, on a switch, to the new harness)
            pinned = harness if switch else parent
            await tx.run("INSERT INTO sessions (id, org_id, created_by, end_user, harness, digest, manifest, provider_id, "
                         "model, store, forked_from) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                         sid, p.org_id, p.actor, user, path.harness, pinned["digest"], pinned["manifest"], provider_id,
                         model, routing.store, routing.previous_response_id)
            made = await self.snapshots.derive(tx, p.org_id, fork_from, sid, "workspace" if switch else None)
            await tx.run("UPDATE sessions SET snapshot_id = %s WHERE id = %s", made["id"], sid)
            session = {"id": sid, "turns": 0}
        elif session is None:
            session = {"id": ids.new("session"), "turns": 0}
            await tx.run("INSERT INTO sessions (id, org_id, created_by, end_user, harness, digest, manifest, provider_id, "
                         "model, store) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)", session["id"], p.org_id, p.actor,
                         user, path.harness, harness["digest"], harness["manifest"], provider_id, model, routing.store)
        seq = session["turns"] + 1
        await tx.run("UPDATE sessions SET turns = %s, provider_id = %s, model = %s, last_active_at = now() WHERE id = %s",
                     seq, provider_id, model, session["id"])
        await tx.run("INSERT INTO turns (id, org_id, session_id, seq, status, model, provider_id, provider_model, stream, "
                     "background, request_key, mcp, previous_id, actor, end_user) VALUES (%s, %s, %s, %s, 'queued', %s, %s, "
                     "%s, %s, %s, %s, %s::jsonb, %s, %s, %s)", rid, p.org_id, session["id"], seq, body["model"], provider_id,
                     model, routing.stream, routing.background, request_key, dumps(resolved.servers),
                     routing.previous_response_id, p.actor, user)
        if resolved.bindings:
            sealed = await self.vault.seal(p.org_id, f"turn/{rid}/bindings", dumps([b.as_dict() for b in resolved.bindings]))
            await tx.run("INSERT INTO turn_bindings (turn_id, org_id, sealed) VALUES (%s, %s, %s)", rid, p.org_id, sealed)
        turn = await tx.get("SELECT * FROM turns WHERE id = %s", rid)
        partial = response(rid=rid, body=resolved.body, created=_ts(turn["created_at"]), status="queued",
                           metadata={"session_id": session["id"]})
        events = await record_events(tx, turn, [{"type": "response.created", "response": partial}])
        session_row = await tx.get("SELECT * FROM sessions WHERE id = %s", session["id"])
        after = await self.boxes.ensure(tx, p.org_id, session_row, org["limits"])
        return turn, events, after

    @staticmethod
    def _checkpoint_of(prev: dict[str, Any], session: dict[str, Any]) -> str:
        """The snapshot of the state after `prev`: what a fork starts from. A turn's save lands shortly after its end;
        until then there is nothing to fork from yet, and a save that failed leaves nothing to fork from at all."""
        rid = prev["id"]
        if not Status(prev["status"]).final:
            raise Conflict(f"{rid} is still running; continue from it once it has ended", param="previous_response_id")
        if prev["snapshot_id"] is None:
            if session["snapshot_seq"] < prev["seq"]:
                raise Conflict(f"the state after {rid} is not saved yet; try again shortly", param="previous_response_id")
            raise Conflict(f"the state after {rid} was not saved, so nothing can continue from it", param="previous_response_id")
        return str(prev["snapshot_id"])

    @staticmethod
    async def _provider(tx: Tx, manifest: Manifest, path: ModelPath, harness: dict[str, Any],
                        session: dict[str, Any] | None) -> dict[str, Any] | None:
        """Named in `model`, else the session's, else the harness's default, else the organization's only provider of
        an API the package speaks."""
        if manifest.provider is None:
            if path.provider:
                raise Invalid(f"{manifest.name} runs without a model provider; use model={manifest.name}", param="model")
            return None
        if path.provider:
            row = await tx.one("SELECT id, name, api FROM providers WHERE name = %s", path.provider)
        elif session is not None and session["provider_id"]:
            row = await tx.one("SELECT id, name, api FROM providers WHERE id = %s", session["provider_id"])
        elif harness["default_provider"]:
            row = await tx.one("SELECT id, name, api FROM providers WHERE name = %s", harness["default_provider"])
        else:
            rows = await tx.all("SELECT id, name, api FROM providers WHERE api = ANY(%s)", list(manifest.provider.apis))
            if len(rows) != 1:
                raise Invalid(f"name the provider: model={manifest.name}/<provider>/<model>, or set the harness's "
                              "default_provider", param="model")
            row = rows[0]
        if not row:
            raise NotFound(f"no provider {path.provider or harness['default_provider']}", param="model")
        if row["api"] not in manifest.provider.apis:
            raise Invalid(f"{manifest.name} speaks {', '.join(manifest.provider.apis)}; provider {row['name']} is "
                          f"{row['api']}", param="model")
        return row

    # ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def _row(self, tx: Tx, p: Principal, rid: str, lock: bool = False) -> dict[str, Any]:
        clause, args = own(p)
        turn = await tx.one(f"SELECT t.* FROM turns t JOIN sessions s ON s.id = t.session_id WHERE t.id = %s{clause}"
                            f"{' FOR UPDATE OF t' if lock else ''}", ids.check(rid, "turn"), *args)
        if not turn:
            raise NotFound(f"no response {rid}")
        return turn

    async def row(self, p: Principal, rid: str) -> dict[str, Any]:
        p.require("responses", "read")
        async with self.db.tenant(p.org_id) as tx:
            return await self._row(tx, p, rid)

    async def response(self, p: Principal, rid: str) -> dict[str, Any]:
        turn = await self.row(p, rid)
        if turn["response"] is not None:
            return turn["response"]  # type: ignore[no-any-return]
        return response(rid=turn["id"], body=await request_body(self.objects, turn), created=_ts(turn["created_at"]),
                        status=turn["status"], metadata={"session_id": turn["session_id"]})

    async def input_items(self, p: Principal, rid: str) -> list[dict[str, Any]]:
        turn = await self.row(p, rid)
        items = input_items(await request_body(self.objects, turn))
        return [{"id": f"item_{rid[5:21]}_{i}", **item} for i, item in enumerate(items)]

    async def follow(self, p: Principal, rid: str, after: int = -1) -> AsyncIterator[dict[str, Any] | None]:
        """The turn's events numbered above `after`: from the live stream while it exists, else from the durable log,
        followed until the terminal event; None is a keep-alive."""
        turn = await self.row(p, rid)
        if await self.moments.has_stream(rid):
            async for event in self.moments.follow(rid, after, terminal=frozenset(TERMINAL), keepalive_s=KEEPALIVE_S):
                yield event
                if event and event["type"] in TERMINAL:
                    return
        while True:
            async with self.db.tenant(p.org_id) as tx:
                rows = await tx.all("SELECT seq, data FROM turn_events WHERE turn_id = %s AND seq > %s ORDER BY seq", rid, after)
                status = await tx.value("SELECT status FROM turns WHERE id = %s", rid)
            for r in rows:
                after = r["seq"]
                yield r["data"]
                if r["data"]["type"] in TERMINAL:
                    return
            if status in FINAL and not rows:
                return
            turn["status"] = status
            waited = 0.0
            while waited < KEEPALIVE_S:
                await asyncio.sleep(POLL_S)
                waited += POLL_S
                async with self.db.tenant(p.org_id) as tx:
                    newer = await tx.value("SELECT count(*) FROM turn_events WHERE turn_id = %s AND seq > %s", rid, after)
                if newer:
                    break
            else:
                yield None

    async def wait(self, p: Principal, rid: str) -> dict[str, Any]:
        async for event in self.follow(p, rid):
            if event and event["type"] in TERMINAL:
                break
        return await self.response(p, rid)

    # ── cancel and delete ──────────────────────────────────────────────────────────────────────────────────────────
    async def cancel(self, p: Principal, rid: str) -> dict[str, Any]:
        """A queued turn ends now; a running one is stopped by its box (SIGTERM, then the package's grace)."""
        p.require("responses", "cancel")
        ended: TurnSummary | None = None
        events: list[dict[str, Any]] = []
        async with self.db.tenant(p.org_id) as tx:
            turn = await self._row(tx, p, rid, lock=True)
            if turn["status"] == Status.QUEUED:
                ended, events = await conclude(tx, self.objects, turn, Status.CANCELLED, reason=Reason.CANCELLED)
                await tx.run("DELETE FROM turn_bindings WHERE turn_id = %s", rid)
            else:
                await tx.run("UPDATE turns SET cancel = %s WHERE id = %s AND cancel IS NULL AND status = 'in_progress'",
                             Reason.CANCELLED.value, rid)
        if ended:
            await self.publish(rid, events, ended=True)
            await self.ext.turn_ended(ended)
        return await self.response(p, rid)

    async def delete(self, p: Principal, rid: str) -> None:
        p.require("responses", "delete")
        async with self.db.tenant(p.org_id) as tx:
            turn = await self._row(tx, p, rid)
            if not Status(turn["status"]).final:
                raise Conflict(f"{rid} is still running; cancel it first")
            current = await tx.value("SELECT snapshot_id FROM sessions WHERE id = %s", turn["session_id"])
            await tx.run("DELETE FROM traces WHERE response_id = %s", rid)
            await tx.run("DELETE FROM turns WHERE id = %s", rid)
        if turn["snapshot_id"] and turn["snapshot_id"] != current:
            await self.snapshots.release(p.org_id, [turn["snapshot_id"]])
        await self.objects.delete_prefix(org_key(p.org_id, "turns", rid, ""))
