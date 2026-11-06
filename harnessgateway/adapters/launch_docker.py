"""The launcher on Docker (SPEC §8.1): one container per box from the sandbox image, on an internal network of its own
whose only other member is the door container, under gVisor unless the operator chose `runc`.

Each box gets: a read-only root; a tmpfs /tmp of `tmp_mb`; its writable directories (workspace, state, home, the
package, the tools, the turn inputs, the trust bundle) bind-mounted from a directory of its own under HG_BOX_DIR,
owned by the sandbox user and removed with the box; no capabilities; no privilege escalation; uid 10001; cpu,
memory and pid limits; no DNS and one hosts entry, the door. The runner is the container's command."""
from __future__ import annotations

import contextlib
import logging
import os
import shutil

from ..config import Settings
from ..ports.launch import BoxSpec
from .docker_cli import LABEL, DockerError, docker, env_args

UID = 10001
DOOR = "hg-door"
RUNNER = ["python3", "/opt/hg/runner.py"]
WRITABLE = {"workspace": "/workspace", "state": "/state", "home": "/home/hg", "package": "/opt/hg/package",
            "tools": "/opt/hg/tools", "run": "/run/hg", "runtime": "/opt/hg/runtime", "blobs": "/tmp/hg/blobs"}  # noqa: S108
log = logging.getLogger("harnessgateway.launcher")


class DockerLauncher:
    def __init__(self, settings: Settings):
        self.s = settings
        if settings.sandbox_runtime != "runsc":
            log.warning("HG_SANDBOX_RUNTIME=%s: boxes run without gVisor and are not isolated from the host kernel",
                        settings.sandbox_runtime)

    @staticmethod
    def names(box_id: str) -> tuple[str, str]:
        return f"hg-box-{box_id[-12:]}", f"hg-net-{box_id[-12:]}"

    async def start(self, spec: BoxSpec) -> str:
        container, network = self.names(spec.box_id)
        folder = self.s.box_dir / spec.box_id
        try:
            for name in WRITABLE:
                (folder / name).mkdir(parents=True, exist_ok=True)
                os.chown(folder / name, UID, UID)
            (await docker("network", "create", "--internal", "--label", f"{LABEL}={spec.box_id}", network)
             ).check("network create")
            (await docker("network", "connect", "--alias", DOOR, network, self.s.door_container)).check("door connect")
            door_ip = (await docker("inspect", "-f", f'{{{{(index .NetworkSettings.Networks "{network}").IPAddress}}}}',
                                    self.s.door_container)).check("door address").out.decode().strip()
            if not door_ip:
                raise DockerError("the door has no address on the box's network")
            env = {**spec.env(self.s.api_url, self.s.door), "HG_RUN_UID": str(UID)}
            mounts = [a for name, target in WRITABLE.items()
                      for a in ("--mount", f"type=bind,source={folder / name},target={target}")]
            memory = spec.memory_mb + spec.tmp_mb
            (await docker("run", "-d", "--name", container, "--label", f"{LABEL}={spec.box_id}",
                          "--label", f"harnessgateway.org={spec.org_id}", "--runtime", self.s.sandbox_runtime,
                          "--network", network, "--hostname", "sandbox", "--add-host", f"{DOOR}:{door_ip}",
                          "--dns", "127.0.0.1", "--read-only", "--tmpfs", f"/tmp:rw,exec,size={spec.tmp_mb}m,mode=1777",  # noqa: S108
                          "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--user", f"{UID}:{UID}",
                          "--cpus", str(spec.vcpus), "--memory", f"{memory}m", "--memory-swap", f"{memory}m",
                          "--pids-limit", str(spec.pids), "--restart", "no", *env_args(env), *mounts,
                          self.s.sandbox_image, *RUNNER, env=env)).check("box run")
        except BaseException:
            await self.stop(container)
            raise
        return container

    async def stop(self, reference: str) -> None:
        box_id = ""
        with contextlib.suppress(DockerError):
            box_id = (await docker("inspect", "-f", f'{{{{index .Config.Labels "{LABEL}"}}}}', reference, timeout=30)
                      ).check("inspect").out.decode().strip()
        with contextlib.suppress(DockerError):
            await docker("rm", "-f", reference, timeout=60)
        network = reference.replace("hg-box-", "hg-net-", 1)
        with contextlib.suppress(DockerError):
            await docker("network", "disconnect", "-f", network, self.s.door_container, timeout=30)
        with contextlib.suppress(DockerError):
            await docker("network", "rm", network, timeout=30)
        if box_id:
            shutil.rmtree(self.s.box_dir / box_id, ignore_errors=True)

    async def alive(self, reference: str) -> bool:
        result = await docker("inspect", "-f", "{{.State.Running}}", reference, timeout=15)
        return result.code == 0 and result.out.strip() == b"true"

    async def reap(self) -> int:
        """Exited box containers removed with their networks and directories; orphaned networks removed."""
        exited = (await docker("ps", "-aq", "--filter", f"label={LABEL}", "--filter", "status=exited", timeout=60)
                  ).out.decode().split()
        for cid in exited:
            name = (await docker("inspect", "-f", "{{.Name}}", cid, timeout=15)).out.decode().strip().lstrip("/")
            await self.stop(name or cid)
        live = set((await docker("ps", "-aq", "--filter", f"label={LABEL}", timeout=60)).out.decode().split())
        for net in (await docker("network", "ls", "-q", "--filter", f"label={LABEL}", timeout=60)).out.decode().split():
            members = (await docker("network", "inspect", "-f", "{{range .Containers}}{{.Name}} {{end}}", net, timeout=15)
                       ).out.decode().split()
            if not [m for m in members if m != self.s.door_container] and not live & set(members):
                with contextlib.suppress(DockerError):
                    await docker("network", "disconnect", "-f", net, self.s.door_container, timeout=30)
                with contextlib.suppress(DockerError):
                    await docker("network", "rm", net, timeout=30)
        return len(exited)

    async def prepare(self) -> None:
        """The images present and the box directory ready, before the first launch."""
        self.s.box_dir.mkdir(parents=True, exist_ok=True)
        if (await docker("image", "inspect", self.s.sandbox_image, timeout=60)).code != 0:
            (await docker("pull", self.s.sandbox_image, timeout=1800)).check(f"pull {self.s.sandbox_image}")
