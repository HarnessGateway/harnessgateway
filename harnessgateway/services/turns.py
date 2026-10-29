"""Turns from the caller's side: a Responses request in, a Response out (SPEC §4, §6.3).

`Turns.create` routes `model` to a harness and a provider, resolves the turn's MCP servers (taking the caller's own
credentials out of the request), lets extensions admit it, and queues it on its session. `EventBus` wakes every stream
following a turn, across API replicas, through Postgres NOTIFY. `record_events` and `conclude` are the writes every
side of a turn shares."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncIterator
from typing import Any

from .. import ids
from ..db import Database, Tx, dumps
from ..domain import contract, egress, mcp
from ..domain.manifest import Manifest
from ..domain.turn import Reason, Status
from ..errors import Conflict, Invalid, NotFound
from ..extensions import Admission, Extensions, TurnSummary
from .objects import Objects, org_key
from .principal import Principal
from .vault import Vault

CHANNEL = "hg_turn_events"
KEEPALIVE_S = 15
IDEMPOTENCY_KEY_MAX = 200
log = logging.getLogger("harnessgateway.turns")


def _ts(dt: Any) -> float:
    return float(dt.timestamp()) if dt else 0.0


class EventBus:
    """One LISTEN connection per API process; each follower of a turn waits on an asyncio.Event for it."""

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
                for waiters in self._waiters.values():
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


async def record_events(tx: Tx, turn: dict[str, Any], events: list[dict[str, Any]]) -> None:
    """Number events from the turn's counter and store them: deltas in live_events, the rest in turn_events."""
    if not events:
        return
    kept: list[tuple[Any, ...]] = []
    live: list[tuple[Any, ...]] = []
    for event in events:
        seq = turn["events"]
        turn["events"] = seq + 1
        row = (turn["id"], seq, turn["org_id"], {**event, "sequence_number": seq})
        (live if event["type"] in contract.DELTAS else kept).append(row)
    await tx.many("INSERT INTO turn_events (turn_id, seq, org_id, data) VALUES (%s, %s, %s, %s)", kept)
    await tx.many("INSERT INTO live_events (turn_id, seq, org_id, data) VALUES (%s, %s, %s, %s)", live)
    await tx.run("UPDATE turns SET events = %s WHERE id = %s", turn["events"], turn["id"])
    await tx.notify(CHANNEL, turn["id"])


async def request_body(objects: Objects, turn: dict[str, Any]) -> dict[str, Any]:
    return json.loads(await objects.get(turn["request_key"]))  # type: ignore[no-any-return]


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
                   ) -> TurnSummary:
    """End a turn, in the transaction holding its row: the final Response stored and its terminal event recorded."""
    assert status.final and not Status(turn["status"]).final, f"{turn['id']}: {turn['status']} -> {status}"
    body = await request_body(objects, turn)
    meta = {**(metadata or {}), "session_id": turn["session_id"]}
    if reason:
        meta["hg_reason"] = reason.value
        if status == Status.FAILED and error is None:
            error = {"code": "server_error", "message": message or reason.value.replace("_", " ")}
    now = time.time()
    try:
        doc = contract.response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=status.value,
                                output=output, usage=usage, error=error, incomplete=incomplete, metadata=meta,
                                completed=now)
    except contract.OutputError as e:
        status, message, reason = Status.FAILED, str(e), Reason.INVALID_OUTPUT
        meta["hg_reason"] = reason.value
        doc = contract.response(rid=turn["id"], body=body, created=_ts(turn["created_at"]), status=status.value,
                                error={"code": "server_error", "message": message}, metadata=meta, completed=now)
    moved = await tx.run("UPDATE turns SET status = %s, response = %s, hg_reason = %s, hg_message = %s, ended_at = now() "
                         "WHERE id = %s AND status = %s", status.value, doc, meta.get("hg_reason"), message[:4000] or None,
                         turn["id"], turn["status"])
    assert moved == 1, "the turn row is held by this transaction"
    turn["status"] = status.value
    await record_events(tx, turn, [{"type": contract.TERMINAL_EVENT[status.value], "response": doc}])
    session = await tx.get("SELECT harness FROM sessions WHERE id = %s", turn["session_id"])
    return TurnSummary(id=turn["id"], org_id=turn["org_id"], session_id=turn["session_id"], harness=session["harness"],
                       model=turn["model"], status=status.value, reason=meta.get("hg_reason"), usage=doc.get("usage"),
                       created_at=_ts(turn["created_at"]), ended_at=now, metadata=doc.get("metadata") or {})


class Turns:
    def __init__(self, db: Database, objects: Objects, vault: Vault, bus: EventBus, extensions: Extensions):
        self.db, self.objects, self.vault, self.bus, self.ext = db, objects, vault, bus, extensions

    # ── create ─────────────────────────────────────────────────────────────────────────────────────────────────────
    async def create(self, p: Principal, body: dict[str, Any], idempotency_key: str = "") -> dict[str, Any]:
        routing = contract.Routing.parse(body)
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
                turn = await self._queue(tx, p, rid, body, routing, request_key)
                if idempotency_key:
                    await tx.run("INSERT INTO idempotency (org_id, key, request_digest, turn_id) VALUES (%s, %s, %s, %s)",
                                 p.org_id, idempotency_key, digest, rid)
        except BaseException:
            await self.objects.delete(request_key)
            raise
        return turn

    async def _replay(self, p: Principal, digest: str, key: str) -> dict[str, Any] | None:
        async with self.db.tenant(p.org_id) as tx:
            row = await tx.one("SELECT request_digest, turn_id FROM idempotency WHERE key = %s", key)
        if not row:
            return None
        if row["request_digest"] != digest:
            raise Conflict("this Idempotency-Key was used with a different request", param="Idempotency-Key")
        return await self.row(p, row["turn_id"])

    async def _queue(self, tx: Tx, p: Principal, rid: str, body: dict[str, Any], routing: contract.Routing,
                     request_key: str) -> dict[str, Any]:
        path = routing.path
        harness = await tx.one("SELECT * FROM harnesses WHERE name = %s", path.harness)
        if not harness:
            raise NotFound(f"no harness {path.harness} installed", param="model", code="model_not_found")
        prev, session = None, None
        if routing.previous_response_id:
            prev = await tx.one("SELECT session_id, provider_id, provider_model FROM turns WHERE id = %s",
                                routing.previous_response_id)
            if not prev:
                raise NotFound(f"no response {routing.previous_response_id}", param="previous_response_id")
            session = await tx.get("SELECT * FROM sessions WHERE id = %s FOR UPDATE", prev["session_id"])
            if session["status"] != "active" or not session["store"]:
                raise Invalid(f"{routing.previous_response_id} belongs to a session that was not stored or has ended",
                              param="previous_response_id")
            if session["harness"] != path.harness:
                raise Conflict(f"that session runs {session['harness']}, not {path.harness}: a session keeps its harness; "
                               "the provider and model may change", param="model", code="harness_mismatch")
        manifest = Manifest.model_validate(session["manifest"] if session else harness["manifest"])
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
        org = await tx.get("SELECT egress FROM orgs WHERE id = %s", p.org_id)
        resolved = mcp.resolve(manifest.mcp, body, egress.Policy.of(org["egress"], manifest.run_entries()))
        await self.objects.put(request_key, dumps(resolved.body).encode(), org_id=p.org_id, kind="request")
        if session is None:
            session = {"id": ids.new("session"), "turns": 0}
            await tx.run("INSERT INTO sessions (id, org_id, harness, base_digest, overlay_digest, manifest, provider_id, "
                         "model, store) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", session["id"], p.org_id, path.harness,
                         harness["base_digest"], harness["overlay_digest"], harness["manifest"], provider_id, model,
                         routing.store)
        seq = session["turns"] + 1
        await tx.run("UPDATE sessions SET turns = %s, provider_id = %s, model = %s, last_active_at = now() WHERE id = %s",
                     seq, provider_id, model, session["id"])
        await tx.run("INSERT INTO turns (id, org_id, session_id, seq, status, model, provider_id, provider_model, stream, "
                     "background, request_key, mcp, previous_id) VALUES (%s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, "
                     "%s::jsonb, %s)", rid, p.org_id, session["id"], seq, body["model"], provider_id, model,
                     routing.stream, routing.background, request_key, dumps(resolved.servers),
                     routing.previous_response_id)
        if resolved.bindings:
            sealed = await self.vault.seal(p.org_id, f"turn/{rid}/bindings", dumps(
                [{"placeholder": b.placeholder, "value": b.value, "hosts": list(b.hosts)} for b in resolved.bindings]))
            await tx.run("INSERT INTO turn_bindings (turn_id, org_id, sealed) VALUES (%s, %s, %s)", rid, p.org_id, sealed)
        turn = await tx.get("SELECT * FROM turns WHERE id = %s", rid)
        partial = contract.response(rid=rid, body=resolved.body, created=_ts(turn["created_at"]), status="queued",
                                    metadata={"session_id": session["id"]})
        await record_events(tx, turn, [{"type": "response.created", "response": partial}])
        return turn

    @staticmethod
    async def _provider(tx: Tx, manifest: Manifest, path: contract.ModelPath, harness: dict[str, Any],
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
    async def row(self, p: Principal, rid: str) -> dict[str, Any]:
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one("SELECT * FROM turns WHERE id = %s", ids.check(rid, "turn"))
        if not turn:
            raise NotFound(f"no response {rid}")
        return turn

    async def response(self, p: Principal, rid: str) -> dict[str, Any]:
        turn = await self.row(p, rid)
        if turn["response"] is not None:
            return turn["response"]  # type: ignore[no-any-return]
        return contract.response(rid=turn["id"], body=await request_body(self.objects, turn),
                                 created=_ts(turn["created_at"]), status=turn["status"],
                                 metadata={"session_id": turn["session_id"]})

    async def input_items(self, p: Principal, rid: str) -> list[dict[str, Any]]:
        turn = await self.row(p, rid)
        items = contract.input_items(await request_body(self.objects, turn))
        return [{"id": f"item_{rid[5:21]}_{i}", **item} for i, item in enumerate(items)]

    async def follow(self, p: Principal, rid: str, after: int = -1) -> AsyncIterator[dict[str, Any] | None]:
        """The turn's events numbered above `after`, live until its terminal event; None is a keep-alive."""
        await self.row(p, rid)
        while True:
            waiter = self.bus.waiter(rid)
            try:
                async with self.db.tenant(p.org_id) as tx:
                    rows = await tx.all("SELECT seq, data FROM turn_events WHERE turn_id = %s AND seq > %s UNION ALL "
                                        "SELECT seq, data FROM live_events WHERE turn_id = %s AND seq > %s ORDER BY seq",
                                        rid, after, rid, after)
                for r in rows:
                    after = r["seq"]
                    yield r["data"]
                    if r["data"]["type"] in contract.TERMINAL:
                        return
                if not rows:
                    try:
                        await asyncio.wait_for(waiter.wait(), timeout=KEEPALIVE_S)
                    except TimeoutError:
                        yield None
            finally:
                self.bus.drop(rid, waiter)

    async def wait(self, p: Principal, rid: str) -> dict[str, Any]:
        async for event in self.follow(p, rid):
            if event and event["type"] in contract.TERMINAL:
                break
        return await self.response(p, rid)

    # ── cancel and delete ──────────────────────────────────────────────────────────────────────────────────────────
    async def cancel(self, p: Principal, rid: str) -> dict[str, Any]:
        """A queued turn ends now; a running one is stopped by its node (SIGTERM, then the package's grace)."""
        ended = None
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one("SELECT * FROM turns WHERE id = %s FOR UPDATE", ids.check(rid, "turn"))
            if not turn:
                raise NotFound(f"no response {rid}")
            if turn["status"] == Status.QUEUED:
                ended = await conclude(tx, self.objects, turn, Status.CANCELLED, reason=Reason.CANCELLED)
                await tx.run("DELETE FROM turn_bindings WHERE turn_id = %s", rid)
            else:
                await tx.run("UPDATE turns SET cancel = %s WHERE id = %s AND cancel IS NULL AND (status = 'in_progress' "
                             "OR EXISTS (SELECT 1 FROM sessions s WHERE s.running_turn_id = turns.id))",
                             Reason.CANCELLED.value, rid)
        if ended:
            await self.ext.turn_ended(ended)
        return await self.response(p, rid)

    async def delete(self, p: Principal, rid: str) -> None:
        async with self.db.tenant(p.org_id) as tx:
            turn = await tx.one("SELECT status FROM turns WHERE id = %s", ids.check(rid, "turn"))
            if not turn:
                raise NotFound(f"no response {rid}")
            if not Status(turn["status"]).final or await tx.one("SELECT 1 FROM sessions WHERE running_turn_id = %s", rid):
                raise Conflict(f"{rid} is still running; cancel it first")
            await tx.run("DELETE FROM live_events WHERE turn_id = %s", rid)
            await tx.run("DELETE FROM traces WHERE response_id = %s", rid)
            await tx.run("DELETE FROM turns WHERE id = %s", rid)
        await self.objects.delete_prefix(org_key(p.org_id, "turns", rid, ""))
