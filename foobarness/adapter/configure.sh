#!/usr/bin/env bash
# The seeds, kept in the platform's neutral shapes, become foobar's own configuration. Every
# adapter has one of these: it is the only place that knows where its CLI reads MCP servers,
# skills and standing instructions. It runs before every turn, so a changed seed takes effect on
# the next turn, and the request's own MCP tools (its `tools` of type mcp) are added for this turn.
set -euo pipefail
seeds="$HG_HARNESS_DIR/seeds"
mkdir -p "$HOME/.foobar/skills"
python3 - "$seeds" "$HOME/.foobar" "$HG_REQUEST" <<'PY'
import json, os, shutil, sys
seeds, home, request = sys.argv[1], sys.argv[2], json.load(open(sys.argv[3]))
mcp = json.load(open(f"{seeds}/mcp.json"))["servers"] if os.path.exists(f"{seeds}/mcp.json") else []
mcp += request.get("mcp_servers", [])
skills = []
if os.path.isdir(f"{seeds}/skills"):
    for name in sorted(os.listdir(f"{seeds}/skills")):
        shutil.copytree(f"{seeds}/skills/{name}", f"{home}/skills/{name}", dirs_exist_ok=True)
        skills.append(name)
instructions = open(f"{seeds}/instructions.md").read().strip() if os.path.exists(f"{seeds}/instructions.md") else ""
json.dump({"mcp": mcp, "skills": skills, "instructions": instructions}, open(f"{home}/config.json", "w"), indent=1)
PY
