#!/usr/bin/env bash
# One Codex turn. run.py writes $HOME/.codex/config.toml from the request (provider -> $HG_PROVIDER_URL/v1 with the
# per-turn token, model, reasoning, instructions, MCP servers), runs `codex exec --json` (or `codex exec resume
# <thread>` on a continuation) in the workspace, and maps Codex's JSONL to Responses events with map.py + hgkit.
set -uo pipefail
cd "$HG_WORKSPACE"
exec python3 "$HG_PACKAGE_DIR/run.py"
