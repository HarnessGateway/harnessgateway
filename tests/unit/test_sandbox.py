"""A sandbox as a plan, and the one command it becomes."""

from harnessgateway.domain.line import Line
from harnessgateway.domain.resolve import resolve
from harnessgateway.domain.resources import Environment, Harness, Provider
from harnessgateway.domain.sandbox import bind_secrets, install_plan, install_policy, turn_plan, turn_policy
from harnessgateway.driver.base import Measured
from harnessgateway.driver.docker import Lines, command
from harnessgateway.driver.meter import with_sample

HOW = {"name": "hg-sess_1", "volume": "hg-data", "network": "hg-sandbox"}


def resolved(**environment):
    return resolve(
        Line.parse("dev/agent/claude/claude-x"),
        Environment.load({"image": "node:22", "harnesses": ["agent"], "variables": {"TEAM": "demo", "MODE": "fast"},
                          "secrets": {"DB": {"hosts": ["db.example.com"], "header": "X-Key", "prefix": "Key "}}, **environment}),
        Harness.load({"digest": "d"}),
        Provider.load({"apis": {"anthropic": "https://api.example.com:8443/v1", "openai-chat": "https://chat.example.com/v1"}}))


def test_a_turns_plan_gives_the_sandbox_its_folders_its_variables_and_placeholders_for_its_secrets():
    found, counter = resolved(), iter(range(100))
    bound, placeholders, key = bind_secrets(found, provider_key="sk-real", environment_secrets={"DB": "db-secret"},
                                            new_placeholder=lambda: f"hgs_{next(counter)}")
    assert key == "hgs_0" and placeholders == {"DB": "hgs_1"}
    assert [(secret.value, secret.hosts, secret.header) for secret in bound] == [
("sk-real", ("api.example.com:8443", "chat.example.com"), None), ("db-secret", ("db.example.com",), "X-Key")]      # the key, to its provider at any of its APIs
    plan = turn_plan(session_id="sess_1", turn_id="resp_1", install_id="inst_1", image_id="sha256:abc", resolved=found,
                     proxy_token="tok", provider_key=key, placeholders=placeholders)
    assert (plan.name, plan.command, plan.args, plan.workdir, plan.networked) == ("sess_1", "/harness/run", (), "/work", True)
    assert [(mount.folder, mount.path, mount.read_only) for mount in plan.mounts] == [
        ("sessions/sess_1/mind", "/mind", False), ("sessions/sess_1/work", "/work", False), ("turns/resp_1", "/turn", True),
        ("installs/inst_1", "/harness", True), ("given", "/etc/hg", True)]
    env = plan.env
    assert (env["HOME"], env["HG_MODEL"], env["HG_PROVIDER"], env["HG_HARNESS_NAME"]) == ("/mind", "claude-x", "claude", "agent")
    # A harness is told where its provider is, for each API the provider speaks, and picks the one its CLI speaks.
    assert (env["HG_PROVIDER_ANTHROPIC"], env["HG_PROVIDER_OPENAI_CHAT"]) == ("https://api.example.com:8443/v1", "https://chat.example.com/v1")
    assert "HG_PROVIDER_GEMINI" not in env and "HG_PROVIDER_URL" not in env and env["HG_STATE"] == "/mind/.harnessgateway/agent"
    assert (env["HG_PROVIDER_KEY"], env["DB"], env["TEAM"], env["MODE"]) == ("hgs_0", "hgs_1", "demo", "fast")
    assert env["HTTPS_PROXY"] == "http://resp_1:tok@egress:8080" and env["SSL_CERT_FILE"] == "/etc/hg/ca.pem"
    assert "HG_CONTRACT" not in env                # the contract is the builder's own: no sandbox is given it
    assert not {"sk-real", "db-secret"} & set(env.values()) and plan.stop_grace_s == 15
    scripted = turn_plan(session_id="s", turn_id="t", install_id="i", image_id="x", resolved=resolved(scripts={"startup": "true"}),
                         proxy_token="tok", provider_key="", placeholders={})
    assert scripted.command == "/bin/sh" and scripted.args[0] == "-c" and scripted.args[1].endswith("exec /harness/run")
    install = install_plan(install_id="inst_1", image_id="x", environment=found.environment, proxy_token="tok")
    assert (install.command, install.workdir, install.tmp_mb) == ("/harness/install", "/harness", None) and "TEAM" not in install.env
    assert [(mount.path, mount.read_only) for mount in install.mounts] == [("/harness", False), ("/etc/hg", True)]


def test_an_environment_decides_what_a_turn_may_reach_and_a_package_decides_nothing():
    limited = turn_policy(resolved(network={"allow": ["api.github.com"]}))
    assert limited.mode == "limited" and {"api.example.com:8443", "chat.example.com", "api.github.com", "db.example.com", "pypi.org"} <= set(limited.allow)
    bare = turn_policy(resolved(network={"package_managers": False}))
    assert set(bare.allow) == {"api.example.com:8443", "chat.example.com", "db.example.com"}
    closed = turn_policy(resolved(network={"mode": "closed"}))
    assert (closed.mode, closed.allow) == ("closed", ("api.example.com:8443", "chat.example.com"))            # its provider, and nothing else
    assert turn_policy(resolved(network={"mode": "open", "deny": ["bad.example.com"]})).deny == ("bad.example.com",)
    install = install_policy(resolved(network={"mode": "closed"}).environment)
    assert install.mode == "limited" and "registry.npmjs.org" in install.allow


def test_every_sandbox_is_hardened_the_same_way():
    plan = turn_plan(session_id="sess_1", turn_id="resp_1", install_id="inst_1", image_id="sha256:abc", resolved=resolved(),
                     proxy_token="tok", provider_key="", placeholders={})
    argv = command(plan, **HOW, runtime="runsc", proxy_address="172.20.0.2")
    text = " ".join(argv)
    for flag in ("--name hg-sess_1", "--init --env TINI_KILL_PROCESS_GROUP=1", "--read-only", "--cap-drop ALL", "--security-opt no-new-privileges",
                 "--user 10000:10000", "--cpus 2", "--memory 4096m", "--memory-swap 4096m", "--pids-limit 512",
                 "--network hg-sandbox", "--tmpfs /tmp:rw,exec,size=1024m", "--env HOME=/mind", "--workdir /work",
                 "--mount type=volume,source=hg-data,target=/work,volume-subpath=folders/sessions/sess_1/work,volume-nocopy ",
                 "--mount type=volume,source=hg-data,target=/turn,volume-subpath=folders/turns/resp_1,volume-nocopy,readonly",
                 "--runtime runsc", "--add-host egress:172.20.0.2"):
        assert flag in text, flag
    assert argv[-3:] == ["--entrypoint", "/harness/run", "sha256:abc"] and "--privileged" not in text
    assert "--volume hg-data" not in text and "source=hg-data,target=/data" not in text        # never the volume itself
    alone = " ".join(command(type(plan)(**{**vars(plan), "networked": False, "tmp_mb": None}), **HOW))
    assert "--network none" in alone and "--volume /tmp" in alone and "--runtime" not in alone and "--add-host" not in alone


def test_output_is_split_into_lines_and_what_a_container_used_is_read_from_its_samples():
    split = Lines(limit=5)
    assert split.feed(b"ab\ncd") == ["ab"] and split.feed(b"e\r\n") == ["cde"]
    assert split.feed(b"0123456789\nx") == ["012345"] and split.rest() == ["x"]            # one over the limit: refused, not cut
    sample = {"cpu_stats": {"cpu_usage": {"total_usage": 1_500_000_000}}, "memory_stats": {"usage": 300, "stats": {"inactive_file": 100}}}
    once = with_sample(Measured(), sample, 2.0)
    assert once == Measured(cpu_seconds=1.5, memory_byte_seconds=400.0)
    assert with_sample(once, {"cpu_stats": {"cpu_usage": {"total_usage": 1_000_000_000}}}, 1.0) == once and with_sample(once, {}, 1.0) == once
