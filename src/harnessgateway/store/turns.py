"""Turns: the queue they wait in, the stream of events each one writes, and how each one ends.

Everything that crosses processes goes through PostgreSQL: a turn is claimed with a row lock, a runner keeps
saying it is alive, and each new event is announced, so the process that answers a request need not be the one
that runs its turn.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import datetime
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.line import NONE, Line
from harnessgateway.domain.outcome import CANCELLED, FINAL, Outcome
from harnessgateway.domain.principal import Principal
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.response import LAST_EVENTS, envelope_event, response_object
from harnessgateway.errors import Conflict, NotFound
from harnessgateway.ids import new_id
from harnessgateway.store.db import Connection, Database, announce

RUNNERS = "hg_runners"                         # the channel runners hear: a turn waits, or one is to be stopped
WAITING = "waiting"
STOP = "stop:"
LOG_MAX = 64 * 1024
_LOOK_AGAIN_S = 5.0                            # how long a follower waits before looking again unprompted
_OPEN = "status IN ('queued', 'in_progress')"


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
    mind_dir: str | None
    provider: str | None
    model: str | None
    request: dict[str, Any] | None
    output: list[dict[str, Any]]
    usage: dict[str, Any] | None
    snapshot_id: str | None
    log: str
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    deleted_at: datetime | None

    @property
    def final(self) -> bool:
        return self.status in FINAL

    @property
    def line(self) -> Line:
        return Line(self.environment, self.harness, self.provider or NONE, self.model or NONE)

    def response(self) -> dict[str, Any]:
        """The turn as the Responses API returns it."""
        outcome = Outcome(self.status, self.reason, self.message, self.code,  # type: ignore[arg-type]
                          self.incomplete_reason) if self.final else None
        return response_object(
            id=self.id, session_id=self.session_id, created_at=int(self.created_at.timestamp()),
            completed_at=int(self.completed_at.timestamp()) if self.completed_at else None, status=self.status,
            model=str(self.line), request=self.request or {}, output=self.output, usage=self.usage, outcome=outcome)


COLUMNS = ", ".join(field.name for field in fields(Turn))


class Turns:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer

    # making and reading

    async def create(self, who: Principal, *, resolved: Resolved, request: Mapping[str, Any],
                     parent: Turn | None, credentials: Sequence[BoundSecret] = ()) -> Turn:
        """Queue a turn. With no parent it starts a session; with a session's latest turn as its parent it
        continues that session; with any other parent it starts a session forked from that turn. The request's
        credentials are kept sealed beside it until the turn ends."""
        turn_id, line, org_id = new_id("response"), resolved.line, who.org_id
        sealed = {secret.placeholder: {"hosts": list(secret.hosts), "value": self._sealer.seal(
            secret.value, org_id, "turns", turn_id, secret.placeholder)} for secret in credentials}
        async with self._db.tx(org_id) as conn:
            session_id = None
            if parent is not None:
                head = await (await conn.execute(
                    "SELECT head_turn_id FROM sessions WHERE org_id = %s AND id = %s FOR UPDATE",
                    [org_id, parent.session_id])).fetchone()
                if head is not None and head["head_turn_id"] == parent.id:
                    session_id = parent.session_id
            if session_id is None:
                session_id = new_id("session")
                await conn.execute("INSERT INTO sessions (id, org_id, forked_from_turn_id) VALUES (%s, %s, %s)",
                                   [session_id, org_id, parent.id if parent else None])
            row = await (await conn.execute(
                "INSERT INTO turns (id, org_id, session_id, parent_id, number, status, environment, harness, "
                "mind_dir, provider, model, request, store, principal) "
                f"VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {COLUMNS}",
                [turn_id, org_id, session_id, parent.id if parent else None, parent.number + 1 if parent else 1,
                 line.environment, line.harness, resolved.harness.manifest.mind,
                 line.provider if resolved.provider else None, resolved.model or None, Jsonb(dict(request)),
                 request.get("store") is not False, who.subject])).fetchone()
            assert row is not None
            turn = Turn(**row)
            await conn.execute("UPDATE sessions SET head_turn_id = %s, last_active_at = now() WHERE id = %s",
                               [turn_id, session_id])
            await self._event(conn, turn.id, envelope_event(turn.response(), 0, "response.created"))
            await announce(conn, RUNNERS, WAITING)
        return turn

    async def get(self, org_id: str, turn_id: str) -> Turn:
        row = await self._db.one(org_id, f"SELECT {COLUMNS} FROM turns WHERE org_id = %s AND id = %s "
                                         "AND deleted_at IS NULL", [org_id, turn_id])
        if row is None:
            raise NotFound(f"there is no response {turn_id}")
        return Turn(**row)

    async def chain(self, org_id: str, turn_id: str) -> list[Turn]:
        """The turns before this one, back to the first of its session and through forks, oldest first."""
        rows = await self._db.all(org_id, (
            "WITH RECURSIVE chain AS ("
            "  SELECT p.* FROM turns t JOIN turns p ON p.id = t.parent_id WHERE t.org_id = %s AND t.id = %s"
            "  UNION ALL SELECT p.* FROM chain c JOIN turns p ON p.id = c.parent_id"
            f") SELECT {COLUMNS} FROM chain WHERE org_id = %s ORDER BY number"), [org_id, turn_id, org_id])
        return [Turn(**row) for row in rows]

    # the queue

    async def claim(self, runner_id: str) -> Turn | None:
        """Take the turn that has waited longest, for this runner; or None when none waits."""
        async with self._db.tx() as conn:
            row = await (await conn.execute(
                "UPDATE turns SET status = 'in_progress', runner_id = %s, started_at = now(), heartbeat_at = now() "
                "WHERE id = (SELECT id FROM turns WHERE status = 'queued' ORDER BY created_at "
                f"           FOR UPDATE SKIP LOCKED LIMIT 1) RETURNING {COLUMNS}", [runner_id])).fetchone()
            if row is None:
                return None
            turn = Turn(**row)
            await self._event(conn, turn.id, envelope_event(turn.response(), 1, "response.in_progress"))
        return turn

    async def heartbeat(self, runner_id: str) -> list[str]:
        """Say that this runner is alive. The ids of its turns that have been asked to stop: a runner that
        missed the announcement learns of it here."""
        rows = await self._db.all(None, "UPDATE turns SET heartbeat_at = now() WHERE runner_id = %s "
                                        "AND status = 'in_progress' RETURNING id, cancel_requested", [runner_id])
        return [row["id"] for row in rows if row["cancel_requested"]]

    async def abandoned(self, *, silent_for_s: float) -> list[Turn]:
        """Turns in progress whose runner has not been heard from for this long."""
        rows = await self._db.all(None, f"SELECT {COLUMNS} FROM turns WHERE status = 'in_progress' "
                                        "AND heartbeat_at < now() - make_interval(secs => %s)", [silent_for_s])
        return [Turn(**row) for row in rows]

    # the stream

    @staticmethod
    async def _event(conn: Connection, turn_id: str, event: Mapping[str, Any]) -> None:
        await conn.execute("INSERT INTO events (turn_id, seq, data) VALUES (%s, %s, %s)",
                           [turn_id, event["sequence_number"], Jsonb(dict(event))])
        await announce(conn, turn_id)

    async def add_event(self, turn: Turn, event: Mapping[str, Any]) -> None:
        """Keep one numbered event, and tell whoever is following the turn."""
        async with self._db.tx(turn.org_id) as conn:
            await self._event(conn, turn.id, event)

    async def follow(self, org_id: str, turn_id: str, *, after: int = -1) -> AsyncIterator[dict[str, Any]]:
        """The turn's events after `after`, then each new one as it is written, ending with its last."""
        async with self._db.listen(turn_id) as listener:
            while True:
                rows = await self._db.all(org_id, (
                    "SELECT e.data FROM events e JOIN turns t ON t.id = e.turn_id "
                    "WHERE t.org_id = %s AND e.turn_id = %s AND e.seq > %s ORDER BY e.seq"), [org_id, turn_id, after])
                for row in rows:
                    yield row["data"]
                    after = row["data"]["sequence_number"]
                    if row["data"]["type"] in LAST_EVENTS:
                        return
                await listener.next(_LOOK_AGAIN_S)

    # ending

    async def conclude(self, turn: Turn, outcome: Outcome, *, output: Sequence[dict[str, Any]] = (),
                       usage: dict[str, Any] | None = None, snapshot_id: str | None = None, log: str = "") -> Turn:
        """Make a turn final, once: its status and reason, its output, the usage its harness reported, and the
        event that ends its stream. Conflict when it is final already."""
        async with self._db.tx(turn.org_id) as conn:
            return await self._conclude(conn, turn.id, outcome, output, usage, snapshot_id, log)

    async def _conclude(self, conn: Connection, turn_id: str, outcome: Outcome, output: Sequence[dict[str, Any]] = (),
                        usage: dict[str, Any] | None = None, snapshot_id: str | None = None, log: str = "") -> Turn:
        row = await (await conn.execute(
            "UPDATE turns SET status = %s, reason = %s, code = %s, message = %s, incomplete_reason = %s, "
            "output = %s, usage = %s, snapshot_id = %s, log = %s, completed_at = now(), runner_id = NULL "
            f"WHERE id = %s AND {_OPEN} RETURNING {COLUMNS}",
            [outcome.status, outcome.reason, outcome.code, outcome.message, outcome.incomplete_reason,
             Jsonb(list(output)), Jsonb(usage) if usage is not None else None, snapshot_id, log[-LOG_MAX:],
             turn_id])).fetchone()
        if row is None:
            raise Conflict(f"response {turn_id} is already final")
        turn = Turn(**row)
        last = await (await conn.execute("SELECT COALESCE(max(seq), -1) AS seq FROM events WHERE turn_id = %s",
                                         [turn_id])).fetchone()
        assert last is not None
        await self._event(conn, turn.id, envelope_event(turn.response(), last["seq"] + 1))
        await conn.execute("UPDATE sessions SET last_active_at = now() WHERE id = %s", [turn.session_id])
        return turn

    async def cancel(self, org_id: str, turn_id: str) -> Turn:
        """Ask for a turn to stop. One still waiting is cancelled here and now; one in progress is marked and
        its runner told, which stops it, saves what it left and ends it; a final one is returned as it is."""
        async with self._db.tx(org_id) as conn:
            row = await (await conn.execute(
                "SELECT status FROM turns WHERE org_id = %s AND id = %s AND deleted_at IS NULL FOR UPDATE",
                [org_id, turn_id])).fetchone()
            if row is None:
                raise NotFound(f"there is no response {turn_id}")
            if row["status"] == "queued":
                return await self._conclude(conn, turn_id, CANCELLED)
            if row["status"] == "in_progress":
                await conn.execute("UPDATE turns SET cancel_requested = true WHERE id = %s", [turn_id])
                await announce(conn, RUNNERS, STOP + turn_id)
        return await self.get(org_id, turn_id)

    async def delete(self, org_id: str, turn_id: str) -> None:
        """Remove a turn's request, output and events. Its row stays: later turns descend from it."""
        async with self._db.tx(org_id) as conn:
            row = await (await conn.execute(
                "SELECT status FROM turns WHERE org_id = %s AND id = %s AND deleted_at IS NULL FOR UPDATE",
                [org_id, turn_id])).fetchone()
            if row is None:
                raise NotFound(f"there is no response {turn_id}")
            if row["status"] not in FINAL:
                raise Conflict(f"response {turn_id} is {row['status']}: cancel it before deleting it")
            await conn.execute("UPDATE turns SET deleted_at = now(), request = NULL, output = '[]', log = '' "
                               "WHERE id = %s", [turn_id])
            await conn.execute("DELETE FROM events WHERE turn_id = %s", [turn_id])

    async def unstored(self, *, final_for_s: float) -> list[tuple[str, str]]:
        """Turns asked not to be stored that ended this long ago and are still there, as (organization, turn):
        the ones whose caller went away before they could be forgotten."""
        rows = await self._db.all(None, "SELECT org_id, id FROM turns WHERE NOT store AND deleted_at IS NULL "
                                        "AND completed_at < now() - make_interval(secs => %s)", [final_for_s])
        return [(row["org_id"], row["id"]) for row in rows]
