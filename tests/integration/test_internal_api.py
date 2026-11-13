"""Spec §15.1: the internal API, as the steps use it over HTTP."""

from pathlib import Path
from typing import Any

import httpx
import pytest

from harnessgateway import packaging
from harnessgateway.api.internal import internal_app
from harnessgateway.domain.modelpath import ModelPath
from harnessgateway.domain.outcome import Outcome
from harnessgateway.domain.resolve import resolve
from harnessgateway.domain.sealing import new_token
from harnessgateway.errors import Refused
from harnessgateway.kit import hgkit
from harnessgateway.steps.errors import StepError
from harnessgateway.steps.http import HttpStepClient
from harnessgateway.steps.restore import restore
from harnessgateway.steps.save import save
from harnessgateway.steps.unpack import unpack
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores
from tests.integration.test_harness_store import SHELL, demo_archive
from tests.integration.test_steps import picture, populate, volumes


@pytest.fixture(autouse=True)
async def empty_queue(stores: Stores) -> None:
    async with stores.db.system() as conn:
        await conn.execute("UPDATE turns SET status = 'cancelled' WHERE status IN ('queued', 'in_progress')")


def client_for(stores: Stores, token: str) -> HttpStepClient:
    return HttpStepClient("http://internal.test", token, transport=httpx.ASGITransport(app=internal_app(stores)))


async def running(stores: Stores, org: str, model: str = "shell", text: Any = "echo hi", *,
                  parent: Turn | None = None, handover: str = "") -> tuple[Turn, str]:
    """A turn as its runner has it just before the first step: claimed, with its step token set."""
    resolved = resolve(ModelPath.parse(model), environments={e.name: e for e in await stores.environments.all(org)},
                       harnesses={h.name: h for h in await stores.harnesses.all(org)}, providers=[])
    queued = await stores.turns.create(org, resolved=resolved, parent=parent, key_id=None,
                                       request={"model": model, "input": text})
    turn = await stores.turns.claim("runner-test")
    assert turn is not None
    assert turn.id == queued.id
    token = new_token()
    await stores.turns.prepare(turn, image_id="sha256:image", handover=handover, step_token_hash=stores.hash(token),
                               proxy_token_hash=stores.hash(new_token()), placeholders={})
    return turn, token


async def saved(stores: Stores, turn: Turn, token: str, roots: dict[str, Path]) -> Turn:
    """Run the save step for a turn and end the turn as completed with the snapshot it recorded."""
    client = client_for(stores, token)
    result = await save(client, turn.id, roots)
    await client.close()
    await stores.turns.volumes_hold(turn.org_id, turn.session_id, result.snapshot_id)
    return await stores.turns.conclude(turn, Outcome("completed"), snapshot_id=result.snapshot_id)


@pytest.fixture
async def shell(stores: Stores, org: str) -> None:
    await stores.harnesses.add(org, packaging.pack(SHELL))


async def test_a_caller_without_a_live_step_token_is_refused(stores: Stores, org: str, shell: None) -> None:
    turn, token = await running(stores, org)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=internal_app(stores)), base_url="http://internal.test")
    path = f"/internal/steps/restore/{turn.id}"
    assert (await http.get(path)).status_code == 401
    assert (await http.get(path, headers={"Authorization": "Bearer not-the-token"})).status_code == 401
    assert (await http.get(path, headers={"Authorization": f"Bearer {token}"})).status_code == 200

    await stores.turns.conclude(turn, Outcome("completed"))
    dead = await http.get(path, headers={"Authorization": f"Bearer {token}"})
    assert dead.status_code == 401
    assert dead.json()["error"]["type"] == "invalid_api_key"
    await http.aclose()


async def test_a_step_token_reaches_only_its_own_turn(stores: Stores, org: str, shell: None) -> None:
    first, _ = await running(stores, org)
    _, other_token = await running(stores, org)
    client = client_for(stores, other_token)
    with pytest.raises(Refused, match="not for that turn"):
        await client.restore_plan(first.id)
    with pytest.raises(Refused, match="not for that turn"):
        await client.record(first.id, "")
    await client.close()


async def test_a_first_turn_s_plan_starts_from_nothing(stores: Stores, org: str, shell: None) -> None:
    turn, token = await running(stores, org, text="echo hello")
    client = client_for(stores, token)
    plan = await client.restore_plan(turn.id)
    assert (plan.manifest_key, plan.warm, plan.history, plan.handover, plan.files) == (None, False, [], "", ())
    assert plan.request == {"model": "shell", "input": "echo hello"}
    assert await client.save_plan(turn.id) is None
    await client.close()


async def test_folders_saved_by_one_turn_are_restored_for_the_next_over_http(
    stores: Stores, org: str, shell: None, tmp_path: Path
) -> None:
    roots = volumes(tmp_path / "a")
    populate(roots)
    first, token = await running(stores, org)
    first = await saved(stores, first, token, roots)

    second, token = await running(stores, org, parent=first)
    client = client_for(stores, token)
    plan = await client.restore_plan(second.id)
    assert plan.warm                                     # the volumes hold exactly the parent's snapshot
    assert plan.manifest_key is not None

    await stores.turns.volumes_hold(org, second.session_id, None)
    assert not (await client.restore_plan(second.id)).warm
    fresh = volumes(tmp_path / "b")
    await restore(client, second.id, fresh, tmp_path / "turn")
    assert picture(fresh) == picture(roots)

    (fresh["work"] / "added.txt").write_bytes(b"added")
    result = await save(client, second.id, fresh)
    assert result.uploaded == 1
    await client.close()


async def test_the_history_is_the_turns_this_harness_has_not_seen(
    stores: Stores, org: str, shell: None, tmp_path: Path
) -> None:
    await stores.harnesses.add(org, demo_archive(tmp_path, name="other", apis=[], mind=".other", settings={},
                                                 secrets={}))
    roots = volumes(tmp_path / "v")
    one, token = await running(stores, org, "shell", "one")
    one = await saved(stores, one, token, roots)
    two, token = await running(stores, org, "other", "two", parent=one)
    two = await saved(stores, two, token, roots)
    three, token = await running(stores, org, "other", "three", parent=two)
    three = await saved(stores, three, token, roots)

    back, token = await running(stores, org, "shell", "four", parent=three,
                                handover="Handover from other. Review /mind/.other.")
    client = client_for(stores, token)
    plan = await client.restore_plan(back.id)
    assert plan.handover == "Handover from other. Review /mind/.other."
    assert [(t["id"], t["harness"], t["status"], t["input"]) for t in plan.history] == [
        (two.id, "other", "completed", "two"), (three.id, "other", "completed", "three")]
    await client.close()

    same, token = await running(stores, org, "other", "five", parent=three)
    client = client_for(stores, token)
    assert (await client.restore_plan(same.id)).history == []
    await client.close()


async def test_input_files_are_part_of_the_plan_and_reach_the_turn_folder(
    stores: Stores, org: str, shell: None, tmp_path: Path
) -> None:
    async def content() -> Any:
        yield b"%PDF-1.7"
    file = await stores.files.create(org, filename="report.pdf", purpose="user_data", parts=content())
    turn, token = await running(stores, org, text=[{"role": "user", "content": [
        {"type": "input_text", "text": "Summarize."}, {"type": "input_file", "file_id": file.id}]}])
    client = client_for(stores, token)
    await restore(client, turn.id, volumes(tmp_path / "v"), tmp_path / "turn")
    assert (tmp_path / "turn" / "files" / file.id / "report.pdf").read_bytes() == b"%PDF-1.7"
    await client.close()


async def test_a_turn_s_step_reads_only_snapshots_and_files_of_its_own_organization(
    stores: Stores, org: str, shell: None
) -> None:
    turn, token = await running(stores, org)
    harness = await stores.harnesses.get(org, "shell")
    client = client_for(stores, token)
    for key in (f"orgs/{org}/packages/{harness.digest}", "orgs/org_other/snapshots/snap_1", f"orgs/{org}/chunks/aa/x"):
        with pytest.raises(Refused, match="may not read"):
            [piece async for piece in client.object(key)]
    with pytest.raises(Refused, match="no such object"):
        [piece async for piece in client.object(f"orgs/{org}/snapshots/snap_absent")]
    with pytest.raises(Refused, match="no such chunk"):
        await client.chunk("a" * 64)
    await client.close()
    assert turn.id


async def test_a_snapshot_over_the_environment_s_disk_limit_fails_the_save(
    stores: Stores, org: str, shell: None, tmp_path: Path
) -> None:
    await stores.environments.update(org, "default", {"disk_mb": 64})
    roots = volumes(tmp_path / "v")
    with (roots["work"] / "big").open("wb") as big:
        big.truncate(65 * 1024 * 1024)
    turn, token = await running(stores, org)
    client = client_for(stores, token)
    with pytest.raises(StepError, match=r"65 MiB.*64 MiB"):
        await save(client, turn.id, roots)
    await client.close()
    assert await stores.snapshots.of_turn(org, turn.id) is None


async def test_an_install_s_step_unpacks_its_package_and_reads_nothing_else(
    stores: Stores, org: str, shell: None, tmp_path: Path
) -> None:
    harness = await stores.harnesses.get(org, "shell")
    install = await stores.harnesses.install_for(org, harness.digest, "sha256:image")
    token = new_token()
    await stores.harnesses.begin_install(org, install.id, environment="default", step_token_hash=stores.hash(token),
                                         proxy_token_hash=stores.hash(new_token()))
    client = client_for(stores, token)
    target = tmp_path / "harness"
    target.mkdir()
    (target / "left-over").write_text("from an earlier attempt")
    await unpack(client, install.id, target)
    assert sorted(p.name for p in target.iterdir()) == ["README.md", "harness.json", "install", "kit", "run", "tests"]
    assert (target / "run").stat().st_mode & 0o111
    assert (target / "kit" / "hgkit.py").read_bytes() == Path(hgkit.__file__).read_bytes()

    with pytest.raises(Refused, match="not for that install"):
        await client.unpack_plan("inst_00000000000000000000000000")
    with pytest.raises(Refused, match="may not read"):
        [piece async for piece in client.object(f"orgs/{org}/snapshots/snap_1")]
    await client.close()

    await stores.harnesses.end_install(org, install.id, ready=True, log="")
    with pytest.raises(Refused, match="not valid"):
        await client_for(stores, token).unpack_plan(install.id)


async def test_when_the_platform_cannot_be_reached_the_step_fails_plainly() -> None:
    client = HttpStepClient("http://127.0.0.1:9", "token")
    with pytest.raises(StepError, match="could not be reached"):
        await client.save_plan("resp_x")
    await client.close()
