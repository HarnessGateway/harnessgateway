"""A box: one gVisor container on an internal network of its own, with an egress door as its only way out.

Guarantees, on every box:
- the gVisor runtime (runsc), unless the operator explicitly allowed another for development;
- no Linux capabilities, no privilege escalation, a read-only root, a size-limited /tmp, CPU, memory and process limits;
- an unprivileged user (uid 10001); writable paths are the box's own bind mounts and nothing else;
- no route out but the door, which tunnels only to the allowed hosts and relays provider calls to the gateway;
- the node never reads or writes a box's writable files as root: seeding, restoring, checkpointing and file operations
  run inside the box, as the box's user, so nothing a box plants can redirect the node.

A session's box stays up between turns (warm) until its idle time runs out; an install box lives for one install.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from .docker import LABEL, DockerError, Exec, docker

UID = 10001
DOOR_UID = 65534
SANDBOX_PATH = "/opt/hg/tools/bin:/opt/hg/tools/node_modules/.bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
CA_PATH = "/etc/ssl/certs/harnessgateway-ca.pem"


@dataclass
class Mount:
    host: Path
    target: str
    readonly: bool = True

    def arg(self) -> list[str]:
        return ["--mount", f"type=bind,source={self.host},target={self.target}" + (",readonly" if self.readonly else "")]


@dataclass
class BoxSpec:
    name: str                       # unique suffix, e.g. the session id without its prefix
    node_id: str
    org_id: str
    vcpus: float
    memory_mb: int
    pids: int
    egress: list[str]
    mounts: list[Mount]
    env: dict[str, str] = field(default_factory=dict)
    tmp_mb: int = 1024


class Box:
    def __init__(self, spec: BoxSpec, settings: Settings, door_dir: Path):
        self.spec, self.settings, self.door_dir = spec, settings, door_dir
        self.container = f"hg-s-{spec.name}"
        self.door = f"hg-d-{spec.name}"
        self.network = f"hg-n-{spec.name}"

    def _labels(self) -> list[str]:
        return ["--label", f"{LABEL}={self.spec.node_id}", "--label", f"harnessgateway.org={self.spec.org_id}"]

    async def start(self) -> None:
        try:
            await self._start()
        except Exception:
            await self.destroy()
            raise

    async def _start(self) -> None:
        s, cfg = self.spec, self.settings
        (await docker("network", "create", "--internal", *self._labels(), self.network)).check("network create")
        self.door_dir.mkdir(parents=True, exist_ok=True)
        os.chown(self.door_dir, DOOR_UID, DOOR_UID)
        door_cmd = ["-m", "harnessgateway.node.door", "--gateway", cfg.gateway_url,
                    "--counters", "/counters/egress.json", "--allow", *s.egress]
        if cfg.upstream_proxy:
            door_cmd += ["--upstream-proxy", cfg.upstream_proxy]
        if cfg.allow_private_egress:
            door_cmd.append("--allow-private")
        door_env: list[str] = []
        if cfg.ca_file:
            door_env = ["-e", f"SSL_CERT_FILE={CA_PATH}", "--mount", f"type=bind,source={cfg.ca_file},target={CA_PATH},readonly"]
        (await docker("run", "-d", "--name", self.door, *self._labels(), "--network", cfg.uplink_network, "--read-only",
                     "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{DOOR_UID}:{DOOR_UID}",
                     "--memory", "256m", "--pids-limit", "256", "--restart", "no", "--entrypoint", "python", *door_env,
                     "--mount", f"type=bind,source={self.door_dir},target=/counters",
                     cfg.platform_image, *door_cmd)).check("door run")
        (await docker("network", "connect", "--alias", "hg-egress", self.network, self.door)).check("door connect")
        if not await wait_until(self._door_ready, timeout=20):
            logs = (await docker("logs", "--tail", "20", self.door, timeout=15)).err.decode(errors="replace")
            raise DockerError(f"the egress door did not start: {logs.strip()[-800:]}")
        # gVisor's netstack does not see Docker's embedded DNS, and the sandbox needs no DNS at all: the door is the
        # one name it resolves, by a hosts entry; everything else goes through the door by name.
        door_ip = (await docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{self.network}").IPAddress}}}}',
                                self.door)).check("door address").out.decode().strip()
        assert door_ip, "the door has an address on the box's network"
        env = {"HOME": "/home/hg", "PATH": SANDBOX_PATH, "LANG": "C.UTF-8",
               "HTTPS_PROXY": "http://hg-egress:3128", "HTTP_PROXY": "http://hg-egress:3128",
               "https_proxy": "http://hg-egress:3128", "http_proxy": "http://hg-egress:3128",
               "NO_PROXY": "hg-egress,localhost,127.0.0.1", "no_proxy": "hg-egress,localhost,127.0.0.1", **s.env}
        mounts = [m.arg() for m in s.mounts]
        if cfg.ca_file:
            env.update({"SSL_CERT_FILE": CA_PATH, "NODE_EXTRA_CA_CERTS": CA_PATH, "REQUESTS_CA_BUNDLE": CA_PATH,
                        "PIP_CERT": CA_PATH, "GIT_SSL_CAINFO": CA_PATH, "CURL_CA_BUNDLE": CA_PATH})
            mounts.append(Mount(cfg.ca_file, CA_PATH).arg())
        runtime = ["--runtime", cfg.runtime] if cfg.runtime else []
        (await docker("run", "-d", "--name", self.container, *self._labels(), *runtime, "--network", self.network,
                     "--hostname", "sandbox", "--add-host", f"hg-egress:{door_ip}", "--dns", "127.0.0.1", "--read-only",
                     "--tmpfs", f"/tmp:rw,exec,size={s.tmp_mb}m,mode=1777",
                     "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{UID}:{UID}",
                     "--cpus", str(s.vcpus), "--memory", f"{s.memory_mb}m", "--memory-swap", f"{s.memory_mb}m",
                     "--pids-limit", str(s.pids), "--restart", "no",
                     *[f"--env={k}={v}" for k, v in env.items()], *[a for m in mounts for a in m],
                     self.settings.sandbox_image, "sleep", "infinity", )).check("sandbox run")

    async def sh(self, script: str, *args: str, stdin: bytes | None = None, timeout: float = 300, user: int = UID) -> bytes:
        """Run a shell script inside the box as its user; positional arguments are passed as $1.. (never interpolated)."""
        result = await docker("exec", *(["-i"] if stdin is not None else []), "-u", f"{user}:{user}", self.container,
                              "sh", "-c", script, "sh", *args, input=stdin, timeout=timeout)
        return result.check(f"sandbox: {script.split()[0]}").out

    async def exec(self, command: list[str], env: dict[str, str]) -> Exec:
        return await Exec.start(self.container, command, user=f"{UID}:{UID}", workdir="/workspace", env=env)

    async def signal_group(self, pidfile: str, signal: str) -> None:
        with contextlib.suppress(DockerError):
            await self.sh(f'p=$(cat {pidfile} 2>/dev/null) && kill -{signal} "-$p" 2>/dev/null; true', timeout=15)

    def counters(self) -> dict[str, int]:
        try:
            doc = json.loads((self.door_dir / "egress.json").read_text())
            return {"out": int(doc.get("out", 0)), "in": int(doc.get("in", 0))}
        except (OSError, ValueError):
            return {"out": 0, "in": 0}

    async def _door_ready(self) -> bool:
        result = await docker("logs", self.door, timeout=15)
        return b"door: ready" in result.err

    async def alive(self) -> bool:
        result = await docker("inspect", "-f", "{{.State.Running}}", self.container, timeout=15)
        return result.code == 0 and result.out.strip() == b"true"

    async def destroy(self) -> None:
        for args in (["rm", "-f", self.container], ["rm", "-f", self.door], ["network", "rm", self.network]):
            with contextlib.suppress(DockerError):
                await docker(*args, timeout=60)


async def sweep(node_id: str) -> int:
    """Remove every container and network this node left behind (after a restart nothing of them is trusted)."""
    containers = (await docker("ps", "-aq", "--filter", f"label={LABEL}={node_id}", timeout=60)).out.split()
    if containers:
        await docker("rm", "-f", *[c.decode() for c in containers], timeout=120)
    networks = (await docker("network", "ls", "-q", "--filter", f"label={LABEL}={node_id}", timeout=60)).out.split()
    for n in networks:
        with contextlib.suppress(DockerError):
            await docker("network", "rm", n.decode(), timeout=60)
    return len(containers)


def fresh_dir(path: Path, owner: int | None = None, mode: int = 0o755) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, mode=mode)
    path.chmod(mode)
    if owner is not None:
        os.chown(path, owner, owner)
    return path


def empty_dir(path: Path) -> Path:
    """Remove a directory's contents but keep the directory itself: a box's bind mount refers to its inode."""
    path.mkdir(parents=True, exist_ok=True)
    for child in path.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    return path


async def wait_until(predicate, timeout: float, every: float = 0.5) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if await predicate():
            return True
        await asyncio.sleep(every)
    return False
