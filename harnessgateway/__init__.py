"""HarnessGateway: coding-agent harnesses, run in sandboxes the platform owns, behind the OpenAI
Responses API. The request payload goes to the harness package whole; the package drives its CLI
and maps what comes back onto the response. Layers, lowest first:

0 errors, execution, secrets, files      1 runtimes (process, docker, door, turn.sh)
2 harness, gitstore, wire                3 records, blobs, limits, store, catalog, accounts
4 sandboxes, gateway                     5 sessions, worker
6 api, __main__, cli
"""
