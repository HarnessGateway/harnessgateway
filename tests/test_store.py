"""The domain: organizations, members, teams, keys, providers sealed, and harnesses as repositories."""
import pytest

from domainfix import domain
from fakes import archive
from harnessgateway.backends.blobs.dir import DirBlobs
from harnessgateway.backends.records.sqlite import SqliteRecords
from harnessgateway.errors import Conflict, Invalid, NotFound


@pytest.fixture
def d(tmp_path, vault):
    return domain(SqliteRecords(tmp_path / "r.db"), DirBlobs(tmp_path / "blobs", "http://x", b"s"), vault, tmp_path / "git")


def test_orgs_members_teams_keys(d):
    o = d.orgs.create("acme", title="ACME")
    assert o.plan == "free" and not o.trace_sharing and o.trace_sharing_since == 0          # the free plan's ceilings, unless given
    assert o.ceilings.requests_per_minute == 30 and o.ceilings.max_runs == 2 and o.ceilings.cpus == 2
    assert d.orgs.create("given", {"max_runs": 7}).ceilings.max_runs == 7 and d.orgs.create("ent", plan="enterprise").ceilings.max_runs == 32
    with pytest.raises(Conflict):
        d.orgs.create("acme")
    with pytest.raises(Invalid):
        d.orgs.create("catalog")
    with pytest.raises(Invalid):
        d.orgs.create("x", plan="platinum")
    assert d.orgs.update("acme", plan="enterprise", trace_sharing=True).trace_sharing is False      # enterprise never shares
    assert d.orgs.get("acme").ceilings.max_runs == 32                                              # a new plan brings its ceilings
    assert d.orgs.update("acme", plan="team", limits={"max_runs": 3}).ceilings.max_runs == 3        # unless others are given
    shared = d.orgs.update("acme", plan="free", trace_sharing=True)
    assert shared.trace_sharing is True and shared.shares and shared.trace_sharing_since > 0        # sharing is dated
    since = shared.trace_sharing_since
    assert d.orgs.update("acme", title="still").trace_sharing_since == since                        # and the date holds while it is on
    assert d.orgs.sharing() == [d.orgs.get("acme")] and d.orgs.get("acme").public()["trace_sharing_since"] == since
    off = d.orgs.update("acme", trace_sharing=False)
    assert off.trace_sharing_since == 0 and d.orgs.sharing() == [] and off.public()["trace_sharing_since"] == 0
    assert d.orgs.update("acme", trace_sharing=True).trace_sharing_since > since                    # turned on again: a new date
    d.orgs.update("acme", trace_sharing=False)
    d.members.create("acme", "chase", "owner")
    d.members.create("acme", "dev", "member")
    t = d.teams.put("acme", "platform", ["dev"], "Platform")
    assert t.members == ["dev"] and d.teams.includes(d.members.get("acme", "dev"), "platform") and d.teams.includes(d.members.get("acme", "chase"), "platform")
    assert d.teams.of(d.members.get("acme", "dev")) == ["platform"] and d.teams.of(d.members.get("acme", "chase")) == []
    with pytest.raises(Invalid):
        d.teams.put("acme", "x", ["nobody"])
    with pytest.raises(Invalid):
        d.members.set_role("acme", "chase", "member")                      # the last owner stays
    key, secret = d.keys.mint("acme", "dev", "laptop")
    assert d.keys.resolve(secret).name == "dev" and d.keys.resolve("hg_nope") is None
    d.members.remove("acme", "dev")
    d.teams.drop_member("acme", "dev")
    d.keys.revoke_all("acme", "dev")
    assert d.keys.resolve(secret) is None and d.teams.get("acme", "platform").members == []
    d.orgs.update("acme", status="suspended")
    k2, s2 = d.keys.mint("acme", "chase")
    assert d.keys.resolve(s2) is None
    with pytest.raises(Invalid):
        d.orgs.active("acme")
    dk = d.keys.set_deployer()
    assert dk.startswith("hgd_") and d.keys.is_deployer(dk) and not d.keys.is_deployer("hgd_other") and d.keys.has_deployer()


def test_providers_sealed(d):
    d.orgs.create("acme")
    p = d.providers.put("acme", "ds", "https://api.deepseek.com/anthropic", "anthropic-messages", "sk-secret")
    assert p.key.startswith("v1:") and "sk-secret" not in p.key and d.providers.open_key(p) == "sk-secret" and p.path == "/anthropic"
    assert "key" not in p.public()
    for args, says in [(("https://x.example", "bogus", "k"), "api is one of"), (("http://x.example", "openai-chat", "k"), "https://"),
                       (("https://x.example", "openai-chat", ""), "has a key"), (("https://x.example", "none", "k"), "has a key"),
                       (("https://x.example", "openai-chat", "two\nlines"), "one line"),
                       (("https://10.0.0.1", "openai-chat", "k"), "public"), (("https://localhost", "openai-chat", "k"), "host name")]:
        with pytest.raises(Invalid, match=says):
            d.providers.put("acme", "p", *args)
    tok = d.providers.issue_token("acme", "ds", "sess_1", "resp_1", "openai-chat", ttl_s=60)
    assert tok.startswith("sk-hg-") and d.providers.resolve_token(tok)["provider"] == "ds"
    assert d.providers.issue_token("acme", "p", "s", "r", "anthropic-messages", 60).startswith("sk-ant-api03-hg-")
    assert d.providers.resolve_token(d.providers.issue_token("acme", "p", "s", "r", "openai-chat", ttl_s=-1)) is None
    d.providers.revoke_token(tok)
    assert d.providers.resolve_token(tok) is None
    d.providers.delete("acme", "ds")
    with pytest.raises(NotFound):
        d.providers.get("acme", "ds")


def test_nodes(d):
    n, secret = d.nodes.register("node-1", slots=4, version="8.0.0")
    assert d.nodes.by_secret(secret).id == n.id and d.nodes.by_secret("hgw_nope") is None and n.alive
    d.nodes.touch(n, running=2)
    assert d.nodes.list()[0].running == 2 and d.nodes.list()[0].version == "8.0.0"
    d.nodes.touch(n, running=0, version="8.1.0", slots=8)                                    # restarted on a new release
    assert d.nodes.list()[0].version == "8.1.0" and d.nodes.list()[0].slots == 8
    d.nodes.forget(n.id)
    assert d.nodes.list() == []


def test_harness_lifecycle(d, tmp_path):
    d.orgs.create("acme")
    d.members.create("acme", "chase", "owner")
    d.members.create("acme", "dev", "member")
    h = d.harnesses.create_from_archive("acme", "echo", archive(), "Chase <c@x>")
    assert h.head and h.config["cli"]["name"] == "echo-cli" and d.blobs.exists(d.harnesses.blob_key("acme", "echo", h.head))
    with pytest.raises(Conflict):
        d.harnesses.create_from_archive("acme", "echo", archive(), "x")
    with pytest.raises(Invalid):
        d.harnesses.create_from_archive("acme", "bad", archive(files={"request.sh": "if then"}), "x")
    assert not d.harnesses.repo_path("acme", "bad").exists()
    with pytest.raises(Invalid, match="egress"):
        d.harnesses.create_from_archive("acme", "bad", archive(run='{"egress": ["169.254.169.254"]}'), "x")
    h2 = d.harnesses.commit("acme", "echo", {"README.md": b"# hi"}, "readme", "dev", parent=h.head)
    assert h2.head != h.head and d.harnesses.file("acme", "echo", "README.md") == b"# hi"
    with pytest.raises(Invalid):
        d.harnesses.commit("acme", "echo", {"harness.json": b"{}"}, "break", "dev")
    assert d.harnesses.get("acme", "echo").head == h2.head and d.harnesses.repo("acme", "echo").head() == h2.head
    assert d.harnesses.config_at("acme", "echo", h.head).cli == "echo-cli"
    f = d.harnesses.fork("acme", "echo", "echo2", "dev")
    assert f.origin == "acme/echo" and len(d.harnesses.log("acme", "echo2")) == 2
    r = d.harnesses.replace("acme", "echo", archive(files={"README.md": None, "extra.txt": "x"}), "push", "chase")
    assert {e.path for e in d.harnesses.files("acme", "echo")} >= {"extra.txt"} and len(d.harnesses.log("acme", "echo")) == 3
    # another replica: no local repository, restored from the bundle
    import shutil
    shutil.rmtree(d.harnesses.repo_path("acme", "echo"))
    assert d.harnesses.repo("acme", "echo").head() == r.head
    # teams decide who may use and change a harness
    d.teams.put("acme", "platform", ["dev"])
    chase, dev = d.members.get("acme", "chase"), d.members.get("acme", "dev")
    d.harnesses.move("acme", "echo2", "platform")
    assert [x.name for x in d.harnesses.visible(dev)] == ["echo", "echo2"] and d.harnesses.editable(dev, "echo2").team == "platform"
    with pytest.raises(Invalid):
        d.harnesses.editable(dev, "echo")                                    # organization-wide: an owner's to change
    assert d.harnesses.editable(chase, "echo").name == "echo"
    d.teams.put("acme", "platform", [])
    with pytest.raises(NotFound):
        d.harnesses.usable(dev, "echo2")
    with pytest.raises(Conflict):
        d.teams.delete("acme", "platform")
    d.orgs.update("acme", limits={"upload_mb": 1})
    import os
    with pytest.raises(Invalid, match="upload limit"):
        d.harnesses.create_from_archive("acme", "big", archive(files={"blob.bin": os.urandom(2 * 1024 * 1024)}), "x")
    d.harnesses.delete("acme", "echo")
    with pytest.raises(NotFound):
        d.harnesses.get("acme", "echo")
    assert d.blobs.list("tenants/acme/harnesses/echo") == []


def test_orgs_are_separate_and_deletable(d):
    d.orgs.create("a")
    d.orgs.create("b")
    d.members.create("a", "m", "owner")
    _, secret = d.keys.mint("a", "m")
    d.providers.put("a", "p", "https://x.example", "openai-chat", "k")
    d.harnesses.create_from_archive("a", "h", archive(), "m")
    with pytest.raises(NotFound):
        d.providers.get("b", "p")
    with pytest.raises(NotFound):
        d.harnesses.get("b", "h")
    assert d.harnesses.list("b") == [] and d.providers.list("b") == []
    d.harnesses.delete_all("a")
    d.orgs.delete("a")
    assert [o.name for o in d.orgs.list()] == ["b"] and d.keys.resolve(secret) is None
    assert d.blobs.list("tenants/a") == [] and not d.harnesses.repo_path("a", "h").exists()
