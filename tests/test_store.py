"""Layer 3: organizations, members, teams, keys, providers, and harnesses as git repositories."""
import pytest

from fakes import archive, folder
from harnessgateway.blobs import DirBlobs
from harnessgateway.errors import Conflict, Invalid, NotFound
from harnessgateway.records import SqliteRecords
from harnessgateway.store import Store


@pytest.fixture
def store(tmp_path, vault):
    return Store(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "blobs", "http://x", b"s"), vault, tmp_path / "git")


def test_orgs_members_teams_keys(store):
    store.create_org("acme", title="ACME")
    with pytest.raises(Conflict):
        store.create_org("acme")
    with pytest.raises(Invalid):
        store.create_org("catalog")
    store.create_member("acme", "chase", "owner")
    store.create_member("acme", "dev", "member")
    t = store.put_team("acme", "platform", ["dev"], "Platform")
    assert t.members == ["dev"] and store.in_team(store.member("acme", "dev"), "platform") and store.in_team(store.member("acme", "chase"), "platform")
    with pytest.raises(Invalid):
        store.put_team("acme", "x", ["nobody"])
    with pytest.raises(Invalid):
        store.set_role("acme", "chase", "member")                      # the last owner stays
    key, secret = store.mint_key("acme", "dev", "laptop")
    assert store.resolve_key(secret).name == "dev" and store.resolve_key("hg_nope") is None
    store.remove_member("acme", "dev")
    assert store.resolve_key(secret) is None and store.team("acme", "platform").members == []
    store.update_org("acme", status="suspended")
    k2, s2 = store.mint_key("acme", "chase")
    assert store.resolve_key(s2) is None


def test_providers_sealed(store):
    store.create_org("acme")
    p = store.put_provider("acme", "ds", "https://api.deepseek.com/anthropic", "anthropic-messages", "sk-secret")
    assert p.key.startswith("v1:") and "sk-secret" not in p.key and store.open_key(p) == "sk-secret" and p.path == "/anthropic"
    with pytest.raises(Invalid):
        store.put_provider("acme", "bad", "http://x", "anthropic-messages", "k")
    with pytest.raises(Invalid):
        store.put_provider("acme", "bad", "https://10.0.0.1/v1", "openai-chat", "k")
    with pytest.raises(Invalid):
        store.put_provider("acme", "bad", "https://x.test", "none", "k")


def test_harness_lifecycle(store, tmp_path):
    store.create_org("acme")
    store.create_member("acme", "chase", "owner")
    h = store.create_harness_from_archive("acme", "echo", archive(), "Chase <c@x>")
    assert h.head and h.config["cli"]["name"] == "echo-cli" and store.blobs.exists(store.harness_blob("acme", "echo", h.head))
    with pytest.raises(Conflict):
        store.create_harness_from_archive("acme", "echo", archive(), "x")
    with pytest.raises(Invalid):
        store.create_harness_from_archive("acme", "bad", archive(files={"request.sh": "if then"}), "x")
    assert not store.repo_path("acme", "bad").exists()
    h2 = store.commit_harness("acme", "echo", {"README.md": b"# hi"}, "readme", "dev", parent=h.head)
    assert h2.head != h.head and store.harness_file("acme", "echo", "README.md") == b"# hi"
    with pytest.raises(Invalid):
        store.commit_harness("acme", "echo", {"harness.json": b"{}"}, "break", "dev")
    assert store.harness("acme", "echo").head == h2.head and store.repo("acme", "echo").head() == h2.head
    assert store.harness_config("acme", "echo", h.head).cli == "echo-cli"
    f = store.fork_harness("acme", "echo", "echo2", "dev")
    assert f.origin == "acme/echo" and len(store.harness_log("acme", "echo2")) == 2
    r = store.replace_harness("acme", "echo", archive(files={"README.md": None, "extra.txt": "x"}), "push", "chase")
    assert {e.path for e in store.harness_files("acme", "echo")} >= {"extra.txt"} and len(store.harness_log("acme", "echo")) == 3
    # another replica: no local repository, restored from the bundle
    import shutil
    shutil.rmtree(store.repo_path("acme", "echo"))
    assert store.repo("acme", "echo").head() == r.head
    store.delete_harness("acme", "echo")
    with pytest.raises(NotFound):
        store.harness("acme", "echo")
    assert store.blobs.list("tenants/acme/harnesses/echo") == []
