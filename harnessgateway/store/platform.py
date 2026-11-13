"""What belongs to the installation as a whole: its certificate authority (spec §11.5)."""

import base64

from psycopg.types.json import Jsonb

from harnessgateway.domain.sealing import Sealer
from harnessgateway.proxy.authority import new_authority
from harnessgateway.store.db import Database

_AUTHORITY = "authority"
_BINDING = {"org": "platform", "kind": "authority", "name": "key"}


class PlatformStore:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer

    async def authority(self) -> tuple[str, str]:
        """The authority's certificate and private key, as PEM. Made the first time it is asked for; the key
        is sealed at rest."""
        cert, key = new_authority()
        made = {"certificate": cert, "key_sealed": base64.b64encode(self._sealer.seal(key, **_BINDING)).decode()}
        async with self._db.system() as conn:
            await conn.execute("INSERT INTO platform (name, value) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               [_AUTHORITY, Jsonb(made)])
            row = await (await conn.execute("SELECT value FROM platform WHERE name = %s", [_AUTHORITY])).fetchone()
        assert row is not None
        value = row["value"]
        return value["certificate"], self._sealer.open(base64.b64decode(value["key_sealed"]), **_BINDING)
