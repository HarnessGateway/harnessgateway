"""Spec §18: the platform as it is deployed: `init`, then `serve` as a process of its own, Docker underneath,
and the OpenAI SDK as the caller."""

import asyncio
import os
import signal
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psycopg
import pytest
from openai import AsyncOpenAI
from psycopg.conninfo import make_conninfo

from harnessgateway import packaging
from harnessgateway.domain.names import DEFAULT_ORG
from harnessgateway.domain.plan import CONTROL_NETWORK, SANDBOX_NETWORK
from harnessgateway.drivers.docker import DockerDriver
from harnessgateway.settings import Settings
from harnessgateway.store.blobs import DirectoryBlobs
from harnessgateway.store.db import Database
from harnessgateway.stores import Stores
from tests.conftest import SERVER
from tests.e2e.conftest import RUNTIME, SANDBOX_IMAGE, _gateway
from tests.helpers import SHELL, free_port

pytestmark = pytest.mark.usefixtures("database_url")

STREAMER = """
import asyncio, sys
from openai import AsyncOpenAI

async def main(url, key, response_id):
    client = AsyncOpenAI(api_key=key, base_url=url + "/v1", max_retries=0)
    stream = await client.responses.retrieve(response_id, stream=True)
    async for event in stream:
        print(event.type, getattr(event, "delta", "").strip(), flush=True)

asyncio.run(main(*sys.argv[1:]))
"""


class Served:
    """A platform running as its own process."""

    def __init__(self, env: dict[str, str], url: str, key: str, stores: Stores) -> None:
        self.env, self.url, self.key, self.stores = env, url, key, stores
        self.client = AsyncOpenAI(api_key=key, base_url=f"{url}/v1", max_retries=0)
        self.process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-m", "harnessgateway", "serve",
                                                            env=self.env)
        async with httpx.AsyncClient() as http, asyncio.timeout(60):
            while True:
                try:
                    if (await http.get(f"{self.url}/healthz")).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                assert self.process.returncode is None, "serve exited while starting"
                await asyncio.sleep(0.2)

    async def stop(self, how: int = signal.SIGTERM) -> int:
        assert self.process is not None
        if self.process.returncode is None:
            self.process.send_signal(how)
        return await asyncio.wait_for(self.process.wait(), 90)

    async def text_seen(self, response_id: str, text: str) -> None:
        async with asyncio.timeout(120):
            while not any(text in event.get("delta", "") for event in await self.stores.turns.events(
                    DEFAULT_ORG, response_id)):
                await asyncio.sleep(0.2)

    async def final(self, response_id: str, timeout: float = 120) -> Any:
        async with asyncio.timeout(timeout):
            while True:
                response = await self.client.responses.retrieve(response_id)
                if response.status not in ("queued", "in_progress"):
                    return response
                await asyncio.sleep(0.3)


async def _containers() -> list[str]:
    process = await asyncio.create_subprocess_exec(
        "docker", "ps", "--filter", "label=hg.org", "--format", "{{.Names}}", stdout=asyncio.subprocess.PIPE)
    out, _ = await process.communicate()
    return out.decode().split()


@pytest.fixture
async def served(tmp_path: Path, platform_image: str) -> AsyncIterator[Served]:
    assert SERVER
    name = f"hg_serve_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(SERVER, autocommit=True) as server:
        server.execute(f'CREATE DATABASE "{name}"')
    driver = DockerDriver(platform_image=platform_image, runtime=RUNTIME)
    await driver.ensure_network(CONTROL_NETWORK, internal=False)
    await driver.ensure_network(SANDBOX_NETWORK, internal=True)
    port, internal_port = free_port(), free_port()
    env = {
        "PATH": os.environ["PATH"], "HG_DATABASE_URL": make_conninfo(SERVER, dbname=name),
        "HG_SECRET_KEY": "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", "HG_PROXY_KEY": "test-proxy-key",
        "HG_DATA": str(tmp_path / "data"), "HG_PLATFORM_IMAGE": platform_image, "HG_SANDBOX_IMAGE": SANDBOX_IMAGE,
        "HG_INTERNAL_URL": f"http://{await _gateway(CONTROL_NETWORK)}:{internal_port}", "HG_PORT": str(port),
        "HG_INTERNAL_PORT": str(internal_port), "HG_RUNTIME": RUNTIME,
    }
    first = subprocess.run([sys.executable, "-m", "harnessgateway", "init"], env=env, capture_output=True, text=True,
                           check=True)
    key = first.stdout.strip().rsplit(" ", 1)[-1]
    assert key.startswith("hg_"), first.stdout
    again = subprocess.run([sys.executable, "-m", "harnessgateway", "init"], env=env, capture_output=True, text=True,
                           check=True)
    assert "Nothing was changed" in again.stdout

    settings = Settings.from_env(env)
    db = Database(settings.database_url)
    await db.open()
    stores = Stores.of(settings, db, DirectoryBlobs(Path(settings.data)))
    await stores.harnesses.add(DEFAULT_ORG, packaging.pack(SHELL))
    served = Served(env, f"http://127.0.0.1:{port}", key, stores)
    await served.start()
    try:
        yield served
    finally:
        await served.client.close()
        await served.stop()
        await driver.reap(keep=set())
        async with db.system() as conn:
            volumes = [f"hg-mind-{row['id']}" for row in await (await conn.execute(
                "SELECT id FROM sessions")).fetchall()]
            volumes += [f"hg-work-{row['id']}" for row in await (await conn.execute(
                "SELECT id FROM sessions")).fetchall()]
            volumes += [f"hg-harness-{row['id']}" for row in await (await conn.execute(
                "SELECT id FROM installs")).fetchall()]
        for volume in volumes:
            await driver.remove_volume(volume)
        await db.close()
        with psycopg.connect(SERVER, autocommit=True) as server:
            server.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


async def test_from_init_to_a_response_through_the_sdk(served: Served) -> None:
    response = await served.client.responses.create(model="shell", input="echo hello from a container; id -u")
    assert response.status == "completed"
    assert response.output_text == "hello from a container\n10000\n"
    again = await served.client.responses.create(model="shell", input="pwd", previous_response_id=response.id)
    assert again.output_text == "/work\n"


async def test_another_process_streams_a_turn_that_is_already_running(served: Served, tmp_path: Path) -> None:
    turn = await served.client.responses.create(model="shell", input="echo one; sleep 3; echo two", background=True)
    await served.text_seen(turn.id, "one")
    script = tmp_path / "streamer.py"
    script.write_text(STREAMER)
    watcher = await asyncio.create_subprocess_exec(
        sys.executable, str(script), served.url, served.key, turn.id, stdout=asyncio.subprocess.PIPE,
        env={"PATH": os.environ["PATH"]})
    out, _ = await asyncio.wait_for(watcher.communicate(), 120)
    lines = out.decode().splitlines()
    assert lines[0].split()[0] == "response.created"
    assert "response.output_text.delta one" in lines
    assert "response.output_text.delta two" in lines
    assert lines[-1].split()[0] == "response.completed"


async def test_a_turn_running_when_the_platform_is_killed_is_ended_when_it_comes_back(served: Served) -> None:
    first = await served.client.responses.create(model="shell", input="echo kept > a.txt")
    lost = await served.client.responses.create(model="shell", input="echo started; sleep 600", background=True,
                                                previous_response_id=first.id)
    await served.text_seen(lost.id, "started")
    assert first.metadata is not None
    sandbox = f"hg-{first.metadata['hg_session']}"
    assert sandbox in await _containers()

    assert await served.stop(signal.SIGKILL) == -signal.SIGKILL          # no chance to clean up
    assert sandbox in await _containers()                                # the sandbox outlived the platform
    await served.start()
    assert sandbox not in await _containers()                            # removed at start

    final = await served.final(lost.id, timeout=90)
    assert final.status == "failed"
    assert final.error is not None
    assert final.error.message == "the platform restarted during this turn"
    assert final.metadata is not None
    assert final.metadata["hg_reason"] == "sandbox_failed"
    onward = await served.client.responses.create(model="shell", input="cat a.txt", previous_response_id=first.id)
    assert onward.output_text == "kept\n"


async def test_a_stop_signal_stops_saves_and_ends_running_turns(served: Served) -> None:
    turn = await served.client.responses.create(model="shell", input="echo partial > kept.txt; echo started; sleep 600",
                                                background=True)
    await served.text_seen(turn.id, "started")
    assert await served.stop(signal.SIGTERM) == 0
    assert await _containers() == []
    final = await served.stores.turns.get(DEFAULT_ORG, turn.id)
    assert (final.status, final.reason) == ("failed", "sandbox_failed")
    assert final.snapshot_id is not None
    manifest = await served.stores.snapshots.manifest(DEFAULT_ORG, final.snapshot_id)
    assert "work/kept.txt" in manifest.entries
    await served.start()                                                 # so the fixture has a process to stop
