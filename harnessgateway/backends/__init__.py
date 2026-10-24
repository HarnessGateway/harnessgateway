"""Layer 2, the backends: everything the platform runs on and might run on something else. Each
directory holds one Protocol in its `__init__` and one module per implementation; `config.Settings`
names the implementation of each, and a managed deployment and a self-hosted one differ in nothing
but those names. A conformance test per protocol runs against every implementation."""
