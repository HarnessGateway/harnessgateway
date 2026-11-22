"""The Docker driver: sandboxes are containers on this host, started with the docker CLI, and their folders are
sub-folders of one data volume, which no sandbox is ever given whole.

Every hardening flag is on every sandbox's command line, and none of them is optional.
"""

import asyncio
import json
import time
from asyncio.subprocess import PIPE, STDOUT
from collections.abc import AsyncIterator, Collection, Mapping
from pathlib import Path

from harnessgateway.domain.contract import PROXY_HOST, SANDBOX_UID
from harnessgateway.domain.events import LINE_MAX
from harnessgateway.domain.sandbox import SandboxPlan
from harnessgateway.domain.snapshot import Manifest
from harnessgateway.driver.base import ChunkStore, DriverError, OnLine, SandboxExit
from harnessgateway.driver.folders import LocalFolders
from harnessgateway.driver.meter import Meter

FOLDERS = "folders"                            # where on the data volume sandboxes' folders are kept
_NEVER_STARTED = "0001-01-01T00:00:00Z"        # what Docker reports as the start of a container that never ran
_PROXY_ADDRESS_S = 5.0                         # how long the proxy's address is believed: a restarted one moved


def command(plan: SandboxPlan, *, name: str, volume: str, network: str, runtime: str | None = None,
            proxy_address: str | None = None) -> list[str]:
    """The command that runs a sandbox. `--quiet` so that what it prints is the container's output and nothing
    else. The sandbox is told the proxy's address: under gVisor it has a network stack of its own, which
    Docker's name service does not reach."""
    mounts = [part for mount in plan.mounts for part in (
        "--mount", f"type=volume,source={volume},target={mount.path},volume-subpath={FOLDERS}/{mount.folder},"
                   f"volume-nocopy{',readonly' if mount.read_only else ''}")]
    # /tmp here is the sandbox's own: in memory and counted against its limit, or on disk and gone with it.
    tmp = (["--tmpfs", f"/tmp:rw,exec,size={plan.tmp_mb}m"] if plan.tmp_mb is not None  # noqa: S108
           else ["--volume", "/tmp"])  # noqa: S108
    return [
        "docker", "run", "--quiet", "--name", name,
        "--init", "--env", "TINI_KILL_PROCESS_GROUP=1",     # a stop reaches everything the sandbox's program started
        "--network", network if plan.networked else "none",
        *(["--add-host", f"{PROXY_HOST}:{proxy_address}"] if plan.networked and proxy_address else []),
        *(["--runtime", runtime] if runtime else []),
        "--cpus", f"{plan.cpus:g}", "--memory", f"{plan.memory_mb}m", "--memory-swap", f"{plan.memory_mb}m",
        "--pids-limit", str(plan.pids),
        "--read-only", *tmp,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "--workdir", plan.workdir, *mounts,
        *(part for key, value in plan.env.items() for part in ("--env", f"{key}={value}")),
        "--entrypoint", plan.command, plan.image, *plan.args,    # whatever entry point the image names never runs
    ]


class Lines:
    """Bytes in, whole lines out. A line past the limit is cut one character over it: it is then refused as too
    long, which says what happened, where a line silently cut to fit would not."""

    def __init__(self, limit: int = LINE_MAX) -> None:
        self._limit = limit
        self._held = bytearray()
        self._over = False

    def _line(self, raw: bytes) -> str:
        return raw[: self._limit + 1].decode("utf-8", errors="replace")

    def feed(self, data: bytes) -> list[str]:
        lines = []
        for piece in data.splitlines(keepends=True):
            if not self._over:
                self._held += piece[: self._limit + 1 - len(self._held)]
            if piece.endswith(b"\n"):
                lines.append(self._line(bytes(self._held).rstrip(b"\r\n")))
                self._held.clear()
                self._over = False
            else:
                self._over = len(self._held) > self._limit
        return lines

    def rest(self) -> list[str]:
        held, self._held = bytes(self._held), bytearray()
        return [self._line(held)] if held else []


async def _pump(stream: asyncio.StreamReader, on_line: OnLine) -> None:
    lines = Lines()
    while data := await stream.read(65536):
        for line in lines.feed(data):
            await on_line(line)
    for line in lines.rest():
        await on_line(line)


class DockerDriver:
    name = "docker"

    def __init__(self, *, namespace: str, data: Path, runtime: str = "auto") -> None:
        self._namespace = namespace                # names every container it makes
        self._volume = f"{namespace}-data"         # the data volume, as Docker knows it
        self._network = f"{namespace}-sandbox"     # internal: its only other member is the egress proxy
        self._folders = LocalFolders(data / FOLDERS)
        self._runtime_setting = runtime
        self._runtime: str | None = None
        self._runtime_known = False
        self._proxy: tuple[float, str | None] = (0.0, None)      # until when its address is believed, and the address

    async def _docker(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec("docker", *args, stdout=PIPE, stderr=STDOUT)
        output, _ = await process.communicate()
        return process.returncode or 0, output.decode("utf-8", errors="replace").strip()

    async def _checked(self, *args: str) -> str:
        code, output = await self._docker(*args)
        if code != 0:
            raise DriverError(f"docker {args[0]} failed: {output[-500:]}")
        return output

    async def _isolating_runtime(self) -> str | None:
        """The runtime to name on a sandbox's command line: gVisor when the daemon has it; None for its default."""
        if not self._runtime_known:
            if self._runtime_setting != "runc":
                if "runsc" in json.loads(await self._checked("info", "--format", "{{json .Runtimes}}")):
                    self._runtime = "runsc"
                elif self._runtime_setting == "runsc":
                    raise DriverError("HG_RUNTIME is runsc, but this Docker daemon has no runsc runtime (gVisor)")
            self._runtime_known = True
        return self._runtime

    async def isolation(self) -> str:
        return "gvisor" if await self._isolating_runtime() == "runsc" else "none"

    async def image_id(self, ref: str) -> str:
        code, output = await self._docker("image", "inspect", "--format", "{{.Id}}", ref)
        if code != 0:
            await self._checked("pull", "--quiet", ref)
            output = await self._checked("image", "inspect", "--format", "{{.Id}}", ref)
        return output

    async def _proxy_address(self) -> str | None:
        """The address of the container that answers to the proxy's name on the sandbox network."""
        if self._proxy[0] > time.monotonic():
            return self._proxy[1]
        found = None
        code, attached = await self._docker("network", "inspect", "--format", "{{json .Containers}}", self._network)
        for container in (json.loads(attached) if code == 0 else {}):
            _, networks = await self._docker("inspect", "--format", "{{json .NetworkSettings.Networks}}", container)
            try:
                here = json.loads(networks).get(self._network) or {}
            except (ValueError, AttributeError):
                continue                                         # it went away meanwhile
            if PROXY_HOST in (here.get("Aliases") or []) or PROXY_HOST in (here.get("DNSNames") or []):
                found = str(here["IPAddress"])
        self._proxy = (time.monotonic() + _PROXY_ADDRESS_S, found)
        return found

    # folders: the work is LocalFolders'

    async def fill(self, folder: str, files: Mapping[str, bytes], *, executable: Collection[str] = (),
                   streams: Mapping[str, AsyncIterator[bytes]] | None = None) -> None:
        await self._folders.fill(folder, files, executable=executable, streams=streams)

    async def restore(self, folders: Mapping[str, str], manifest: Manifest, chunks: ChunkStore) -> None:
        await self._folders.restore(folders, manifest, chunks)

    async def save(self, folders: Mapping[str, str], parent: Manifest, leave_out: frozenset[str],
                   chunks: ChunkStore) -> Manifest:
        return await self._folders.save(folders, parent, leave_out, chunks)

    async def measure(self, folders: Mapping[str, str]) -> int:
        return await self._folders.measure(folders)

    async def remove(self, folder: str) -> None:
        await self._folders.remove(folder)

    # sandboxes

    def _container(self, name: str) -> str:
        return f"{self._namespace}-{name}"

    async def run(self, plan: SandboxPlan, on_stdout: OnLine, on_stderr: OnLine) -> SandboxExit:
        name = self._container(plan.name)
        argv = command(plan, name=name, volume=self._volume, network=self._network,
                       runtime=await self._isolating_runtime(),
                       proxy_address=await self._proxy_address() if plan.networked else None)
        process = await asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)
        assert process.stdout is not None and process.stderr is not None
        said: list[str] = []                       # the end of its standard error: when the sandbox never ran, it

        async def keep_and_pass(line: str) -> None:               # is the docker client saying why
            said.append(line)
            del said[:-20]
            await on_stderr(line)

        try:
            async with Meter(name) as meter:
                await asyncio.gather(_pump(process.stdout, on_stdout), _pump(process.stderr, keep_and_pass))
                await process.wait()
            code, state = await self._docker(
                "inspect", "--format", "{{.State.StartedAt}} {{.State.OOMKilled}} {{.State.ExitCode}}", name)
            if code != 0 or state.startswith(_NEVER_STARTED):
                return SandboxExit(started=False, error="\n".join(said)[-2000:] or state)
            _, oom, exit_code = state.split()
            return SandboxExit(started=True, exit_code=int(exit_code), out_of_memory=oom == "true",
                               measured=meter.measured)
        finally:
            if process.returncode is None:
                process.kill()
            await self._docker("rm", "--force", "--volumes", name)

    async def stop(self, name: str, grace_s: int) -> None:
        await self._docker("stop", "--time", str(grace_s), self._container(name))

    async def discard(self, name: str) -> None:
        await self._docker("rm", "--force", "--volumes", self._container(name))
