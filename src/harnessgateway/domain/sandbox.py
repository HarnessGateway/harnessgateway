"""A sandbox as plain data: the one hardened container a turn's harness or a package's install runs in, the
folders it is given, and what it may reach. Building a plan is pure; running one is a driver's whole job, so a
plan says nothing about how any particular infrastructure would run it."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from harnessgateway.domain import contract
from harnessgateway.domain.egress import PACKAGE_MANAGER_HOSTS, BoundSecret, EgressPolicy
from harnessgateway.domain.resolve import Resolved
from harnessgateway.domain.resources import Environment
from harnessgateway.domain.snapshot import ROOTS

GIVEN_FOLDER = "given"                         # holds what every sandbox is given: its certificates, and the contract
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


def provider_variable(api: str) -> str:
    """The variable a harness finds a provider's base URL in, for one of the APIs it speaks."""
    return "HG_PROVIDER_" + api.upper().replace("-", "_")


def bind_secrets(resolved: Resolved, *, provider_key: str | None, environment_secrets: Mapping[str, str],
                 new_placeholder: Callable[[], str]) -> tuple[tuple[BoundSecret, ...], dict[str, str], str]:
    """Every secret a turn may use, bound to where it may go and to the placeholder its sandbox holds for it.
    Returns them, the variables that carry each placeholder, and the provider key's placeholder (empty when the
    provider has no key). The provider's key goes to the provider, at whichever of its APIs is used."""
    environment, provider = resolved.environment, resolved.provider
    bound, variables, key_placeholder = [], {}, ""
    if provider and provider_key:
        key_placeholder = new_placeholder()
        bound.append(BoundSecret(tuple(dict.fromkeys(provider.host(api) for api in provider.apis)), provider_key,
                                 key_placeholder))
    for name, declared in environment.secrets.items():
        variables[name] = new_placeholder()
        bound.append(BoundSecret(declared.hosts, environment_secrets[name], variables[name], declared.header,
                                 declared.prefix))
    return tuple(bound), variables, key_placeholder


def turn_plan(*, session_id: str, turn_id: str, install_id: str, image_id: str, resolved: Resolved,
              proxy_token: str, provider_key: str, placeholders: Mapping[str, str]) -> SandboxPlan:
    """One turn's sandbox. Its variables are the contract's, then the environment's, then a placeholder for each
    secret: a later one of the same name wins. A harness is told its provider's base URL for each API the
    provider speaks, and uses the one its agent CLI speaks."""
    environment, line, provider = resolved.environment, resolved.line, resolved.provider
    networked = environment.network.mode != "closed" or provider is not None
    env = {
        "HOME": contract.MIND, "HG_CONTRACT": contract.CONTRACT, "HG_TURN": contract.TURN,
        "HG_MIND": contract.MIND, "HG_WORK": contract.WORK, "HG_HARNESS": contract.HARNESS,
        "HG_STATE": f"{contract.MIND}/{contract.STATE_DIR}/{line.harness}", "HG_SESSION": session_id,
        "HG_RESPONSE": turn_id, "HG_HARNESS_NAME": line.harness, "HG_ENVIRONMENT": line.environment,
        "HG_PROVIDER": line.provider if provider else "", "HG_PROVIDER_KEY": provider_key,
        "HG_MODEL": resolved.model,
        **({provider_variable(api): url for api, url in provider.apis.items()} if provider else {}),
        **(_through_the_proxy(turn_id, proxy_token) if networked else {}),
        **environment.variables, **placeholders,
    }
    folders, startup = session_folders(session_id), bool(environment.scripts.startup)
    return SandboxPlan(
        name=session_id,                           # named for its session: a second one for it cannot start
        image=image_id, command="/bin/sh" if startup else f"{contract.HARNESS}/run",
        args=("-c", _AFTER_STARTUP) if startup else (), workdir=contract.WORK, env=env,
        mounts=(Mount(folders["mind"], contract.MIND, False), Mount(folders["work"], contract.WORK, False),
                Mount(turn_folder(turn_id), contract.TURN, True),
                Mount(install_folder(install_id), contract.HARNESS, True), Mount(GIVEN_FOLDER, contract.GIVEN, True)),
        cpus=environment.cpus, memory_mb=environment.memory_mb, pids=environment.pids, tmp_mb=environment.tmp_mb,
        networked=networked, stop_grace_s=contract.STOP_GRACE_S)


def install_plan(*, install_id: str, image_id: str, environment: Environment, proxy_token: str) -> SandboxPlan:
    """A package's `install`, run once for an image, under whatever names its harnesses have. It is given no
    provider, no secret and no variable, and its own folder to write into."""
    return SandboxPlan(
        name=install_id, image=image_id, command=f"{contract.HARNESS}/install", args=(), workdir=contract.HARNESS,
        env={"HOME": "/tmp/home", "HG_CONTRACT": contract.CONTRACT, "HG_HARNESS": contract.HARNESS,  # noqa: S108
             **_through_the_proxy(install_id, proxy_token)},
        mounts=(Mount(install_folder(install_id), contract.HARNESS, False),
                Mount(GIVEN_FOLDER, contract.GIVEN, True)),
        cpus=environment.cpus, memory_mb=environment.memory_mb, pids=environment.pids, tmp_mb=None,
        networked=True, stop_grace_s=contract.STOP_GRACE_S)


def turn_policy(resolved: Resolved) -> EgressPolicy:
    """What a turn's sandbox may reach: its provider, in every mode, and what its environment says. A limited
    environment adds what it allows, the hosts of its own secrets and, unless it says not, the package managers.
    A harness package adds nothing: the environment constrains the agent, never the other way round."""
    environment, network = resolved.environment, resolved.environment.network
    provider = resolved.provider
    allow = list(dict.fromkeys(provider.host(api) for api in provider.apis)) if provider else []
    if network.mode == "limited":
        allow += [*network.allow, *(host for secret in environment.secrets.values() for host in secret.hosts)]
        allow += PACKAGE_MANAGER_HOSTS if network.package_managers else ()
    return EgressPolicy(network.mode, tuple(allow), network.deny)


def install_policy(environment: Environment) -> EgressPolicy:
    """What a package's install may reach, whatever its environment's mode: the package managers, and what the
    environment allows. A host the environment denies stays denied."""
    return EgressPolicy("limited", (*PACKAGE_MANAGER_HOSTS, *environment.network.allow), environment.network.deny)
