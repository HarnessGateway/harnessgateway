"""Turns: a Responses request in, a Response out.

`Turns` takes requests from clients: it routes `model` to an installed package and a provider, admits the turn against
the organization's ceilings, queues it on its session, and answers reads, cancels and deletes. `Delivery` takes the
package's output from the node running it: it validates each line against the Responses schema, numbers and stores
the events, and concludes the turn with the final `Response`. `EventBus` wakes every stream following a turn when its
events change, across API replicas, through Postgres NOTIFY.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from .. import ids
from ..blobs import Blobs, org_key
from ..db import Database, Tx, dumps
from ..domain import wire
from ..domain.orglimits import OrgLimits
from ..domain.package import Manifest
from ..domain.turn import FINAL, Reason, Status
from ..errors import Conflict, Invalid, NotFound, QuotaExceeded
from .metering import Metering
from .principal import Principal
from .tenancy import Tenancy

CHANNEL = "hg_turn_events"
LOG_TAIL = 16 * 1024
log = logging.getLogger("harnessgateway.turns")


def _final(status: str) -> bool:
    return Status(status) in FINAL


def _ts(dt: Any) -> float:
    return dt.timestamp() if dt else 0.0


class EventBus:
    """One LISTEN connection per API process; every follower of a turn waits on an asyncio.Event for it."""

    def __init__(self, db: Database):
        self.db = db
        self._waiters: dict[str, set[asyncio.Event]] = defaultdict(set)
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _listen(self) -> None:
        while True:
            try:
                async with self.db.listen(CHANNEL) as cx:
                    async for note in cx.notifies():
                        for ev in self._waiters.get(note.payload, ()):
                            ev.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("event bus connection lost; reconnecting")
                for waiters in self._waiters.values():   # wake everyone: they re-read the store
                    for ev in waiters:
                        ev.set()
                await asyncio.sleep(1)

    def waiter(self, rid: str) -> asyncio.Event:
        ev = asyncio.Event()
        self._waiters[rid].add(ev)
        return ev

    def drop(self, rid: str, ev: asyncio.Event) -> None:
        self._waiters[rid].discard(ev)
        if not self._waiters[rid]:
            self._waiters.pop(rid, None)


async def _append_event(tx: Tx, turn: dict[str, Any], event: dict[str, Any]) -> int:
    seq = turn["events"]
    await tx.run("INSERT INTO turn_events (turn_id, seq, org_id, data) VALUES (%s, %s, %s, %s)",
                 turn["id"], seq, turn["org_id"], {**event, "sequence_number": seq})
    turn["events"] = seq + 1
    return seq


class Turns:
    def __init__(self, db: Database, blobs: Blobs, tenancy: Tenancy, metering: Metering, bus: EventBus):
        self.db, self.blobs, self.tenancy, self.metering, self.bus = db, blobs, tenancy, metering, bus

    # ── create ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def create(self, p: Principal, body: dict[str, Any], idempotency_key: str = "") -> dict[str, Any]:
        routing = wire.Routing.parse(body)
        if idempotency_key:
            replay = await self._replay(p, body, idempotency_key)
            if replay:
                return replay
        limits = await self.tenancy.limits(p.org_id)
        await self._admit(p.org_id, limits)
        rid = ids.new("turn")
        request_key = org_key(p.org_id, "turns", rid, "request.json")
        await self.blobs.put(request_key, dumps(body).encode(), org_id=p.org_id, kind="request")
        try:
            async with self.db.tenant(p.org_id) as tx:
                turn = await self._queue(tx, p, rid, body, routing, request_key)
                if idempotency_key:
                    await tx.run("INSERT INTO idempotency (org_id, key, request_digest, turn_id) VALUES (%s, %s, %s, %s)",
                                 p.org_id, idempotency_key[:200], ids.sha256(dumps(body).encode()), rid)
                await tx.notify(CHANNEL, rid)
        except Exception:
            await self.blobs.delete(request_key)
            raise
        return turn

    async def _replay(self, p: Principal, body: dict[str, Any], key: str) -> dict[str, Any] | None:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT request_digest, turn_id FROM idempotency WHERE key = %s", key[:200])
        if not row:
            return None
        if row["request_digest"] != ids.sha256(dumps(body).encode()):
            raise Conflict("this Idempotency-Key was used with a different request", param="Idempotency-Key")
        return await self.row(p, row["turn_id"])

    async def _admit(self, org_id: str, limits: OrgLimits) -> None:
        async with self.db.tenant(org_id) as tx:
            recent = await tx.value("SELECT count(*) FROM turns WHERE created_at > now() - interval '1 minute'")
        if recent >= limits.requests_per_minute:
            raise QuotaExceeded(f"the organization's limit of {limits.requests_per_minute} requests per minute is reached",
                                code="rate_limit_exceeded")
        if limits.monthly_charge_usd and await self.metering.month_to_date_charge(org_id) >= limits.monthly_charge_usd:
            raise QuotaExceeded("the organization's monthly spending limit is reached", code="spending_limit_reached")
        if await self.metering.storage_bytes(org_id) >= limits.storage_gb * 1024 ** 3:
            raise QuotaExceeded(f"the organization's storage limit of {limits.storage_gb} GB is reached; delete sessions "
                                "or files", code="storage_limit_reached")

    async def _queue(self, tx: Tx, p: Principal, rid: str, body: dict[str, Any], routing: wire.Routing,
                     request_key: str) -> dict[str, Any]:
        path = routing.path
        install = await tx.one("SELECT i.*, b.manifest FROM installs i JOIN package_blobs b ON b.digest = i.digest "
                               "WHERE i.name = %s", path.harness)
        if not install:
            raise NotFound(f"no harness {path.harness} installed", param="model", code="model_not_found")
        prev, session = None, None
        if routing.previous_response_id:
            prev = await tx.one("SELECT session_id, provider_id, provider_model FROM turns WHERE id = %s",
                                routing.previous_response_id)
            if not prev:
                raise NotFound(f"no response {routing.previous_response_id}", param="previous_response_id")
            session = await tx.one("SELECT * FROM sessions WHERE id = %s FOR UPDATE", prev["session_id"])
            assert session is not None
            if session["status"] != "active" or not session["store"]:
                raise Invalid(f"{routing.previous_response_id} belongs to a session that was not stored or has ended",
                              param="previous_response_id")
            if session["install_name"] != path.harness:
                raise Invalid(f"that session runs {session['install_name']}, not {path.harness}", param="model")
            manifest = Manifest.model_validate((await tx.one("SELECT manifest FROM package_blobs WHERE digest = %s",
                                                             session["digest"]))["manifest"])
        else:
            manifest = Manifest.model_validate(install["manifest"])
        provider = await self._provider(tx, manifest, path, install, session)
        provider_id = provider["id"] if provider else None
        # a continuation keeps its model unless it names another, or moves to another provider (which may not have it)
        kept = prev["provider_model"] if prev and (prev["provider_id"] or session["provider_id"]) == provider_id else ""
        model = path.model or kept or install["default_model"] or manifest.defaults.model
        if routing.file_ids:
            found = {r["id"] for r in await tx.all("SELECT id FROM files WHERE id = ANY(%s) AND deleted_at IS NULL",
                                                   list(routing.file_ids))}
            missing = [f for f in routing.file_ids if f not in found]
            if missing:
                raise NotFound(f"no file {missing[0]}", param="input")
        if session is None:
            sid = ids.new("session")
            await tx.run("INSERT INTO sessions (id, org_id, install_name, digest, provider_id, store) "
                         "VALUES (%s, %s, %s, %s, %s, %s)", sid, p.org_id, path.harness, install["digest"],
                         provider_id, routing.store)
            session = {"id": sid, "turns": 0}
        seq = session["turns"] + 1
        # the session follows the provider its latest turn chose, so a continuation that names none stays on it
        await tx.run("UPDATE sessions SET turns = %s, provider_id = %s, last_active_at = now() WHERE id = %s",
                     seq, provider_id, session["id"])
        await tx.run("INSERT INTO turns (id, org_id, session_id, seq, status, model, provider_id, provider_model, stream, "
                     "background, request_key, previous_id) VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s)",
                     rid, p.org_id, session["id"], seq, body["model"], provider_id, model, routing.stream,
                     routing.background, request_key, routing.previous_response_id)
        turn = await tx.one("SELECT * FROM turns WHERE id = %s", rid)
        assert turn is not None
        partial = wire.response(rid=rid, body=body, created=_ts(turn["created_at"]), status="queued",
                                metadata={"session_id": session["id"]})
        await _append_event(tx, turn, {"type": "response.created", "response": partial})
        await tx.run("UPDATE turns SET events = %s WHERE id = %s", turn["events"], rid)
        return {**turn, "body": body, "session_id": session["id"]}

    async def _provider(self, tx: Tx, manifest: Manifest, path: wire.ModelPath, install: dict[str, Any],
                        session: dict[str, Any] | None) -> dict[str, Any] | None:
        if manifest.provider is None:
            if path.provider:
                raise Invalid(f"{manifest.name} runs without a provider; use model={manifest.name}", param="model")
            return None
        if path.provider:
            row = await tx.one("SELECT id, name, api FROM providers WHERE name = %s", path.provider)
        elif session is not None and session["provider_id"]:
            row = await tx.one("SELECT id, name, api FROM providers WHERE id = %s", session["provider_id"])
        elif install["default_provider"]:
            row = await tx.one("SELECT id, name, api FROM providers WHERE name = %s", install["default_provider"])
        else:
            rows = await tx.all("SELECT id, name, api FROM providers WHERE api = ANY(%s)", list(manifest.provider.apis))
            if len(rows) != 1:
                raise Invalid(f"name the provider: model={manifest.name}/<provider>/<model>, or set the harness's "
                              "default_provider", param="model")
            row = rows[0]
        if not row:
            raise NotFound(f"no provider {path.provider or install['default_provider']}", param="model")
        if row["api"] not in manifest.provider.apis:
            raise Invalid(f"{manifest.name} speaks {', '.join(manifest.provider.apis)}; provider {row['name']} is "
                          f"{row['api']}", param="model")
        return row

    # ── reads ──────────────────────────────────────────────────────────────────────────────────────────────────────
    async def row(self, p: Principal, rid: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one("SELECT * FROM turns WHERE id = %s", ids.check(rid, "turn"))
        if not turn:
            raise NotFound(f"no response {rid}")
        return turn

    async def response(self, p: Principal, rid: str) -> dict[str, Any]:
        turn = await self.row(p, rid)
        if turn["response"] is not None:
            return turn["response"]
        body = json.loads(await self.blobs.get(turn["request_key"]))
        return wire.response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=turn["status"],
                             metadata={"session_id": turn["session_id"]})

    async def input_items(self, p: Principal, rid: str) -> list[dict[str, Any]]:
        turn = await self.row(p, rid)
        items = wire.input_items(json.loads(await self.blobs.get(turn["request_key"])))
        return [{"id": f"item_{rid[5:21]}_{i}", **item} for i, item in enumerate(items)]

    async def follow(self, p: Principal, rid: str, after: int = -1) -> AsyncIterator[dict[str, Any] | None]:
        """The turn's events with sequence numbers above `after`, live until its terminal event. Yields None while
        waiting (a keep-alive) at least every 15 seconds."""
        await self.row(p, rid)
        while True:
            waiter = self.bus.waiter(rid)
            try:
                async with self.db.tenant(p.org_id) as tx:
                    rows = await tx.all("SELECT seq, data FROM turn_events WHERE turn_id = %s AND seq > %s ORDER BY seq",
                                        rid, after)
                    status = await tx.value("SELECT status FROM turns WHERE id = %s", rid)
                for r in rows:
                    after = r["seq"]
                    yield r["data"]
                    if r["data"]["type"] in wire.TERMINAL:
                        return
                if not rows and status is not None and _final(status) and await self._ended(p, rid, after):
                    return
                if not rows:
                    try:
                        await asyncio.wait_for(waiter.wait(), timeout=15)
                    except TimeoutError:
                        yield None
            finally:
                self.bus.drop(rid, waiter)

    async def _ended(self, p: Principal, rid: str, after: int) -> bool:
        async with self.db.tenant(p.org_id) as tx:
            return not await tx.one("SELECT 1 FROM turn_events WHERE turn_id = %s AND seq > %s", rid, after)

    async def wait(self, p: Principal, rid: str) -> dict[str, Any]:
        async for event in self.follow(p, rid):
            if event and event["type"] in wire.TERMINAL:
                break
        return await self.response(p, rid)

    # ── cancel and delete ──────────────────────────────────────────────────────────────────────────────────────────
    async def cancel(self, p: Principal, rid: str, reason: Reason = Reason.CANCELLED) -> dict[str, Any]:
        """A queued turn ends now; a running one is stopped by its node (SIGTERM, then the package's grace)."""
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one("SELECT * FROM turns WHERE id = %s FOR UPDATE", ids.check(rid, "turn"))
            if not turn:
                raise NotFound(f"no response {rid}")
            if turn["status"] == Status.QUEUED:
                await conclude(tx, self.blobs, turn, Status.CANCELLED, reason=reason)
            elif turn["status"] == Status.IN_PROGRESS and not turn["cancel"]:
                await tx.run("UPDATE turns SET cancel = %s WHERE id = %s", reason.value, rid)
        return await self.response(p, rid)

    async def delete(self, p: Principal, rid: str) -> None:
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one("SELECT status, request_key FROM turns WHERE id = %s", ids.check(rid, "turn"))
            if not turn:
                raise NotFound(f"no response {rid}")
            if not _final(turn["status"]):
                raise Conflict(f"{rid} is still running; cancel it first")
            await tx.run("DELETE FROM turns WHERE id = %s", rid)
        await self.blobs.delete_prefix(org_key(p.org_id, "turns", rid, ""))


async def conclude(tx: Tx, blobs: Blobs, turn: dict[str, Any], status: Status, *, output: list[dict[str, Any]] | None = None,
                   usage: dict[str, Any] | None = None, error: dict[str, Any] | None = None,
                   incomplete: dict[str, Any] | None = None, metadata: dict[str, str] | None = None,
                   reason: Reason | None = None, message: str = "", exit_code: int | None = None,
                   stderr_tail: str = "") -> dict[str, Any]:
    """End a turn (in a transaction holding its row): the final Response stored, the terminal event appended, the
    session freed for its next turn, the turn's provider token revoked."""
    assert status in FINAL and not _final(turn["status"]), f"{turn['id']}: {turn['status']} -> {status}"
    body = json.loads(await blobs.get(turn["request_key"]))
    meta = {**(metadata or {}), "session_id": turn["session_id"]}
    if reason:
        meta["hg_reason"] = reason.value
        if status == Status.FAILED and error is None:
            error = {"code": "server_error", "message": message or reason.value.replace("_", " ")}
    try:
        doc = wire.response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=status.value,
                            output=output, usage=usage, error=error, incomplete=incomplete, metadata=meta,
                            completed=wire.now())
    except wire.HarnessOutputError as e:
        meta["hg_reason"] = Reason.INVALID_OUTPUT.value
        status = Status.FAILED
        doc = wire.response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=status.value,
                            error={"code": "server_error", "message": str(e)}, metadata=meta, completed=wire.now())
    n = await tx.run("UPDATE turns SET status = %s, response = %s, hg_reason = %s, hg_message = %s, exit_code = %s, "
                     "stderr_tail = %s, ended_at = now() WHERE id = %s AND status = %s",
                     status.value, doc, meta.get("hg_reason"), message or None, exit_code, stderr_tail[-LOG_TAIL:],
                     turn["id"], turn["status"])
    assert n == 1, "the turn row is held by this transaction"
    await tx.run("UPDATE sessions SET running_turn_id = NULL, last_active_at = now() WHERE id = %s AND running_turn_id = %s",
                 turn["session_id"], turn["id"])
    await tx.run("UPDATE turn_tokens SET revoked_at = now() WHERE turn_id = %s AND revoked_at IS NULL", turn["id"])
    await _append_event(tx, turn, {"type": wire.TERMINAL_EVENT[status.value], "response": doc})
    await tx.run("UPDATE turns SET events = %s WHERE id = %s", turn["events"], turn["id"])
    await tx.notify(CHANNEL, turn["id"])
    return doc


class Delivery:
    """The node's side of a turn's output."""

    def __init__(self, db: Database, blobs: Blobs):
        self.db, self.blobs = db, blobs

    async def _held(self, tx: Tx, node_id: str, rid: str) -> dict[str, Any] | None:
        turn = await tx.one("SELECT * FROM turns WHERE id = %s FOR UPDATE", ids.check(rid, "turn"))
        if not turn:
            raise NotFound(f"no turn {rid}")
        if turn["node_id"] != node_id:
            raise Conflict(f"{rid} is not this node's turn")
        return turn if turn["status"] == Status.IN_PROGRESS else None

    async def append(self, node_id: str, rid: str, batch: int, lines: list[str]) -> dict[str, Any]:
        """A batch of stdout lines, in order. Batches are numbered so a retried one is applied once. Returns
        {"stop": reason} when the node must stop the turn (its output broke the schema)."""
        async with self.db.platform() as tx:
            turn = await self._held(tx, node_id, rid)
            if turn is None or batch <= turn["last_batch"]:
                return {"stop": None}
            out = wire.TurnOutput()
            if turn["terminal"] is not None:
                out.terminal = wire.Terminal(turn["terminal"]["status"], turn["terminal"]["response"])
            logs: list[str] = []
            stop = None
            for text in lines:
                try:
                    kind, value = out.line(text)
                except wire.HarnessOutputError as e:
                    await tx.run("UPDATE turns SET hg_reason = %s, hg_message = %s, cancel = coalesce(cancel, %s) "
                                 "WHERE id = %s", Reason.INVALID_OUTPUT.value, str(e)[:4000], Reason.INVALID_OUTPUT.value, rid)
                    stop = Reason.INVALID_OUTPUT.value
                    break
                if kind == "event":
                    await _append_event(tx, turn, value)
                elif kind == "log":
                    logs.append(text)
            terminal = {"status": out.terminal.status, "response": out.terminal.response} if out.terminal else None
            tail = (turn["log_tail"] + "".join(f"{x}\n" for x in logs))[-LOG_TAIL:]
            await tx.run("UPDATE turns SET events = %s, last_batch = %s, terminal = %s, log_tail = %s WHERE id = %s",
                         turn["events"], batch, terminal, tail, rid)
            await tx.notify(CHANNEL, rid)
        return {"stop": stop}

    async def finish(self, node_id: str, rid: str, report: dict[str, Any]) -> None:
        """The node's report ends the turn. Outcome: exited | timed_out | stopped | failed."""
        outcome = report.get("outcome")
        assert outcome in ("exited", "timed_out", "stopped", "failed"), f"unknown outcome {outcome!r}"
        async with self.db.platform() as tx:
            turn = await self._held(tx, node_id, rid)
            if turn is None:
                return
            stderr = str(report.get("stderr_tail", ""))[-LOG_TAIL:]
            code = report.get("exit_code")
            snapshot = report.get("snapshot")
            if snapshot:
                await tx.run("UPDATE sessions SET snapshot_key = %s, snapshot_bytes = %s, manifest_key = %s WHERE id = %s",
                             snapshot["key"], int(snapshot["bytes"]), snapshot["manifest_key"], turn["session_id"])
            if report.get("applied_ops"):
                await tx.run("UPDATE container_ops SET applied_at = now() WHERE id = ANY(%s) AND session_id = %s",
                             list(report["applied_ops"]), turn["session_id"])
            kwargs: dict[str, Any] = {"exit_code": code, "stderr_tail": stderr}
            if turn["hg_reason"] == Reason.INVALID_OUTPUT:
                await conclude(tx, self.blobs, turn, Status.FAILED, reason=Reason.INVALID_OUTPUT,
                               message=turn["hg_message"] or "", **kwargs)
            elif turn["cancel"] and (outcome == "stopped" or turn["terminal"] is None):
                await conclude(tx, self.blobs, turn, Status.CANCELLED, reason=Reason(turn["cancel"]),
                               output=await self._items(tx, turn, wire.TurnOutput()), **kwargs)
            elif outcome == "failed":
                reason = Reason(report.get("reason") or Reason.SANDBOX_FAILED)
                await conclude(tx, self.blobs, turn, Status.FAILED, reason=reason,
                               message=str(report.get("error", ""))[:4000], **kwargs)
            elif turn["terminal"] is not None:
                await self._from_terminal(tx, turn, **kwargs)
            elif outcome == "timed_out":
                await conclude(tx, self.blobs, turn, Status.INCOMPLETE, reason=Reason.TIMEOUT,
                               output=await self._items(tx, turn, wire.TurnOutput()), **kwargs)
            else:
                last = " | ".join(stderr.strip().splitlines()[-3:])
                await conclude(tx, self.blobs, turn, Status.FAILED, reason=Reason.NO_TERMINAL_EVENT,
                               message=f"request.sh exited {code} without a terminal event"
                                       + (f"; stderr: {last[-1000:]}" if last else ""), **kwargs)

    @staticmethod
    async def _items(tx: Tx, turn: dict[str, Any], out: wire.TurnOutput) -> list[dict[str, Any]]:
        """The items the package finished, in order (each was validated when it arrived)."""
        for r in await tx.all("SELECT data FROM turn_events WHERE turn_id = %s AND data->>'type' = "
                              "'response.output_item.done' ORDER BY seq", turn["id"]):
            out.add_item(r["data"]["item"])
        return [out.items[k] for k in out.order]

    async def _from_terminal(self, tx: Tx, turn: dict[str, Any], **kwargs: Any) -> None:
        out = wire.TurnOutput(terminal=wire.Terminal(turn["terminal"]["status"], turn["terminal"]["response"]))
        await self._items(tx, turn, out)
        try:
            status, output, usage, error, incomplete, meta = out.outcome()
        except wire.HarnessOutputError as e:
            await conclude(tx, self.blobs, turn, Status.FAILED, reason=Reason.INVALID_OUTPUT, message=str(e), **kwargs)
            return
        await conclude(tx, self.blobs, turn, Status(status), output=output, usage=usage, error=error,
                       incomplete=incomplete, metadata=meta, **kwargs)
