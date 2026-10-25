"""Layer 4, the services: what moves between the domain and the backends. The scheduler hands turns
to nodes and takes their results; intake stands before a queued turn; the worker is the node; the
gateway holds the provider keys; housekeeping fails the turns of lost nodes."""
