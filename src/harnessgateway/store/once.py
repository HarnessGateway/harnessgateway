"""What the installation makes once and keeps, sealed at rest: its certificate authority, and the key view links
are signed with. Whichever process asks first makes it, and every process is given the same one."""

import json
from collections.abc import Callable
from typing import Any

from psycopg.types.json import Jsonb

from harnessgateway.domain.sealing import Sealer
from harnessgateway.store.db import Database


class Once:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer

    async def get(self, name: str, make: Callable[[], Any]) -> Any:
        """What is kept under `name`. `make` makes it, as something JSON holds, the first time it is asked for."""
        row = await self._db.one(None, "SELECT value FROM platform WHERE name = %s", [name])
        if row is None:
            sealed = self._sealer.seal(json.dumps(make()), "platform", name)
            await self._db.run(None, "INSERT INTO platform (name, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               [name, Jsonb(sealed)])
            row = await self._db.one(None, "SELECT value FROM platform WHERE name = %s", [name])
        assert row is not None
        return json.loads(self._sealer.open(row["value"], "platform", name))
