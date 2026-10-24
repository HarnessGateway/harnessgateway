"""Layer 4, the services: what moves between the domain and the backends. The scheduler hands turns
to nodes and takes their results; the worker is the node; the gateway holds the provider keys;
metering turns what ran into usage; webhooks tell clients when a turn ends."""
