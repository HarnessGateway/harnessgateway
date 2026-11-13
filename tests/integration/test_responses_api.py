"""Spec §13: the public API, used the way applications use it: with the official OpenAI SDK, over HTTP."""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from fastapi import Request
from openai import AsyncOpenAI

from harnessgateway import packaging
from harnessgateway.api import public
from harnessgateway.api.internal import internal_app
from harnessgateway.api.public import public_app
from harnessgateway.domain.principal import Principal
from harnessgateway.errors import Forbidden
from harnessgateway.extensions import Extension, Extensions
from harnessgateway.runner.runner import Runner
from harnessgateway.stores import Stores
from tests.helpers import SHELL, serving
from tests.integration.local_driver import LocalDriver


class Api:
    def __init__(self, stores: Stores, org: str, url: str, key: str) -> None:
        self.stores, self.org, self.url, self.key = stores, org, url, key
        self.client = AsyncOpenAI(api_key=key, base_url=f"{url}/v1", max_retries=0)

    async def final(self, response_id: str) -> Any:
        """Poll a background response until it is final."""
        async with asyncio.timeout(30):
            while True:
                response = await self.client.responses.retrieve(response_id)
                if response.status not in ("queued", "in_progress"):
                    return response
                await asyncio.sleep(0.1)


@contextlib.asynccontextmanager
async def running_api(stores: Stores, org: str, tmp_path: Path, extensions: Extensions) -> AsyncIterator[Api]:
    async with stores.db.system() as conn:               # the queue is shared: start with nothing waiting
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")
    await stores.harnesses.add(org, packaging.pack(SHELL))
    _, key = await stores.keys.create(org, name="test", role="runner")
    driver = LocalDriver(tmp_path / "volumes", internal_app(stores))
    runner = asyncio.create_task(Runner(stores, driver, heartbeat_s=0.2).run())
    try:
        async with serving(public_app(stores, extensions)) as url:
            api = Api(stores, org, url, key)
            yield api
            await api.client.close()
    finally:
        runner.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await runner


@pytest.fixture
async def api(stores: Stores, org: str, tmp_path: Path) -> AsyncIterator[Api]:
    async with running_api(stores, org, tmp_path, Extensions()) as running:
        yield running


# ── running a turn ──────────────────────────────────────────────────────────────────────────────────

async def test_the_sdk_runs_a_turn_and_reads_its_text(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="echo hello", metadata={"ticket": "42"})
    assert response.status == "completed"
    assert response.output_text == "hello\n"
    assert response.model == "default:shell"
    assert response.id.startswith("resp_")
    assert response.error is None
    assert response.metadata is not None
    assert response.metadata["ticket"] == "42"
    assert response.metadata["hg_session"].startswith("sess_")


async def test_a_turn_that_fails_still_answers_200_with_the_failure_in_the_response(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="echo before; exit 3")
    assert response.status == "failed"
    assert response.error is not None
    assert (response.error.code, response.error.message) == ("server_error", "The command exited with code 3.")
    assert response.metadata is not None
    assert (response.metadata["hg_reason"], response.metadata["hg_code"]) == ("harness_failed", "command_failed")
    assert response.output_text == "before\n"


async def test_the_sdk_streams_a_turn_as_it_happens(api: Api) -> None:
    stream = await api.client.responses.create(model="shell", input="echo one; sleep 1; echo two", stream=True)
    seen: list[tuple[str, float]] = []
    numbers = []
    async for event in stream:
        seen.append((event.type, time.monotonic()))
        numbers.append(event.sequence_number)
    kinds = [kind for kind, _ in seen]
    assert kinds[:2] == ["response.created", "response.in_progress"]
    assert kinds[-1] == "response.completed"
    assert kinds.count("response.output_text.delta") == 2
    assert numbers == list(range(len(numbers)))
    first_delta = next(at for kind, at in seen if kind == "response.output_text.delta")
    assert seen[-1][1] - first_delta > 0.7                      # the first line arrived while the turn still ran
    assert event.response.output_text == "one\ntwo\n"  # type: ignore[union-attr]


async def test_the_sdk_s_stream_helper_gathers_the_final_response(api: Api) -> None:
    async with api.client.responses.stream(model="shell", input="echo streamed") as stream:
        deltas = [event.delta async for event in stream if event.type == "response.output_text.delta"]
        final = await stream.get_final_response()
    assert deltas == ["streamed\n"]
    assert (final.status, final.output_text) == ("completed", "streamed\n")


async def test_continuing_and_forking_with_previous_response_id(api: Api) -> None:
    first = await api.client.responses.create(model="shell", input="echo one > a.txt")
    second = await api.client.responses.create(model="shell", input="ls; echo two > b.txt",
                                               previous_response_id=first.id)
    assert second.output_text == "a.txt\n"
    assert second.previous_response_id == first.id
    assert second.metadata is not None
    assert first.metadata is not None
    assert second.metadata["hg_session"] == first.metadata["hg_session"]
    fork = await api.client.responses.create(model="shell", input="ls", previous_response_id=first.id)
    assert fork.output_text == "a.txt\n"
    assert fork.metadata is not None
    assert fork.metadata["hg_session"] != first.metadata["hg_session"]


async def test_a_background_turn_returns_at_once_and_is_polled(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="sleep 0.5; echo done", background=True)
    assert response.status == "queued"
    assert response.output == []
    final = await api.final(response.id)
    assert (final.status, final.output_text) == ("completed", "done\n")


async def test_a_running_turn_is_cancelled(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="echo started; sleep 60", background=True)
    async with asyncio.timeout(20):
        while "started" not in (await api.client.responses.retrieve(response.id)).output_text and not any(
                "started" in event.get("delta", "") for event in await api.stores.turns.events(api.org, response.id)):
            await asyncio.sleep(0.05)
    asked = await api.client.responses.cancel(response.id)
    assert asked.status in ("in_progress", "cancelled")
    final = await api.final(response.id)
    assert final.status == "cancelled"
    assert final.metadata is not None
    assert final.metadata["hg_reason"] == "cancelled"
    assert (await api.client.responses.cancel(response.id)).status == "cancelled"        # unchanged


async def test_a_finished_or_running_turn_s_stream_can_be_joined_from_any_point(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="echo one; sleep 1; echo two", background=True)
    live = await api.client.responses.retrieve(response.id, stream=True)
    kinds = [event.type async for event in live]
    assert kinds[0] == "response.created"
    assert kinds[-1] == "response.completed"

    replay = await api.client.responses.retrieve(response.id, stream=True, starting_after=1)
    events = [event async for event in replay]
    assert events[0].sequence_number == 2
    assert [event.type for event in events] == kinds[2:]


async def test_a_turn_can_be_retrieved_its_input_listed_and_then_deleted(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="echo hi")
    assert (await api.client.responses.retrieve(response.id)).output_text == "hi\n"
    items = await api.client.responses.input_items.list(response.id)
    assert [(item.type, item.role, item.content[0].text) for item in items.data] == [  # type: ignore[union-attr]
        ("message", "user", "echo hi")]

    await api.client.responses.delete(response.id)
    with pytest.raises(openai.NotFoundError, match=response.id):
        await api.client.responses.retrieve(response.id)
    with pytest.raises(openai.BadRequestError, match="names no response"):
        await api.client.responses.create(model="shell", input="ls", previous_response_id=response.id)


async def test_a_turn_that_is_not_to_be_stored_is_gone_once_it_has_answered(api: Api) -> None:
    response = await api.client.responses.create(model="shell", input="echo once", store=False)
    assert (response.status, response.output_text) == ("completed", "once\n")
    with pytest.raises(openai.NotFoundError):
        await api.client.responses.retrieve(response.id)

    stream = await api.client.responses.create(model="shell", input="echo once", store=False, stream=True)
    events = [event async for event in stream]
    with pytest.raises(openai.NotFoundError):
        await api.client.responses.retrieve(events[-1].response.id)  # type: ignore[union-attr]
    with pytest.raises(openai.BadRequestError, match="store cannot be false"):
        await api.client.responses.create(model="shell", input="x", store=False, background=True)


async def test_installed_harnesses_are_the_models(api: Api) -> None:
    models = await api.client.models.list()
    assert [(model.id, model.object, model.owned_by) for model in models.data] == [
        ("shell", "model", "harnessgateway")]
    assert (await api.client.models.retrieve("shell")).id == "shell"
    with pytest.raises(openai.NotFoundError, match="a model is an installed harness"):
        await api.client.models.retrieve("absent")


# ── refusals ────────────────────────────────────────────────────────────────────────────────────────

async def test_a_request_without_a_valid_key_is_refused(api: Api) -> None:
    for key in ("hg_" + "x" * 40, "sk-not-ours"):
        stranger = AsyncOpenAI(api_key=key, base_url=f"{api.url}/v1", max_retries=0)
        with pytest.raises(openai.AuthenticationError) as refusal:
            await stranger.responses.create(model="shell", input="echo hi")
        assert refusal.value.status_code == 401
        await stranger.close()
    async with httpx.AsyncClient() as http:
        bare = await http.post(f"{api.url}/v1/responses", json={"model": "shell", "input": "x"})
    assert bare.status_code == 401
    assert bare.json()["error"]["type"] == "invalid_api_key"


@pytest.mark.parametrize(("request_", "message", "param"), [
    ({"model": "absent", "input": "x"}, "there is no harness named 'absent'", "model"),
    ({"model": "Not A Name", "input": "x"}, r"model is \[environment:\]harness\[/provider\[/model\]\]", "model"),
    ({"model": "shell/deepseek", "input": "x"}, "uses no provider", "model"),
    ({"model": "shell", "input": "x", "tools": [{"type": "web_search"}]}, "only tools of type mcp", "tools"),
    ({"model": "shell", "input": "x", "metadata": {"hg_reason": "mine"}}, "hg_", "metadata"),
    ({"model": "shell", "input": "x", "previous_response_id": "resp_00000000000000000000000000"},
     "names no response", "previous_response_id"),
])
async def test_a_request_that_is_not_valid_is_refused_with_the_openai_error_shape(
    api: Api, request_: dict[str, Any], message: str, param: str
) -> None:
    with pytest.raises(openai.BadRequestError, match=message) as refusal:
        await api.client.responses.create(**request_)
    assert refusal.value.status_code == 400
    assert refusal.value.param == param
    assert refusal.value.type == "invalid_request_error"


async def test_fields_the_api_does_not_have_and_bodies_that_are_not_json_are_refused(api: Api) -> None:
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {api.key}"}) as http:
        unknown = await http.post(f"{api.url}/v1/responses", json={"model": "shell", "input": "x", "session": "s"})
        garbage = await http.post(f"{api.url}/v1/responses", content=b"{not json")
        missing = await http.get(f"{api.url}/v1/responses/not-an-id")
    assert (unknown.status_code, unknown.json()["error"]["message"]) == (
        400, "session is not a field of the Responses API")
    assert (garbage.status_code, garbage.json()["error"]["message"]) == (400, "the request body is not JSON")
    assert (missing.status_code, missing.json()["error"]["type"]) == (404, "not_found")


async def test_a_body_over_the_limit_is_refused(api: Api, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(public, "BODY_MAX", 1000)
    with pytest.raises(openai.APIStatusError, match="at most") as refusal:
        await api.client.responses.create(model="shell", input="x" * 2000)
    assert refusal.value.status_code == 413


async def test_a_session_with_a_turn_in_progress_refuses_another(api: Api) -> None:
    running = await api.client.responses.create(model="shell", input="sleep 60", background=True)
    with pytest.raises(openai.ConflictError, match=f"the session has a turn in progress: {running.id}"):
        await api.client.responses.create(model="shell", input="ls", previous_response_id=running.id)
    await api.client.responses.cancel(running.id)
    await api.final(running.id)


async def test_the_process_says_it_is_up(api: Api) -> None:
    async with httpx.AsyncClient() as http:
        assert (await http.get(f"{api.url}/healthz")).json() == {"status": "ok"}


# ── extensions ──────────────────────────────────────────────────────────────────────────────────────

class Gatekeeper(Extension):
    """An edition's extension: it knows another way to authenticate, refuses some turns, and hears of each."""

    def __init__(self, org: str) -> None:
        self.org = org
        self.finished: list[str] = []

    async def authenticate(self, request: Request) -> Principal | None:
        if request.headers.get("authorization") == "Bearer session-of-alice":
            return Principal(self.org, None, "developer")
        return None

    async def before_turn(self, principal: Principal, body: dict[str, Any]) -> None:
        if "forbidden" in str(body.get("input")):
            raise Forbidden("your plan does not allow that")


async def test_an_extension_can_authenticate_and_refuse_a_turn(stores: Stores, org: str, tmp_path: Path) -> None:
    async with running_api(stores, org, tmp_path, Extensions([Gatekeeper(org)])) as api:
        alice = AsyncOpenAI(api_key="session-of-alice", base_url=f"{api.url}/v1", max_retries=0)
        assert (await alice.responses.create(model="shell", input="echo hi")).output_text == "hi\n"
        with pytest.raises(openai.PermissionDeniedError, match="your plan does not allow that"):
            await alice.responses.create(model="shell", input="echo forbidden")
        assert (await api.client.responses.create(model="shell", input="echo key")).output_text == "key\n"
        await alice.close()
