#!/usr/bin/env bash
# Pi, one turn: `pi -p --mode json`, mapped by map.py.
#   input text -> the prompt; input_file and input_image parts -> @file arguments
#   instructions -> --append-system-prompt (Pi's flag; AGENTS.md is read too, for standing instructions)
#   previous_response_id -> --session-id (Pi opens the session by the id we give it: the platform's)
#   reasoning.effort -> --thinking;  tools of type mcp -> .pi/mcp.json;  store: false -> --no-session
set -uo pipefail
cd "$HG_WORKSPACE"
q() { jq -r "$1" "$HG_REQUEST"; }
prompt=$(q 'if (.input|type)=="string" then .input else [.input[] | (if .type=="input_text" then .text elif (.type // "message")=="message" then (if (.content|type)=="string" then .content else [.content[] | select(.type=="input_text") | .text] | join("\n") end) else empty end)] | join("\n") end')
instructions=$(q 'if (.instructions|type)=="array" then [.instructions[] | .content // "" | if type=="string" then . else [.[] | .text // ""] | join("\n") end] | join("\n") else .instructions // "" end')
effort=$(q '.reasoning.effort // ""'); store=$(q '.store // true')
attach=()
for f in "$HG_FILES"/*; do [ -f "$f" ] && cp -f "$f" "$HG_WORKSPACE/" && attach+=("@$(basename "$f")"); done
if [ "$(q '[.tools // [] | .[] | select(.type=="mcp")] | length')" != "0" ]; then
  mkdir -p .pi
  q '{mcpServers: ([.tools[] | select(.type=="mcp") | {key: .server_label, value: ({type: "http", url: .server_url, exposure: "direct"} + (if .headers or .authorization then {headers: ((.headers // {}) + (if .authorization then {Authorization: ("Bearer " + .authorization)} else {} end))} else {} end))}] | from_entries)}' > .pi/mcp.json
fi

export PI_OFFLINE=1 PI_TELEMETRY=0 PI_SKIP_VERSION_CHECK=1
mkdir -p "$HOME/.pi/agent"
api="anthropic-messages"; [ "${HG_PROVIDER_API:-}" = "openai-chat" ] && api="openai-completions"; [ "${HG_PROVIDER_API:-}" = "openai-responses" ] && api="openai-responses"
cat > "$HOME/.pi/agent/models.json" <<CFG
{"providers": {"hg": {"baseUrl": "${HG_PROVIDER_URL%/v1}", "api": "$api", "apiKey": "\$ANTHROPIC_API_KEY", "models": [{"id": "$HG_MODEL", "reasoning": true, "input": ["text", "image"], "contextWindow": 128000, "maxTokens": 16384}]}}}
CFG

run_wait() { "$@" </dev/null & local pid=$! rc; while :; do wait "$pid"; rc=$?; kill -0 "$pid" 2>/dev/null || break; done; return $rc; }

turn() {
  local args=(-p --mode json --provider hg --model "$HG_MODEL" -a)
  if [ "$store" = "false" ]; then args+=(--no-session); else args+=(--session-id "$HG_SESSION"); fi
  [ -n "$instructions" ] && args+=(--append-system-prompt "$instructions")
  case "$effort" in off|minimal|low|medium|high|xhigh|max) args+=(--thinking "$effort") ;; esac
  pi "${args[@]}" "${attach[@]}" "$1" | python3 "$HG_HARNESS_DIR/map.py"
}
run_wait turn "$prompt"; rc=$?
exit $rc
