#!/usr/bin/env bash
# The shell harness needs nothing beyond the sandbox image's bash.
set -euo pipefail
echo "installed: bash $(bash --version | head -1)"
