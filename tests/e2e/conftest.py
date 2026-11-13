"""End-to-end tests run real containers.

HG_TEST_RUNTIME chooses the runtime (default runc). HG_TEST_SANDBOX_IMAGE is the image sandboxes are built
from: it needs sh and python3. HG_TEST_PLATFORM_IMAGE is the image steps run from; when it is not set the
image is built from this working tree.
"""

import asyncio
import contextlib
import dataclasses
import os
import subprocess
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from harnessgateway import packaging
from harnessgateway.api.internal import internal_app
from harnessgateway.domain.plan import CONTROL_NETWORK, SANDBOX_NETWORK, install_volume, mind_volume, work_volume
from harnessgateway.drivers.docker import DockerDriver
from harnessgateway.runner.runner import Runner
from harnessgateway.serve import trust_bundle
from harnessgateway.stores import Stores
from tests.helpers import SHELL, Platform, free_port, serving

IMAGE = os.environ.get("HG_TEST_IMAGE", "node:22-slim")      # any image with sh; steps stand in with it too
SANDBOX_IMAGE = os.environ.get("HG_TEST_SANDBOX_IMAGE", "python:3.13")
RUNTIME = os.environ.get("HG_TEST_RUNTIME", "runc")
ROOT = Path(__file__).parents[2]


@pytest.fixture
def unique() -> str:
    """A suffix that keeps one test's containers and volumes apart from another's."""
    return uuid.uuid4().hex[:12]


@pytest.fixture
async def driver() -> AsyncIterator[DockerDriver]:
    driver = DockerDriver(platform_image=IMAGE, runtime=RUNTIME)
    yield driver
    await driver.reap(keep=set())


@pytest.fixture(scope="session")
def platform_image() -> str:
    given = os.environ.get("HG_TEST_PLATFORM_IMAGE")
    if given:
        return given
    tag = "harnessgateway:test"
    subprocess.run(["docker", "build", "-q", "-f", str(ROOT / "images/platform/Dockerfile"), "-t", tag, str(ROOT)],
                   check=True, capture_output=True)
    return tag


async def _gateway(network: str) -> str:
    """The address at which containers on a network reach this machine."""
    process = await asyncio.create_subprocess_exec(
        "docker", "network", "inspect", network, "--format", "{{(index .IPAM.Config 0).Gateway}}",
        stdout=asyncio.subprocess.PIPE)
    out, _ = await process.communicate()
    return out.decode().strip()


@pytest.fixture
async def platform(stores: Stores, org: str, platform_image: str) -> AsyncIterator[Platform]:
    """A whole platform on this machine's Docker: the internal API on a port steps can reach, a runner, and
    the shell harness added."""
    async with stores.db.system() as conn:               # the queue is shared: start with nothing waiting
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    driver = DockerDriver(platform_image=platform_image, runtime=RUNTIME)
    await driver.ensure_network(CONTROL_NETWORK, internal=False)
    await driver.ensure_network(SANDBOX_NETWORK, internal=True)
    port = free_port()
    settings = dataclasses.replace(
        stores.settings, sandbox_image=SANDBOX_IMAGE,
        internal_url=f"http://{await _gateway(CONTROL_NETWORK)}:{port}")
    stores = Stores.of(settings, stores.db, stores.blobs)
    await stores.environments.update(org, "default", {"image": SANDBOX_IMAGE})
    await stores.harnesses.add(org, packaging.pack(SHELL))

    async with serving(internal_app(stores), host="0.0.0.0", port=port):
        running = asyncio.create_task(Runner(stores, driver, heartbeat_s=0.5).run())
        try:
            yield Platform(stores, org, driver)
        finally:
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
    try:
        await driver.reap(keep=set())
    finally:
        async with stores.db.org(org) as conn:
            sessions = await (await conn.execute("SELECT id FROM sessions WHERE org_id = %s", [org])).fetchall()
            installs = await (await conn.execute("SELECT id FROM installs WHERE org_id = %s", [org])).fetchall()
        for row in sessions:
            await driver.remove_volume(mind_volume(row["id"]))
            await driver.remove_volume(work_volume(row["id"]))
        for row in installs:
            await driver.remove_volume(install_volume(row["id"]))


OUTSIDE = "hg-test-outside"
ECHO = """
import http.server, json

class Echo(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path, "headers": dict(self.headers)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass

http.server.ThreadingHTTPServer(("0.0.0.0", 8000), Echo).serve_forever()
"""


async def _docker(*args: str) -> str:
    process = await asyncio.create_subprocess_exec("docker", *args, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.STDOUT)
    out, _ = await process.communicate()
    assert process.returncode == 0, out.decode()
    return out.decode().strip()


@pytest.fixture
async def egress(platform: Platform, platform_image: str) -> AsyncIterator[str]:
    """The egress proxy in front of the platform's sandboxes, and a server standing in for the outside world.
    Yields that server's host:port. It is on a private network, so the operator's setting lists it."""
    settings = platform.stores.settings
    proxy = f"hg-test-egress-{uuid.uuid4().hex[:8]}"
    await trust_bundle(platform.stores, platform.driver)
    for name in (OUTSIDE, proxy):
        await _docker("rm", "--force", name)
    await _docker("run", "--detach", "--name", OUTSIDE, "--network", CONTROL_NETWORK, SANDBOX_IMAGE,
                  "python3", "-c", ECHO)
    await _docker("run", "--detach", "--name", proxy, "--network", CONTROL_NETWORK,
                  "--env", f"HG_PROXY_KEY={settings.proxy_key}", "--env", f"HG_INTERNAL_URL={settings.internal_url}",
                  "--env", f"HG_PRIVATE_HOSTS={OUTSIDE}:8000", platform_image, "harnessgateway", "proxy")
    await _docker("network", "connect", "--alias", "egress", SANDBOX_NETWORK, proxy)
    try:
        yield f"{OUTSIDE}:8000"
    finally:
        print(await _docker("logs", "--tail", "30", proxy))
        await _docker("rm", "--force", OUTSIDE, proxy)
