"""The Docker driver: a plan becomes the `docker run` command of the specification (§5.1), run with the docker CLI."""

import asyncio
import json
import re
import shlex
from asyncio.subprocess import PIPE, STDOUT

from harnessgateway.domain.contract import SANDBOX_UID
from harnessgateway.domain.events import LINE_MAX
from harnessgateway.domain.plan import Mount, SandboxPlan, StepPlan
from harnessgateway.drivers.base import DriverError, OnLine, SandboxExit, StepResult

LABEL = "hg.org"                       # every container of this platform carries it
OUTPUT_TAIL = 64 * 1024
_NEVER_STARTED = "0001-01-01T00:00:00Z"

_MASKS = (
    (re.compile(r"(://[^:/@\s]+):[^@\s]+@"), r"\1:***@"),
    (re.compile(r"hgs_[0-9a-f]{32}"), "hgs_***"),
    (re.compile(r"(HG_STEP_TOKEN=)\S+"), r"\1***"),
)


def _common(labels: dict[str, str], network: str | None) -> list[str]:
    argv = []
    for key, value in labels.items():
        argv += ["--label", f"{key}={value}"]
    return [*argv, "--network", network or "none"]


def _mounts_and_env(mounts: list[Mount], env: dict[str, str]) -> list[str]:
    argv = []
    for mount in mounts:
        argv += ["--volume", f"{mount.volume}:{mount.path}{':ro' if mount.read_only else ''}"]
    for name, value in env.items():
        argv += ["--env", f"{name}={value}"]
    return argv


def render_sandbox(plan: SandboxPlan, *, runtime: str | None) -> list[str]:
    """The command that runs an untrusted container: every hardening flag, none of them optional."""
    in_memory, on_disk = f"/tmp:rw,exec,size={plan.tmp_mb}m", "/tmp"      # noqa: S108 - paths inside the sandbox
    tmp = ["--tmpfs", in_memory] if plan.tmp_mb is not None else ["--volume", on_disk]
    return [
        "docker", "run", "--name", plan.name, "--init",
        *_common(plan.labels, plan.network),
        *(["--runtime", runtime] if runtime else []),
        "--cpus", f"{plan.cpus:g}", "--memory", f"{plan.memory_mb}m", "--memory-swap", f"{plan.memory_mb}m",
        "--pids-limit", str(plan.pids),
        "--read-only", *tmp,
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", f"{SANDBOX_UID}:{SANDBOX_UID}",
        "--workdir", plan.workdir,
        *_mounts_and_env(plan.mounts, plan.env),
        plan.image, *plan.command,
    ]


def render_step(step: StepPlan, *, platform_image: str) -> list[str]:
    """The command that runs a trusted step: the platform's own image, as root, gone when it ends."""
    return [
        "docker", "run", "--rm", "--name", step.name,
        *_common(step.labels, step.network),
        "--user", "0:0",
        *_mounts_and_env(step.mounts, step.env),
        platform_image, *step.command,
    ]


def mask(argv: list[str]) -> str:
    """A command as it may be logged and shown: no token, no placeholder."""
    shown = shlex.join(argv)
    for pattern, replacement in _MASKS:
        shown = pattern.sub(replacement, shown)
    return shown


class LineSplitter:
    """Bytes in, whole lines out. A line past the limit is cut one character over it, so it is refused, not lost."""

    def __init__(self, limit: int = LINE_MAX) -> None:
        self._limit = limit
        self._buffer = bytearray()
        self._overflowing = False

    def _line(self, raw: bytes) -> str:
        return raw[: self._limit + 1].decode("utf-8", errors="replace")

    def feed(self, data: bytes) -> list[str]:
        lines = []
        for piece in data.splitlines(keepends=True):
            if not self._overflowing:
                self._buffer += piece[: self._limit + 1 - len(self._buffer)]
            if piece.endswith(b"\n"):
                lines.append(self._line(bytes(self._buffer).rstrip(b"\r\n")))
                self._buffer.clear()
                self._overflowing = False
            else:
                self._overflowing = len(self._buffer) > self._limit
        return lines

    def finish(self) -> list[str]:
        rest, self._buffer = bytes(self._buffer), bytearray()
        return [self._line(rest)] if rest else []


async def _pump(stream: asyncio.StreamReader, on_line: OnLine) -> None:
    splitter = LineSplitter()
    while chunk := await stream.read(65536):
        for line in splitter.feed(chunk):
            await on_line(line)
    for line in splitter.finish():
        await on_line(line)


class DockerDriver:
    name = "docker"

    def __init__(self, *, platform_image: str, runtime: str = "auto") -> None:
        self._platform_image = platform_image
        self._runtime_setting = runtime
        self._runtime: str | None = None
        self._resolved = False

    async def _docker(self, *args: str) -> tuple[int, str]:
        process = await asyncio.create_subprocess_exec("docker", *args, stdout=PIPE, stderr=STDOUT)
        output, _ = await process.communicate()
        assert process.returncode is not None
        return process.returncode, output.decode("utf-8", errors="replace").strip()

    async def _checked(self, *args: str) -> str:
        code, output = await self._docker(*args)
        if code != 0:
            raise DriverError(f"docker {args[0]} failed: {output[-500:]}")
        return output

    async def _resolve_runtime(self) -> str | None:
        """The isolating runtime to name on the command line, or None for the daemon's default."""
        if not self._resolved:
            if self._runtime_setting == "runc":
                self._runtime = None
            else:
                runtimes = json.loads(await self._checked("info", "--format", "{{json .Runtimes}}"))
                if "runsc" in runtimes:
                    self._runtime = "runsc"
                elif self._runtime_setting == "runsc":
                    raise DriverError("HG_RUNTIME is runsc, but this Docker daemon has no runsc runtime (gVisor)")
            self._resolved = True
        return self._runtime

    async def isolation(self) -> str:
        return "gvisor" if await self._resolve_runtime() == "runsc" else "none"

    async def image_id(self, ref: str) -> str:
        code, output = await self._docker("image", "inspect", "--format", "{{.Id}}", ref)
        if code != 0:
            await self._checked("pull", "--quiet", ref)
            output = await self._checked("image", "inspect", "--format", "{{.Id}}", ref)
        return output

    async def ensure_network(self, name: str, *, internal: bool) -> None:
        code, _ = await self._docker("network", "inspect", name)
        if code != 0:
            await self._checked("network", "create", *(["--internal"] if internal else []), name)

    async def ensure_volume(self, name: str) -> None:
        await self._checked("volume", "create", name)

    async def remove_volume(self, name: str) -> None:
        await self._docker("volume", "rm", "--force", name)

    async def run_step(self, step: StepPlan) -> StepResult:
        argv = render_step(step, platform_image=self._platform_image)
        code, output = await self._docker(*argv[1:])
        return StepResult(code, output[-OUTPUT_TAIL:])

    async def run_sandbox(self, plan: SandboxPlan, on_stdout: OnLine, on_stderr: OnLine) -> SandboxExit:
        argv = render_sandbox(plan, runtime=await self._resolve_runtime())
        process = await asyncio.create_subprocess_exec(*argv, stdout=PIPE, stderr=PIPE)
        assert process.stdout is not None
        assert process.stderr is not None
        client_errors: list[str] = []

        async def keep_and_pass(line: str) -> None:
            client_errors.append(line)
            del client_errors[:-20]
            await on_stderr(line)

        try:
            await asyncio.gather(_pump(process.stdout, on_stdout), _pump(process.stderr, keep_and_pass))
            await process.wait()
            code, state = await self._docker(
                "inspect", "--format", "{{.State.StartedAt}} {{.State.OOMKilled}} {{.State.ExitCode}}", plan.name)
            if code != 0 or state.startswith(_NEVER_STARTED):
                return SandboxExit(started=False, error="\n".join(client_errors)[-2000:] or state)
            _, oom, exit_code = state.split()
            return SandboxExit(started=True, exit_code=int(exit_code), out_of_memory=oom == "true")
        finally:
            if process.returncode is None:
                process.kill()
            await self._docker("rm", "--force", "--volumes", plan.name)

    async def stop(self, name: str, grace_s: int) -> None:
        await self._docker("stop", "--time", str(grace_s), name)

    async def reap(self, keep: set[str]) -> list[str]:
        names = (await self._checked("ps", "--all", "--filter", f"label={LABEL}", "--format", "{{.Names}}")).split()
        gone = [name for name in names if name not in keep]
        if gone:
            await self._docker("rm", "--force", "--volumes", *gone)
        return gone
