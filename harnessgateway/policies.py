"""What a turn's sandbox, an install or a build may reach, and which secrets go where (spec §10.4, §11.2)."""

from harnessgateway.domain.plan import install_policy, setup_policy, turn_policy
from harnessgateway.domain.policy import BoundSecret, Policy
from harnessgateway.errors import Refused
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores


class Policies:
    def __init__(self, stores: Stores) -> None:
        self._stores = stores

    async def of(self, subject_id: str, token: str) -> Policy | None:
        """The policy for the turn, the install or the build this id and proxy token belong to; None when they
        belong to nothing that is running."""
        stores, token_hash = self._stores, self._stores.hash(token)
        turn = await stores.turns.by_proxy_token(subject_id, token_hash)
        if turn is not None:
            return await self._of_turn(turn)
        build = await stores.builds.of_proxy_token(subject_id, token_hash)
        if build is not None:
            return await self._of_build(subject_id, *build)
        install = await stores.harnesses.install_of_proxy_token(subject_id, token_hash)
        if install is None:
            return None
        org_id, environment_name, manifest = install
        try:
            environment = await stores.environments.get(org_id, environment_name)
        except Refused:
            return None
        return Policy(org_id, subject_id, environment.name, install_policy(environment, manifest))

    async def _of_build(self, build_id: str, org_id: str, environment_name: str,
                        placeholders: dict[str, str]) -> Policy | None:
        """A build may reach what its environment's setup may, with the environment's secrets in place."""
        stores = self._stores
        try:
            environment = await stores.environments.get(org_id, environment_name)
        except Refused:
            return None
        values = await stores.environments.secret_values(org_id, environment.name)
        secrets = []
        for placeholder, name in placeholders.items():
            if name in environment.secrets and name in values:
                declared = environment.secrets[name]
                secrets.append(BoundSecret(declared.hosts, values[name], placeholder, declared.header,
                                           declared.prefix))
        return Policy(org_id, build_id, environment.name, setup_policy(environment), tuple(secrets))

    async def _of_turn(self, turn: Turn) -> Policy | None:
        stores, org_id = self._stores, turn.org_id
        try:
            resolved = await stores.resolve(org_id, turn.path)
        except Refused:
            return None                                         # a definition it ran with is gone
        environment, harness, provider = resolved.environment, resolved.harness, resolved.provider
        values = {
            "environment": await stores.environments.secret_values(org_id, environment.name),
            "harness": await stores.harnesses.secret_values(org_id, harness.name),
        }
        secrets = []
        for placeholder, meaning in turn.placeholders.items():
            kind, name = meaning["kind"], meaning["name"]
            if kind == "provider" and provider and resolved.api:
                key = await stores.providers.key(org_id, provider.name)
                if key is not None:
                    secrets.append(BoundSecret((provider.host(resolved.api),), key, placeholder))
            elif kind == "environment" and name in environment.secrets and name in values[kind]:
                declared = environment.secrets[name]
                secrets.append(BoundSecret(declared.hosts, values[kind][name], placeholder, declared.header,
                                           declared.prefix))
            elif kind == "harness" and name in harness.manifest.secrets and name in values[kind]:
                secrets.append(BoundSecret(harness.manifest.secrets[name].hosts, values[kind][name], placeholder))
        return Policy(org_id, turn.id, environment.name, turn_policy(resolved), tuple(secrets))
