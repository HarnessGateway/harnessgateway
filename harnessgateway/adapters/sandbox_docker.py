"""The sandbox port on Docker with gVisor (`runsc`): one container per box on an internal network of its own, whose
only peer is the box's door (SPEC §6.1, §7.1).

On every box: the gVisor runtime unless the operator set HG_UNSAFE_RUNTIME; no capabilities, no privilege escalation,
a read-only root, a size-limited /tmp, CPU, memory and process limits; uid 10001; writable paths are the box's own
bind mounts; no route out but the door; no DNS. The node never touches a box's writable files as root: seeding,
restoring, snapshotting and staging run inside the box as its user."""
from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings
from ..node.trust import DOOR_UID, Trust
from ..ports.sandbox import UID, BoxSpec
from .docker_cli import LABEL, DockerError, docker, docker_file, env_args

CHUNK = 64 * 1024
SANDBOX_PATH = "/opt/hg/tools/bin:/opt/hg/tools/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
TRUST_DIR = "/etc/hg/trust"
OPERATOR_CA = "/etc/hg/operator-ca.pem"
DOOR = "hg-door"
IMAGE_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"

# Every process but PID 1 (tini), the keeper (`sleep infinity`, tini's child) and this shell.
KILL_ALL = r"""for d in /proc/[0-9]*; do
  p=${d#/proc/}
  [ "$p" = 1 ] || [ "$p" = $$ ] && continue
  ppid=$(sed 's/.*) //' "$d/stat" 2>/dev/null | cut -d' ' -f2)
  [ "$ppid" = 1 ] && [ "$(tr '\0' ' ' < "$d/cmdline" 2>/dev/null)" = "sleep infinity " ] && continue
  kill -KILL "$p" 2>/dev/null
done; true"""

# The existing paths among the arguments (relative to /), as one gzipped tar on stdout; an empty tar when none exist.
TAR_OUT = r"""cd / || exit 2
have=()
for p in "$@"; do [ -e "$p" ] && have+=("$p"); done
if [ ${#have[@]} -eq 0 ]; then exec tar czf - -T /dev/null; fi
exec tar czf - --warning=no-file-changed -- "${have[@]}"
"""


class DockerProcess:
    def __init__(self, proc: asyncio.subprocess.Process):
        self._proc = proc

    async def read(self) -> bytes:
        assert self._proc.stdout is not None
        return await self._proc.stdout.read(CHUNK)

    async def stderr(self) -> AsyncIterator[bytes]:
        assert self._proc.stderr is not None
        while chunk := await self._proc.stderr.read(CHUNK):
            yield chunk

    async def wait(self) -> int:
        return await self._proc.wait()

    def kill(self) -> None:
        if self._proc.returncode is None:
            self._proc.kill()


class DockerBox:
    def __init__(self, spec: BoxSpec, settings: Settings, trust: Trust, node_id: str):
        self.spec, self.settings, self.trust, self.node_id = spec, settings, trust, node_id
        self.container, self.door, self.network = f"hg-s-{spec.name}", f"hg-d-{spec.name}", f"hg-n-{spec.name}"

    def _labels(self) -> list[str]:
        return ["--label", f"{LABEL}={self.node_id}", "--label", f"harnessgateway.org={self.spec.org_id}"]

    async def start(self) -> None:
        try:
            await self._start()
        except BaseException:
            await self.destroy()
            raise

    async def _start(self) -> None:
        s, cfg = self.spec, self.settings
        (await docker("network", "create", "--internal", *self._labels(), self.network)).check("network create")
        door_cmd = ["door", "--dir", "/door", "--ca", "/ca", "--gateway", cfg.gateway_url]
        mounts = ["--mount", f"type=bind,source={s.door_dir},target=/door",
                  "--mount", f"type=bind,source={self.trust.ca_dir},target=/ca,readonly"]
        if cfg.upstream_proxy:
            door_cmd += ["--upstream-proxy", cfg.upstream_proxy]
        if cfg.ca_file:
            door_cmd += ["--ca-file", OPERATOR_CA]
            mounts += ["--mount", f"type=bind,source={cfg.ca_file},target={OPERATOR_CA},readonly"]
        if cfg.allow_private_egress:
            door_cmd.append("--allow-private")
        (await docker("run", "-d", "--name", self.door, *self._labels(), "--network", cfg.uplink_network, "--read-only",
                      "--tmpfs", "/tmp:rw,size=16m", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                      "--user", f"{DOOR_UID}:{DOOR_UID}", "--memory", "256m", "--pids-limit", "256", "--restart", "no",
                      "--entrypoint", "harnessgateway", *mounts, cfg.platform_image, *door_cmd)).check("door run")
        (await docker("network", "connect", "--alias", DOOR, self.network, self.door)).check("door connect")
        if not await _wait_until(self._door_ready, timeout=30):
            logs = (await docker("logs", "--tail", "20", self.door, timeout=15)).err.decode(errors="replace")
            raise DockerError(f"the egress door did not start: {logs.strip()[-800:]}")
        # gVisor's netstack does not see Docker's embedded DNS and a box needs none: the door is the one name it
        # resolves, by a hosts entry; everything else goes through the door by name.
        door_ip = (await docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self.network}").IPAddress}}}}',
                                self.door)).check("door address").out.decode().strip()
        if not door_ip:
            raise DockerError("the egress door has no address on the box's network")
        proxy = f"http://{DOOR}:3128"
        bundle, extra = f"{TRUST_DIR}/bundle.pem", f"{TRUST_DIR}/extra.pem"
        env = {"HOME": "/home/hg", "PATH": SANDBOX_PATH, "LANG": "C.UTF-8",
               "HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "https_proxy": proxy, "http_proxy": proxy,
               "NO_PROXY": f"{DOOR},localhost,127.0.0.1", "no_proxy": f"{DOOR},localhost,127.0.0.1",
               "SSL_CERT_FILE": bundle, "REQUESTS_CA_BUNDLE": bundle, "CURL_CA_BUNDLE": bundle, "GIT_SSL_CAINFO": bundle,
               "PIP_CERT": bundle, "NODE_EXTRA_CA_CERTS": extra, **s.env}
        box_mounts = [m for mount in s.mounts for m in ("--mount", f"type=bind,source={mount.host},target={mount.target}"
                                                                    + (",readonly" if mount.readonly else ""))]
        box_mounts += ["--mount", f"type=bind,source={self.trust.trust_dir},target={TRUST_DIR},readonly"]
        (await docker("run", "-d", "--name", self.container, *self._labels(), "--runtime", cfg.runtime,
                      "--network", self.network, "--hostname", "sandbox", "--add-host", f"{DOOR}:{door_ip}",
                      "--dns", "127.0.0.1", "--read-only", "--tmpfs", f"/tmp:rw,exec,size={s.tmp_mb}m,mode=1777",
                      "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{UID}:{UID}",
                      "--cpus", str(s.vcpus), "--memory", f"{s.memory_mb}m", "--memory-swap", f"{s.memory_mb}m",
                      "--pids-limit", str(s.pids), "--restart", "no", *env_args(env), *box_mounts,
                      cfg.sandbox_image, "sleep", "infinity", env=env)).check("sandbox run")

    async def _door_ready(self) -> bool:
        return b"door: ready" in (await docker("logs", self.door, timeout=15)).err

    async def alive(self) -> bool:
        result = await docker("inspect", "-f", "{{.State.Running}}", self.container, timeout=15)
        return result.code == 0 and result.out.strip() == b"true"

    async def sh(self, script: str, *args: str, timeout: float = 300) -> bytes:
        result = await docker("exec", "-u", f"{UID}:{UID}", self.container, "bash", "-c", script, "bash", *args,
                              timeout=timeout)
        return result.check(f"sandbox: {script.split()[0]}").out

    async def exec(self, argv: list[str], env: dict[str, str]) -> DockerProcess:
        proc = await asyncio.create_subprocess_exec(
            "docker", "exec", "-u", f"{UID}:{UID}", "-w", "/workspace", *env_args(env), self.container, *argv,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, **env})
        return DockerProcess(proc)

    async def tar_out(self, paths: list[str], dest: Path, timeout: float) -> None:
        result = await docker_file("exec", "-u", f"{UID}:{UID}", self.container, "bash", "-c", TAR_OUT, "bash", *paths,
                                   stdout_path=dest, timeout=timeout)
        if result.code not in (0, 1):                     # 1: a file changed while it was read
            raise DockerError(f"snapshot: {result.err.decode(errors='replace')[-300:]}")

    async def tar_in(self, src: Path, timeout: float) -> None:
        (await docker_file("exec", "-i", "-u", f"{UID}:{UID}", self.container, "tar", "xzf", "-", "-C", "/",
                           "--no-same-owner", "--no-overwrite-dir", stdin_path=src, timeout=timeout)).check("restore")

    async def terminate(self, pidfile: str) -> None:
        with contextlib.suppress(DockerError):
            await self.sh('p=$(cat "$1" 2>/dev/null) && kill -TERM -- "-$p" 2>/dev/null; true', pidfile, timeout=15)

    async def kill_all(self) -> None:
        with contextlib.suppress(DockerError):
            await self.sh(KILL_ALL, timeout=15)

    async def destroy(self) -> None:
        for args in (["rm", "-f", self.container], ["rm", "-f", self.door], ["network", "rm", self.network]):
            with contextlib.suppress(DockerError):
                await docker(*args, timeout=60)


class DockerRuntime:
    def __init__(self, settings: Settings, node_id: str, trust: Trust):
        self.settings, self.node_id, self.trust = settings, node_id, trust

    def box(self, spec: BoxSpec) -> DockerBox:
        return DockerBox(spec, self.settings, self.trust, self.node_id)

    async def prepare(self) -> str:
        for image in (self.settings.sandbox_image, self.settings.platform_image):
            if (await docker("image", "inspect", image, timeout=60)).code != 0:
                (await docker("pull", image, timeout=1800)).check(f"pull {image}")
        return (await docker("image", "inspect", "-f", "{{.Id}}", self.settings.sandbox_image, timeout=60)
                ).check("sandbox image").out.decode().strip()

    async def ca_bundle(self) -> bytes:
        return (await docker("run", "--rm", "--network", "none", "--entrypoint", "cat", self.settings.sandbox_image,
                             IMAGE_BUNDLE, timeout=120)).check("the sandbox image's CA bundle").out

    async def sweep(self) -> int:
        containers = (await docker("ps", "-aq", "--filter", f"label={LABEL}={self.node_id}", timeout=60)).out.split()
        if containers:
            await docker("rm", "-f", *[c.decode() for c in containers], timeout=120)
        for net in (await docker("network", "ls", "-q", "--filter", f"label={LABEL}={self.node_id}", timeout=60)).out.split():
            with contextlib.suppress(DockerError):
                await docker("network", "rm", net.decode(), timeout=60)
        return len(containers)


async def _wait_until(predicate, timeout: float, every: float = 0.25) -> bool:  # type: ignore[no-untyped-def]
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(every)
    return False
