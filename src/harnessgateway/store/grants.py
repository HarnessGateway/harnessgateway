"""What running sandboxes may reach. A grant is made when a turn's or an install's sandbox is about to start and
deleted when it has ended: from then on its token is worth nothing. It is kept in the database, not in a
process, so the egress proxy can ask any process of the platform."""

import json

from harnessgateway.domain.egress import Policy
from harnessgateway.domain.sealing import Sealer
from harnessgateway.ids import fingerprint
from harnessgateway.store.db import Database

_SEALED_AS = ("platform", "grant")


class Grants:
    def __init__(self, db: Database, sealer: Sealer) -> None:
        self._db = db
        self._sealer = sealer

    async def put(self, policy: Policy, token: str) -> None:
        """Let a sandbox begin: its connections through the proxy are known from now until `end`. The policy is
        sealed, since it carries the secrets the proxy puts in."""
        sealed = self._sealer.seal(json.dumps(policy.to_json()), *_SEALED_AS, policy.subject_id)
        await self._db.run(policy.org_id, (
            "INSERT INTO grants (subject_id, org_id, token_hash, policy) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (subject_id) DO UPDATE SET token_hash = EXCLUDED.token_hash, policy = EXCLUDED.policy, "
            "created_at = now()"), [policy.subject_id, policy.org_id, fingerprint(token), sealed])

    async def end(self, subject_id: str) -> None:
        await self._db.run(None, "DELETE FROM grants WHERE subject_id = %s", [subject_id])

    async def policy(self, subject_id: str, token: str) -> Policy | None:
        """What the sandbox with this id and token may reach; None when nothing of that id is running."""
        row = await self._db.one(None, "SELECT policy FROM grants WHERE subject_id = %s AND token_hash = %s",
                                 [subject_id, fingerprint(token)])
        return Policy.from_json(json.loads(self._sealer.open(row["policy"], *_SEALED_AS, subject_id))) if row else None

    async def expire(self, *, older_than_s: float) -> None:
        """Forget grants whose sandbox cannot still be running: what a process that died left behind."""
        await self._db.run(None, "DELETE FROM grants WHERE created_at < now() - make_interval(secs => %s)",
                           [older_than_s])
