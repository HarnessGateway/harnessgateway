"""Layer 4: one worker node's capabilities: the platform's base image, a package's tools layer in that
image (`install.sh` run once per package version, cached on the node), and a runtime for a turn with
the uid of its scope (an organization, or a team within it) and the organization's ceilings. Nothing
of any package is in the image; the CLI arrives as the tools layer, mounted read-only at the same
path on every turn.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Sequence

from .execution import Event, Outcome, Spec
from .harness import Config
from .limits import Limits
from .runtimes.docker import DOOR_SCRIPT, SECRETS, TURN_SCRIPT, DockerRuntime, Host, Resources, base_image, identity, inspect_self, pull, sweep, whole
from .runtimes.door import GATEWAY_PORT

MARKER = ".hg-installed"
SYSTEM_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
UID_BASE, UID_RANGE = 100_000, 100_000


class InstallFailed(RuntimeError):
    pass


def scope_of(org: str, team: str = "") -> str:
    """Whose runs these are, for the sandbox: the organization's, or one team's within it. Each
    scope gets its own uid, so a team's sandboxes are as apart from another team's as from another
    organization's."""
    return f"{org}/{team}" if team else org


def uid_for(scope: str) -> int:
    """The unprivileged uid a scope's runs use on a node: the same for the same scope everywhere."""
    return UID_BASE + int(hashlib.sha256(scope.encode()).hexdigest()[:8], 16) % UID_RANGE


def path_with(tools: Path) -> str:
    return f"{tools}/bin:{SYSTEM_PATH}"


def describe(outcome: Outcome) -> str:
    if outcome.kind == "failed":
        return f"could not start: {outcome.reason}"
    if outcome.kind == "timed_out":
        return "ran out of time"
    return f"exited {outcome.code}" if outcome.code is not None else "was killed"


@contextlib.asynccontextmanager
async def locked(path: Path) -> AsyncIterator[None]:
    """Hold an exclusive lock on `path`, waiting for it without blocking the loop."""
    with open(path, "a") as f:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def log_line(event: Event) -> bytes:
    if event.stream != "door":
        return event.data
    return b"".join(b"door: " + line + b"\n" for line in event.data.splitlines())


@dataclass
class Node:
    """How this node runs things: the base image (the shipped one, pulled or built here, when empty), the
    sandboxed runtime if any, the CA file, whether private egress is allowed (a self-hosted box with
    internal hosts), the owner its runs carry, and, with a `root` under the data directory, where
    the sandboxes' mounts come from: the platform's own scripts and each run's secrets are kept there,
    so that a platform running as a container needs nothing of its own filesystem shared."""
    image: str
    owner: str
    runtime: str = ""
    ca_file: Path | None = None
    allow_private: bool = False
    root: Path | None = None                          # <data>/worker: platform/ (the scripts, the CA) and secrets/
    gateway_port: int = GATEWAY_PORT
    host: Host = Host()
    turn_script: Path = TURN_SCRIPT
    door_script: Path = DOOR_SCRIPT
    secrets: Path = SECRETS

    async def prepare(self) -> str:
        """The base image, pulled or built once; its id, which keys the tools layers. Also where this
        process runs, and the scripts and secrets under the root when there is one."""
        self.host = await inspect_self()
        if self.root:
            platform = self.root / "platform"
            platform.mkdir(parents=True, exist_ok=True)
            for src, name in ((TURN_SCRIPT, "turn.sh"), (DOOR_SCRIPT, "door.py")):
                (platform / name).write_bytes(src.read_bytes())
            self.turn_script, self.door_script = platform / "turn.sh", platform / "door.py"
            if self.ca_file:
                (platform / "ca.pem").write_bytes(Path(self.ca_file).read_bytes())
                self.ca_file = platform / "ca.pem"
            self.secrets = self.root / "secrets"
            self.secrets.mkdir(mode=0o700, exist_ok=True)
        self.image = await pull(self.image) if self.image else await base_image()
        return (await identity(self.image))[0]

    @property
    def gateway_url(self) -> str:
        """Where this node's doors reach the gateway in this same container: its address on its own
        network. '' on a host process, and '' when the gateway is not in this process (port 0)."""
        return f"http://{self.host.address}:{self.gateway_port}" if self.host.address and self.gateway_port else ""

    async def recover(self) -> None:
        await sweep(self.owner, self.secrets)

    def runtime_for(self, scope: str, limits: Limits, *, read_only: Sequence[Path] = (), writable: Sequence[Path] = (),
                    egress: Sequence[str] = (), gateway_url: str = "", token: str = "") -> DockerRuntime:
        return DockerRuntime(self.image, uid=uid_for(scope), limits=Resources(limits.cpus, limits.memory, limits.pids),
                             read_only=read_only, writable=writable, egress=egress, gateway_url=gateway_url, token=token,
                             ca_file=self.ca_file, owner=self.owner, runtime=self.runtime, allow_private=self.allow_private,
                             host=self.host, secrets=self.secrets, turn_script=self.turn_script, door_script=self.door_script)

    async def tools_for(self, scope: str, limits: Limits, folder: Path, config: Config, root: Path, image_id: str) -> Path:
        """The package's tools in the base image, installed now unless they are already."""
        root.mkdir(parents=True, exist_ok=True)
        tools, log = root / f"{folder.name}-{image_id[:12]}", root / f"{folder.name}-{image_id[:12]}.log"
        async with locked(root / f".{tools.name}.lock"):
            if (tools / MARKER).is_file():
                return tools
            shutil.rmtree(tools, ignore_errors=True)
            tools.mkdir()
            began = time.monotonic()
            try:
                outcome = await self._install(scope, limits, folder, config, tools, log)
            except BaseException:
                await asyncio.shield(asyncio.to_thread(shutil.rmtree, tools, True))
                raise
            if not outcome.ok:
                await asyncio.to_thread(shutil.rmtree, tools, True)
                tail = log.read_text(errors="replace")[-2000:] if log.is_file() else ""
                raise InstallFailed(f"install.sh {describe(outcome)}; its log ends:\n{tail}")
            (tools / MARKER).write_text(json.dumps({"image": image_id, "seconds": round(time.monotonic() - began, 1)}))
            return tools

    async def _install(self, scope: str, limits: Limits, folder: Path, config: Config, tools: Path, log: Path) -> Outcome:
        runtime = self.runtime_for(scope, limits, read_only=(folder,), writable=(tools,), egress=config.install_egress)
        env = {"HG_TOOLS": str(tools), "HG_HARNESS_DIR": str(folder), "HOME": "/tmp", "PATH": path_with(tools)}
        execution = await runtime.start(Spec(("bash", str(folder / "install.sh")), env=env, workspace=tools,
                                             timeout_s=min(config.install_timeout_s, limits.install_timeout_s)))
        try:
            with open(log, "wb") as f:
                async for event in execution.events():
                    f.write(log_line(event))
                    f.flush()
            return await execution.wait()
        except BaseException:
            execution.kill()
            await whole(execution.wait())
            raise


class ProcessNode:
    """A node without Docker, for development only: install.sh and every turn run as processes on
    this machine, with no isolation at all (`harnessgateway serve --dev`). Never for anyone else's
    packages."""
    image = "process"

    def __init__(self, root: Path, gateway_url: str = ""):
        self.root = Path(root)
        self.gateway_url = gateway_url                 # the gateway as a process on this machine reaches it

    async def prepare(self) -> str:
        return "process"

    async def recover(self) -> None:
        pass

    def runtime_for(self, scope: str, limits: Limits, *, read_only: Sequence[Path] = (), writable: Sequence[Path] = (),
                    egress: Sequence[str] = (), gateway_url: str = "", token: str = ""):
        from .runtimes.process import ProcessExecution, host_env
        # no door here: the CLI calls the gateway itself, with the session token in a file of the turn's
        rundir = next((p for p in writable if p.name == "run"), None) or (writable[-1] if writable else self.root)
        rundir.mkdir(parents=True, exist_ok=True)
        token_file = rundir / ".token"
        token_file.write_text(token)
        token_file.chmod(0o600)

        class Runtime:
            provider_base = self.gateway_url or gateway_url   # this node's own, else the job's; the worker appends the provider's path
            token_path = token_file

            async def start(self, spec: Spec):
                env = {**host_env(), **spec.env}
                env["PATH"] = f"{spec.env.get('PATH', '')}:{host_env().get('PATH', '/usr/bin:/bin')}"
                return await ProcessExecution.spawn(list(spec.command), env=env, cwd=spec.workspace, input=spec.input,
                                                    interactive=spec.interactive, timeout_s=spec.timeout_s, stop_signal=15)

            async def recover(self) -> None:
                pass
        return Runtime()

    async def tools_for(self, scope: str, limits: Limits, folder: Path, config: Config, root: Path, image_id: str) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        tools, log = root / f"{folder.name}-process", root / f"{folder.name}-process.log"
        async with locked(root / f".{tools.name}.lock"):
            if (tools / MARKER).is_file():
                return tools
            shutil.rmtree(tools, ignore_errors=True)
            (tools / "bin").mkdir(parents=True)
            from .runtimes.process import ProcessExecution, host_env
            env = {**host_env(), "HG_TOOLS": str(tools), "HG_HARNESS_DIR": str(folder), "HOME": str(tools)}
            execution = await ProcessExecution.spawn(["bash", str(folder / "install.sh")], env=env, cwd=tools, input=b"", interactive=False,
                                                     timeout_s=min(config.install_timeout_s, limits.install_timeout_s), stop_signal=15)
            with open(log, "wb") as f:
                async for event in execution.events():
                    f.write(event.data)
            outcome = await execution.wait()
            if not outcome.ok:
                shutil.rmtree(tools, ignore_errors=True)
                raise InstallFailed(f"install.sh {describe(outcome)}; its log ends:\n{log.read_text(errors='replace')[-2000:]}")
            (tools / MARKER).write_text(json.dumps({"image": "process"}))
            return tools
