"""The domain, built from a records store and a blob store, for the unit tests: what `api.app.Service`
does without settings or HTTP."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harnessgateway.backends.events.records import RecordEvents
from harnessgateway.domain.accounts import Accounts
from harnessgateway.domain.harnesses import Harnesses
from harnessgateway.domain.keys import Keys
from harnessgateway.domain.members import Members
from harnessgateway.domain.nodes import Nodes
from harnessgateway.domain.orgs import Orgs
from harnessgateway.domain.providers import Providers
from harnessgateway.domain.rodeos import Rodeos
from harnessgateway.domain.sessions import Sessions
from harnessgateway.domain.teams import Teams
from harnessgateway.services.dataset import Dataset
from harnessgateway.services.housekeeping import Housekeeping
from harnessgateway.services.intake import Intake
from harnessgateway.services.metering import Metering
from harnessgateway.services.scheduler import Scheduler


def domain(records, blobs, vault, git_root: Path, allow_private: bool = False, gateway_url: str = "http://gateway.test:8081") -> SimpleNamespace:
    orgs = Orgs(records, blobs)
    members = Members(records, orgs)
    teams = Teams(records, orgs, members)
    keys = Keys(records, orgs, members)
    providers = Providers(records, vault, orgs, allow_private)
    harnesses = Harnesses(records, blobs, git_root, orgs, teams, allow_private)
    nodes = Nodes(records)
    sessions = Sessions(records, blobs, RecordEvents(records), orgs, teams, harnesses, providers)
    metering = Metering(records, blobs, orgs)
    scheduler = Scheduler(sessions, harnesses, providers, orgs, nodes, gateway_url, on_final=metering.turn_ended)
    return SimpleNamespace(records=records, blobs=blobs, vault=vault, orgs=orgs, members=members, teams=teams, keys=keys, providers=providers,
                           harnesses=harnesses, nodes=nodes, sessions=sessions, rodeos=Rodeos(records, sessions),
                           intake=Intake(records, orgs, sessions), metering=metering,
                           scheduler=scheduler, housekeeping=Housekeeping(scheduler, metering),
                           dataset=Dataset(records, blobs, orgs, vault), accounts=Accounts(records, vault, orgs, members))
