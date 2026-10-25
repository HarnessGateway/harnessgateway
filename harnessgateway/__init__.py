"""HarnessGateway: any command-line harness, run in sandboxes the platform owns, behind the OpenAI
Responses API. The request payload goes to the harness package whole; the package drives its program
and maps what comes back onto the response. Layers, lowest first; a module imports its layer or below:

0 kernel      execution, files, vault, ids, limits
1 contract    package, wire, turn.sh
2 backends    records, blobs, repos, sandboxes, egress, events, kms (one protocol each, one module per implementation)
3 domain      orgs, members, keys, providers, harnesses, nodes, sessions, catalog, accounts
4 services    scheduler, intake, worker, gateway, housekeeping
5 api, cli    the HTTP routes; the hg command; config and __main__
"""
