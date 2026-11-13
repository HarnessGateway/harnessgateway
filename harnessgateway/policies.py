"""Working out what a turn's or an install's sandbox may reach, and which secrets go where (spec §10.4, §11.2)."""

from harnessgateway.domain.plan import install_policy, turn_policy
from harnessgateway.domain.policy import BoundSecret, Policy
from harnessgateway.errors import Refused
from harnessgateway.store.turns import Turn
from harnessgateway.stores import Stores


class Policies:
    def __init__(self, stores: Stores) -> None:
        self._stores = stores

    async def of(self, subject_id: str, token: str) -> Policy | None:
        """The policy for the turn or the install this id and proxy token belong to; None when they belong to
        nothing that is running."""
        stores, token_hash = self._stores, self._stores.hash(token)
        turn = await stores.turns.by_proxy_token(subject_id, token_hash)
        if turn is not None:
            return await self._of_turn(turn)
        install = await stores.harnesses.install_of_proxy_token(subject_id, token_hash)
        if install is None:
            return None
        org_id, environment_name, manifest = install
        try:
            environment = await stores.environments.get(org_id, environment_name)
        except Refused:
            return None
        return Policy(org_id, subject_id, environment.name, install_policy(environment, manifest))

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
