"""Work that is done on a timer by one process though several may run (housekeeping): each round is taken by
whichever process asks first, and it is told how long it has been since the last one, whoever did that."""

from harnessgateway.store.db import Database


class Rounds:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def take(self, name: str, *, every_s: float) -> float | None:
        """Take this round of `name`. Returns the seconds since the last round (0 for the first there ever was);
        or None when a round was done less than `every_s` ago, by this process or another."""
        async with self._db.tx() as conn:
            row = await (await conn.execute(
                "SELECT EXTRACT(EPOCH FROM now() - at)::float AS since FROM rounds WHERE name = %s FOR UPDATE",
                [name])).fetchone()
            if row is not None and row["since"] < every_s:
                return None
            await conn.execute("INSERT INTO rounds (name, at) VALUES (%s, now()) "
                               "ON CONFLICT (name) DO UPDATE SET at = now()", [name])
        return row["since"] if row else 0.0
