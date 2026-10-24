"""Layer 5, the API: HTTP in, HTTP out, nothing else. `responses` is the OpenAI Responses API and
what hangs off a response; `control` the organization's own routes; `signin` the console's accounts;
`deployer` the operator's; `internal` the nodes'. `app` builds the service from its settings and
mounts them all."""
