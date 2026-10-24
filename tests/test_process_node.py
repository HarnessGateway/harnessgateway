"""The development node: no Docker, no door. A turn's provider address is the gateway itself and
the session token is a file of the turn, so a package's CLI reaches the provider as it would in a
sandbox; the worker appends the provider's path to that base."""
import asyncio
import os

from harnessgateway.kernel.execution import Spec
from harnessgateway.kernel.limits import Limits
from harnessgateway.backends.sandboxes.process import ProcessNode
from harnessgateway.services.worker import turn_env


def test_process_node_names_the_gateway_and_writes_the_token(tmp_path):
    node = ProcessNode(tmp_path / "worker", gateway_url="http://127.0.0.1:8081")
    run = tmp_path / "s" / "run"
    rt = node.runtime_for("acme", Limits(), writable=(tmp_path / "s" / "home", run, tmp_path / "s" / "state"), token="tok_1")
    assert rt.provider_base == "http://127.0.0.1:8081"
    assert rt.token_path == run / ".token" and rt.token_path.read_text() == "tok_1"
    assert oct(os.stat(rt.token_path).st_mode & 0o777) == "0o600"
    job = {"session": "sess_1", "id": "resp_1", "turn": 1, "model": "", "token": "tok_1", "provider": {"api": "anthropic-messages", "path": "/p", "url": "http://hg-provider:8080/p"}}
    base = rt.provider_base
    env = turn_env(job, __import__("harnessgateway.contract.package", fromlist=["Config"]).Config(key_env="ANTHROPIC_API_KEY", url_env="ANTHROPIC_BASE_URL"),
                   workspace=tmp_path, home=tmp_path, rundir=run, state=run, folder=tmp_path, tools=tmp_path,
                   provider_url=base + job["provider"]["path"], token_path=str(rt.token_path))
    assert env["HG_PROVIDER_URL"] == "http://127.0.0.1:8081/p" == env["ANTHROPIC_BASE_URL"]
    assert env["ANTHROPIC_API_KEY"] == "tok_1" and env["HG_PROVIDER_KEY_FILE"] == str(run / ".token")

    async def go():
        ex = await rt.start(Spec(("bash", "-c", "cat \"$HG_PROVIDER_KEY_FILE\""), input=b"", env={**env, "PATH": os.environ["PATH"]}, workspace=tmp_path, timeout_s=10))
        out = b""
        async for e in ex.events():
            if e.stream == "stdout":
                out += e.data
        return out, await ex.wait()
    out, outcome = asyncio.run(go())
    assert out == b"tok_1" and outcome.kind == "exited" and outcome.code == 0
