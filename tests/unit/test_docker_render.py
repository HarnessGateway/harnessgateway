"""Spec §5.1: the Docker driver renders a plan as the exact `docker run` command of the specification."""

from harnessgateway.domain.plan import Mount, SandboxPlan, StepPlan
from harnessgateway.drivers.docker import LineSplitter, mask, render_sandbox, render_step

SANDBOX = SandboxPlan(
    name="hg-sess_X",
    labels={"hg.org": "org_default", "hg.session": "sess_X", "hg.turn": "resp_Y"},
    image="sha256:abc",
    command=["/harness/run"],
    workdir="/work",
    env={"HOME": "/mind", "HG_HANDOVER": "Handover from codex. Review /mind/.codex.", "EMPTY": ""},
    mounts=[Mount("hg-mind-sess_X", "/mind", read_only=False), Mount("hg-harness-inst_Z", "/harness", read_only=True)],
    cpus=1.5,
    memory_mb=2048,
    pids=256,
    tmp_mb=512,
    network="hg-sandbox",
    stop_grace_s=7,
)


def test_a_sandbox_is_rendered_as_the_command_of_the_specification() -> None:
    assert render_sandbox(SANDBOX, runtime=None) == [
        "docker", "run", "--name", "hg-sess_X", "--init",
        "--label", "hg.org=org_default", "--label", "hg.session=sess_X", "--label", "hg.turn=resp_Y",
        "--network", "hg-sandbox",
        "--cpus", "1.5", "--memory", "2048m", "--memory-swap", "2048m", "--pids-limit", "256",
        "--read-only", "--tmpfs", "/tmp:rw,exec,size=512m",
        "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
        "--user", "10000:10000",
        "--workdir", "/work",
        "--volume", "hg-mind-sess_X:/mind:nocopy", "--volume", "hg-harness-inst_Z:/harness:ro,nocopy",
        "--env", "HOME=/mind", "--env", "HG_HANDOVER=Handover from codex. Review /mind/.codex.", "--env", "EMPTY=",
        "--entrypoint", "/harness/run", "sha256:abc",
    ]


def test_the_image_s_own_entrypoint_never_runs_and_the_command_s_arguments_follow_the_image() -> None:
    """An image may name an ENTRYPOINT of its own. What runs in a sandbox is the platform's command, whatever
    the image says."""
    from dataclasses import replace

    argv = render_sandbox(replace(SANDBOX, command=["/bin/sh", "-c", "echo hi"]), runtime=None)
    assert argv[-5:] == ["--entrypoint", "/bin/sh", "sha256:abc", "-c", "echo hi"]


def test_an_isolating_runtime_is_named_when_there_is_one() -> None:
    argv = render_sandbox(SANDBOX, runtime="runsc")
    assert argv[argv.index("--runtime"):argv.index("--runtime") + 2] == ["--runtime", "runsc"]
    assert "--runtime" not in render_sandbox(SANDBOX, runtime=None)


def test_a_sandbox_with_no_network_is_attached_to_none() -> None:
    from dataclasses import replace

    argv = render_sandbox(replace(SANDBOX, network=None), runtime=None)
    assert argv[argv.index("--network") + 1] == "none"


def test_an_install_gets_a_disk_backed_tmp_that_goes_with_the_container() -> None:
    from dataclasses import replace

    argv = render_sandbox(replace(SANDBOX, tmp_mb=None), runtime=None)
    assert "--tmpfs" not in argv
    assert argv[argv.index("--read-only") + 1:argv.index("--read-only") + 3] == ["--volume", "/tmp"]


def test_a_step_runs_from_the_platform_image_as_root_and_removes_itself() -> None:
    step = StepPlan(
        name="hg-save-resp_Y",
        labels={"hg.org": "org_default", "hg.turn": "resp_Y"},
        command=["harnessgateway", "step", "save", "resp_Y"],
        env={"HG_API": "http://serve:8001", "HG_STEP_TOKEN": "stok"},
        mounts=[Mount("hg-mind-sess_X", "/mind", read_only=True)],
        network="hg-control",
    )
    assert render_step(step, platform_image="platform:4") == [
        "docker", "run", "--rm", "--name", "hg-save-resp_Y",
        "--label", "hg.org=org_default", "--label", "hg.turn=resp_Y",
        "--network", "hg-control",
        "--user", "0:0",
        "--volume", "hg-mind-sess_X:/mind:ro,nocopy",
        "--env", "HG_API=http://serve:8001", "--env", "HG_STEP_TOKEN=stok",
        "platform:4", "harnessgateway", "step", "save", "resp_Y",
    ]


def test_a_logged_command_shows_no_token_and_no_placeholder() -> None:
    argv = ["docker", "run", "--env", "HTTPS_PROXY=http://resp_Y:s3cr3t-token@egress:8080",
            "--env", "HG_PROVIDER_KEY=hgs_" + "a" * 32, "--env", "HG_STEP_TOKEN=stok", "--env", "TZ=UTC", "img"]
    shown = mask(argv)
    assert "s3cr3t-token" not in shown
    assert "a" * 32 not in shown
    assert "stok" not in shown
    assert "http://resp_Y:***@egress:8080" in shown
    assert "TZ=UTC" in shown


def test_lines_are_split_across_chunks() -> None:
    splitter = LineSplitter(limit=100)
    assert splitter.feed(b"one\ntw") == ["one"]
    assert splitter.feed(b"o\nthree\n") == ["two", "three"]
    assert splitter.feed(b"") == []
    assert splitter.finish() == []


def test_a_last_line_without_a_newline_is_still_a_line() -> None:
    splitter = LineSplitter(limit=100)
    assert splitter.feed(b"one\npartial") == ["one"]
    assert splitter.finish() == ["partial"]


def test_a_line_over_the_limit_is_cut_just_past_it_so_that_it_is_refused_later() -> None:
    splitter = LineSplitter(limit=10)
    lines = splitter.feed(b"x" * 25 + b"\nok\n")
    assert [len(line) for line in lines] == [11, 2]            # one character over the limit, then a normal line
    assert lines[1] == "ok"


def test_bytes_that_are_not_text_do_not_stop_the_stream() -> None:
    assert LineSplitter(limit=100).feed(b"\xff\xfe\n") == ["\ufffd\ufffd"]
