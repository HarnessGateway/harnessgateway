"""Layer 1: a process as an Execution: output as it comes, stop and kill, timeout, a failed start."""
import asyncio

from harnessgateway.kernel.execution import NeverStarted, Spec
from harnessgateway.backends.sandboxes.process import ProcessExecution


async def run(argv, **kw):
    spec = Spec(tuple(argv), **kw)
    ex = await ProcessExecution.spawn(list(spec.command), env=dict(spec.env), cwd=None, input=spec.input,
                                      interactive=spec.interactive, timeout_s=spec.timeout_s)
    events = [e async for e in ex.events()]
    return ex, events, await ex.wait()


def test_echo_and_exit_code():
    ex, events, outcome = asyncio.run(run(["bash", "-c", "cat; echo err >&2; exit 3"], input=b"hi"))
    assert outcome.kind == "exited" and outcome.code == 3 and not outcome.ok
    assert b"".join(e.data for e in events if e.stream == "stdout") == b"hi"
    assert b"".join(e.data for e in events if e.stream == "stderr") == b"err\n"


def test_timeout_and_kill():
    _, _, outcome = asyncio.run(run(["sleep", "30"], timeout_s=0.3))
    assert outcome.kind == "timed_out"

    async def go():
        ex = await ProcessExecution.spawn(["sleep", "30"], env={}, cwd=None, input=b"", interactive=False, timeout_s=None)
        ex.kill()
        return await ex.wait()
    assert asyncio.run(go()).kind == "killed"


def test_stop_then_kill():
    async def go():
        ex = await ProcessExecution.spawn(["bash", "-c", "trap 'echo stopping; exit 7' INT; sleep 30 & wait"],
                                          env={}, cwd=None, input=b"", interactive=False, timeout_s=None)
        await asyncio.sleep(0.3)
        ex.stop(5)
        events = [e async for e in ex.events()]
        return events, await ex.wait()
    events, outcome = asyncio.run(go())
    assert outcome.kind == "stopped" and outcome.code == 7 and b"stopping" in b"".join(e.data for e in events)


def test_never_started():
    async def go():
        return await ProcessExecution.spawn(["/no/such/binary"], env={}, cwd=None, input=b"", interactive=False, timeout_s=None)
    ex = asyncio.run(go())
    assert isinstance(ex, NeverStarted) and asyncio.run(ex.wait()).kind == "failed"
