"""Spec §13.9, §13.10: uploaded files as inputs, and a session's work read through the Containers API.

Everything here goes through the official OpenAI SDK.
"""

from collections.abc import AsyncIterator
from pathlib import Path

import openai
import pytest

from harnessgateway.stores import Stores
from tests.integration.api import Running, running


@pytest.fixture
async def api(stores: Stores, org: str, tmp_path: Path) -> AsyncIterator[Running]:
    async with running(stores, org, tmp_path) as api:
        await api.developer.post("/v1/harnesses", json={"catalog": "shell"})
        yield api


async def test_a_file_is_uploaded_listed_read_and_deleted(api: Running) -> None:
    first = await api.openai.files.create(file=("report.txt", b"quarterly numbers"), purpose="user_data")
    second = await api.openai.files.create(file=("data.csv", b"a,b\n1,2\n"), purpose="user_data")
    assert (first.object, first.filename, first.bytes, first.purpose, first.status) == (
        "file", "report.txt", 17, "user_data", "processed")
    assert first.id.startswith("file_")

    listed = await api.openai.files.list()
    assert [file.id for file in listed.data] == [second.id, first.id]
    assert (await api.openai.files.retrieve(first.id)).filename == "report.txt"
    assert (await api.openai.files.content(first.id)).content == b"quarterly numbers"

    deleted = await api.openai.files.delete(first.id)
    assert (deleted.id, deleted.deleted) == (first.id, True)
    with pytest.raises(openai.NotFoundError):
        await api.openai.files.retrieve(first.id)
    one_page = await api.openai.files.list(limit=1)
    assert [file.id for file in one_page.data] == [second.id]


async def test_an_upload_that_is_not_acceptable_is_refused(api: Running) -> None:
    with pytest.raises(openai.BadRequestError, match="purpose is one of"):
        await api.openai.files.create(file=("x.txt", b"x"), purpose="mischief")  # type: ignore[arg-type]
    bad = await api.runner.post("/v1/files", files={"file": ("a/b.txt", b"x")}, data={"purpose": "user_data"})
    assert bad.status_code in (200, 400)                       # a client may strip the folder; never a path
    if bad.status_code == 200:
        assert "/" not in bad.json()["filename"]


async def test_a_file_named_in_a_request_is_there_for_the_turn(api: Running) -> None:
    file = await api.openai.files.create(file=("notes.txt", b"remember the milk"), purpose="user_data")
    response = await api.openai.responses.create(model="shell", input=[{"role": "user", "content": [
        {"type": "input_text", "text": f"cat $HG_TURN/files/{file.id}/notes.txt"},
        {"type": "input_file", "file_id": file.id},
    ]}])
    assert response.output_text == "remember the milk\n"
    with pytest.raises(openai.BadRequestError, match="a file that does not exist"):
        await api.openai.responses.create(model="shell", input=[{"role": "user", "content": [
            {"type": "input_text", "text": "ls"}, {"type": "input_file", "file_id": "file_absent"}]}])


async def test_what_an_agent_made_is_listed_and_downloaded_with_the_containers_api(api: Running) -> None:
    response = await api.openai.responses.create(
        model="shell", input="mkdir out && echo '# Report' > out/report.md && echo raw > data.txt && echo x > ~/.mind")
    assert response.metadata is not None
    container = "cntr_" + response.metadata["hg_session"].removeprefix("sess_")

    found = await api.openai.containers.retrieve(container)
    assert (found.id, found.object, found.name) == (container, "container", response.metadata["hg_session"])

    files = await api.openai.containers.files.list(container)
    assert [(file.path, file.bytes, file.object) for file in files.data] == [       # the work, not the mind
        ("/work/data.txt", 4, "container.file"), ("/work/out/report.md", 9, "container.file")]
    report = files.data[1]
    assert report.id.startswith("cfile_")
    one = await api.openai.containers.files.retrieve(report.id, container_id=container)
    assert one.path == "/work/out/report.md"
    content = await api.openai.containers.files.content.retrieve(report.id, container_id=container)
    assert content.content == b"# Report\n"

    later = await api.openai.responses.create(model="shell", input="echo more >> data.txt; rm out/report.md",
                                              previous_response_id=response.id)
    assert later.status == "completed"
    now = await api.openai.containers.files.list(container)
    assert [(file.path, file.bytes) for file in now.data] == [("/work/data.txt", 9)]
    assert now.data[0].id == files.data[0].id                   # the same path keeps the same id
    with pytest.raises(openai.NotFoundError):
        await api.openai.containers.files.retrieve(report.id, container_id=container)


async def test_a_container_that_is_not_there_is_not_found(api: Running) -> None:
    with pytest.raises(openai.NotFoundError, match="there is no container"):
        await api.openai.containers.retrieve("cntr_00000000000000000000000000")
    with pytest.raises(openai.NotFoundError):
        await api.openai.containers.files.list("not-a-container")


async def test_a_long_listing_is_paged(api: Running) -> None:
    response = await api.openai.responses.create(model="shell", input="for n in 1 2 3 4 5; do echo $n > f$n.txt; done")
    assert response.metadata is not None
    container = "cntr_" + response.metadata["hg_session"].removeprefix("sess_")
    first = await api.openai.containers.files.list(container, limit=2)
    assert ([file.path for file in first.data], first.has_more) == (["/work/f1.txt", "/work/f2.txt"], True)
    everything = [file.path async for file in api.openai.containers.files.list(container, limit=2)]
    assert everything == [f"/work/f{n}.txt" for n in range(1, 6)]
