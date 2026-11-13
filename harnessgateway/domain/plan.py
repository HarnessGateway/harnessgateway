"""A turn as plain data: the sandbox, the trusted steps around it, and what it may reach (spec §5.1, §7, §25.2).

Building a plan is pure. Rendering one on some infrastructure is a driver's whole job.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from harnessgateway.domain import contract
from harnessgateway.domain.environment import Environment
from harnessgateway.domain.hosts import PACKAGE_MANAGER_HOSTS, EgressPolicy
from harnessgateway.domain.package import Manifest
from harnessgateway.domain.resolve import Resolved

SANDBOX_NETWORK = "hg-sandbox"
CONTROL_NETWORK = "hg-control"
CA_VOLUME = "hg-ca"
PROXY_HOST = "egress"
PROXY_PORT = 8080


@dataclass(frozen=True)
class Mount:
    volume: str
    path: str
    read_only: bool


@dataclass(frozen=True)
class SandboxPlan:
    """An untrusted container: one turn's harness, or a package's install."""

    name: str
    labels: dict[str, str]
    image: str
    command: list[str]
    workdir: str
    env: dict[str, str]
    mounts: list[Mount]
    cpus: float
    memory_mb: int
    pids: int
    tmp_mb: int | None                 # the size of an in-memory /tmp; None for a disk-backed one that is discarded
    network: str | None                # None: attached to no network at all
    stop_grace_s: int


@dataclass(frozen=True)
class StepPlan:
    """A short, trusted container run from the platform's own image."""

    name: str
    labels: dict[str, str]
    command: list[str]
    env: dict[str, str]
    mounts: list[Mount]
    network: str | None


@dataclass(frozen=True)
class TurnFacts:
    """Everything a turn's sandbox plan is made from, already decided."""

    org_id: str
    session_id: str
    turn_id: str
    install_id: str
    image_id: str
    resolved: Resolved
    handover: str
    proxy_token: str
    provider_key_placeholder: str | None
    secret_placeholders: Mapping[str, str] = field(default_factory=dict)      # variable name -> placeholder


def mind_volume(session_id: str) -> str:
    return f"hg-mind-{session_id}"


def work_volume(session_id: str) -> str:
    return f"hg-work-{session_id}"


def turn_volume(turn_id: str) -> str:
    return f"hg-turn-{turn_id}"


def install_volume(install_id: str) -> str:
    return f"hg-harness-{install_id}"


def sandbox_name(session_id: str) -> str:
    """A sandbox is named for its session, so a second one for the same session cannot start."""
    return f"hg-{session_id}"


_CA_MOUNT = Mount(CA_VOLUME, contract.CA_BUNDLE.rpartition("/")[0], read_only=True)


def _network_variables(user: str, token: str) -> dict[str, str]:
    proxy = f"http://{user}:{token}@{PROXY_HOST}:{PROXY_PORT}"
    return {
        **dict.fromkeys(contract.PROXY_VARIABLES, proxy),
        **dict.fromkeys(contract.NO_PROXY_VARIABLES, ""),
        contract.NODE_PROXY_VARIABLE: "1",
        **dict.fromkeys(contract.CA_VARIABLES, contract.CA_BUNDLE),
    }


def sandbox_plan(facts: TurnFacts) -> SandboxPlan:
    resolved = facts.resolved
    environment, harness, provider = resolved.environment, resolved.harness, resolved.provider
    networked = environment.network.mode != "none"
    env = {
        "HOME": contract.MIND,
        "HG_CONTRACT": str(contract.CONTRACT_VERSION),
        "HG_TURN": contract.TURN,
        "HG_MIND": contract.MIND,
        "HG_WORK": contract.WORK,
        "HG_HARNESS": contract.HARNESS,
        "HG_STATE": f"{contract.MIND}/{contract.STATE_DIR}/{harness.name}",
        "HG_SESSION": facts.session_id,
        "HG_RESPONSE": facts.turn_id,
        "HG_HARNESS_NAME": harness.name,
        "HG_HARNESS_VERSION": harness.manifest.version,
        "HG_ENVIRONMENT": environment.name,
        "HG_PROVIDER": provider.name if provider else "",
        "HG_PROVIDER_API": resolved.api or "",
        "HG_PROVIDER_URL": resolved.url or "",
        "HG_PROVIDER_KEY": facts.provider_key_placeholder or "",
        "HG_MODEL": resolved.model or "",
        "HG_HANDOVER": facts.handover,
        **(_network_variables(facts.turn_id, facts.proxy_token) if networked else {}),
        **environment.variables,
        **harness.setting_values(),
        **facts.secret_placeholders,
    }
    return SandboxPlan(
        name=sandbox_name(facts.session_id),
        labels={"hg.org": facts.org_id, "hg.session": facts.session_id, "hg.turn": facts.turn_id},
        image=facts.image_id,
        command=[f"{contract.HARNESS}/run"],
        workdir=contract.WORK,
        env=env,
        mounts=[
            Mount(mind_volume(facts.session_id), contract.MIND, read_only=False),
            Mount(work_volume(facts.session_id), contract.WORK, read_only=False),
            Mount(turn_volume(facts.turn_id), contract.TURN, read_only=True),
            Mount(install_volume(facts.install_id), contract.HARNESS, read_only=True),
            _CA_MOUNT,
        ],
        cpus=environment.cpus,
        memory_mb=environment.memory_mb,
        pids=environment.pids,
        tmp_mb=environment.tmp_mb,
        network=SANDBOX_NETWORK if networked else None,
        stop_grace_s=harness.manifest.run.stop_grace_s,
    )


def install_plan(
    *, org_id: str, install_id: str, image_id: str, environment: Environment, manifest: Manifest, proxy_token: str
) -> SandboxPlan:
    """A package's `install`, run once for an image (spec §8.3). It sees no provider, secret or variable."""
    return SandboxPlan(
        name=f"hg-install-{install_id}",
        labels={"hg.org": org_id, "hg.install": install_id},
        image=image_id,
        command=[f"{contract.HARNESS}/install"],
        workdir=contract.HARNESS,
        env={
            "HOME": "/tmp/home",  # noqa: S108 - a path inside the sandbox, not on this machine
            "HG_CONTRACT": str(contract.CONTRACT_VERSION),
            "HG_HARNESS": contract.HARNESS,
            "HG_HARNESS_NAME": manifest.name,
            "HG_HARNESS_VERSION": manifest.version,
            **_network_variables(install_id, proxy_token),
        },
        mounts=[Mount(install_volume(install_id), contract.HARNESS, read_only=False), _CA_MOUNT],
        cpus=environment.cpus,
        memory_mb=environment.memory_mb,
        pids=environment.pids,
        tmp_mb=None,
        network=SANDBOX_NETWORK,
        stop_grace_s=manifest.run.stop_grace_s,
    )


def turn_policy(resolved: Resolved) -> EgressPolicy:
    """What a turn's sandbox may reach: what its environment, package, provider and secrets entitle it to."""
    environment, harness = resolved.environment, resolved.harness
    allow = [*environment.network.allow, *harness.manifest.run.egress]
    if resolved.provider and resolved.api:
        allow.append(resolved.provider.host(resolved.api))
    for secret in environment.secrets.values():
        allow.extend(secret.hosts)
    for name, declared in harness.manifest.secrets.items():
        if name in harness.secrets_set:
            allow.extend(declared.hosts)
    if environment.network.package_managers:
        allow.extend(PACKAGE_MANAGER_HOSTS)
    return EgressPolicy(mode=environment.network.mode, allow=tuple(allow), deny=environment.network.deny)


def install_policy(environment: Environment, manifest: Manifest) -> EgressPolicy:
    return EgressPolicy(
        mode="limited",
        allow=(*PACKAGE_MANAGER_HOSTS, *manifest.install.egress),
        deny=environment.network.deny,
    )


def _step(
    kind: str, target: str, *, labels: dict[str, str], mounts: list[Mount], internal_url: str, token: str,
    extra: tuple[str, ...] = (), network: str | None = CONTROL_NETWORK,
) -> StepPlan:
    return StepPlan(
        name=f"hg-{kind}-{target}",
        labels=labels,
        command=["harnessgateway", "step", kind, target, *extra],
        env={"HG_API": internal_url, "HG_STEP_TOKEN": token},
        mounts=mounts,
        network=network,
    )


def _session_mounts(session_id: str, *, read_only: bool) -> list[Mount]:
    return [Mount(mind_volume(session_id), contract.MIND, read_only),
            Mount(work_volume(session_id), contract.WORK, read_only)]


def _turn_labels(org_id: str, session_id: str, turn_id: str) -> dict[str, str]:
    return {"hg.org": org_id, "hg.session": session_id, "hg.turn": turn_id}


def restore_step(*, org_id: str, session_id: str, turn_id: str, internal_url: str, token: str) -> StepPlan:
    mounts = [*_session_mounts(session_id, read_only=False), Mount(turn_volume(turn_id), contract.TURN, False)]
    return _step("restore", turn_id, labels=_turn_labels(org_id, session_id, turn_id), mounts=mounts,
                 internal_url=internal_url, token=token)


def save_step(*, org_id: str, session_id: str, turn_id: str, internal_url: str, token: str) -> StepPlan:
    return _step("save", turn_id, labels=_turn_labels(org_id, session_id, turn_id),
                 mounts=_session_mounts(session_id, read_only=True), internal_url=internal_url, token=token)


def watch_step(
    *, org_id: str, session_id: str, turn_id: str, disk_mb: int, internal_url: str, token: str
) -> StepPlan:
    return _step("watch", turn_id, labels=_turn_labels(org_id, session_id, turn_id),
                 mounts=_session_mounts(session_id, read_only=True), internal_url=internal_url, token=token,
                 extra=("--limit-mb", str(disk_mb)), network=None)


def unpack_step(*, org_id: str, install_id: str, internal_url: str, token: str) -> StepPlan:
    return _step("unpack", install_id, labels={"hg.org": org_id, "hg.install": install_id},
                 mounts=[Mount(install_volume(install_id), contract.HARNESS, read_only=False)],
                 internal_url=internal_url, token=token)


def ca_step(*, internal_url: str) -> StepPlan:
    """Writes the certificate bundle sandboxes trust into its volume (spec §11.5)."""
    return _step("ca", "bundle", labels={"hg.org": "platform"},
                 mounts=[Mount(CA_VOLUME, contract.CA_BUNDLE.rpartition("/")[0], read_only=False)],
                 internal_url=internal_url, token="")
