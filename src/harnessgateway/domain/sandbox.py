"""A sandbox as plain data: the one hardened container a turn's harness or a package's install runs in, the
folders it is given, and what it may reach. Building a plan is pure; running one is a driver's whole job, so a
plan says nothing about how any particular infrastructure would run it."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from harnessgateway.domain import contract
from harnessgateway.domain.egress import PACKAGE_MANAGER_HOSTS, BoundSecret, EgressPolicy
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.resources import Environment, Manifest
from harnessgateway.domain.snapshot import ROOTS

TRUST_FOLDER = "trust"                         # holds the certificate bundle every sandbox trusts
# The startup script first, with what it prints sent to the log (standard output is the harness's events), then
# the harness in its place.
_AFTER_STARTUP = (f'{contract.STARTUP} 1>&2 || {{ echo "the startup script exited with code $?" >&2; '
                  f"exit {contract.STARTUP_FAILED}; }}; exec {contract.HARNESS}/run")


def session_folders(session_id: str) -> dict[str, str]:
    """A session's two folders, by the root a snapshot knows each as."""
    return {root: f"sessions/{session_id}/{root}" for root in ROOTS}


def turn_folder(turn_id: str) -> str:
    return f"turns/{turn_id}"


def install_folder(install_id: str) -> str:
    return f"installs/{install_id}"


@dataclass(frozen=True)
class Mount:
    folder: str                        # a folder the driver keeps, by name
    path: str                          # where the sandbox finds it
    read_only: bool


@dataclass(frozen=True)
class SandboxPlan:
    name: str                          # one sandbox of a name runs at a time
    image: str
    command: str                       # what is run in place of the image's own entry point
    args: tuple[str, ...]
    workdir: str
    env: dict[str, str]
    mounts: tuple[Mount, ...]
    cpus: float
    memory_mb: int
    pids: int
    tmp_mb: int | None                 # the size of an in-memory /tmp; None: one on disk, discarded with the sandbox
    networked: bool                    # False: it is attached to no network at all
    stop_grace_s: int


def _through_the_proxy(subject_id: str, token: str) -> dict[str, str]:
    """The variables that send a sandbox's traffic to the egress proxy, as itself, trusting what the proxy shows."""
    proxy = f"http://{subject_id}:{token}@{contract.PROXY_HOST}:{contract.PROXY_PORT}"
    return {**dict.fromkeys(contract.PROXY_VARIABLES, proxy), **dict.fromkeys(contract.NO_PROXY_VARIABLES, ""),
            contract.NODE_PROXY_VARIABLE: "1", **dict.fromkeys(contract.CA_VARIABLES, contract.CA_BUNDLE)}


def bind_secrets(resolved: Resolved, *, provider_key: str | None, environment_secrets: Mapping[str, str],
                 harness_secrets: Mapping[str, str], new_placeholder: Callable[[], str],
                 ) -> tuple[tuple[BoundSecret, ...], dict[str, str], str]:
    """Every secret a turn may use, bound to where it may go and to the placeholder its sandbox holds for it.
    Returns them, the variables that carry each placeholder, and the provider key's placeholder (empty when the
    provider has no key). A harness's secret goes only to the hosts its package named."""
    environment, manifest = resolved.environment, resolved.harness.manifest
    bound, variables, key_placeholder = [], {}, ""
    if resolved.provider and resolved.api and provider_key:
        key_placeholder = new_placeholder()
        bound.append(BoundSecret((resolved.provider.host(resolved.api),), provider_key, key_placeholder))
    for name, declared in environment.secrets.items():
        variables[name] = new_placeholder()
        bound.append(BoundSecret(declared.hosts, environment_secrets[name], variables[name], declared.header,
                                 declared.prefix))
    for name, value in harness_secrets.items():
        if name in manifest.secrets:
            variables[name] = new_placeholder()
            bound.append(BoundSecret(manifest.secrets[name].hosts, value, variables[name]))
    return tuple(bound), variables, key_placeholder


def turn_plan(*, session_id: str, turn_id: str, install_id: str, image_id: str, resolved: Resolved, handover: str,
              proxy_token: str, provider_key: str, placeholders: Mapping[str, str]) -> SandboxPlan:
    """One turn's sandbox. Its variables are the contract's, then the environment's, then the harness's settings,
    then a placeholder for each secret: a later one of the same name wins."""
    environment, manifest, line = resolved.environment, resolved.harness.manifest, resolved.line
    networked = environment.network.mode != "closed" or resolved.provider is not None
    env = {
        "HOME": contract.MIND, "HG_CONTRACT": str(contract.VERSION), "HG_TURN": contract.TURN,
        "HG_MIND": contract.MIND, "HG_WORK": contract.WORK, "HG_HARNESS": contract.HARNESS,
        "HG_STATE": f"{contract.MIND}/{contract.STATE_DIR}/{manifest.name}", "HG_SESSION": session_id,
        "HG_RESPONSE": turn_id, "HG_HARNESS_NAME": manifest.name, "HG_HARNESS_VERSION": manifest.version,
        "HG_ENVIRONMENT": line.environment, "HG_PROVIDER": line.provider if resolved.provider else "",
        "HG_PROVIDER_API": resolved.api or "", "HG_PROVIDER_URL": resolved.url, "HG_PROVIDER_KEY": provider_key,
        "HG_MODEL": resolved.model, "HG_HANDOVER": handover,
        **(_through_the_proxy(turn_id, proxy_token) if networked else {}),
        **environment.variables, **resolved.harness.values(), **placeholders,
    }
    folders, startup = session_folders(session_id), bool(environment.scripts.startup)
    return SandboxPlan(
        name=session_id,                           # named for its session: a second one for it cannot start
        image=image_id, command="/bin/sh" if startup else f"{contract.HARNESS}/run",
        args=("-c", _AFTER_STARTUP) if startup else (), workdir=contract.WORK, env=env,
        mounts=(Mount(folders["mind"], contract.MIND, False), Mount(folders["work"], contract.WORK, False),
                Mount(turn_folder(turn_id), contract.TURN, True),
                Mount(install_folder(install_id), contract.HARNESS, True), Mount(TRUST_FOLDER, contract.TRUST, True)),
        cpus=environment.cpus, memory_mb=environment.memory_mb, pids=environment.pids, tmp_mb=environment.tmp_mb,
        networked=networked, stop_grace_s=manifest.run.stop_grace_s)


def install_plan(*, install_id: str, image_id: str, environment: Environment, manifest: Manifest,
                 proxy_token: str) -> SandboxPlan:
    """A package's `install`, run once for an image. It is given no provider, no secret and no variable, and its
    own folder to write into."""
    return SandboxPlan(
        name=install_id, image=image_id, command=f"{contract.HARNESS}/install", args=(), workdir=contract.HARNESS,
        env={"HOME": "/tmp/home", "HG_CONTRACT": str(contract.VERSION), "HG_HARNESS": contract.HARNESS,  # noqa: S108
             "HG_HARNESS_NAME": manifest.name, "HG_HARNESS_VERSION": manifest.version,
             **_through_the_proxy(install_id, proxy_token)},
        mounts=(Mount(install_folder(install_id), contract.HARNESS, False),
                Mount(TRUST_FOLDER, contract.TRUST, True)),
        cpus=environment.cpus, memory_mb=environment.memory_mb, pids=environment.pids, tmp_mb=None,
        networked=True, stop_grace_s=manifest.run.stop_grace_s)


def turn_policy(resolved: Resolved) -> EgressPolicy:
    """What a turn's sandbox may reach: its provider, in every mode, and what its environment says. A limited
    environment adds what it allows, the hosts of its own secrets and, unless it says not, the package managers.
    A harness package adds nothing: the environment constrains the agent, never the other way round."""
    environment, network = resolved.environment, resolved.environment.network
    allow = [resolved.provider.host(resolved.api)] if resolved.provider and resolved.api else []
    if network.mode == "limited":
        allow += [*network.allow, *(host for secret in environment.secrets.values() for host in secret.hosts)]
        allow += PACKAGE_MANAGER_HOSTS if network.package_managers else ()
    return EgressPolicy(network.mode, tuple(allow), network.deny)


def install_policy(environment: Environment) -> EgressPolicy:
    """What a package's install may reach, whatever its environment's mode: the package managers, and what the
    environment allows. A host the environment denies stays denied."""
    return EgressPolicy("limited", (*PACKAGE_MANAGER_HOSTS, *environment.network.allow), environment.network.deny)
