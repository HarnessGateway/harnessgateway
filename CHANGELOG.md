# Changelog

## 0.1.0

The first version of the open-source core.

- The OpenAI Responses API, unmodified, with the Files and Containers APIs beside it.
- Providers, environments and harnesses as definitions, with sealed secrets.
- Harness packages run in hardened sandboxes; sessions saved as content-addressed snapshots.
- An egress proxy that enforces each environment's network and keeps secrets out of sandboxes.
- Sessions as their requests and responses; usage as CPU, memory, network and storage.
- The seams an edition builds on, and a sample extension that uses each.
- A console at `/console`: chat, providers, environments, harnesses, sessions and usage.
- Links a browser can open a session's files through, sandboxed (`POST /v1/containers/{id}/view`).
- What an organization starts with: the `default` environment, and the `shell` and `harness-package-builder`
  harnesses.
