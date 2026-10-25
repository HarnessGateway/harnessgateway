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
from harnessgateway.domain.sessions import Sessions
from harnessgateway.services.housekeeping import Housekeeping
from harnessgateway.services.intake import Intake
from harnessgateway.services.scheduler import Scheduler


def domain(records, blobs, vault, git_root: Path, allow_private: bool = False, gateway_url: str = "http://gateway.test:8081") -> SimpleNamespace:
    orgs = Orgs(records, blobs)
    members = Members(records, orgs)
    keys = Keys(records, orgs, members)
    providers = Providers(records, vault, orgs, allow_private)
    harnesses = Harnesses(records, blobs, git_root, orgs, allow_private)
    nodes = Nodes(records)
    sessions = Sessions(records, blobs, RecordEvents(records), orgs, harnesses, providers)
    scheduler = Scheduler(sessions, harnesses, providers, orgs, nodes, gateway_url)
    return SimpleNamespace(records=records, blobs=blobs, vault=vault, orgs=orgs, members=members, keys=keys, providers=providers,
                           harnesses=harnesses, nodes=nodes, sessions=sessions, intake=Intake(records, orgs, sessions),
                           scheduler=scheduler, housekeeping=Housekeeping(scheduler), accounts=Accounts(records, vault, orgs, members))
