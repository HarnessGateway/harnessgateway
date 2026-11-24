# Security

HarnessGateway runs code an agent wrote. Please report anything that lets that code do more than its environment
allows, and anything that exposes a secret.

**Report privately**, through this repository's security advisories ("Report a vulnerability"), not in a public
issue. Say what you did, what happened and what you expected.

## What the core promises

- A sandbox runs as a non-root user on a read-only root, with no capabilities, its own CPU, memory, process, disk
  and time limits, and no network but the egress proxy.
- A sandbox reaches only what its environment allows, and never a private or local address unless the operator
  named it in `HG_PRIVATE_HOSTS`.
- A secret's value is sealed at rest and is never in a sandbox, a response, a log or an error. A sandbox holds a
  placeholder; the egress proxy puts the value in, only on the hosts the secret is bound to.
- A sandbox is given its own folders and nothing else of the installation's data.

## What it does not

- Without gVisor (`runsc`), sandboxes share the host's kernel: isolation is `none`, the platform says so at every
  start, and that is for development and trusted code only.
- The platform holds the Docker socket. Whoever can change its settings or its image controls the host.
- A harness package is code you chose to run. It runs inside the sandbox, with the sandbox's limits, and sees the
  placeholders for the secrets you gave it.
