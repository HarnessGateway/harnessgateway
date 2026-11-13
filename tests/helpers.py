"""What several levels of test share: the shell package, a second harness made from it, and a running platform."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

from harnessgateway import packaging
from harnessgateway.intake import Intake
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores

SHELL = Path(__file__).parents[1] / "packages" / "shell"


def variant(tmp_path: Path, name: str, **changes: Any) -> bytes:
    """The shell package under another name: a second harness to switch to."""
    folder = tmp_path / f"package-{name}"
    shutil.copytree(SHELL, folder)
    manifest = json.loads((folder / "harness.json").read_text())
    (folder / "harness.json").write_text(json.dumps({**manifest, "name": name, **changes}))
    return packaging.pack(folder)


class Platform:
    """The pieces of a running platform, and the few things a test does with them."""

    def __init__(self, stores: Stores, org: str, driver: Any) -> None:
        self.stores, self.org, self.driver = stores, org, driver
        self.intake = Intake(stores)

    async def ask(self, text: Any, model: str = "shell", **request: Any) -> Turn:
        return await self.intake.accept(self.org, {"model": model, "input": text, **request})

    async def final(self, turn: Turn, timeout: float = 30) -> Turn:
        """Wait for a turn's final event, then read the turn."""
        async def last() -> None:
            async for _ in self.stores.turns.follow(self.org, turn.id):
                pass
        await asyncio.wait_for(last(), timeout)
        return await self.stores.turns.get(self.org, turn.id)

    async def run(self, text: Any, model: str = "shell", **request: Any) -> Turn:
        return await self.final(await self.ask(text, model, **request))

    async def started(self, turn: Turn, marker: str = "started") -> None:
        """Wait until a running turn has printed `marker`."""
        async with asyncio.timeout(20):
            while not any(marker in event.get("delta", "") for event in await self.stores.turns.events(
                    self.org, turn.id)):
                await asyncio.sleep(0.05)


def text_of(turn: Turn) -> str:
    return "".join(part["text"] for item in turn.output if item["type"] == "message" for part in item["content"])
