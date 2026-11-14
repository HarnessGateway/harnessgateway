"""Spec §10.4, §11, end to end: a real sandbox whose only way out is the egress proxy."""

import asyncio
import json
from pathlib import Path

import pytest

from tests.helpers import Platform, text_of, variant

pytestmark = pytest.mark.usefixtures("database_url")


async def test_an_allowed_host_is_reached_and_a_denied_one_is_refused_by_name(platform: Platform, egress: str) -> None:
    await platform.stores.environments.update(platform.org, "default", {
        "network": {"mode": "limited", "allow": [egress], "package_managers": False}})
    turn = await platform.run(
        f"curl -sS http://{egress}/hello | head -c 60; echo; curl -sS http://example.com/ ; "
        f"curl -sS -o /dev/null -w '%{{http_code}}\\n' https://example.com/", timeout=180)
    lines = text_of(turn).splitlines()
    assert lines[0].startswith('{"path": "/hello"'), turn.log_tail
    assert lines[1] == ("example.com:80 is not allowed: add example.com to the environment's network.allow "
                        "(environment 'default')")
    assert "403" in lines[2]                                # curl: the proxy refused the tunnel

    async with asyncio.timeout(30):                          # the proxy sends its records every two seconds
        while len(records := await platform.stores.egress.of(platform.org, turn.id)) < 3:
            await asyncio.sleep(0.5)
    assert [(r.host, r.port, r.allowed) for r in records] == [
        (egress.split(":")[0], 8000, True), ("example.com", 80, False), ("example.com", 443, False)]
    assert records[0].bytes_down > 0


async def test_a_secret_reaches_its_host_and_the_sandbox_only_ever_holds_a_placeholder(
    platform: Platform, egress: str
) -> None:
    await platform.stores.environments.update(platform.org, "default", {
        "network": {"mode": "limited", "package_managers": False},
        "secrets": {"EXAMPLE_TOKEN": {"hosts": [egress], "value": "the-real-secret"},
                    "SECOND_TOKEN": {"hosts": [egress], "header": "X-Api-Key", "prefix": "Key ",
                                     "value": "the-second-secret"}}})
    turn = await platform.run(
        f'echo "$EXAMPLE_TOKEN"; env | grep -c the-real-secret; '
        f'curl -sS -H "Authorization: Bearer $EXAMPLE_TOKEN" http://{egress}/echo', timeout=180)
    lines = text_of(turn).splitlines()
    assert lines[0].startswith("hgs_"), turn.log_tail
    assert len(lines[0]) == 36
    assert lines[1] == "0"                                    # the secret is nowhere in the sandbox
    seen = json.loads(lines[2])["headers"]
    assert seen["Authorization"] == "Bearer the-real-secret"  # and it arrived
    assert seen["X-Api-Key"] == "Key the-second-secret"
    assert "the-real-secret" not in (turn.command or "")
    assert "hgs_***" in (turn.command or "")


async def test_in_a_closed_environment_a_turn_reaches_its_provider_with_its_key_and_nothing_else(
    platform: Platform, egress: str, tmp_path: Path
) -> None:
    await platform.stores.environments.update(platform.org, "default", {"network": {"mode": "closed"}})
    await platform.stores.providers.create(platform.org, {
        "name": "outside", "apis": {"openai-chat": f"http://{egress}/v1"}, "api_key": "the-provider-key"})
    await platform.stores.harnesses.add(platform.org, variant(tmp_path, "caller", apis=["openai-chat"]))
    turn = await platform.run(
        'curl -sS -H "Authorization: Bearer $HG_PROVIDER_KEY" "$HG_PROVIDER_URL/models"; echo; '
        "curl -sS http://example.com/; curl -sS -o /dev/null -w '%{http_code}\\n' https://registry.npmjs.org/",
        model="caller/outside/a-model", timeout=180)
    lines = text_of(turn).splitlines()
    seen = json.loads(lines[0])
    assert (seen["path"], seen["headers"]["Authorization"]) == ("/v1/models", "Bearer the-provider-key"), turn.log_tail
    assert lines[1] == ("example.com:80 is not allowed: a closed environment reaches the turn's provider only "
                        "(environment 'default')")
    assert "403" in lines[2]                                # a package manager is no nearer than anything else


async def test_in_a_closed_environment_a_turn_that_calls_no_model_has_no_network(
    platform: Platform, egress: str
) -> None:
    await platform.stores.environments.update(platform.org, "default", {"network": {"mode": "closed"}})
    turn = await platform.run(f'echo "[$HTTPS_PROXY]"; curl -sS --max-time 5 http://{egress}/x 2>&1 | head -c 200',
                              timeout=180)
    lines = text_of(turn).splitlines()
    assert lines[0] == "[]"
    assert "Could not resolve host" in lines[1] or "Failed to connect" in lines[1] or "resolve" in lines[1]
