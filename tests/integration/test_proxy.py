"""Spec §10.4, §11: the egress proxy, against local servers standing in for the outside world."""

import asyncio
import base64
import contextlib
import json
import socket
import ssl
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from harnessgateway.api.internal import internal_app
from harnessgateway.domain.hosts import HostEntry
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.sealing import new_placeholder, new_token
from harnessgateway.proxy import platform as platform_module
from harnessgateway.proxy.authority import Authority, new_authority
from harnessgateway.proxy.platform import PlatformClient
from harnessgateway.proxy.server import EgressProxy
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores
from tests.helpers import serving
from tests.integration.test_harness_store import demo_archive


def outside() -> FastAPI:
    """A server that says back what it was sent."""
    app = FastAPI()

    @app.api_route("/echo", methods=["GET", "POST"])
    async def echo(request: Request) -> dict[str, Any]:
        return {"headers": dict(request.headers), "query": dict(request.query_params),
                "body": (await request.body()).decode()}

    @app.get("/stream")
    async def stream() -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            for number in range(3):
                yield f"data: {number}\n\n"
                await asyncio.sleep(0.4)
        return StreamingResponse(events(), media_type="text/event-stream")

    return app


@contextlib.asynccontextmanager
async def tls_in_front(port: int, context: ssl.SSLContext) -> AsyncIterator[int]:
    """TLS in front of a plain server on this machine. Yields the port it listens on."""
    async def pass_on(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        far_reader, far_writer = await asyncio.open_connection("127.0.0.1", port)

        async def pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            with contextlib.suppress(Exception):
                while data := await source.read(65536):
                    sink.write(data)
                    await sink.drain()
            with contextlib.suppress(Exception):
                sink.close()
        await asyncio.gather(pipe(reader, far_writer), pipe(far_reader, writer))

    server = await asyncio.start_server(pass_on, "127.0.0.1", 0, ssl=context)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()


@dataclass
class World:
    stores: Stores
    org: str
    turn: Turn
    token: str
    proxy: int                                   # the proxy's port
    http: int                                    # the outside server, plain
    https: int                                   # the same, behind TLS; secrets are bound to it
    unbound: int                                 # the same, behind TLS; no secret is bound to it
    trusted: ssl.SSLContext                      # what a sandbox trusts, plus the outside server's own authority
    platform: PlatformClient
    placeholders: dict[str, str]                 # which placeholder stands for which secret

    def client(self, user: str | None = None, token: str | None = None, **more: Any) -> httpx.AsyncClient:
        credentials = f"{user or self.turn.id}:{token or self.token}@"
        return httpx.AsyncClient(proxy=f"http://{credentials}127.0.0.1:{self.proxy}", verify=self.trusted,
                                 timeout=20, **more)


@pytest.fixture
async def world(stores: Stores, org: str, tmp_path: Path) -> AsyncIterator[World]:
    async with stores.db.system() as conn:
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    their_cert, their_key = new_authority()                    # the authority the outside server's certificate is from
    theirs = Authority(their_cert, their_key, tmp_path / "theirs").context_for("localhost")
    async with serving(outside()) as url, tls_in_front(int(url.rsplit(":", 1)[1]), theirs) as https, tls_in_front(
            int(url.rsplit(":", 1)[1]), theirs) as unbound:
        http = int(url.rsplit(":", 1)[1])
        await stores.providers.create(org, {"name": "local", "apis": {"openai-chat": f"http://localhost:{http}/v1"},
                                            "api_key": "the-provider-key", "default_model": "m"})
        await stores.environments.update(org, "default", {
            "network": {"mode": "limited", "allow": [f"127.0.0.1:{http}", f"localhost:{unbound}"],
                        "deny": ["denied.example.com"]},
            "secrets": {"EXAMPLE_TOKEN": {"hosts": [f"localhost:{https}"], "header": "X-Api-Key", "prefix": "Key ",
                                          "value": "the-environment-secret"}}})
        await stores.harnesses.add(org, demo_archive(tmp_path, apis=["openai-chat"], settings={}, secrets={
            "GITHUB_TOKEN": {"hosts": [f"localhost:{https}"]}}))
        await stores.harnesses.update(org, "demo", {"secrets": {"GITHUB_TOKEN": "the-harness-secret"}})

        resolved = await stores.resolve(org, ModelPath.parse("demo"))
        await stores.turns.create(org, resolved=resolved, parent=None, key_id=None,
                                  request={"model": "demo", "input": "x"})
        turn = await stores.turns.claim("runner-test")
        assert turn is not None
        token = new_token()
        names = {"provider": new_placeholder(), "EXAMPLE_TOKEN": new_placeholder(), "GITHUB_TOKEN": new_placeholder()}
        await stores.turns.prepare(turn, image_id="sha256:x", handover="", step_token_hash=stores.hash(new_token()),
                                   proxy_token_hash=stores.hash(token), placeholders={
                                       names["provider"]: {"kind": "provider", "name": "local"},
                                       names["EXAMPLE_TOKEN"]: {"kind": "environment", "name": "EXAMPLE_TOKEN"},
                                       names["GITHUB_TOKEN"]: {"kind": "harness", "name": "GITHUB_TOKEN"}})

        platform = PlatformClient("http://internal.test", stores.settings.proxy_key,
                                  transport=httpx.ASGITransport(app=internal_app(stores)))
        our_cert, our_key = await platform.authority()
        trusted = ssl.create_default_context(cadata=our_cert + their_cert)
        proxy = EgressProxy(
            platform, Authority(our_cert, our_key, tmp_path / "ours"),
            private_hosts=[HostEntry("localhost", http), HostEntry("localhost", https),
                           HostEntry("localhost", unbound), HostEntry("127.0.0.1", http)],
            upstream_tls=ssl.create_default_context(cadata=their_cert))
        server = await proxy.start("127.0.0.1", 0)
        try:
            yield World(stores, org, turn, token, server.sockets[0].getsockname()[1], http, https, unbound, trusted,
                        platform, names)
        finally:
            server.close()
            await platform.close()


# ── who may use it, and for what ────────────────────────────────────────────────────────────────────

async def test_without_a_running_turn_s_credentials_nothing_passes(world: World) -> None:
    async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{world.proxy}") as bare:
        refused = await bare.get(f"http://127.0.0.1:{world.http}/echo")
    assert refused.status_code == 407
    assert "running turns only" in refused.text
    async with world.client(token="not-the-token") as wrong:
        assert (await wrong.get(f"http://127.0.0.1:{world.http}/echo")).status_code == 407
        with pytest.raises(httpx.ProxyError, match="407"):
            await wrong.get(f"https://localhost:{world.https}/echo")
    async with world.client(user="resp_00000000000000000000000000") as stranger:
        assert (await stranger.get(f"http://127.0.0.1:{world.http}/echo")).status_code == 407


async def test_an_allowed_host_is_reached_and_the_proxy_s_own_headers_stay_behind(world: World) -> None:
    async with world.client() as client:
        answer = await client.post(f"http://127.0.0.1:{world.http}/echo?a=1&b=two", content=b"the body",
                                   headers={"X-Mine": "kept"})
    assert answer.status_code == 200
    said = answer.json()
    assert (said["query"], said["body"], said["headers"]["x-mine"]) == ({"a": "1", "b": "two"}, "the body", "kept")
    assert "proxy-authorization" not in said["headers"]
    assert said["headers"]["host"] == f"127.0.0.1:{world.http}"


async def test_a_host_the_environment_does_not_allow_is_refused_naming_it_and_the_environment(world: World) -> None:
    async with world.client() as client:
        refused = await client.get("http://not-allowed.example.com/x")
        denied = await client.get("http://denied.example.com/x")
        with pytest.raises(httpx.ProxyError, match="403"):
            await client.get("https://not-allowed.example.com/x")
    assert refused.status_code == 403
    assert refused.text.strip() == ("not-allowed.example.com:80 is not allowed: add not-allowed.example.com to the "
                                    "environment's network.allow (environment 'default')")
    assert "deny list" in denied.text


async def test_a_name_that_resolves_to_a_private_address_is_refused_unless_the_operator_listed_it(
    world: World, tmp_path: Path
) -> None:
    await world.stores.environments.update(world.org, "default", {"network": {"mode": "open"}})
    world.platform._policies.clear()
    async with world.client() as client:
        for url in ("http://127.0.0.1:9/x", "http://localhost:9/x", "http://169.254.169.254/latest/meta-data",
                    "http://10.0.0.5/x"):
            refused = await client.get(url)
            assert refused.status_code == 403, url
            assert "private or local network" in refused.text
        listed = await client.get(f"http://localhost:{world.http}/echo")          # the operator listed this one
    assert listed.status_code == 200


async def test_a_host_is_reached_at_the_first_of_its_addresses_that_answers(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name often has several addresses, and the first is not always one this host can reach: an IPv6
    address where there is no IPv6, say. Nothing listens at the first one here."""
    loop = asyncio.get_running_loop()
    real = loop.getaddrinfo

    async def two_addresses(host: Any, port: Any, **more: Any) -> Any:
        if host != "localhost":
            return await real(host, port, **more)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port)) for address in ("127.0.0.9", "127.0.0.1")]

    monkeypatch.setattr(loop, "getaddrinfo", two_addresses)
    async with world.client() as client:
        answer = await client.get(f"http://localhost:{world.http}/echo")
        secure = await client.get(f"https://localhost:{world.https}/echo")
    assert (answer.status_code, secure.status_code) == (200, 200)


async def test_a_request_the_proxy_cannot_make_sense_of_is_answered_400(world: World) -> None:
    reader, writer = await asyncio.open_connection("127.0.0.1", world.proxy)
    credentials = base64.b64encode(f"{world.turn.id}:{world.token}".encode()).decode()
    writer.write(f"GET http://::1:9/x HTTP/1.1\r\nProxy-Authorization: Basic {credentials}\r\n\r\n".encode())
    answer = await reader.read()
    writer.close()
    assert answer.startswith(b"HTTP/1.1 400 ")


# ── secrets ─────────────────────────────────────────────────────────────────────────────────────────

async def test_bytes_to_a_host_with_no_secret_pass_unread(world: World) -> None:
    placeholder = world.placeholders["GITHUB_TOKEN"]
    async with world.client() as client:
        answer = await client.get(f"https://localhost:{world.unbound}/echo",
                                  headers={"Authorization": f"Bearer {placeholder}"})
    assert answer.status_code == 200
    assert answer.json()["headers"]["authorization"] == f"Bearer {placeholder}"   # a useless string, left as it is


async def test_a_placeholder_becomes_the_secret_on_the_way_to_its_host_only(world: World) -> None:
    placeholder = world.placeholders["GITHUB_TOKEN"]
    async with world.client() as client:
        answer = await client.get(f"https://localhost:{world.https}/echo?token={placeholder}&other=1",
                                  headers={"Authorization": f"Bearer {placeholder}"})
    said = answer.json()
    assert said["headers"]["authorization"] == "Bearer the-harness-secret"
    assert said["query"] == {"token": "the-harness-secret", "other": "1"}


async def test_a_secret_with_a_header_is_set_on_every_request_to_its_host(world: World) -> None:
    async with world.client() as client:
        unasked = (await client.get(f"https://localhost:{world.https}/echo")).json()
        overridden = (await client.get(f"https://localhost:{world.https}/echo",
                                       headers={"X-Api-Key": "what the agent tried to send"})).json()
    assert unasked["headers"]["x-api-key"] == "Key the-environment-secret"
    assert overridden["headers"]["x-api-key"] == "Key the-environment-secret"


async def test_the_provider_s_key_reaches_the_provider_and_the_body_is_untouched(world: World) -> None:
    placeholder = world.placeholders["provider"]
    body = f'{{"model": "m", "note": "{placeholder} in a body is not replaced"}}'
    async with world.client() as client:
        answer = await client.post(f"http://localhost:{world.http}/echo", content=body,
                                   headers={"Authorization": f"Bearer {placeholder}"})
    said = answer.json()
    assert said["headers"]["authorization"] == "Bearer the-provider-key"
    assert said["body"] == body


async def test_plain_http_inside_a_tunnel_is_read_and_given_its_secret_too(world: World) -> None:
    """Some clients open a tunnel for every destination and then speak plain HTTP through it."""
    placeholder = world.placeholders["provider"]
    credentials = base64.b64encode(f"{world.turn.id}:{world.token}".encode()).decode()
    reader, writer = await asyncio.open_connection("127.0.0.1", world.proxy)
    writer.write(f"CONNECT localhost:{world.http} HTTP/1.1\r\nHost: localhost:{world.http}\r\n"
                 f"Proxy-Authorization: Basic {credentials}\r\n\r\n".encode())
    assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
    body = b'{"model": "m"}'
    writer.write(f"POST /echo?k={placeholder} HTTP/1.1\r\nHost: localhost:{world.http}\r\n"
                 f"Authorization: Bearer {placeholder}\r\nContent-Type: application/json\r\n"
                 f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    head, _, said = answer.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200")
    echoed = json.loads(said[said.index(b"{"):said.rindex(b"}") + 1])
    assert echoed["headers"]["authorization"] == "Bearer the-provider-key"
    assert echoed["query"] == {"k": "the-provider-key"}
    assert echoed["body"] == body.decode()


async def test_a_sandbox_that_trusts_only_the_platform_s_bundle_can_talk_to_an_intercepted_host(world: World) -> None:
    our_cert, _ = await world.platform.authority()
    only_ours = ssl.create_default_context(cadata=our_cert)
    async with httpx.AsyncClient(proxy=f"http://{world.turn.id}:{world.token}@127.0.0.1:{world.proxy}",
                                 verify=only_ours) as client:
        assert (await client.get(f"https://localhost:{world.https}/echo")).status_code == 200


async def test_a_streamed_answer_arrives_as_it_is_sent(world: World) -> None:
    arrived = []
    async with world.client() as client, client.stream("GET", f"https://localhost:{world.https}/stream") as answer:
        async for line in answer.aiter_lines():
            if line:
                arrived.append((line, time.monotonic()))
    assert [line for line, _ in arrived] == ["data: 0", "data: 1", "data: 2"]
    assert arrived[-1][1] - arrived[0][1] > 0.6


# ── the record, and the end ─────────────────────────────────────────────────────────────────────────

async def test_every_connection_is_recorded_allowed_or_not(world: World) -> None:
    async with world.client() as client:
        await client.post(f"http://127.0.0.1:{world.http}/echo", content=b"x" * 5000)
        await client.get("http://not-allowed.example.com/x")
        await client.get(f"https://localhost:{world.https}/echo")
    async with asyncio.timeout(10):                          # a record is made when its connection has ended
        while len(world.platform._records) < 3:
            await asyncio.sleep(0.05)
    await world.platform.flush()
    records = await world.stores.egress.of(world.org, world.turn.id)
    assert [(r.host, r.port, r.allowed) for r in records] == [
        ("127.0.0.1", world.http, True), ("not-allowed.example.com", 80, False), ("localhost", world.https, True)]
    assert records[0].bytes_up > 5000
    assert records[0].bytes_down > 100
    assert (records[1].bytes_up, records[1].bytes_down) == (0, 0)
    assert "not allowed" in records[1].reason
    assert records[2].bytes_down > 100
    assert records[0].started_at <= records[0].ended_at


async def test_when_the_turn_is_over_its_credentials_are_worth_nothing(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_module, "KEEP_S", 0.0)
    world.platform._policies.clear()
    async with world.client() as client:
        assert (await client.get(f"http://127.0.0.1:{world.http}/echo")).status_code == 200
        await world.stores.turns.conclude(world.turn, Outcome("completed"))
        assert (await client.get(f"http://127.0.0.1:{world.http}/echo")).status_code == 407


async def test_an_install_reaches_package_managers_and_what_its_manifest_names_and_nothing_else(
    world: World, tmp_path: Path
) -> None:
    harness = await world.stores.harnesses.add(world.org, demo_archive(
        tmp_path, name="installing", apis=[], settings={}, secrets={},
        install={"egress": [f"127.0.0.1:{world.http}"]}))
    install = await world.stores.harnesses.install_for(world.org, harness.digest, "sha256:image")
    token = new_token()
    await world.stores.harnesses.begin_install(world.org, install.id, environment="default",
                                               step_token_hash=world.stores.hash(new_token()),
                                               proxy_token_hash=world.stores.hash(token))
    policy = await world.platform.policy(install.id, token)
    assert policy is not None
    assert policy.secrets == ()
    assert policy.egress.decide("registry.npmjs.org", 443).allowed
    assert policy.egress.decide("pypi.org", 443).allowed
    assert not policy.egress.decide("api.github.com", 443).allowed
    assert not policy.egress.decide("denied.example.com", 443).allowed
    async with world.client(user=install.id, token=token) as client:
        assert (await client.get(f"http://127.0.0.1:{world.http}/echo")).status_code == 200
        assert (await client.get(f"http://localhost:{world.unbound}/echo")).status_code == 403

    await world.stores.harnesses.end_install(world.org, install.id, ready=True, log="")
    world.platform._policies.clear()
    assert await world.platform.policy(install.id, token) is None
