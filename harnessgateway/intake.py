"""Accepting a request: what it will run with, which session it belongs to, and its place in the queue
(spec §6.2, §6.3, §6.4, §10.2)."""

from collections.abc import Mapping
from typing import Any

from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.request import file_ids
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores

_MIB = 1024 * 1024


class Intake:
    def __init__(self, stores: Stores) -> None:
        self._stores = stores

    async def accept(self, org_id: str, body: Mapping[str, Any], *, key_id: str | None = None) -> Turn:
        """Queue the turn a valid request asks for; or refuse, naming what is wrong."""
        stores = self._stores
        resolved = await stores.resolve(org_id, ModelPath.parse(body.get("model")))
        parent = await self._parent(org_id, body.get("previous_response_id"))
        if parent is not None and parent.snapshot_id is not None:
            held = (await stores.snapshots.get(org_id, parent.snapshot_id)).bytes
            allowed = resolved.environment.disk_mb * _MIB
            if held > allowed:
                raise Invalid(
                    f"the session's files are {held // _MIB} MiB, and environment {resolved.environment.name!r} "
                    f"allows {allowed // _MIB} MiB", param="model")
        for file_id in file_ids(dict(body)):
            try:
                await stores.files.get(org_id, file_id)
            except NotFound:
                raise Invalid(f"input names a file that does not exist: {file_id}", param="input") from None
        return await stores.turns.create(org_id, resolved=resolved, request=body, parent=parent, key_id=key_id)

    async def _parent(self, org_id: str, previous: object) -> Turn | None:
        """The turn a request continues from. It must be final and have a saved state (spec §6.2)."""
        if previous is None:
            return None
        try:
            parent = await self._stores.turns.get(org_id, str(previous))
        except NotFound:
            raise Invalid(f"previous_response_id names no response: {previous}",
                          param="previous_response_id") from None
        if not parent.final:
            raise Conflict(f"the session has a turn in progress: {parent.id}")
        if parent.snapshot_id is None:
            raise Conflict(f"response {parent.id} ended as {parent.status} ({parent.reason}) with no saved state; "
                           f"continue from the response before it")
        return parent
