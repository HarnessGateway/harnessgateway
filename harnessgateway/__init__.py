"""HarnessGateway: run any coding-agent harness in a platform-owned sandbox behind the OpenAI Responses API.

The platform reads a Responses request, hands it whole to a harness package inside a sandbox, and returns the
Responses events the package prints. It owns three things: the tenant boundary, sandboxed execution, and metering.

Layers, lowest first (a module imports only its own layer or below):

    errors, ids, limits      vocabulary
    domain                   pure rules: manifests, the wire, turn states, pricing, org limits
    crypto, db, blobs        infrastructure adapters
    services                 one use case per class
    api, gateway, node       the three processes
"""

__version__ = "1.0.0"
