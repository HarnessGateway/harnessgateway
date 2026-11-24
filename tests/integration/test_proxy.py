"""The egress proxy against a real platform: what it is told, what it lets through, and what it puts in."""

import asyncio
import base64

import httpx

from harnessgateway.api.app import internal_app
from harnessgateway.domain.egress import BoundSecret, EgressPolicy, Policy
from harnessgateway.platform import Platform
from harnessgateway.proxy.authority import Authority, new_authority
from harnessgateway.proxy.control import PlatformLink
from harnessgateway.proxy.server import EgressProxy
from tests.helpers import package
from tests.integration.conftest import ORG, run


async def test_the_proxy_is_told_what_a_running_sandbox_may_reach_and_no_secret_enters_the_sandbox(platform: Platform):
    await platform.resources.add_package(ORG, "agent", package())
    await platform.resources.apply(ORG, "providers", "claude", {"apis": {"anthropic": "https://api.example.com"}, "key": "sk-real"})
    await platform.resources.apply(ORG, "environments", "default", {
        "image": "node:22", "harnesses": ["agent"], "network": {"allow": ["db.example.com"]},
        "secrets": {"DB": {"hosts": ["db.example.com"], "header": "X-Key", "value": "db-secret"},
                    "TOOL": {"hosts": ["tool.example.com"], "value": "tool-secret"}}})
    transport, seen = httpx.ASGITransport(app=internal_app(platform)), {}
    link = PlatformLink("http://internal", "proxy-key", transport=transport)
    async with httpx.AsyncClient(transport=transport, base_url="http://internal") as stranger:
        assert (await stranger.get("/internal/authority")).status_code == 401
    assert "BEGIN CERTIFICATE" in (await link.authority())[0]
    original = platform.driver.run

    async def sandbox(plan, on_stdout, on_stderr):
        subject, token = plan.env["HTTPS_PROXY"].split("//")[1].split("@")[0].split(":")
        seen["install" if plan.command.endswith("/install") else "turn"] = (plan, await link.policy(subject, token))
        assert await link.policy(subject, "not-its-token") is None
        link.count(ORG, 10, 20)
        await link.tell()
        return await original(plan, on_stdout, on_stderr)
    platform.driver.run = sandbox
    turn = await run(platform, model="default/agent/claude/claude-x")
    plan, policy = seen["turn"]
    assert seen["install"][1].secrets == () and "registry.npmjs.org" in seen["install"][1].egress.allow
    assert policy.egress.mode == "limited" and {"api.example.com", "db.example.com"} <= set(policy.egress.allow)
    assert "tool.example.com" in policy.egress.allow                                   # where a secret goes is reachable
    secrets = {secret.value: secret for secret in policy.secrets}
    assert secrets["sk-real"].placeholder == plan.env["HG_PROVIDER_KEY"] and secrets["sk-real"].hosts == ("api.example.com",)
    assert secrets["db-secret"].header == "X-Key" and secrets["tool-secret"].placeholder == plan.env["TOOL"]
    assert not {"sk-real", "db-secret", "tool-secret"} & set(plan.env.values())        # no secret enters a sandbox
    assert (plan.env["HG_PROVIDER_ANTHROPIC"], plan.env["HG_MODEL"]) == ("https://api.example.com", "claude-x")
    fresh = PlatformLink("http://internal", "proxy-key", transport=transport)          # one that has kept nothing
    assert await fresh.policy(turn.id, plan.env["HTTPS_PROXY"].split("@")[0].rsplit(":", 1)[1]) is None
    used = await platform.usage.total(ORG)
    assert (used["network_out_bytes"], used["network_in_bytes"]) == (20, 40)
    await link.close()
    await fresh.close()


async def test_the_proxy_refuses_what_a_policy_does_not_allow_and_puts_secrets_into_what_it_does():
    asked: list[bytes] = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        asked.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
    host = await asyncio.start_server(upstream, "127.0.0.1", 0)
    port = host.sockets[0].getsockname()[1]
    policy = Policy(ORG, "resp_1", "default", EgressPolicy("limited", (f"127.0.0.1:{port}", "127.0.0.9")),
                    (BoundSecret((f"127.0.0.1:{port}",), "real-value", "hgs_placeholder", "X-Key", "Key "),))
    counted: list[tuple[str, int, int]] = []

    async def policy_of(subject: str, token: str) -> Policy | None:
        return policy if (subject, token) == ("resp_1", "token") else None
    proxy = EgressProxy(policy_of, lambda *count: counted.append(count), Authority(*new_authority()),
                        private_hosts=[f"127.0.0.1:{port}"])
    server = await proxy.start("127.0.0.1", 0)
    proxy_port = server.sockets[0].getsockname()[1]

    async def through(request: bytes) -> bytes:
        reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
        writer.write(request)
        await writer.drain()
        answer = await reader.read()
        writer.close()
        return answer
    credentials = base64.b64encode(b"resp_1:token").decode()
    known = f"Proxy-Authorization: Basic {credentials}\r\n".encode()
    assert (await through(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n")).startswith(b"HTTP/1.1 407")
    refused = await through(b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n" + known + b"\r\n")
    assert refused.startswith(b"HTTP/1.1 403") and b"not in the environment's network.allow (environment 'default')" in refused
    private = await through(b"GET http://127.0.0.9/ HTTP/1.1\r\n" + known + b"\r\n")       # allowed, yet never reachable
    assert private.startswith(b"HTTP/1.1 403") and b"private or local network" in private
    answer = await through(f"GET http://127.0.0.1:{port}/data?token=hgs_placeholder HTTP/1.1\r\nHost: x\r\n"
                           f"Authorization: Bearer hgs_placeholder\r\n".encode() + known + b"\r\n")
    assert answer.endswith(b"ok") and len(asked) == 1
    sent = asked[0].decode()
    assert sent.startswith("GET /data?token=real-value HTTP/1.1") and "Authorization: Bearer real-value" in sent
    assert "X-Key: Key real-value" in sent and "Proxy-Authorization" not in sent and "hgs_placeholder" not in sent
    assert counted[-1][0] == ORG and counted[-1][1] > 0 and counted[-1][2] > 0
    server.close()
    host.close()
