"""Spec §5.5, §7.2: the Docker driver against a real daemon. A sandbox runs, is limited, and its end is told apart."""

import asyncio
from dataclasses import replace

import pytest

from harnessgateway.domain.plan import Mount, SandboxPlan, StepPlan
from harnessgateway.drivers.base import SandboxExit
from harnessgateway.drivers.docker import DockerDriver
from tests.e2e.conftest import IMAGE


def sandbox(name: str, script: str, **changes: object) -> SandboxPlan:
    plan = SandboxPlan(
        name=f"hg-test-{name}", labels={"hg.org": "org_test"}, image=IMAGE, command=["sh", "-c", script],
        workdir="/tmp", env={"HOME": "/tmp", "GREETING": "hello there"}, mounts=[], cpus=1, memory_mb=256,
        pids=128, tmp_mb=64, network=None, stop_grace_s=2,
    )
    return replace(plan, **changes)      # type: ignore[arg-type]


async def run(driver: DockerDriver, plan: SandboxPlan) -> tuple[SandboxExit, list[str], list[str]]:
    out: list[str] = []
    err: list[str] = []

    async def on_out(line: str) -> None:
        out.append(line)

    async def on_err(line: str) -> None:
        err.append(line)

    return await driver.run_sandbox(plan, on_out, on_err), out, err


async def test_a_sandbox_runs_and_its_lines_arrive_in_order(driver: DockerDriver, unique: str) -> None:
    result, out, err = await run(driver, sandbox(unique, 'echo one; echo "$GREETING"; echo problem >&2; echo three'))
    assert result == SandboxExit(started=True, exit_code=0)
    assert out == ["one", "hello there", "three"]
    assert err == ["problem"]


async def test_the_exit_code_is_reported(driver: DockerDriver, unique: str) -> None:
    result, _, _ = await run(driver, sandbox(unique, "exit 3"))
    assert (result.started, result.exit_code, result.out_of_memory) == (True, 3, False)


async def test_a_sandbox_is_the_user_10000_with_a_read_only_root_and_no_capabilities(
    driver: DockerDriver, unique: str
) -> None:
    script = ("id -u; id -g; touch /etc/x 2>/dev/null && echo wrote-root || echo root-is-read-only; "
              "touch /tmp/x && echo tmp-is-writable; grep CapEff /proc/self/status")
    _, out, _ = await run(driver, sandbox(unique, script))
    assert out[:4] == ["10000", "10000", "root-is-read-only", "tmp-is-writable"]
    assert out[4].split()[-1].strip("0") == ""                 # no effective capabilities


async def test_a_sandbox_with_no_network_reaches_nothing(driver: DockerDriver, unique: str) -> None:
    script = "node -e \"fetch('https://example.com').then(()=>console.log('reached'),()=>console.log('unreachable'))\""
    _, out, _ = await run(driver, sandbox(unique, script))
    assert out == ["unreachable"]


async def test_the_memory_limit_kills_and_is_told_apart(driver: DockerDriver, unique: str) -> None:
    hog = "node -e \"const a=[]; for(;;) a.push(Buffer.alloc(8<<20, 1))\""
    result, _, _ = await run(driver, sandbox(unique, hog, memory_mb=64, tmp_mb=16))
    assert result.started
    assert result.out_of_memory
    assert result.exit_code != 0


async def test_a_running_sandbox_can_be_stopped(driver: DockerDriver, unique: str) -> None:
    plan = sandbox(unique, "echo started; sleep 300")
    started = asyncio.Event()

    async def on_out(line: str) -> None:
        started.set()

    async def ignore(line: str) -> None:
        pass

    task = asyncio.create_task(driver.run_sandbox(plan, on_out, ignore))
    await asyncio.wait_for(started.wait(), 60)
    await driver.stop(plan.name, grace_s=1)
    result = await asyncio.wait_for(task, 30)
    assert result.started
    assert result.exit_code != 0
    assert not result.out_of_memory


async def test_stopping_what_is_not_running_is_not_an_error(driver: DockerDriver, unique: str) -> None:
    await driver.stop(f"hg-test-{unique}", grace_s=1)


async def test_a_second_sandbox_with_the_same_name_cannot_start(driver: DockerDriver, unique: str) -> None:
    plan = sandbox(unique, "echo started; sleep 300")
    started = asyncio.Event()

    async def on_out(line: str) -> None:
        started.set()

    async def ignore(line: str) -> None:
        pass

    first = asyncio.create_task(driver.run_sandbox(plan, on_out, ignore))
    await asyncio.wait_for(started.wait(), 60)
    try:
        process = await asyncio.create_subprocess_exec(
            "docker", "run", "--name", plan.name, IMAGE, "true",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, error = await process.communicate()
        assert process.returncode != 0
        assert b"already in use" in error
    finally:
        await driver.stop(plan.name, grace_s=1)
        await first


async def test_a_program_that_does_not_exist_is_a_sandbox_that_exits_with_an_error(
    driver: DockerDriver, unique: str
) -> None:
    result, _, err = await run(driver, sandbox(unique, "", command=["/no/such/program"]))
    assert result.started                                       # the init process ran; the program did not
    assert result.exit_code != 0
    assert any("/no/such/program" in line for line in err)


async def test_an_image_that_does_not_exist_is_a_sandbox_that_did_not_start(driver: DockerDriver, unique: str) -> None:
    result, _, _ = await run(driver, sandbox(unique, "true", image="localhost:1/no-such-image:0"))
    assert result.started is False
    assert result.error


async def test_a_step_writes_into_a_volume_and_a_sandbox_reads_it(driver: DockerDriver, unique: str) -> None:
    volume = f"hg-test-vol-{unique}"
    await driver.ensure_volume(volume)
    await driver.ensure_volume(volume)                          # making it twice is fine
    try:
        step = StepPlan(
            name=f"hg-test-step-{unique}", labels={"hg.org": "org_test"},
            command=["sh", "-c",
                     "id -u; echo from-the-step > /mind/note; chown -R 10000:10000 /mind; echo \"$HG_API\""],
            env={"HG_API": "http://serve:8001", "HG_STEP_TOKEN": "t"},
            mounts=[Mount(volume, "/mind", read_only=False)], network=None,
        )
        result = await driver.run_step(step)
        assert (result.exit_code, result.output.split()) == (0, ["0", "http://serve:8001"])

        reader = sandbox(unique, "cat /mind/note; echo more >> /mind/note && echo appended; touch /ro/x || echo ro",
                         mounts=[Mount(volume, "/mind", read_only=False), Mount(volume, "/ro", read_only=True)])
        _, out, _ = await run(driver, reader)
        assert out == ["from-the-step", "appended", "ro"]
    finally:
        await driver.remove_volume(volume)
        await driver.remove_volume(volume)                      # removing what is gone is fine


async def test_a_failing_step_reports_its_code_and_what_it_printed(driver: DockerDriver, unique: str) -> None:
    step = StepPlan(name=f"hg-test-step-{unique}", labels={"hg.org": "org_test"},
                    command=["sh", "-c", "echo it broke; exit 4"], env={}, mounts=[], network=None)
    result = await driver.run_step(step)
    assert (result.exit_code, result.output) == (4, "it broke")


async def test_reaping_removes_what_nobody_owns_and_keeps_the_rest(driver: DockerDriver, unique: str) -> None:
    keep, orphan = sandbox(f"keep-{unique}", "sleep 300"), sandbox(f"orphan-{unique}", "sleep 300")

    async def ignore(line: str) -> None:
        pass

    tasks = [asyncio.create_task(driver.run_sandbox(plan, ignore, ignore)) for plan in (keep, orphan)]
    for plan in (keep, orphan):
        for _ in range(100):
            process = await asyncio.create_subprocess_exec(
                "docker", "inspect", "--format", "{{.State.Running}}", plan.name,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            output, _ = await process.communicate()
            if output.strip() == b"true":
                break
            await asyncio.sleep(0.2)
    gone = await driver.reap(keep={keep.name})
    assert orphan.name in gone
    assert keep.name not in gone
    await driver.stop(keep.name, grace_s=1)
    await asyncio.gather(*tasks)


async def test_the_image_id_is_immutable_and_an_unknown_image_is_an_error(driver: DockerDriver) -> None:
    from harnessgateway.drivers.base import DriverError

    image_id = await driver.image_id(IMAGE)
    assert image_id.startswith("sha256:")
    assert await driver.image_id(image_id) == image_id
    with pytest.raises(DriverError):
        await driver.image_id("localhost:1/no-such-image:0")


async def test_isolation_is_reported(driver: DockerDriver) -> None:
    assert await driver.isolation() in ("gvisor", "none")
