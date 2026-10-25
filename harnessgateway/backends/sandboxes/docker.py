"""Backend: the Docker sandbox. Every run gets a fresh container from the platform's base image, an
internal network of its own, and an egress door on that network as its only way out.

Its guarantees, on every run:
- no Linux capabilities, no privilege escalation, a read-only root, cpu, memory and pid limits, a
  sandboxed runtime when the node has one (`runtime`), and the tenant's own unprivileged user when the
  server runs as root (it hands the writable paths over), else the server's own user;
- tini as PID 1, so a stop (SIGTERM) reaches the command's whole process group, and a kill follows;
- no route out but the door, which tunnels only to the hosts it is given, refuses private addresses,
  and has no DNS to give; provider calls go through the door's forwarder to the platform's gateway,
  which is where the keys are. The run reads its session token from `TOKEN_PATH`; it never sees a key;
- the spec's environment and nothing of the host's, passed by name so no value is on a command line
  (but PATH, HOME and DOCKER_*, which the Docker client runs by); the client itself gets only the
  allowlisted host variables;
- teardown of the container, the door, the network and the token on every path, shielded;
- every container and network it makes carries its owner (`owner_of` a data directory, or one of its
  own); a sweep removes one owner's leftovers, never another's.

Paths are the same inside as outside: the workspace and every mount appear at their own paths.
When the platform itself is a container, `Host` says what the daemon sees behind this container's
mounts and where this container is on its network, so the sandboxes it starts get real host paths
and a gateway address they can reach; nothing is asked of the operator but a volume and the socket.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Sequence, TypeVar

from ...errors import Invalid
from ...kernel.execution import Execution, NeverStarted, Outcome, Spec
from ..egress.door import DOOR_PORT, GATEWAY_PORT, READY, normalize
from .process import ProcessExecution, host_env
from . import MARKER, InstallFailed, describe, locked, log_line, path_with, uid_for
from ...contract.package import Config
from ...kernel.limits import Limits

DOOR_IMAGE = "python:3.12-alpine"
DOOR_SCRIPT = Path(__file__).resolve().parents[1] / "egress" / "door.py"
TURN_SCRIPT = Path(__file__).resolve().parents[2] / "contract" / "turn.sh"
DOOR_HOST, PROVIDER_HOST = "hg-door", "hg-provider"  # the door's aliases, the same on every run's network
PROVIDER_URL = f"http://{PROVIDER_HOST}:{GATEWAY_PORT}"  # where a run calls its provider; the gateway's path is appended
PROXY = f"http://{DOOR_HOST}:{DOOR_PORT}"
NO_PROXY = f"127.0.0.1,localhost,{DOOR_HOST},{PROVIDER_HOST}"
TOKEN_PATH = Path("/run/hg/token")                   # where every run reads its session token
CA_PATH = "/etc/hg/ca.pem"
CA_VARS = ("SSL_CERT_FILE", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GIT_SSL_CAINFO")
SECRETS = Path("/dev/shm") if Path("/dev/shm").is_dir() else Path(tempfile.gettempdir())
PREFIX = "hg-"
LABEL = "hg.owner"
LOCKDOWN = ("--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--read-only")
HARDENING = (*LOCKDOWN, "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=512m")
DOOR_LIMITS = ("--pids-limit", "64", "--memory", "256m")
T = TypeVar("T")


def clients_own(name: str) -> bool:
    return name in ("PATH", "HOME") or name.startswith("DOCKER_")


def client_env(env: dict[str, str] | None = None) -> dict[str, str]:
    host = host_env()
    host.setdefault("DOCKER_CONFIG", str(Path.home() / ".docker"))
    return {**host, **{k: v for k, v in (env or {}).items() if not clients_own(k)}}


def docker_bin() -> str:
    found = shutil.which("docker", path=host_env().get("PATH"))
    if not found:
        raise RuntimeError("Docker is not installed")
    return found


async def docker(*args: str, stdin: bytes | None = None) -> tuple[int, str]:
    """Run the Docker CLI; its exit code and its output."""
    proc = await asyncio.create_subprocess_exec(
        docker_bin(), *args, env=client_env(), stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await proc.communicate(stdin)
    except asyncio.CancelledError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise
    return proc.returncode or 0, out.decode(errors="replace")


async def pull(reference: str) -> str:
    """An image by reference, pulled if it is not here; its reference back."""
    if (await docker("image", "inspect", reference))[0] == 0:
        return reference
    code, log = await docker("pull", "-q", reference)
    if code != 0:
        raise RuntimeError(f"image {reference} could not be pulled:\n{log[-1000:]}")
    return reference


BASE_DOCKERFILE = Path(__file__).with_name("base.Dockerfile")
BASE_IMAGE = "ghcr.io/harnessgateway/harnessgateway-base:latest"   # built from BASE_DOCKERFILE by the base-image workflow


async def build_base() -> str:
    """The platform's own base image, built from the shipped Dockerfile once per content; its tag."""
    text = BASE_DOCKERFILE.read_text()
    tag = f"{PREFIX}base:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
    if (await docker("image", "inspect", tag))[0] == 0:
        return tag
    with tempfile.TemporaryDirectory() as context:
        code, log = await docker("build", "-t", tag, "-f", "-", context, stdin=text.encode())
    if code != 0:
        raise RuntimeError(f"the base image failed to build:\n{log[-2000:]}")
    return tag


async def base_image() -> str:
    """The shipped base image: the published one, pulled once; built here from the same Dockerfile when
    it cannot be pulled (no route to the registry, or a private copy). Its reference."""
    try:
        return await pull(BASE_IMAGE)
    except RuntimeError as e:
        print(f"{str(e).splitlines()[0]}; building it here instead (a few minutes)", file=sys.stderr, flush=True)
        return await build_base()


async def identity(reference: str) -> tuple[str, str]:
    """An image's id and CPU architecture."""
    code, out = await docker("image", "inspect", "-f", "{{.Id}} {{.Architecture}}", reference)
    if code != 0 or len(out.split()) != 2:
        raise RuntimeError(f"cannot inspect {reference}: {out.strip()}")
    image_id, arch = out.split()
    return image_id.removeprefix("sha256:"), arch


def owner_of(data: Path) -> str:
    return hashlib.sha256(str(Path(data).resolve()).encode()).hexdigest()[:12]


def checked(owner: str) -> str:
    if not isinstance(owner, str) or not re.fullmatch(r"[0-9a-f]{12}", owner):
        raise Invalid(f"an owner is 12 lowercase hex digits, not {owner!r}")
    return owner


@dataclass(frozen=True)
class Host:
    """Where this process runs, as the Docker daemon sees it. On a host process, the identity. In a
    container: the host paths behind its mounts, the network it is on, its address there."""
    mounts: tuple[tuple[Path, Path], ...] = ()      # (a mount's destination here, its source on the host), deepest first
    network: str = "bridge"                         # the network a door is connected to for its way out
    address: str = ""                               # this container's address on that network; empty on a host

    def path(self, p: Path | str) -> Path:
        """The host's path for `p`, a path here."""
        p = Path(p)
        for dest, src in self.mounts:
            if p == dest or dest in p.parents:
                return src / p.relative_to(dest)
        return p

    @classmethod
    def from_inspect(cls, doc: dict) -> Host:
        mounts = sorted(((Path(m["Destination"]), Path(m["Source"])) for m in doc.get("Mounts", ())
                         if m.get("Source") and m.get("Destination")), key=lambda m: -len(m[0].parts))
        networks = doc.get("NetworkSettings", {}).get("Networks", {})
        name, address = next(((n, v.get("IPAddress", "")) for n, v in networks.items() if v.get("IPAddress")), ("bridge", ""))
        return cls(tuple(mounts), name, address)


def own_container_id() -> str:
    """The id of the container this process runs in, from what Docker leaves in /proc; '' on a host."""
    if not Path("/.dockerenv").exists():
        return ""
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            if m := re.search(r"/containers/([0-9a-f]{64})/", line):
                return m.group(1)
    except OSError:
        pass
    return socket.gethostname()                      # Docker's default hostname: the id's first 12 digits


async def inspect_self() -> Host:
    """This process's container as the daemon sees it, or the identity Host on a host process."""
    cid = own_container_id()
    if not cid:
        return Host()
    code, out = await docker("inspect", cid)
    if code != 0:
        return Host()
    try:
        return Host.from_inspect(json.loads(out)[0])
    except (ValueError, IndexError, KeyError, TypeError):
        return Host()


@dataclass(frozen=True)
class Names:
    run: str
    door: str
    network: str
    secrets: Path

    @classmethod
    def new(cls, owner: str, secrets: Path = SECRETS) -> Names:
        rid = uuid.uuid4().hex[:12]
        return cls(f"{PREFIX}run-{rid}", f"{PREFIX}door-{rid}", f"{PREFIX}net-{rid}", secrets / f"{PREFIX}{owner}-{rid}")


async def signal(container: str, name: str) -> bool:
    return (await docker("kill", "--signal", name, container))[0] == 0


@dataclass(frozen=True)
class Resources:
    """What one run may use."""
    cpus: float = 2
    memory: str = "4g"
    pids: int = 512


class DockerRuntime:
    """Runs specs in fresh containers of one image, each reaching only `egress` and, through the door's
    forwarder, the gateway at `gateway_url` with `token` as its credential."""

    def __init__(self, image: str, *, uid: int, limits: Resources = Resources(), read_only: Sequence[Path] = (),
                 writable: Sequence[Path] = (), egress: Sequence[str] = (), gateway_url: str = "", token: str = "",
                 ca_file: Path | None = None, owner: str, runtime: str = "", allow_private: bool = False,
                 host: Host = Host(), secrets: Path = SECRETS, turn_script: Path = TURN_SCRIPT, door_script: Path = DOOR_SCRIPT):
        if bool(gateway_url) != bool(token):
            raise Invalid("a gateway comes with a token, or neither")
        if token and any(c in token for c in "\r\n\0"):
            raise Invalid("a token is one line")
        self.image, self.uid, self.limits, self.ca_file = image, uid, limits, ca_file
        self.gateway_url, self.token, self.runtime, self.allow_private = gateway_url, token, runtime, allow_private
        self.host, self.secrets, self.turn_script, self.door_script = host, Path(secrets), Path(turn_script), Path(door_script)
        self.owner = checked(owner)
        self.read_only, self.writable = tuple(Path(p) for p in read_only), tuple(Path(p) for p in writable)
        self.allowed = sorted({normalize(h) for h in egress if h.strip()})

    @property
    def provider_url(self) -> str | None:
        return PROVIDER_URL if self.gateway_url else None

    @property
    def token_path(self) -> Path | None:
        return TOKEN_PATH if self.gateway_url else None

    async def start(self, spec: Spec) -> Execution:
        names, door = Names.new(self.owner, self.secrets), None
        try:
            run_bin = docker_bin()
            await whole(asyncio.to_thread(self._prepare, spec, names))
            await self._step("network", "create", "--internal", *self.label, names.network)
            door = await self._spawn_door(names)
            await self._door_ready(door)
            await self._step("network", "connect", self.host.network, names.door)
            env = {**spec.env, "HTTPS_PROXY": PROXY, "https_proxy": PROXY, "NO_PROXY": NO_PROXY, "no_proxy": NO_PROXY,
                   **({v: CA_PATH for v in CA_VARS} if self.ca_file else {})}
            execution = await ProcessExecution.spawn(
                [run_bin, *self.run_argv(spec, names, env)], env=client_env(env), cwd=None, input=spec.input,
                interactive=spec.interactive, timeout_s=spec.timeout_s,
                signal=lambda: interrupt(names.run), kill=lambda: docker("kill", names.run),
                cleanup=lambda: teardown(names, door), sidecars={"door": door.stdout})
            return execution
        except asyncio.CancelledError:
            await asyncio.shield(teardown(names, door))
            raise
        except (RuntimeError, OSError) as e:
            await asyncio.shield(teardown(names, door))
            return NeverStarted(str(e) or repr(e))

    async def recover(self) -> None:
        await sweep(self.owner, self.secrets)

    @property
    def label(self) -> tuple[str, str]:
        return "--label", f"{LABEL}={self.owner}"

    @property
    def user(self) -> str:
        return f"{self.uid}:{self.uid}" if os.geteuid() == 0 else f"{os.getuid()}:{os.getgid()}"

    def run_argv(self, spec: Spec, names: Names, env: dict[str, str]) -> list[str]:
        v = lambda src, dst, ro="": f"{self.host.path(src)}:{dst}{ro}"     # a mount, by the path the daemon sees
        argv = ["run", "--rm", "-i", "--init", "-e", "TINI_KILL_PROCESS_GROUP=1", "--name", names.run,
                *self.label, "--network", names.network, *HARDENING, "--cpus", str(self.limits.cpus),
                "--memory", self.limits.memory, "--pids-limit", str(self.limits.pids), "--user", self.user]
        if self.runtime:
            argv += ["--runtime", self.runtime]
        if spec.workspace:
            argv += ["-v", v(spec.workspace, spec.workspace), "-w", str(spec.workspace)]
        for path in self.writable:
            argv += ["-v", v(path, path)]
        for path in self.read_only:
            argv += ["-v", v(path, path, ":ro")]
        argv += ["-v", v(self.turn_script, TURN_SCRIPT, ":ro")]      # the runner, at its own path, read-only
        argv += ["-v", v(names.secrets / "passwd", "/etc/passwd", ":ro"), "-v", v(names.secrets / "group", "/etc/group", ":ro")]   # the run's user has a name and a home
        if self.gateway_url:
            argv += ["-v", v(names.secrets / "token", TOKEN_PATH, ":ro")]
        if self.ca_file:
            argv += ["-v", v(self.ca_file, CA_PATH, ":ro")]
        for name, value in env.items():
            argv += ["-e", f"{name}={value}" if clients_own(name) else name]
        return [*argv, self.image, *spec.command]

    def door_argv(self, names: Names) -> list[str]:
        argv = ["run", "-i", "--rm", "--name", names.door, *self.label, "--network", names.network,
                "--network-alias", DOOR_HOST, "--network-alias", PROVIDER_HOST, "--add-host", "host.docker.internal:host-gateway",
                *LOCKDOWN, *DOOR_LIMITS,
                "--user", "65534:65534", "-v", f"{self.host.path(self.door_script)}:/door/door.py:ro"]
        if self.ca_file:
            argv += ["-v", f"{self.host.path(self.ca_file)}:{CA_PATH}:ro", "-e", f"SSL_CERT_FILE={CA_PATH}"]
        argv += [DOOR_IMAGE, "python3", "/door/door.py", f"0.0.0.0:{DOOR_PORT}", *self.allowed]
        if self.gateway_url:
            argv += ["--gateway", f"0.0.0.0:{GATEWAY_PORT}", self.gateway_url]
        if self.allow_private:
            argv.append("--allow-private")
        return argv

    def _prepare(self, spec: Spec, names: Names) -> None:
        """Hand the writable paths to the run's user, give it a name, and write its token."""
        if os.geteuid() == 0:
            for path in (*([spec.workspace] if spec.workspace else []), *self.writable):
                hand_over(path, self.uid)
        names.secrets.parent.mkdir(parents=True, exist_ok=True)
        names.secrets.mkdir(mode=0o700)
        uid = self.uid if os.geteuid() == 0 else os.getuid()
        home = spec.env.get("HOME", "/tmp")
        (names.secrets / "passwd").write_text(f"root:x:0:0:root:/root:/usr/sbin/nologin\nnobody:x:65534:65534:nobody:/nonexistent:/usr/sbin/nologin\n"
                                              f"sandbox:x:{uid}:{uid}:sandbox:{home}:/bin/bash\n")
        (names.secrets / "group").write_text(f"root:x:0:\nnogroup:x:65534:\nsandbox:x:{uid}:\n")
        for name in ("passwd", "group"):
            os.chmod(names.secrets / name, 0o644)
        os.chmod(names.secrets, 0o711)
        if self.gateway_url:
            token = names.secrets / "token"
            fd = os.open(token, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
            with os.fdopen(fd, "w") as f:
                f.write(self.token)
            if os.geteuid() == 0:
                os.chown(token, self.uid, self.uid)

    async def _spawn_door(self, names: Names) -> asyncio.subprocess.Process:
        door = await asyncio.create_subprocess_exec(
            docker_bin(), *self.door_argv(names), env=client_env(), start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        return door

    @staticmethod
    async def _door_ready(door: asyncio.subprocess.Process) -> None:
        said: list[str] = []
        try:
            async with asyncio.timeout(30):
                while (line := (await door.stdout.readline()).decode(errors="replace")) and line.strip() != READY:
                    said.append(line.strip())
        except TimeoutError:
            line = ""
        if not line:
            raise RuntimeError("egress door did not start: " + " | ".join(said[-5:]))

    @staticmethod
    async def _step(*args: str) -> None:
        code, out = await docker(*args)
        if code != 0:
            raise RuntimeError(f"docker {' '.join(args[:2])} failed: {out[-500:]}")



async def whole(awaitable: Awaitable[T]) -> T:
    """Await it to its end even if cancelled meanwhile, then let the cancel stand."""
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await asyncio.wait([task])
        raise


async def interrupt(name: str) -> None:
    """SIGTERM to a run's container, tried until it lands. The run's end cancels it."""
    while not await signal(name, "TERM"):
        await asyncio.sleep(0.05)


def hand_over(path: Path, uid: int) -> None:
    """Make a tree the run's user's, without following links."""
    for root, dirs, files in os.walk(path):
        for name in (root, *(os.path.join(root, n) for n in dirs + files)):
            try:
                os.lchown(name, uid, uid)
            except FileNotFoundError:
                pass


def shred(secrets: Path) -> None:
    token = secrets / "token"
    if token.is_file():
        token.chmod(0o600)
        with open(token, "r+b") as f:
            f.write(b"\0" * max(token.stat().st_size, 64))
            f.flush()
            os.fsync(f.fileno())
        token.unlink()
    if secrets.exists():
        shutil.rmtree(secrets, ignore_errors=True)


async def teardown(names: Names, door: asyncio.subprocess.Process | None) -> None:
    try:
        if door:
            try:
                door.kill()
            except ProcessLookupError:
                pass
            await door.wait()
        await docker("rm", "-f", names.run, names.door)
        await docker("network", "rm", "-f", names.network)
    except RuntimeError:
        pass
    finally:
        await asyncio.to_thread(shred, names.secrets)


async def sweep(owner: str, secrets: Path = SECRETS) -> None:
    """Remove the containers, networks and tokens of the owner's runs a crash cut off."""
    label = f"label={LABEL}={checked(owner)}"
    code, out = await docker("ps", "-aq", "--filter", label)
    if code != 0:
        raise RuntimeError(f"Docker is not reachable: {out.strip()}")
    if ids := out.split():
        await docker("rm", "-f", *ids)
    _, out = await docker("network", "ls", "-q", "--filter", label)
    if ids := out.split():
        await docker("network", "rm", *ids)
    for left in Path(secrets).glob(f"{PREFIX}{owner}-" + "[0-9a-f]" * 12):
        await asyncio.to_thread(shred, left)


# the node
@dataclass
class DockerNode:
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
