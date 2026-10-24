#!/usr/bin/env bash
# OpenClaw, one turn: `openclaw agent exec --json`, one envelope mapped by map.py.
#   input text -> the message; input_file parts -> the workspace
#   instructions -> AGENTS.md in the workspace (OpenClaw injects the exec folder's AGENTS.md; it has no system-prompt key)
#   previous_response_id -> --state-dir kept in $HG_STATE (exec has no session flag: the directory is the continuity)
#   reasoning.effort -> --thinking
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""')
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/"; done
python3 "$HG_HARNESS_DIR/instructions.py" "$instructions"

export OPENCLAW_STATE_DIR="$HG_STATE/openclaw" OPENCLAW_WORKSPACE_DIR="$HG_WORKSPACE" OPENCLAW_OFFLINE=1
mkdir -p "$OPENCLAW_STATE_DIR"
api="anthropic-messages"; [ "${HG_PROVIDER_API:-}" = "openai-chat" ] && api="openai-completions"; [ "${HG_PROVIDER_API:-}" = "openai-responses" ] && api="openai-responses"
cat > "$HG_DIR/openclaw.json" <<CFG
{"models": {"mode": "merge", "providers": {"hg": {"baseUrl": "${HG_PROVIDER_URL%/v1}", "api": "$api", "apiKey": "\${ANTHROPIC_API_KEY}",
                                                   "models": [{"id": "$HG_MODEL", "name": "$HG_MODEL", "reasoning": true, "input": ["text"], "contextWindow": 128000, "maxTokens": 16384}]}}},
 "agents": {"defaults": {"model": "hg/$HG_MODEL", "workspace": "$HG_WORKSPACE", "skipBootstrap": true, "timeoutSeconds": 1500,
                         "sandbox": {"mode": "off"}}},
 "tools": {"profile": "coding", "exec": {"mode": "full"}}}
CFG

steer=""
trap 'steer=$(cat "$HG_DIR/interrupt" 2>/dev/null || true)' USR1
run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(agent exec --json --config "$HG_DIR/openclaw.json" --state-dir "$OPENCLAW_STATE_DIR" --cwd "$HG_WORKSPACE" --model "hg/$HG_MODEL")
  case "$effort" in off|minimal|low|medium|high|xhigh|adaptive|max|ultra) args+=(--thinking "$effort") ;; esac
  openclaw "${args[@]}" "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
if [ -n "$steer" ]; then run_wait turn "$steer"; rc=$?; fi
exit $rc
