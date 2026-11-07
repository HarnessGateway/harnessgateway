"""The Docker launcher's reap: orphaned box networks go, a network whose launch is still in progress stays."""
from __future__ import annotations

import time

import pytest

from harnessgateway.adapters import launch_docker
from harnessgateway.adapters.docker_cli import Result
from harnessgateway.config import Settings


class FakeDocker:
    """Answers `docker` calls from a script keyed by the subcommand and records every call."""

    def __init__(self, networks: dict[str, tuple[float, list[str]]]):
        self.networks = networks  # network id -> (created unix time, member containers)
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, *args: str, timeout: float = 0, env: dict[str, str] | None = None) -> Result:
        self.calls.append(args)
        if args[0] == "ps":
            return Result(0, b"", b"")
        if args[:2] == ("network", "ls"):
            return Result(0, " ".join(self.networks).encode(), b"")
        if args[:2] == ("network", "inspect"):
            created, members = self.networks[args[-1]]
            return Result(0, f"{int(created)} {' '.join(members)} ".encode(), b"")
        return Result(0, b"", b"")


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> launch_docker.DockerLauncher:
    return launch_docker.DockerLauncher(Settings(sandbox_runtime="runc"))


async def test_reap_removes_old_orphaned_network(monkeypatch: pytest.MonkeyPatch, launcher) -> None:
    fake = FakeDocker({"old": (time.time() - 3600, [launcher.s.door_container])})
    monkeypatch.setattr(launch_docker, "docker", fake)
    await launcher.reap()
    assert ("network", "disconnect", "-f", "old", launcher.s.door_container) in fake.calls
    assert ("network", "rm", "old") in fake.calls


async def test_reap_leaves_a_network_whose_launch_is_in_progress(monkeypatch: pytest.MonkeyPatch, launcher) -> None:
    fake = FakeDocker({"young": (time.time(), [launcher.s.door_container])})
    monkeypatch.setattr(launch_docker, "docker", fake)
    await launcher.reap()
    assert not [c for c in fake.calls if c[:2] in (("network", "rm"), ("network", "disconnect"))]


async def test_reap_leaves_a_network_with_a_box_on_it(monkeypatch: pytest.MonkeyPatch, launcher) -> None:
    fake = FakeDocker({"busy": (time.time() - 3600, [launcher.s.door_container, "hg-box-abc"])})
    monkeypatch.setattr(launch_docker, "docker", fake)
    await launcher.reap()
    assert not [c for c in fake.calls if c[:2] in (("network", "rm"), ("network", "disconnect"))]
