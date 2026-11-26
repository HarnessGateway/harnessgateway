"""Sessions: each a chain of turns on the same two folders, and what is known of where those folders stand."""

from dataclasses import dataclass
from datetime import datetime

from harnessgateway.domain.change import Change
from harnessgateway.errors import Conflict, NotFound
from harnessgateway.store.db import Database, Tell
from harnessgateway.store.turns import COLUMNS, Turn

_OPEN = "status IN ('queued', 'in_progress')"
# A session is whoever started it's: who sent its first turn. It is kept there, and nowhere else.
_OWNER = "(SELECT f.principal FROM turns f WHERE f.session_id = s.id ORDER BY f.number, f.created_at LIMIT 1)"
_SUMMARY = (
    f"SELECT s.id, {_OWNER} AS owner, s.head_turn_id, s.forked_from_turn_id, s.folder_snapshot_id, s.created_at, "
    "s.last_active_at, "
    "(SELECT count(*) FROM turns t WHERE t.session_id = s.id) AS turns, "
    "COALESCE((SELECT n.bytes FROM snapshots n WHERE n.id = h.snapshot_id), 0) AS bytes, "
    "h.environment, h.harness, h.provider, h.model, h.status "
    "FROM sessions s LEFT JOIN turns h ON h.id = s.head_turn_id"
)


@dataclass(frozen=True)
class Session:
    """A session, and what its latest turn ran with."""

    id: str
    owner: str | None                          # who started it
    head_turn_id: str | None
    forked_from_turn_id: str | None
    folder_snapshot_id: str | None             # the snapshot its folders hold where the driver keeps them
    created_at: datetime
    last_active_at: datetime
    turns: int
    bytes: int                                 # the size of its latest turn's snapshot
    environment: str | None
    harness: str | None
    provider: str | None
    model: str | None
    status: str | None


class Sessions:
    def __init__(self, db: Database, tell: Tell) -> None:
        self._db = db
        self._tell = tell

    async def get(self, org_id: str, session_id: str) -> Session:
        row = await self._db.one(org_id, f"{_SUMMARY} WHERE s.org_id = %s AND s.id = %s", [org_id, session_id])
        if row is None:
            raise NotFound(f"there is no session {session_id}")
        return Session(**row)

    async def owner(self, org_id: str, session_id: str) -> str | None:
        """Who started the session."""
        row = await self._db.one(org_id, f"SELECT {_OWNER} AS owner FROM sessions s WHERE s.org_id = %s AND s.id = %s",
                                 [org_id, session_id])
        if row is None:
            raise NotFound(f"there is no session {session_id}")
        return row["owner"]  # type: ignore[no-any-return]

    async def page(self, org_id: str, *, owner: str | None, after: str | None, limit: int) -> list[Session]:
        """Sessions, most recently active first; those after `after` in that order when it is given. With
        `owner`, that person's alone."""
        rows = await self._db.all(org_id, (
            f"{_SUMMARY} WHERE s.org_id = %s AND (%s::text IS NULL OR {_OWNER} = %s) "
            "AND (%s::text IS NULL OR (s.last_active_at, s.id) < "
            "(SELECT last_active_at, id FROM sessions WHERE id = %s)) "
            "ORDER BY s.last_active_at DESC, s.id DESC LIMIT %s"), [org_id, owner, owner, after, after, limit])
        return [Session(**row) for row in rows]

    async def turns(self, org_id: str, session_id: str) -> list[Turn]:
        """A session's turns, oldest first. A deleted one is there without its content: its place is kept."""
        await self.get(org_id, session_id)
        rows = await self._db.all(org_id, f"SELECT {COLUMNS} FROM turns WHERE org_id = %s AND session_id = %s "
                                          "ORDER BY number, created_at", [org_id, session_id])
        return [Turn(**row) for row in rows]

    async def saved(self, org_id: str, session_id: str) -> str | None:
        """The snapshot of the session's latest turn that has one: its files as they were last saved."""
        row = await self._db.one(org_id, (
            "SELECT snapshot_id FROM turns WHERE org_id = %s AND session_id = %s AND snapshot_id IS NOT NULL "
            "ORDER BY number DESC, created_at DESC LIMIT 1"), [org_id, session_id])
        return row["snapshot_id"] if row else None

    async def emptied_by(self, org_id: str, turn_id: str) -> str | None:
        """The session of a deleted turn, if every one of its turns is now deleted: nothing of it is left to keep."""
        row = await self._db.one(org_id, (
            "SELECT t.session_id FROM turns t WHERE t.org_id = %s AND t.id = %s AND NOT EXISTS ("
            "  SELECT 1 FROM turns o WHERE o.session_id = t.session_id AND o.deleted_at IS NULL)"), [org_id, turn_id])
        return row["session_id"] if row else None

    async def remove(self, org_id: str, session_id: str, *, by: str | None) -> list[str]:
        """Delete a session, its turns and their events. Returns the ids of its snapshots, which the caller
        releases. Refused while one of its turns is waiting or running. `by` is who deletes it; None when it is
        the session of a turn that was not to be stored, whose removal is no change of anyone's."""
        async with self._db.tx(org_id) as conn:
            row = await (await conn.execute("SELECT id FROM sessions WHERE org_id = %s AND id = %s FOR UPDATE",
                                            [org_id, session_id])).fetchone()
            if row is None:
                raise NotFound(f"there is no session {session_id}")
            busy = await (await conn.execute(f"SELECT id FROM turns WHERE session_id = %s AND {_OPEN} LIMIT 1",
                                             [session_id])).fetchone()
            if busy is not None:
                raise Conflict(f"the session has a turn in progress: {busy['id']}; cancel it first")
            snapshots = await (await conn.execute("SELECT id FROM snapshots WHERE org_id = %s AND session_id = %s",
                                                  [org_id, session_id])).fetchall()
            await conn.execute("DELETE FROM sessions WHERE id = %s", [session_id])
            if by is not None:
                await self._tell(conn, Change("sessions.deleted", org_id, by, session_id))
        return [snapshot["id"] for snapshot in snapshots]

    async def folders_hold(self, org_id: str, session_id: str, snapshot_id: str | None) -> None:
        """Record which snapshot the session's folders hold; None: they are about to change, or cannot be trusted."""
        await self._db.run(org_id, "UPDATE sessions SET folder_snapshot_id = %s, folder_used_at = now() "
                                   "WHERE org_id = %s AND id = %s", [snapshot_id, org_id, session_id])

    async def idle(self, *, for_s: float) -> list[str]:
        """Sessions whose folders have not been used for this long, each now recorded as having none: the caller
        removes the folders, and the session's next turn brings them back from its snapshot."""
        rows = await self._db.all(None, (
            "UPDATE sessions s SET folder_snapshot_id = NULL, folder_used_at = NULL "
            "WHERE folder_used_at < now() - make_interval(secs => %s) AND NOT EXISTS ("
            f"  SELECT 1 FROM turns t WHERE t.session_id = s.id AND t.{_OPEN}) RETURNING id"), [for_s])
        return [row["id"] for row in rows]
