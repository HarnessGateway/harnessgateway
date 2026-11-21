"""The installation's certificate authority, kept: its certificate, and its private key sealed at rest. It is what
lets the egress proxy read a request in order to put a secret into it."""

from collections.abc import Callable

from psycopg.types.json import Jsonb

from harnessgateway.domain.sealing import Sealer
from harnessgateway.store.db import Database

_NAME = "authority"


class AuthorityStore:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer

    async def get(self, make: Callable[[], tuple[str, str]]) -> tuple[str, str]:
        """The authority's certificate and private key, as PEM. `make` makes them, the first time they are asked
        for; whichever process asks first, every process gets the same ones."""
        row = await self._db.one(None, "SELECT value FROM platform WHERE name = %s", [_NAME])
        if row is None:
            certificate, key = make()
            made = {"certificate": certificate, "key": self._sealer.seal(key, "platform", _NAME)}
            await self._db.run(None, "INSERT INTO platform (name, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               [_NAME, Jsonb(made)])
            row = await self._db.one(None, "SELECT value FROM platform WHERE name = %s", [_NAME])
        assert row is not None
        return row["value"]["certificate"], self._sealer.open(row["value"]["key"], "platform", _NAME)
