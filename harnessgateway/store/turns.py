"""Sessions, turns and their events: the queue, the stream, and the record of what happened (spec §5, §6, §17)."""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.handover import PastTurn
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.response import TurnView, envelope_event, response_object, terminal_event_type
from harnessgateway.errors import Conflict, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.db import Connection, Database

CONTROL = "hg_control"                         # the channel turn runners listen on
QUEUED = "queued"                              # on it: a turn is waiting
CANCEL = "cancel:"                             # on it: stop this turn
FINAL = ("completed", "failed", "cancelled", "incomplete")
LOG_TAIL = 64 * 1024
_FOLLOW_POLL_S = 5.0                           # how long a follower waits before looking again unprompted


@dataclass(frozen=True)
class Turn:
    id: str
    org_id: str
    session_id: str
    parent_id: str | None
    number: int
    status: str
    reason: str | None
    code: str | None
    message: str | None
    incomplete_reason: str | None
    environment: str
    harness: str
    harness_version: str
    digest: str
    mind_dir: str | None
    provider: str | None
    api: str | None
    model: str | None
    image_id: str | None
    request: dict[str, Any] | None
    output: list[dict[str, Any]]
    usage: dict[str, Any] | None
    snapshot_id: str | None
    handover: str
    background: bool
    store: bool
    cancel_requested: bool
    key_id: str | None
    placeholders: dict[str, Any]
    runner_id: str | None
    log_tail: str
    command: str | None
    created_at: datetime
    started_at: datetime | None
    restored_at: datetime | None
    ran_at: datetime | None
    exited_at: datetime | None
    saved_at: datetime | None
    completed_at: datetime | None
    deleted_at: datetime | None

    @property
    def final(self) -> bool:
        return self.status in FINAL

    @property
    def path(self) -> ModelPath:
        """What the turn runs with, as a model string in full."""
        return ModelPath(harness=self.harness, environment=self.environment, provider=self.provider, model=self.model)

    @property
    def outcome(self) -> Outcome | None:
        if not self.final:
            return None
        return Outcome(self.status, self.reason, self.message, self.code, self.incomplete_reason)  # type: ignore[arg-type]

    def view(self) -> TurnView:
        return TurnView(id=self.id, session_id=self.session_id, created_at=int(self.created_at.timestamp()),
                        status=self.status, model=str(self.path), request=self.request or {}, output=self.output,
                        usage=self.usage, outcome=self.outcome)

    def response(self) -> dict[str, Any]:
        return response_object(self.view())


@dataclass(frozen=True)
class Session:
    id: str
    head_turn_id: str | None
    forked_from_turn_id: str | None
    volume_snapshot_id: str | None
    created_at: datetime
    last_active_at: datetime


@dataclass(frozen=True)
class ChainTurn:
    """An earlier turn of a chain, as the handover and the history need it."""

    id: str
    harness: str
    mind_dir: str | None
    status: str
    input: Any                                 # None once the turn is deleted
    output: list[dict[str, Any]]

    @property
    def past(self) -> PastTurn:
        return PastTurn(self.harness, self.mind_dir)


_COLUMNS = ", ".join(field.name for field in fields(Turn))
_SESSION_COLUMNS = ", ".join(field.name for field in fields(Session))


def _turn(row: Mapping[str, Any]) -> Turn:
    return Turn(**row)


def turn_channel(turn_id: str) -> str:
    return turn_id


class Turns:
    def __init__(self, db: Database) -> None:
        self._db = db

    # making and reading

    async def create(self, org_id: str, *, resolved: Resolved, request: Mapping[str, Any], parent: Turn | None,
                     key_id: str | None) -> Turn:
        """Queue a turn. With no parent it starts a session; with the head of a session as its parent it
        continues that session; with any other parent it starts a session forked from that turn (spec §6.2)."""
        turn_id = new_id("turn")
        harness, provider = resolved.harness, resolved.provider
        async with self._db.org(org_id) as conn:
            session_id = await self._session_for(conn, org_id, parent)
            row = await (await conn.execute(
                "INSERT INTO turns (id, org_id, session_id, parent_id, number, status, environment, harness, "
                "harness_version, digest, mind_dir, provider, api, model, request, background, store, key_id) "
                "VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                f"RETURNING {_COLUMNS}",
                [turn_id, org_id, session_id, parent.id if parent else None, parent.number + 1 if parent else 1,
                 resolved.environment.name, harness.name, harness.manifest.version, harness.digest,
                 harness.manifest.mind, provider.name if provider else None, resolved.api, resolved.model,
                 Jsonb(dict(request)), bool(request.get("background", False)), bool(request.get("store", True)),
                 key_id])).fetchone()
            assert row is not None
            turn = _turn(row)
            await conn.execute("UPDATE sessions SET head_turn_id = %s, last_active_at = now() WHERE id = %s",
                               [turn_id, session_id])
            await self._event(conn, turn, 0, envelope_event("response.created", turn.response(), sequence_number=0))
            await conn.execute("SELECT pg_notify(%s, %s)", [CONTROL, QUEUED])
        return turn

    @staticmethod
    async def _session_for(conn: Connection, org_id: str, parent: Turn | None) -> str:
        if parent is not None:
            row = await (await conn.execute(
                "SELECT head_turn_id FROM sessions WHERE org_id = %s AND id = %s FOR UPDATE",
                [org_id, parent.session_id])).fetchone()
            if row is not None and row["head_turn_id"] == parent.id:
                return parent.session_id
        session_id = new_id("session")
        await conn.execute("INSERT INTO sessions (id, org_id, forked_from_turn_id) VALUES (%s, %s, %s)",
                           [session_id, org_id, parent.id if parent else None])
        return session_id

    async def get(self, org_id: str, turn_id: str) -> Turn:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM turns WHERE org_id = %s AND id = %s AND deleted_at IS NULL",
                [org_id, turn_id])).fetchone()
        if row is None:
            raise NotFound(f"there is no response {turn_id}")
        return _turn(row)

    async def session(self, org_id: str, session_id: str) -> Session:
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                f"SELECT {_SESSION_COLUMNS} FROM sessions WHERE org_id = %s AND id = %s",
                [org_id, session_id])).fetchone()
        if row is None:
            raise NotFound(f"there is no session {session_id}")
        return Session(**row)

    async def chain(self, org_id: str, turn_id: str) -> list[ChainTurn]:
        """The turns before this one, back to the root of its session and through forks, oldest first."""
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                "WITH RECURSIVE chain AS ("
                "  SELECT p.* FROM turns t JOIN turns p ON p.id = t.parent_id WHERE t.org_id = %s AND t.id = %s"
                "  UNION ALL"
                "  SELECT p.* FROM chain c JOIN turns p ON p.id = c.parent_id"
                ") SELECT id, harness, mind_dir, status, request -> 'input' AS input, output "
                "FROM chain WHERE org_id = %s ORDER BY number", [org_id, turn_id, org_id])).fetchall()
        return [ChainTurn(**row) for row in rows]

    # the queue

    async def claim(self, runner_id: str) -> Turn | None:
        """Take the turn that has waited longest, for this runner; or None when none waits."""
        async with self._db.system() as conn:
            row = await (await conn.execute(
                "UPDATE turns SET status = 'in_progress', runner_id = %s, started_at = now(), heartbeat_at = now() "
                "WHERE id = (SELECT id FROM turns WHERE status = 'queued' ORDER BY created_at "
                f"           FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING {_COLUMNS}", [runner_id])).fetchone()
            if row is None:
                return None
            turn = _turn(row)
            await conn.execute("SELECT set_config('hg.org_id', %s, true)", [turn.org_id])
            await self._event(conn, turn, 1,
                              envelope_event("response.in_progress", turn.response(), sequence_number=1))
        return turn

    async def prepare(self, turn: Turn, *, image_id: str, handover: str, step_token_hash: str,
                      proxy_token_hash: str, placeholders: Mapping[str, Any]) -> None:
        """What a runner decides before the first step: the image, the handover, and the turn's tokens."""
        async with self._db.org(turn.org_id) as conn:
            await conn.execute(
                "UPDATE turns SET image_id = %s, handover = %s, step_token_hash = %s, proxy_token_hash = %s, "
                "placeholders = %s WHERE id = %s",
                [image_id, handover, step_token_hash, proxy_token_hash, Jsonb(dict(placeholders)), turn.id])

    async def stage(self, turn: Turn, name: str, *, command: str | None = None) -> None:
        """Record when a stage of the turn was reached: restored, ran, exited or saved."""
        assert name in ("restored", "ran", "exited", "saved")
        async with self._db.org(turn.org_id) as conn:
            await conn.execute(f"UPDATE turns SET {name}_at = now(), command = COALESCE(%s, command) WHERE id = %s",
                               [command, turn.id])

    async def heartbeat(self, runner_id: str) -> list[str]:
        """Say that this runner is alive. The ids of its turns that have been asked to stop."""
        async with self._db.system() as conn:
            rows = await (await conn.execute(
                "UPDATE turns SET heartbeat_at = now() WHERE runner_id = %s AND status = 'in_progress' "
                "RETURNING id, cancel_requested", [runner_id])).fetchall()
        return [row["id"] for row in rows if row["cancel_requested"]]

    async def abandoned(self, *, silent_for_s: float) -> list[Turn]:
        """Turns in progress whose runner has not been heard from for this long."""
        async with self._db.system() as conn:
            rows = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM turns WHERE status = 'in_progress' "
                "AND heartbeat_at < now() - make_interval(secs => %s)", [silent_for_s])).fetchall()
        return [_turn(row) for row in rows]

    async def by_step_token(self, token_hash: str) -> Turn | None:
        """The turn a step token belongs to. The token dies when the turn is final."""
        async with self._db.system() as conn:
            row = await (await conn.execute(
                f"SELECT {_COLUMNS} FROM turns WHERE step_token_hash = %s", [token_hash])).fetchone()
        return _turn(row) if row else None

    # the stream

    @staticmethod
    async def _event(conn: Connection, turn: Turn, seq: int, data: Mapping[str, Any]) -> None:
        await conn.execute("INSERT INTO events (org_id, turn_id, seq, data) VALUES (%s, %s, %s, %s)",
                           [turn.org_id, turn.id, seq, Jsonb(dict(data))])
        await conn.execute("SELECT pg_notify(%s, %s)", [turn_channel(turn.id), str(seq)])

    async def add_event(self, turn: Turn, event: Mapping[str, Any]) -> None:
        """Store one numbered output event and tell whoever is following the turn."""
        async with self._db.org(turn.org_id) as conn:
            await self._event(conn, turn, event["sequence_number"], event)

    async def events(self, org_id: str, turn_id: str, *, after: int = -1) -> list[dict[str, Any]]:
        async with self._db.org(org_id) as conn:
            rows = await (await conn.execute(
                "SELECT data FROM events WHERE org_id = %s AND turn_id = %s AND seq > %s ORDER BY seq",
                [org_id, turn_id, after])).fetchall()
        return [row["data"] for row in rows]

    async def follow(self, org_id: str, turn_id: str, *, after: int = -1) -> AsyncIterator[dict[str, Any]]:
        """The turn's events after `after`, then each new one as it is stored, ending with its final event."""
        ending = {terminal_event_type(status) for status in FINAL}
        async with self._db.listen(turn_channel(turn_id)) as listener:
            while True:
                for event in await self.events(org_id, turn_id, after=after):
                    yield event
                    after = event["sequence_number"]
                    if event["type"] in ending:
                        return
                await listener.next(_FOLLOW_POLL_S)

    # ending

    async def conclude(self, turn: Turn, outcome: Outcome, *, output: list[dict[str, Any]] | None = None,
                       usage: dict[str, Any] | None = None, snapshot_id: str | None = None, log: str = "",
                       snapshot_bytes: int = 0) -> Turn:
        """Make a turn final: its status and reason, its output, its last event, and its usage record."""
        async with self._db.org(turn.org_id) as conn:
            return await self._conclude(conn, turn.id, outcome, output or [], usage, snapshot_id, log, snapshot_bytes)

    async def _conclude(self, conn: Connection, turn_id: str, outcome: Outcome, output: list[dict[str, Any]],
                        usage: dict[str, Any] | None, snapshot_id: str | None, log: str,
                        snapshot_bytes: int) -> Turn:
        row = await (await conn.execute(
            "UPDATE turns SET status = %s, reason = %s, code = %s, message = %s, incomplete_reason = %s, "
            "output = %s, usage = %s, snapshot_id = %s, log_tail = %s, completed_at = now(), "
            "step_token_hash = NULL, proxy_token_hash = NULL, placeholders = '{}', runner_id = NULL "
            f"WHERE id = %s AND status IN ('queued', 'in_progress') RETURNING {_COLUMNS}",
            [outcome.status, outcome.reason, outcome.code, outcome.message, outcome.incomplete_reason,
             Jsonb(output), Jsonb(usage) if usage is not None else None, snapshot_id, log[-LOG_TAIL:],
             turn_id])).fetchone()
        if row is None:
            raise Conflict(f"response {turn_id} is already final")
        turn = _turn(row)
        last = await (await conn.execute(
            "SELECT COALESCE(max(seq), -1) AS seq FROM events WHERE turn_id = %s", [turn_id])).fetchone()
        assert last is not None
        seq = last["seq"] + 1
        await self._event(conn, turn, seq,
                          envelope_event(terminal_event_type(turn.status), turn.response(), sequence_number=seq))
        await conn.execute("UPDATE sessions SET last_active_at = now() WHERE id = %s", [turn.session_id])
        ran = (turn.exited_at - turn.ran_at).total_seconds() if turn.ran_at and turn.exited_at else 0.0
        tokens = usage or {}
        await conn.execute(
            "INSERT INTO usage (turn_id, org_id, status, harness, provider, model, environment, input_tokens, "
            "output_tokens, sandbox_seconds, snapshot_bytes) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            [turn.id, turn.org_id, turn.status, turn.harness, turn.provider, turn.model, turn.environment,
             tokens.get("input_tokens", 0), tokens.get("output_tokens", 0), ran, snapshot_bytes])
        return turn

    async def cancel(self, org_id: str, turn_id: str) -> Turn:
        """Ask for a turn to stop (spec §6.7). One still waiting is cancelled here and now; one in progress is
        stopped by its runner; one that is final is returned as it is."""
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT status FROM turns WHERE org_id = %s AND id = %s AND deleted_at IS NULL FOR UPDATE",
                [org_id, turn_id])).fetchone()
            if row is None:
                raise NotFound(f"there is no response {turn_id}")
            if row["status"] == "queued":
                return await self._conclude(conn, turn_id, Outcome("cancelled", "cancelled", "the turn was cancelled"),
                                            [], None, None, "", 0)
            if row["status"] == "in_progress":
                await conn.execute("UPDATE turns SET cancel_requested = true WHERE id = %s", [turn_id])
                await conn.execute("SELECT pg_notify(%s, %s)", [CONTROL, CANCEL + turn_id])
        return await self.get(org_id, turn_id)

    async def delete(self, org_id: str, turn_id: str) -> None:
        """Remove a turn's request, output and events (spec §6.8). Its row stays: later turns descend from it."""
        async with self._db.org(org_id) as conn:
            row = await (await conn.execute(
                "SELECT status FROM turns WHERE org_id = %s AND id = %s AND deleted_at IS NULL FOR UPDATE",
                [org_id, turn_id])).fetchone()
            if row is None:
                raise NotFound(f"there is no response {turn_id}")
            if row["status"] not in FINAL:
                raise Conflict(f"response {turn_id} is {row['status']}: cancel it before deleting it")
            await conn.execute(
                "UPDATE turns SET deleted_at = now(), request = NULL, output = '[]', log_tail = '' WHERE id = %s",
                [turn_id])
            await conn.execute("DELETE FROM events WHERE turn_id = %s", [turn_id])

    # the session's volumes

    async def volumes_hold(self, org_id: str, session_id: str, snapshot_id: str | None) -> None:
        """Record which snapshot the session's volumes hold; None: they are about to change (spec §12.5)."""
        async with self._db.org(org_id) as conn:
            await conn.execute(
                "UPDATE sessions SET volume_snapshot_id = %s, volume_used_at = now() WHERE org_id = %s AND id = %s",
                [snapshot_id, org_id, session_id])

    async def idle_volumes(self, *, idle_for_s: float) -> list[tuple[str, str]]:
        """Sessions whose volumes have not been used for this long, as (organization, session). Each is
        recorded as having no volumes: the caller removes them."""
        async with self._db.system() as conn:
            rows = await (await conn.execute(
                "UPDATE sessions s SET volume_snapshot_id = NULL, volume_used_at = NULL "
                "WHERE volume_used_at < now() - make_interval(secs => %s) AND NOT EXISTS ("
                "  SELECT 1 FROM turns t WHERE t.session_id = s.id AND t.status IN ('queued', 'in_progress')) "
                "RETURNING org_id, id", [idle_for_s])).fetchall()
        return [(row["org_id"], row["id"]) for row in rows]
